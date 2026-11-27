"""記録済みセッションを別の model alias へ引き継ぐ (fork) API。

POST /v1/sessions/continue               API key で実行する
POST /admin/sessions/continue            管理認証で実行する
POST /admin/sessions/handoff-preview     引き継ぎ内容の件数と拒否理由を実行前に確認する
"""

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..auth import require_basic_auth
from ..config import HarnessConfig, get_config
from ..execution_http import authorize_execution, enforce_model_scope
from ..interactions import get_scoped_session_turns, get_session_turns
from ..models import ChatCompletionRequest, ChatMessage
from ..session_continue import (
    DEFAULT_MAX_CONTEXT_BYTES, DEFAULT_MAX_TURNS, active_turns, build_fork_metadata, build_handoff_context,
    check_handoff_data_policy, compose_fork_prompt, derive_origin_hint, new_fork_conversation_key,
)
from .chat import execute_continued_turn

log = structlog.get_logger()

_IMPORT_KEY_PREFIX = "import:"
_IMPORT_SCOPES = frozenset({"sessions:continue", "sessions:export"})
_NOT_FOUND = "Source session not found or has no turns"

client_router = APIRouter(prefix="/v1", tags=["sessions"], dependencies=[Depends(authorize_execution)])
admin_router = APIRouter(prefix="/admin/sessions", tags=["sessions"], dependencies=[Depends(require_basic_auth)])


class HandoffPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_conversation_key: str = Field(min_length=1, max_length=512)
    source_provider: str | None = None
    source_runtime_session_id: str | None = None
    model: str = Field(min_length=1, max_length=256)
    max_turns: int = Field(default=DEFAULT_MAX_TURNS, ge=1, le=100)
    max_context_bytes: int = Field(default=DEFAULT_MAX_CONTEXT_BYTES, ge=1024, le=131072)


class ContinueSessionRequest(HandoffPreviewRequest):
    message: str = Field(min_length=1)
    cwd: str | None = None
    reasoning_effort: str | None = None

    @field_validator("message")
    @classmethod
    def _reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("message cannot be blank")
        return value


@client_router.post("/sessions/continue", summary="セッションの引き継ぎ", response_model=None)
async def continue_session(body: ContinueSessionRequest, request: Request) -> dict | JSONResponse:
    """自分の key のセッション、または scope がある場合は取り込み履歴を引き継ぐ。"""
    enforce_model_scope(request, body.model)
    if body.source_conversation_key.startswith(_IMPORT_KEY_PREFIX):
        # 取り込み履歴は API key に紐づかないので、scope で読み取りを許可する。
        scopes = getattr(request.state.execution_policy, "scopes", ()) or ()
        if not _IMPORT_SCOPES.intersection(scopes):
            raise HTTPException(status_code=403, detail="API key lacks 'sessions:continue' scope")
        turns = await _load_unscoped_turns(body)
        origin_turns = await _load_origin_turns_unscoped(body, turns)
    else:
        turns = await get_scoped_session_turns(
            body.source_conversation_key, request.state.api_key_name,
            body.source_runtime_session_id, body.source_provider,
        )
        origin_turns = await _load_origin_turns_scoped(body, request.state.api_key_name, turns)
    return await _continue_session(body, request, turns, origin_turns=origin_turns)


@admin_router.post("/handoff-preview", summary="引き継ぎ内容の確認")
async def preview_handoff(body: HandoffPreviewRequest) -> dict:
    turns = await _load_unscoped_turns(body)
    total = len(active_turns(turns))
    if not total:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    origin_turns = await _load_origin_turns_unscoped(body, turns)
    origin_hint = derive_origin_hint(origin_turns)
    handoff, included, truncated = _build_handoff(body, turns)
    return {
        "source_conversation_key": body.source_conversation_key,
        "model": body.model,
        "handoff_turns_included": included,
        "handoff_turns_total": total,
        "handoff_truncated": truncated,
        "preview_bytes": len(handoff.encode("utf-8")),
        "origin_hint": origin_hint,
        "policy_blocked_reason": check_handoff_data_policy(get_config(), body.model, origin_hint=origin_hint),
    }


@admin_router.post("/continue", summary="セッションの引き継ぎ (管理)", response_model=None)
async def admin_continue_session(body: ContinueSessionRequest, request: Request) -> dict | JSONResponse:
    turns = await _load_unscoped_turns(body)
    origin_turns = await _load_origin_turns_unscoped(body, turns)
    return await _continue_session(body, request, turns, origin_turns=origin_turns)


async def _load_unscoped_turns(body: HandoffPreviewRequest) -> list[dict]:
    return await get_session_turns(
        body.source_conversation_key, body.source_runtime_session_id, body.source_provider,
    )


async def _load_origin_turns_unscoped(body: HandoffPreviewRequest, turns: list[dict]) -> list[dict]:
    """source_provider / source_runtime_session_id で絞っても work 由来を見落とさないよう全 turn を照合する。"""
    if body.source_provider is None and body.source_runtime_session_id is None:
        return turns
    all_turns = await get_session_turns(body.source_conversation_key)
    return [*turns, *all_turns]


async def _load_origin_turns_scoped(
    body: HandoffPreviewRequest, api_key_name: str | None, turns: list[dict],
) -> list[dict]:
    if body.source_provider is None and body.source_runtime_session_id is None:
        return turns
    all_turns = await get_scoped_session_turns(body.source_conversation_key, api_key_name)
    return [*turns, *all_turns]


def _build_handoff(body: HandoffPreviewRequest, turns: list[dict]) -> tuple[str, int, bool]:
    return build_handoff_context(
        turns, parent_conversation_key=body.source_conversation_key,
        max_turns=body.max_turns, max_bytes=body.max_context_bytes,
    )


async def _continue_session(
    body: ContinueSessionRequest, request: Request, turns: list[dict], *, origin_turns: list[dict] | None = None,
) -> dict | JSONResponse:
    """読み込んだ turn を新しい会話キーへ引き継ぎ、最初の 1 turn を実行する。"""
    candidates = active_turns(turns)
    if not candidates:
        raise HTTPException(status_code=404, detail=_NOT_FOUND)
    cfg = get_config()
    if body.model not in cfg.models and body.model not in cfg.pools:
        raise HTTPException(status_code=404, detail=f"Model '{body.model}' does not exist")
    origin_hint = derive_origin_hint(origin_turns if origin_turns is not None else turns)
    if refusal := check_handoff_data_policy(cfg, body.model, origin_hint=origin_hint):
        raise HTTPException(status_code=403, detail=refusal)

    handoff, included, truncated = _build_handoff(body, turns)
    parent = candidates[-1]
    new_key = new_fork_conversation_key()
    fork_metadata = build_fork_metadata(
        parent_conversation_key=body.source_conversation_key,
        parent_provider=parent.get("provider") or body.source_provider,
        parent_runtime_session_id=(
            parent.get("runtime_session_id") or parent.get("claude_session_id") or body.source_runtime_session_id
        ),
        included_turns=included,
        total_turns=len(candidates),
        truncated=truncated,
        origin_hint=origin_hint,
    )
    chat_request = ChatCompletionRequest(
        model=body.model,
        messages=[ChatMessage(role="user", content=compose_fork_prompt(handoff, body.message))],
        stream=False,
        cwd=body.cwd,
        reasoning_effort=body.reasoning_effort,
    )
    result = await execute_continued_turn(
        chat_request, request, new_key, extra_metadata=fork_metadata, source_override="continue",
    )
    if isinstance(result, JSONResponse):
        return result
    return {
        "conversation_key": new_key,
        "parent_conversation_key": body.source_conversation_key,
        "model": body.model,
        "provider": await _resolve_provider(cfg, new_key, result.resolved_model or body.model),
        "handoff_turns_included": included,
        "handoff_turns_total": len(candidates),
        "handoff_truncated": truncated,
        "origin_hint": origin_hint,
        "response": result.choices[0].message.content,
    }


async def _resolve_provider(config: HarnessConfig, conversation_key: str, alias: str) -> str | None:
    """記録された provider を返し、記録が無ければ alias の設定から解決する。"""
    try:
        recorded = await get_session_turns(conversation_key)
    except Exception as exc:  # noqa: BLE001 - 完了した turn の応答を、記録の読み取り失敗で捨てない
        log.warning("session_continue.provider_lookup_failed", error_type=type(exc).__name__)
        recorded = []
    if recorded and recorded[-1].get("provider"):
        return recorded[-1]["provider"]
    return config.preset_for(alias).provider if alias in config.models else None
