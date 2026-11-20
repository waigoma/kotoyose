"""Authentication shared by compatibility and native HTTP entry points."""

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import get_config
from .execution_resources import ExecutionError, agent_profile_allowed, authenticate_client
from .runtime_identity import require_expected_identity

_bearer = HTTPBearer(auto_error=False)


async def authorize_execution(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
) -> None:
    token = credentials.credentials if credentials is not None else None
    store = getattr(request.app.state, "api_key_store", None)
    key = await store.authenticate(token) if store is not None and token and token.startswith("kys_") else None
    if key is None:
        raise HTTPException(status_code=401, detail="Invalid or missing API key", headers={"WWW-Authenticate": "Bearer"})
    if not key["caller"]:
        raise HTTPException(
            status_code=401,
            detail="API key has no client bound; re-issue this key with a client",
            headers={"WWW-Authenticate": "Bearer"},
        )
    principal, policy = authenticate_client(key["caller"])
    service = getattr(request.app.state, "execution_service", None)
    if service is not None:
        require_expected_identity(service, principal, request.headers)
    elif any(name in request.headers for name in ("x-kotoyose-runtime-store-id", "x-kotoyose-caller-namespace")):
        raise ExecutionError("unsupported_capability", "Native runtime identity is unavailable", 501)
    request.state.api_key_name = key["name"]
    request.state.api_key_caller = key["caller"]
    request.state.execution_principal = principal
    request.state.execution_policy = policy


def enforce_model_scope(request: Request, alias: str) -> None:
    """Enforce client model/profile scope when set."""
    state = getattr(request, "state", None)
    policy = getattr(state, "execution_policy", None)
    if policy is None:
        return
    service = getattr(request.app.state, "execution_service", None)
    settings = getattr(service, "config", None)
    if not agent_profile_allowed(alias, policy, settings, config=get_config()):
        raise ExecutionError("permission_denied", f"Model '{alias}' is outside this API key's client scope", 403)


def execution_service(request: Request):
    service = getattr(request.app.state, "execution_service", None)
    if service is None:
        raise ExecutionError("unsupported_capability", "Native execution is not configured", 501)
    return service
