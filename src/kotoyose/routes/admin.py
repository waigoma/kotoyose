"""
管理エンドポイント。

- POST /admin/reload       : config.yml を再読み込み
- GET  /admin/config       : 現在の設定を確認
- GET  /admin/browse       : サーバー上のファイル/ディレクトリ一覧
- POST /admin/models       : モデルを追加
- PUT  /admin/models/{id}  : モデルを更新
- DELETE /admin/models/{id}: モデルを削除
"""

import errno
import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Literal

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator

from ..auth import admin_username, require_basic_auth
from ..data_policy import TrainingCapability, alias_data_policy, connection_data_policy, training_digest
from ..config import (
    AgentPreset,
    McpDefinition,
    ModelPreset,
    PromptFileDefinition,
    SECRET_SETTING_FIELDS,
    SkillDefinition,
    TrainingAck,
    get_clients,
    get_config,
    reload_config,
    save_config,
)
from ..model_discovery import discover_models
from ..agent_route import published_agent
from ..agent_sanction import with_sanction
from .admin_catalog import driver_catalog, pool_catalog, route_sanction_view
from ..execution_resources import agent_model_allowed

log = structlog.get_logger()

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


def _save_config_or_422(config) -> None:
    """複数階層にまたがる config 不変条件の違反を 422 として返す。"""
    try:
        save_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _validate_and_save_config_or_422(config) -> None:
    """Validate referenced secrets asynchronously before saving config."""
    try:
        if config.referenced_secret_names():
            await config.validate_secret_references_async()
        save_config(config)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _settings_to_dict() -> dict[str, Any]:
    """取得系レスポンス用に Settings を変換し、secret の生値を除外する。"""
    settings = get_config().settings
    result: dict[str, Any] = {}
    for field in settings.__class__.model_fields:
        value = getattr(settings, field)
        if field in SECRET_SETTING_FIELDS:
            result[field] = {"set": bool(value)}
            if isinstance(value, str) and value.startswith("secret:"):
                result[field]["hint"] = value
                result[field]["secret"] = value.removeprefix("secret:")
            elif value:
                result[field]["hint"] = f"…{value[-4:]}"
        else:
            result[field] = value
    return result


def _route_sanction_dict(cfg, name: str) -> dict[str, Any]:
    """route の公式の口かどうかの判定と承諾の記録 (表示用。route の定義とは別に返す)。"""
    return route_sanction_view(cfg, name)


def _config_to_dict() -> dict[str, Any]:
    """現在の HarnessConfig を JSON-safe な dict に変換する。"""
    cfg = get_config()

    def preset_dict(preset) -> dict[str, Any]:
        return {
            "execution": preset.execution,
            **({"on": preset.on} if preset.on != "local" else {}),
            **({"place": preset.place} if preset.place is not None else {}),
            **({"allow": preset.allow.model_dump(mode="json")} if preset.allow is not None else {}),
            **({"execution_environment": preset.execution_environment}
               if preset.execution_environment is not None else {}),
            "native": preset.native.model_dump(mode="json") if preset.native is not None else None,
            **({"route": preset.route, "agent": preset.agent.model_dump() if preset.agent else None}
               if preset.route is not None else {
                   "provider": preset.provider, "model": preset.model,
                   "anthropic_base_url": preset.anthropic_base_url,
                   "anthropic_auth_token_env": preset.anthropic_auth_token_env,
               }),
            "bare": preset.bare,
            "codex_home": preset.codex_home,
            "cwd": preset.cwd,
            "cwd_allowed_roots": preset.cwd_allowed_roots,
            "deny": preset.deny,
            "prompt_file_refs": preset.prompt_file_refs,
            "mcp_servers": {
                name: {k: v for k, v in srv.items()}
                for name, srv in preset.mcp_servers.items()
            },
            "mcp_refs": preset.mcp_refs,
            "memory_enabled": preset.memory_enabled,
            "memory_force": preset.memory_force,
            "memory_source": preset.memory_source,
            "memory_channel": preset.memory_channel,
            "memory_sender": preset.memory_sender,
            "fixed_conversation_key": preset.fixed_conversation_key,
            "thinking": preset.thinking,
            "thinking_budget_tokens": preset.thinking_budget_tokens,
            "effort": preset.effort,
            "timeout_seconds": preset.timeout_seconds,
            "permission_mode": preset.permission_mode,
            "required_request_headers": preset.required_request_headers,
            "allow_cwd_override": preset.allow_cwd_override,
            "setting_sources": preset.setting_sources,
            "tools": preset.tools,
            "require_isolation": preset.require_isolation,
            "strict_mcp_config": preset.strict_mcp_config,
            "system_prompt_snapshot": preset.system_prompt_snapshot,
            "output_schema": preset.output_schema,
            "plugins": preset.plugins,
            "skill_refs": preset.skill_refs,
            "skills": preset.skills,
            "agent_refs": preset.agent_refs,
            "agents": {
                name: agent.model_dump()
                for name, agent in preset.agents.items()
            },
            "codex": preset.codex,
            "cursor": preset.cursor,
            "antigravity": preset.antigravity,
        }

    return {
        "config_file": os.environ.get("KOTOYOSE_CONFIG", "config.yml"),
        "prompt_root": str(_prompt_root()),
        "models": {name: preset_dict(p) for name, p in cfg.models.items()},
        "alias_data_policies": {name: alias_data_policy(cfg, name).view() for name in cfg.models},
        "connections": {
            name: {
                **conn.model_dump(mode="json"),
                **({"home": conn.home or "default"} if conn.kind == "subscription" else {}),
                "show_quota": conn.show_quota,
                "effective_data_policy": connection_data_policy(conn),
            }
            for name, conn in cfg.connections.items()
        },
        "routes": {name: route.model_dump(mode="json") for name, route in cfg.routes.items()},
        "route_sanctions": {name: _route_sanction_dict(cfg, name) for name in cfg.routes},
        "pools": {name: pool.model_dump(mode="json") for name, pool in cfg.pools.items()},
        "clients": {name: client.model_dump(mode="json") for name, client in cfg.clients.items()},
        "catalog": {"drivers": driver_catalog(), "pool": pool_catalog()},
        "skill_definitions": {
            name: definition.model_dump()
            for name, definition in cfg.skill_definitions.items()
        },
        "agent_definitions": {
            name: definition.model_dump()
            for name, definition in cfg.agent_definitions.items()
        },
        "mcps": {name: definition.model_dump() for name, definition in cfg.mcps.items()},
        "prompt_files": {
            name: definition.model_dump() for name, definition in cfg.prompt_files.items()
        },
        "settings": _settings_to_dict(),
    }


@router.post(
    "/reload",
    summary="設定リロード",
    description=(
        "config.yml を再読み込みして設定を更新します。\n"
        "サーバーを再起動せずにモデル設定の変更を反映できます。"
    ),
)
async def reload_config_endpoint():
    try:
        cfg = reload_config()
        if cfg.referenced_secret_names():
            await cfg.validate_secret_references_async()
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    model_ids = list(cfg.models.keys()) or ["kotoyose-chat"]
    log.info("admin.config.reloaded", models=model_ids)
    return {
        "status": "ok",
        "message": "設定をリロードしました",
        "models": model_ids,
    }


@router.get(
    "/config",
    summary="設定確認",
    description=(
        "現在読み込まれている設定の内容を返します。\n"
        "モデルプリセット・デフォルト設定・MCP サーバー設定などを確認できます。"
    ),
)
async def show_config():
    return _config_to_dict()


def _admin_agent(alias, policy, service, config) -> dict | None:
    agent = published_agent(alias, policy, service.config if service is not None else None, config=config)
    return with_sanction(agent, config, alias)


@router.get("/models", summary="モデル一覧と Agent 実行 mode")
async def list_admin_models(request: Request, client: str | None = None):
    config = get_config()
    service = getattr(request.app.state, "execution_service", None)
    principal = client or getattr(request.state, "execution_principal", None)
    policy = get_clients(config).get(principal) if principal is not None else None
    if client is not None and policy is None:
        raise HTTPException(status_code=404, detail="Unknown client")
    return {"data": [
        {"id": alias, "agent": _admin_agent(alias, policy, service, config)}
        for alias in config.models
    ]}


@router.get("/native/options", summary="Native alias の選択肢取得")
async def native_options(request: Request):
    """編集に必要な公開 policy 上限だけを返し、認証情報は投影しない。"""
    service = getattr(request.app.state, "execution_service", None)
    kotoyose = get_config()
    clients = get_clients(kotoyose)
    if service is None:
        return {"enabled": False, "clients": [], "routes": [], "drivers": [], "agents": []}
    config = service.config
    return {
        "enabled": True,
        "clients": [
            {
                "name": name,
                "models": list(policy.models),
                "places": list(policy.places),
                "max_surface": policy.max_surface,
                "allowed_agent_models": [alias for alias in kotoyose.models
                                         if agent_model_allowed(alias, policy)],
                "workspace_roots": [str(root) for root in policy.workspace_roots],
                "max_run_seconds": policy.max_run_seconds,
                "max_memory_bytes": policy.max_memory_bytes,
                "max_processes": policy.max_processes,
                "max_cpus": policy.max_cpus,
            }
            for name, policy in clients.items()
        ],
        "routes": [
            {"name": name, **route.model_dump(mode="json"), **_route_sanction_dict(kotoyose, name)}
            for name, route in kotoyose.routes.items()
        ],
        "drivers": [
            {"name": name, **install.model_dump(mode="json")}
            for name, install in (config.drivers or {}).items()
        ],
        "agents": [
            {"alias": alias, "route": preset.route, "place": preset.agent.place, "surface": preset.agent.surface}
            for alias, preset in kotoyose.models.items() if preset.agent is not None
        ],
    }


class UpdateSettingsRequest(BaseModel):
    """Settings の部分更新。未指定の field は保存済み値を維持する。"""

    anthropic_auth_token: str | None = None
    anthropic_base_url: str | None = None


@router.get("/settings", summary="Provider credentials 設定取得")
async def show_settings():
    """Settings を返す。secret は設定状態と末尾 4 文字だけを公開する。"""
    return _settings_to_dict()


@router.put("/settings", summary="Provider credentials 設定更新")
async def update_settings(body: UpdateSettingsRequest):
    """Settings を部分更新する。secret の空文字は削除、未指定は変更しない。"""
    new_cfg = get_config().model_copy(deep=True)
    for field in body.model_fields_set:
        value = getattr(body, field)
        # secret / plain を問わず空文字は「削除」。YAML に "" を残さない。
        if value == "":
            value = None
        setattr(new_cfg.settings, field, value)
    try:
        new_cfg.settings = new_cfg.settings.model_validate(new_cfg.settings.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await _validate_and_save_config_or_422(new_cfg)
    return {"status": "ok"}


@router.get(
    "/browse",
    summary="ファイルブラウザ",
    description=(
        "指定ディレクトリの内容を返します。\n"
        "管理 UI のディレクトリ選択ダイアログで使用します。\n"
        "path を省略すると HOME ディレクトリを返します。"
    ),
)
async def browse_files(path: str = Query(default="~")):
    target = Path(os.path.expanduser(path)).resolve()
    cfg = get_config()
    root_candidates = [Path.home()]
    for preset in cfg.models.values():
        root_candidates.append(Path(preset.cwd))
        root_candidates.extend(Path(root) for root in (preset.cwd_allowed_roots or []))
    root_candidates.extend(
        Path(root)
        for root in os.environ.get("KOTOYOSE_BROWSE_ROOTS", "").split(os.pathsep)
        if root
    )
    # target と root の両方を resolve し、symlink 経由の root 外逃逸も同じ境界判定で拒否する。
    allowed_roots = [root.expanduser().resolve() for root in root_candidates]
    if not any(target.is_relative_to(root) for root in allowed_roots):
        raise HTTPException(status_code=403, detail="許可されたディレクトリの範囲外です")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail=f"'{target}' はディレクトリではありません")

    entries = []
    try:
        for entry in sorted(target.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())):
            entries.append({
                "name": entry.name,
                "path": str(entry),
                "is_dir": entry.is_dir(),
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="アクセス権限がありません")

    parent = None if target in allowed_roots else str(target.parent)
    return {"current": str(target), "parent": parent, "entries": entries}


@router.get("/providers/{provider}/models", summary="Provider のモデル候補取得")
async def provider_models(
    request: Request,
    provider: str,
    endpoint_base_url: str | None = None,
    endpoint_api_key_env: str | None = None,
    model_id: str | None = None,
    connection: str | None = None,
):
    """Provider で利用可能な model ID 候補を取得する。

    Args:
        provider: 対象 Provider ID。
        endpoint_base_url: route に明示された endpoint の base URL。
        endpoint_api_key_env: endpoint の token を読む環境変数名。
        model_id: 選択された model alias。
        connection: 選択された connection ID。

    Returns:
        候補、取得元、取得日時、警告を含む discovery 結果。

    Raises:
        HTTPException: 不正な組み合わせ (400) または未対応 Provider / 不明な接続 (404)。
    """
    try:
        kwargs: dict[str, Any] = {}
        if connection is not None:
            if (
                endpoint_base_url is not None
                or endpoint_api_key_env is not None
                or model_id is not None
            ):
                raise HTTPException(
                    status_code=400,
                    detail="connection cannot be combined with endpoint overrides or model_id",
                )
            kwargs["connection"] = connection
        elif model_id is not None:
            if endpoint_base_url is not None or endpoint_api_key_env is not None:
                raise HTTPException(status_code=400, detail="model_id cannot be combined with endpoint overrides")
            cfg = get_config()
            if model_id not in cfg.models:
                raise ValueError("Unknown model alias")
            preset = cfg.preset_for(model_id)
            route = cfg.routes.get(preset.route) if preset.route is not None else None
            effective_provider = (
                cfg.connections[route.connection].provider
                if route is not None and route.profile is not None else preset.provider
            )
            if effective_provider.lower() != provider.lower():
                raise ValueError("Model alias belongs to another provider")
            kwargs["preset"] = preset
        service = getattr(request.app.state, "execution_service", None)
        if service is not None and (connection is not None or model_id is not None):
            kwargs["execution_service"] = service
        return await discover_models(
            provider.lower(),
            endpoint_base_url=endpoint_base_url,
            endpoint_api_key_env=endpoint_api_key_env,
            **kwargs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


class TrainingApproval(BaseModel):
    capabilities: list[TrainingCapability] = Field(min_length=1)
    note: str = Field(min_length=1)

    @field_validator("note")
    @classmethod
    def nonempty_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Training acknowledgement requires a note")
        return value.strip()


class AdminModelPreset(ModelPreset):
    training_acknowledgement: TrainingApproval | None = Field(default=None, exclude=True)


def _set_model(cfg, alias: str, preset: ModelPreset, owner: str) -> None:
    cfg.models[alias] = preset
    approval = getattr(preset, "training_acknowledgement", None)
    if approval is None:
        return
    try:
        decision = alias_data_policy(cfg, alias)
        if not decision.restricted:
            raise ValueError("Training acknowledgement requires a restricted route")
        digest = training_digest(cfg, alias, approval.capabilities)
    except (KeyError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    cfg.training_acks = [ack for ack in cfg.training_acks if ack.alias != alias]
    cfg.training_acks.append(TrainingAck(
        alias=alias, digest=digest, reasons=[decision.policy], capabilities=approval.capabilities,
        acknowledged_by=owner, at=datetime.now(timezone.utc).isoformat(), note=approval.note,
    ))


class CreateModelRequest(BaseModel):
    model_id: str
    preset: AdminModelPreset


class RenameModelRequest(BaseModel):
    new_name: str = Field(min_length=1)
    preset: AdminModelPreset


def _prompt_root() -> Path:
    """管理 UI が新規 prompt file を作成できる安全な保存先を返す。"""
    configured = os.environ.get("KOTOYOSE_PROMPT_ROOT")
    if configured:
        return Path(os.path.abspath(os.fspath(Path(configured).expanduser())))
    config_path = Path(os.path.abspath(os.fspath(Path(os.environ.get("KOTOYOSE_CONFIG", "config.yml")).expanduser())))
    return config_path.parent / "prompts"


class PromptFileRequest(BaseModel):
    """Admin API 用の prompt file 定義。path 省略時は管理 root の既定名を使う。"""

    description: str = ""
    path: str = ""

    def to_definition(self, name: str) -> PromptFileDefinition:
        """保存用の必須 path 定義へ変換する。"""
        path = self.path or str(_prompt_root() / f"{name}.md")
        return PromptFileDefinition(description=self.description, path=path)


def _reject_unsafe_prompt_path(exc: OSError) -> None:
    """openat の symlink / 非 directory エラーを一貫して API エラーへ変換する。"""
    if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
        raise HTTPException(status_code=403, detail="prompt file の path に symlink は使用できません") from exc
    raise exc


def _managed_prompt_parts(path: str) -> tuple[Path, tuple[str, ...]]:
    """root からの純粋な相対 component を返す (filesystem 解決は行わない)。"""
    root = _prompt_root()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    target = Path(os.path.abspath(os.fspath(candidate)))
    if not target.is_relative_to(root) or target == root:
        raise HTTPException(
            status_code=403,
            detail=f"prompt file は管理用保存先 '{root}' の配下に置いてください",
        )
    return root, target.relative_to(root).parts


def _open_directory_no_symlinks(path: Path, *, create: bool) -> int:
    """絶対 path の各 component を O_NOFOLLOW で開く。"""
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for component in path.parts[1:]:
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
            except FileNotFoundError:
                if not create:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                except OSError as exc:
                    _reject_unsafe_prompt_path(exc)
            except OSError as exc:
                _reject_unsafe_prompt_path(exc)
            os.close(fd)
            fd = child_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def _open_managed_prompt_file(path: str, *, write: bool):
    """symlink を辿らず、root 配下に固定した prompt file descriptor を開く。"""
    root, parts = _managed_prompt_parts(path)
    directory_fd = _open_directory_no_symlinks(root, create=write)
    try:
        for component in parts[:-1]:
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=directory_fd,
                )
            except FileNotFoundError:
                if not write:
                    raise
                try:
                    os.mkdir(component, mode=0o700, dir_fd=directory_fd)
                except FileExistsError:
                    pass
                try:
                    child_fd = os.open(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                except OSError as exc:
                    _reject_unsafe_prompt_path(exc)
            except OSError as exc:
                _reject_unsafe_prompt_path(exc)
            os.close(directory_fd)
            directory_fd = child_fd
        flags = (os.O_WRONLY | os.O_CREAT) if write else os.O_RDONLY
        try:
            file_fd = os.open(parts[-1], flags | os.O_NOFOLLOW, 0o600, dir_fd=directory_fd)
        except OSError as exc:
            _reject_unsafe_prompt_path(exc)
        try:
            file_stat = os.fstat(file_fd)
            if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
                raise HTTPException(status_code=403, detail="prompt file は通常の非リンク file でなければなりません")
            yield file_fd
        finally:
            os.close(file_fd)
    finally:
        os.close(directory_fd)


@router.post("/models", summary="モデル追加")
async def create_model(body: CreateModelRequest, owner: str = Depends(admin_username)):
    cfg = get_config()
    if body.model_id in cfg.models or body.model_id in cfg.pools:
        raise HTTPException(status_code=409, detail=f"Model '{body.model_id}' already exists")
    new_cfg = cfg.model_copy(deep=True)
    _set_model(new_cfg, body.model_id, body.preset, owner)
    await _validate_and_save_config_or_422(new_cfg)
    log.info("admin.model.created", model_id=body.model_id)
    return {"status": "ok", "model_id": body.model_id}


@router.put("/models/{model_id}", summary="モデル更新")
async def update_model(model_id: str, preset: AdminModelPreset, owner: str = Depends(admin_username)):
    cfg = get_config()
    if model_id not in cfg.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    new_cfg = cfg.model_copy(deep=True)
    _set_model(new_cfg, model_id, preset, owner)
    await _validate_and_save_config_or_422(new_cfg)
    log.info("admin.model.updated", model_id=model_id)
    return {"status": "ok", "model_id": model_id}


@router.post("/models/{model_id}/rename", summary="モデル alias の名前変更")
async def rename_model(model_id: str, body: RenameModelRequest, owner: str = Depends(admin_username)):
    cfg = get_config()
    if model_id not in cfg.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    if body.new_name != model_id and (body.new_name in cfg.models or body.new_name in cfg.pools):
        raise HTTPException(status_code=409, detail=f"Model '{body.new_name}' already exists")
    new_cfg = cfg.model_copy(deep=True)
    if body.new_name != model_id:
        del new_cfg.models[model_id]
        new_cfg.training_acks = [ack for ack in new_cfg.training_acks if ack.alias != model_id]
        # pool alias の候補は alias 名で参照している。名前だけ追従させる。
        for pool in new_cfg.pools.values():
            pool.candidates = [body.new_name if item == model_id else item for item in pool.candidates]
    _set_model(new_cfg, body.new_name, body.preset, owner)
    _save_config_or_422(new_cfg)
    return {"status": "ok", "model_id": body.new_name}


@router.delete("/models/{model_id}", summary="モデル削除")
async def delete_model(model_id: str):
    cfg = get_config()
    if model_id not in cfg.models:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    if pools := [name for name, pool in cfg.pools.items() if model_id in pool.candidates]:
        raise HTTPException(
            status_code=409, detail=f"Model '{model_id}' is referenced by pool: {', '.join(pools)}")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.models[model_id]
    new_cfg.training_acks = [ack for ack in new_cfg.training_acks if ack.alias != model_id]
    _save_config_or_422(new_cfg)
    log.info("admin.model.deleted", model_id=model_id)
    return {"status": "ok", "model_id": model_id}


def _resource_references(field: str, resource_name: str) -> list[str]:
    """モデルから個別リソースへの参照元を収集する。"""
    return [
        name
        for name, preset in get_config().models.items()
        if (
            resource_name in getattr(preset, field)
            if isinstance(getattr(preset, field), list)
            else resource_name == getattr(preset, field)
        )
    ]


async def _save_resource(kind: str, name: str, value: Any, is_update: bool):
    """個別リソースを作成または更新し、全体参照整合性を保って保存する。"""
    cfg = get_config()
    collection = getattr(cfg, kind)
    if is_update and name not in collection:
        raise HTTPException(status_code=404, detail=f"{kind} '{name}' not found")
    if not is_update and name in collection:
        raise HTTPException(status_code=409, detail=f"{kind} '{name}' already exists")
    new_cfg = cfg.model_copy(deep=True)
    getattr(new_cfg, kind)[name] = value
    await _validate_and_save_config_or_422(new_cfg)
    return {"status": "ok", "name": name}


@router.post("/skills/{name}", summary="個別 Skill 追加")
async def create_skill(name: str, definition: SkillDefinition):
    return await _save_resource("skill_definitions", name, definition, False)


@router.put("/skills/{name}", summary="個別 Skill 更新")
async def update_skill(name: str, definition: SkillDefinition):
    return await _save_resource("skill_definitions", name, definition, True)


@router.delete("/skills/{name}", summary="個別 Skill 削除")
async def delete_skill(name: str):
    cfg = get_config()
    if name not in cfg.skill_definitions:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    if references := _resource_references("skill_refs", name):
        raise HTTPException(status_code=409, detail=f"Skill '{name}' is referenced by: {', '.join(references)}")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.skill_definitions[name]
    _save_config_or_422(new_cfg)
    return {"status": "ok", "name": name}


@router.post("/agents/{name}", summary="個別 agent 追加")
async def create_agent(name: str, definition: AgentPreset):
    return await _save_resource("agent_definitions", name, definition, False)


@router.put("/agents/{name}", summary="個別 agent 更新")
async def update_agent(name: str, definition: AgentPreset):
    return await _save_resource("agent_definitions", name, definition, True)


@router.delete("/agents/{name}", summary="個別 agent 削除")
async def delete_agent(name: str):
    cfg = get_config()
    if name not in cfg.agent_definitions:
        raise HTTPException(status_code=404, detail=f"agent '{name}' not found")
    if references := _resource_references("agent_refs", name):
        raise HTTPException(
            status_code=409, detail=f"agent '{name}' is referenced by: {', '.join(references)}"
        )
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.agent_definitions[name]
    _save_config_or_422(new_cfg)
    return {"status": "ok", "name": name}


async def _mcp_probe_disconnected(request: Request) -> bool:
    """Wait for disconnect after body parsing; immediate polling misses BaseHTTPMiddleware checkpoints."""
    while True:
        if (await request.receive())["type"] == "http.disconnect":
            return True


@router.post("/mcp-probe", summary="未保存 MCP の疎通確認")
async def probe_mcp_draft(request: Request):
    """設定を保存せず、明示された一件の initialize / tools/list だけを確認する。"""
    from ..mcp_probe import MAX_CONFIG_BYTES, probe_error, run_mcp_probe
    import asyncio

    try:
        # Require JSON (as the CRUD endpoints do); simple cross-origin form posts must
        # not trigger configured commands using the browser's cached Basic credentials.
        if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise ValueError()
        body = bytearray()
        async with asyncio.timeout(5):
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_CONFIG_BYTES:
                    raise ValueError()
        payload = json.loads(body)
        if not isinstance(payload, dict) or set(payload) != {"server"}:
            raise ValueError()
    except (ValueError, UnicodeError, TimeoutError, RecursionError):
        return {**probe_error("validation", "invalid_config", "server を含むサイズ上限以内の JSON を指定してください"), "elapsed_ms": 0}
    return await run_mcp_probe(payload["server"], disconnected=lambda: _mcp_probe_disconnected(request))


@router.post("/mcps/{name}/probe", summary="保存済み MCP の疎通確認")
async def probe_saved_mcp(name: str, request: Request):
    from ..mcp_probe import run_mcp_probe

    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        raise HTTPException(status_code=415, detail="application/json is required")
    definition = get_config().mcps.get(name)
    if definition is None:
        raise HTTPException(status_code=404, detail="MCP not found")
    return await run_mcp_probe(definition.server, disconnected=lambda: _mcp_probe_disconnected(request))


@router.post("/mcps/{name}", summary="個別 MCP 追加")
async def create_mcp(name: str, definition: McpDefinition):
    return await _save_resource("mcps", name, definition, False)


@router.put("/mcps/{name}", summary="個別 MCP 更新")
async def update_mcp(name: str, definition: McpDefinition):
    return await _save_resource("mcps", name, definition, True)


@router.delete("/mcps/{name}", summary="個別 MCP 削除")
async def delete_mcp(name: str):
    cfg = get_config()
    if name not in cfg.mcps:
        raise HTTPException(status_code=404, detail=f"MCP '{name}' not found")
    if references := _resource_references("mcp_refs", name):
        raise HTTPException(status_code=409, detail=f"MCP '{name}' is referenced by: {', '.join(references)}")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.mcps[name]
    _save_config_or_422(new_cfg)
    return {"status": "ok", "name": name}


@router.post("/prompt-files/{name}", summary="説明付き prompt file 追加")
async def create_prompt_file(name: str, body: PromptFileRequest):
    definition = body.to_definition(name)
    return await _save_resource("prompt_files", name, definition, False)


class PromptFileContentRequest(BaseModel):
    content: str = ""


class ResourceRenameRequest(BaseModel):
    """個別リソースの参照名を変更するリクエスト。"""

    new_name: str

    @field_validator("new_name")
    @classmethod
    def _reject_blank_name(cls, value: str) -> str:
        """空白だけの名前では参照先を特定できないため拒否する。"""
        normalized = value.strip()
        if not normalized:
            raise ValueError("new_name は空白だけにできません")
        if "/" in normalized:
            raise ValueError("new_name に '/' は使用できません")
        return normalized


class PromptFileRenameRequest(ResourceRenameRequest):
    """Prompt file の参照名を変更するリクエスト。"""

    description: str | None = None
    path: str | None = None


class McpRenameRequest(ResourceRenameRequest):
    """MCP の参照名を変更するリクエスト。"""

    description: str | None = None
    server: dict[str, object] | None = None


class AgentRenameRequest(ResourceRenameRequest):
    """個別 agent の参照名と定義を変更するリクエスト。"""

    description: str | None = None
    prompt: str | None = None
    tools: list[str] | None = None
    model: Literal["haiku", "sonnet", "opus", "inherit"] | None = None


async def _rename_resource(
    collection_name: str,
    refs_field: str,
    resource_label: str,
    name: str,
    new_name: str,
    mutate: Callable[[Any], Any],
) -> dict[str, str]:
    """個別リソース名と全 preset の参照を 1 回の保存で変更する。"""
    cfg = get_config()
    collection = getattr(cfg, collection_name)
    if name not in collection:
        raise HTTPException(status_code=404, detail=f"{resource_label} '{name}' not found")
    if new_name == name:
        return {"status": "ok", "name": name}
    if new_name in collection:
        raise HTTPException(status_code=409, detail=f"{resource_label} '{new_name}' already exists")

    new_cfg = cfg.model_copy(deep=True)
    new_collection = getattr(new_cfg, collection_name)
    definition = mutate(new_collection.pop(name))
    new_collection[new_name] = definition
    for preset in new_cfg.models.values():
        references = getattr(preset, refs_field)
        if isinstance(references, list):
            setattr(
                preset,
                refs_field,
                [new_name if reference == name else reference for reference in references],
            )
        elif references == name:
            setattr(preset, refs_field, new_name)
    _save_config_or_422(new_cfg)
    log.info(f"admin.{collection_name.removesuffix('s')}.renamed", old=name, new=new_name)
    return {"status": "ok", "name": new_name}


@router.post("/prompt-files/{name}/rename", summary="prompt file の名前変更")
async def rename_prompt_file(name: str, body: PromptFileRenameRequest):
    """Prompt file の参照名と全 preset の参照を 1 回の保存で変更する。"""
    def mutate(definition: PromptFileDefinition) -> PromptFileDefinition:
        if body.description is not None:
            definition.description = body.description
        if body.path is not None:
            definition.path = body.path
        return definition

    return await _rename_resource(
        "prompt_files", "prompt_file_refs", "prompt file", name, body.new_name, mutate
    )


@router.post("/mcps/{name}/rename", summary="MCP の名前変更")
async def rename_mcp(name: str, body: McpRenameRequest):
    """MCP の参照名と全 preset の参照を 1 回の保存で変更する。"""
    def mutate(definition: McpDefinition) -> McpDefinition:
        # in-place 更新でなく再構築するのは server の validator を通すため (pydantic は
        # 属性代入を検証しない)。save_config の全体検証より的確な 422 を返せる。
        try:
            return McpDefinition(
                description=definition.description if body.description is None else body.description,
                server=definition.server if body.server is None else body.server,
            )
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _rename_resource("mcps", "mcp_refs", "MCP", name, body.new_name, mutate)


@router.post("/agents/{name}/rename", summary="個別 agent の名前変更")
async def rename_agent(name: str, body: AgentRenameRequest):
    """agent 名と全 preset の agent_refs を 1 回の保存で変更する。"""
    def mutate(definition: AgentPreset) -> AgentPreset:
        values = definition.model_dump()
        # tools:null (継承) と「送っていない」を区別するため model_fields_set で判定する。
        for field in ("description", "prompt", "tools", "model"):
            if field in body.model_fields_set:
                values[field] = getattr(body, field)
        try:
            return AgentPreset.model_validate(values)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    return await _rename_resource(
        "agent_definitions", "agent_refs", "agent", name, body.new_name, mutate
    )


@router.put("/prompt-files/{name}/content", summary="管理対象 prompt file 作成・更新")
async def write_prompt_file_content(name: str, body: PromptFileContentRequest):
    """KOTOYOSE_PROMPT_ROOT 配下に限り prompt file の内容を安全に保存する。"""
    definition = get_config().prompt_files.get(name)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"prompt file '{name}' not found")
    root, parts = _managed_prompt_parts(definition.path)
    with _open_managed_prompt_file(definition.path, write=True) as file_fd:
        os.ftruncate(file_fd, 0)
        with os.fdopen(file_fd, "w", encoding="utf-8", closefd=False) as file:
            file.write(body.content)
    return {"status": "ok", "path": str(root.joinpath(*parts))}


@router.get("/prompt-files/{name}/content", summary="prompt file 内容取得")
async def read_prompt_file_content(name: str):
    """登録済み prompt file の内容を返し、書き込み可否を明示する。"""
    definition = get_config().prompt_files.get(name)
    if definition is None:
        raise HTTPException(status_code=404, detail=f"prompt file '{name}' not found")
    try:
        root, parts = _managed_prompt_parts(definition.path)
    except HTTPException as exc:
        if exc.status_code != 403:
            raise
        # runtime が既に読み込む登録済み external path は、preview でも読み取りだけ許可する。
        path = Path(os.path.expanduser(definition.path))
        try:
            content = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            content = ""
        except IsADirectoryError as exc:
            raise HTTPException(status_code=422, detail="prompt file の path は directory を指せません") from exc
        except UnicodeDecodeError as exc:
            raise HTTPException(status_code=422, detail="prompt file は UTF-8 でなければなりません") from exc
        return {"content": content, "path": str(path), "writable": False}

    try:
        with _open_managed_prompt_file(definition.path, write=False) as file_fd:
            with os.fdopen(file_fd, "r", encoding="utf-8", closefd=False) as file:
                content = file.read()
    except FileNotFoundError:
        content = ""
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=422, detail="prompt file は UTF-8 でなければなりません") from exc
    return {"content": content, "path": str(root.joinpath(*parts)), "writable": True}


@router.put("/prompt-files/{name}", summary="説明付き prompt file 更新")
async def update_prompt_file(name: str, body: PromptFileRequest):
    definition = body.to_definition(name)
    return await _save_resource("prompt_files", name, definition, True)


@router.delete("/prompt-files/{name}", summary="説明付き prompt file 削除")
async def delete_prompt_file(name: str):
    cfg = get_config()
    if name not in cfg.prompt_files:
        raise HTTPException(status_code=404, detail=f"prompt file '{name}' not found")
    if references := _resource_references("prompt_file_refs", name):
        raise HTTPException(status_code=409, detail=f"prompt file '{name}' is referenced by: {', '.join(references)}")
    new_cfg = cfg.model_copy(deep=True)
    del new_cfg.prompt_files[name]
    _save_config_or_422(new_cfg)
    return {"status": "ok", "name": name}
