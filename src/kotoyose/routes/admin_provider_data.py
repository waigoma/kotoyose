"""Upload, list, and remove host provider data declarations.

The stored bytes are the request body. Re-serializing would change the content hash.
"""

from fastapi import APIRouter, Depends, HTTPException, Request

from ..auth import require_basic_auth, verify_admin_csrf
from ..child_server import active_child_hub
from ..cli_install_spec import platform_key
from ..config import get_config
from ..provider_data import MAX_DOCUMENT
from ..provider_data_host import Catalog, host_catalog, reload_catalog, remove_declaration, upload_declaration

router = APIRouter(
    prefix="/admin",
    tags=["admin"],
    dependencies=[Depends(require_basic_auth)],
)


def _owner(request: Request) -> None:
    if getattr(request.state, "admin_auth_scheme", None) not in {"basic", "cookie"}:
        raise HTTPException(status_code=403, detail="Changing provider declarations requires owner authentication")
    if request.state.admin_auth_scheme == "basic":
        verify_admin_csrf(request)


def _view(catalog: Catalog) -> dict:
    key = platform_key()
    return {
        "error": catalog.error,
        "startup_warnings": list(get_config().startup_warnings),
        "declarations": [
            {
                "id": row.id,
                "profile_id": row.profile_id,
                "display_name": row.name,
                "publisher": row.publisher,
                "sha256": row.sha256,
                "driver": row.driver,
                "contract_version": row.contract_version,
                "commands": row.commands(key),
                "artifacts": [
                    {
                        "url": url, "sha256": item.artifact.digest, "version": row.install.version,
                        "platform": item.artifact.platform,
                    }
                    for item in row.platforms for url in item.artifact.url_templates
                ],
            }
            for row in catalog.rows
        ] + [
            {
                "id": entry.id or entry.filename,
                "profile_id": entry.profile_id,
                "filename": entry.filename,
                "status": "invalid",
                "reason": entry.reason,
            }
            for entry in catalog.invalid
        ],
    }


async def _publish(catalog):
    hub = active_child_hub()
    if hub is not None:
        await hub.broadcast_catalog()
    return _view(catalog)


@router.get("/provider-declarations", summary="List data provider declarations")
def list_provider_declarations():
    return _view(host_catalog())


async def read_declaration_body(request: Request) -> bytes:
    """Stop at Content-Length, then stop again while streaming, before the body is buffered."""
    header = request.headers.get("content-length")
    if header is None or not header.isascii() or not header.isdigit():
        raise HTTPException(status_code=411, detail="Content-Length is required")
    declared = int(header)
    if declared <= 0 or declared > MAX_DOCUMENT:
        raise HTTPException(status_code=413, detail="declaration exceeds its size limit")
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > declared or len(raw) + len(chunk) > MAX_DOCUMENT:
            raise HTTPException(status_code=413, detail="declaration exceeds its size limit")
        raw.extend(chunk)
    if len(raw) != declared:
        raise HTTPException(status_code=400, detail="Content-Length does not match the body")
    return bytes(raw)


@router.post("/provider-declarations", summary="Upload a data provider declaration")
async def create_provider_declaration(request: Request):
    _owner(request)
    raw = await read_declaration_body(request)
    try:
        catalog = upload_declaration(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _publish(catalog)


@router.post("/provider-declarations/reload", summary="Reload data provider declarations from disk")
async def reload_provider_declarations(request: Request):
    _owner(request)
    return await _publish(reload_catalog())


@router.delete("/provider-declarations/{provider}", summary="Remove a data provider declaration")
async def delete_provider_declaration(provider: str, request: Request):
    _owner(request)
    try:
        catalog = remove_declaration(provider)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="provider declaration was not found") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return await _publish(catalog)
