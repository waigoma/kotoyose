"""child の登録コード、一覧、revoke。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..auth import require_basic_auth
from ..child_identity import default_host_child_home
from ..child_registry import child_on_name
from ..child_server import ChildHub
from ..child_workspace import workspace_alias_warnings
from ..config import get_config
from ..print_cli_profiles import get_print_cli_profile

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


class IssueEnrollment(BaseModel):
    address: str = Field(min_length=3, max_length=255)


def child_hub_for(request: Request) -> ChildHub:
    hub = getattr(request.app.state, "child_hub", None)
    if hub is None:
        hub = ChildHub(default_host_child_home())
        request.app.state.child_hub = hub
    return hub


@router.get("/children", summary="List enrolled children")
async def list_children(request: Request):
    children = child_hub_for(request).list_children()
    config = get_config() if any(child["online"] or child["workspace_policy"] is not None for child in children) else None
    for child in children:
        if config is None:
            continue
        report = child["workspace_policy"]
        warnings = workspace_alias_warnings(child["name"], report, config.models, config.routes) if report is not None else []
        if child["online"]:
            warnings.extend(_provider_alias_warnings(child, config))
        if warnings or report is not None:
            child["alias_warnings"] = warnings
    return {"children": children}


def _provider_alias_warnings(child, config):
    warnings = []
    from ..provider_data import UNRESTRICTED, supports_capability
    from ..runtime.child import declared_turn_capability

    approved = {(row["profile"], row["sha256"]): row["capability"] for row in child["providers"]}
    for name, preset in config.models.items():
        if child_on_name(preset.on) != child["name"]:
            continue
        route = config.routes.get(preset.route)
        if route is None or route.driver != "cli.acp" or route.profile is None:
            continue
        try:
            declaration = get_print_cli_profile(route.profile).data_declaration
        except KeyError:
            continue
        if declaration is None:
            continue
        if not declaration.supports(UNRESTRICTED) and (preset.agent is None or preset.agent.surface != "write"):
            continue
        try:
            capability = declared_turn_capability(
                declaration, preset, driver=route.driver, name=child["name"], platform=child.get("platform"),
                features=child["features"], providers=child["providers"],
            )
        except ValueError as exc:
            warnings.append({"alias": name, "message": str(exc)})
            continue
        if capability in ("read", UNRESTRICTED):
            continue
        advertised = approved.get((declaration.profile_id, declaration.sha256), "read")
        if not supports_capability(advertised, capability):
            warnings.append({
                "alias": name,
                "message": f"Child '{child['name']}' has not advertised {capability} approval for the current '{declaration.id}' declaration.",
            })
    return warnings


@router.post("/children/enrollment-codes", summary="Issue a one-time child enrollment code")
async def issue_enrollment_code(body: IssueEnrollment, request: Request):
    hub = child_hub_for(request)
    if not hub.listening:
        raise HTTPException(status_code=409, detail="child listener is off (set KOTOYOSE_CHILD_PORT)")
    try:
        issued = hub.issue_enrollment(body.address)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return JSONResponse(issued, headers={"Cache-Control": "no-store"})


@router.post("/children/{name}/revoke", summary="Revoke a child")
async def revoke_child(name: str, request: Request):
    try:
        return await child_hub_for(request).revoke(name)
    except KeyError:
        raise HTTPException(status_code=404, detail="child not found") from None
