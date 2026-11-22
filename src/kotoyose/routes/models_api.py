from fastapi import APIRouter, Depends, Request

from ..execution_http import authorize_execution
from ..compatibility_http import compatibility_adapter
from ..config import get_config
from ..data_policy import alias_data_policy
from ..agent_route import published_agent
from ..agent_sanction import with_sanction
from ..execution_resources import agent_profile_allowed
from ..model_discovery import cached_reasoning_efforts
from ..model_pools import pool_data_policy, published_pool

router = APIRouter(dependencies=[Depends(authorize_execution)])


def _agent_with_sanction(alias, policy, service, config) -> dict | None:
    """published_agent に route の判定を足す。published_agent 自体は変えない。"""
    return with_sanction(published_agent(alias, policy, service.config, config=config), config, alias)


def _published_model(config, alias: str, policy, service) -> dict:
    """One alias row. reasoning_efforts is added only when the discovery cache knows it."""
    row = {
        "id": alias, "object": "model", "owned_by": "local",
        **alias_data_policy(config, alias).view(),
        "agent": _agent_with_sanction(alias, policy, service, config)
        if service is not None and policy is not None and alias in config.models else None,
    }
    efforts = cached_reasoning_efforts(config, alias) if alias in config.models else None
    if efforts is not None:
        row["reasoning_efforts"] = efforts
    return row


@router.get(
    "/v1/models",
    summary="モデル一覧",
    description=(
        "config.yml の models キーが定義するモデルを返します。\n"
        "Open WebUI が起動時に呼び出すエンドポイントです。"
    ),
)
async def list_models(request: Request):
    config = get_config()
    service = getattr(request.app.state, "execution_service", None)
    policy = getattr(request.state, "execution_policy", None)
    if service is not None and policy is not None:
        adapter = compatibility_adapter(request)
        principal = request.state.execution_principal
        model_ids = [
            alias for alias, preset in config.models.items()
            if preset.execution == "direct" and agent_profile_allowed(alias, policy, service.config, config=config)
        ]
        model_ids.extend(
            alias for alias, profile in adapter.config.callers.get(principal, {}).items()
            if agent_profile_allowed(alias, policy, service.config, config=config)
            and alias not in model_ids
        )
    else:
        model_ids = [
            alias for alias in config.models
            if agent_profile_allowed(alias, policy, None, config=config)
        ]
    data = [_published_model(config, m, policy, service) for m in model_ids]
    if service is not None and policy is not None:
        data.extend(await _pool_entries(request, service, policy, config))
    return {"object": "list", "data": data}


async def pool_view(service, config) -> dict | None:
    """pool の状態。読めないときは None で、公開を止めない。"""
    pools = getattr(getattr(service, "store", None), "model_pools", None)
    if not config.pools or getattr(getattr(service, "store", None), "pool", None) is None or pools is None:
        return None
    try:
        return await pools.view(config)
    except Exception:
        return None


async def _pool_entries(request, service, policy, config) -> list[dict]:
    names = [name for name in config.pools if agent_profile_allowed(name, policy, service.config, config=config)]
    if not names:
        return []
    view = await pool_view(service, config)
    return [{"id": name, "object": "model", "owned_by": "local",
             **pool_data_policy(config, name),
             **published_pool(config, name, (view or {}).get(name))} for name in names]


@router.get(
    "/v1/pools",
    summary="pool alias と候補の状態",
    description="pool alias ごとの戦略・候補・pool の状態 (読み取りのみ。問い合わせはしない)。",
)
async def list_pools(request: Request):
    config = get_config()
    service = getattr(request.app.state, "execution_service", None)
    if service is None or request.state.execution_policy is None:
        return {"object": "list", "data": []}
    policy = request.state.execution_policy
    view = await pool_view(service, config) or {}
    return {"object": "list", "data": [
        {"id": name, **view[name]} for name in config.pools
        if name in view and agent_profile_allowed(name, policy, service.config, config=config)
    ]}
