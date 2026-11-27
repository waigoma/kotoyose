"""セッション履歴インポートの管理 API。

GET  /admin/imports/sources   既定ソースの検出結果と許可ルート
POST /admin/imports/run       取り込みの実行 (dry_run 可)
GET  /admin/imports/runs      直近の実行履歴
"""

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from .. import db
from ..auth import require_basic_auth
from ..config import get_config
from ..session_import import (
    ImportOptions, SessionImportStore, list_import_runs, list_import_sources, parse_since, run_import,
)
from ..session_import.models import format_utc_iso
from ..session_import.remote import ingest_remote_batches, resolve_effective_origin_hint
from ..session_import.paths import PathSecurityError, configured_allowed_roots

router = APIRouter(
    prefix="/admin/imports",
    tags=["imports"],
    dependencies=[Depends(require_basic_auth)],
)

# 管理 API の同期実行を有界にする上限。大量取り込みは CLI を使う。
DEFAULT_ADMIN_MAX_FILES = 500


def _get_store(request: Request) -> SessionImportStore:
    """DB があれば DB 保存、無ければ app 単位の永続インメモリ領域を使う。

    インメモリ領域は app.state に保持するため、dev / DB なしでも実行履歴と
    台帳が後続の要求から見える。
    """
    if db._pool is not None:
        return SessionImportStore(db._pool)
    store = getattr(request.app.state, "session_import_store", None)
    if store is None:
        store = SessionImportStore(None)
        request.app.state.session_import_store = store
    return store


class RunImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_kind: str = "all"
    path: str | None = None
    since: str | None = None
    origin_hint: str | None = None
    source_machine: str | None = None
    dry_run: bool = False
    max_files: int = Field(default=DEFAULT_ADMIN_MAX_FILES, ge=1, le=5000)


@router.get("/sources", summary="ローカル履歴ソースの検出")
async def get_import_sources(request: Request) -> dict:
    store = _get_store(request)
    allowed_roots = configured_allowed_roots(extra_roots=get_config().session_import_allowed_roots)
    sources = await list_import_sources(store=store, allowed_roots=allowed_roots)
    return {
        "sources": [source.to_dict() for source in sources],
        "allowed_roots": [str(path) for path in allowed_roots],
    }


@router.post("/run", summary="セッション履歴の取り込み")
async def run_session_import(body: RunImportRequest, request: Request) -> dict:
    store = _get_store(request)
    try:
        parsed_since = parse_since(body.since)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    allowed_roots = configured_allowed_roots(extra_roots=get_config().session_import_allowed_roots)
    options = ImportOptions(
        source=body.source_kind,
        path=Path(body.path) if body.path else None,
        since=parsed_since,
        origin_hint=body.origin_hint or None,
        source_machine=body.source_machine or None,
        dry_run=body.dry_run,
        allowed_roots=allowed_roots,
        max_files=body.max_files,
    )
    try:
        summary = await run_import(options, store=store)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return summary.to_dict()


@router.get("/runs", summary="取り込み実行履歴")
async def get_import_runs(
    request: Request,
    limit: int = Query(default=20, ge=1, le=200),
) -> dict:
    runs = await list_import_runs(limit=limit, store=_get_store(request))
    return {"runs": runs}


class ChildOriginHintRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    origin_hint: Literal["personal", "work"] | None = None


class CollectChildRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_kind: str = "all"
    since: str | None = None
    origin_hint: Literal["personal", "work"] | None = None
    dry_run: bool = False
    max_files: int = Field(default=200, ge=1, le=2000)
    max_turns: int = Field(default=2000, ge=1, le=20000)


@router.get("/children")
async def get_import_children(request: Request) -> dict:
    hub = getattr(request.app.state, "child_hub", None)
    if hub is None:
        return {"children": []}
    store = _get_store(request)
    children = []
    for child in hub.session_collect_children():
        stats = await store.source_ledger_stats(source_machine=child["name"])
        timestamps = [parse_since(v["last_imported_at"]) for v in stats.values() if v.get("last_imported_at")]
        children.append({
            **child,
            "imported_turns": sum(int(v.get("imported_turns") or 0) for v in stats.values()),
            "imported_files": sum(int(v.get("imported_files") or 0) for v in stats.values()),
            "last_imported_at": format_utc_iso(max(timestamps)) if timestamps else None,
        })
    return {"children": children}


@router.put("/children/{name}/origin-hint")
async def set_child_origin_hint(name: str, body: ChildOriginHintRequest, request: Request) -> dict:
    hub = getattr(request.app.state, "child_hub", None)
    if hub is None:
        raise HTTPException(status_code=404, detail="Child hub is not enabled")
    try:
        hub.registry.set_origin_hint(name, body.origin_hint)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Child '{name}' not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"name": name, "origin_hint": body.origin_hint}


@router.post("/children/{name}/collect")
async def collect_child_sessions(name: str, body: CollectChildRequest, request: Request) -> dict:
    hub = getattr(request.app.state, "child_hub", None)
    if hub is None:
        raise HTTPException(status_code=404, detail="Child hub is not enabled")
    try:
        parsed_since = parse_since(body.since)
        since = format_utc_iso(parsed_since) if parsed_since else None
        batches, done_stats, hub_effective_hint = await hub.collect_sessions(
            name, source_kind=body.source_kind, since=since,
            max_files=body.max_files, max_turns=body.max_turns,
        )
        effective_origin = resolve_effective_origin_hint(hub_effective_hint, body.origin_hint)
        summary = await ingest_remote_batches(
            batches, source_machine=name, origin_hint=effective_origin,
            source_kind=body.source_kind, since=since, dry_run=body.dry_run,
            remote_stats=done_stats, store=_get_store(request),
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"Child '{name}' not found") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except (RuntimeError, TimeoutError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return summary.to_dict()
