"""Non-secret identity for the durable Native store and authenticated Caller scope."""
from uuid import UUID, uuid4

from .execution_resources import ExecutionError


async def initialize_runtime_identity(pool) -> str:
    """Keep one identity in the same database as workspaces and Attempts."""
    await pool.execute("""
        CREATE TABLE IF NOT EXISTS kotoyose.runtime_identity (
            singleton boolean PRIMARY KEY CHECK (singleton),
            store_id uuid NOT NULL
        )
    """)
    value = await pool.fetchval(
        "INSERT INTO kotoyose.runtime_identity(singleton,store_id) VALUES(true,$1::uuid) "
        "ON CONFLICT (singleton) DO UPDATE SET singleton=EXCLUDED.singleton RETURNING store_id",
        uuid4(),
    )
    return str(UUID(str(value)))


def runtime_identity(service, principal: str) -> dict[str, str]:
    """Do not pretend that an uninitialized/in-memory store has durable identity."""
    value = getattr(service.store, "runtime_store_id", None)
    if value is None:
        raise ExecutionError("unsupported_capability", "Durable runtime identity is unavailable", 501)
    return {"schema_version": "1", "runtime_store_id": str(UUID(str(value))), "caller_namespace": principal}


def require_expected_identity(service, principal: str, headers) -> None:
    """Check/use fence: reject stale pins before any Native endpoint runs."""
    store = headers.get("x-kotoyose-runtime-store-id")
    caller = headers.get("x-kotoyose-caller-namespace")
    if store is None and caller is None:
        return  # Existing clients remain compatible; they have no identity guarantee.
    if not store or not caller:
        raise ExecutionError("invalid_request", "Both runtime identity headers are required", 400)
    expected = runtime_identity(service, principal)
    if store != expected["runtime_store_id"] or caller != expected["caller_namespace"]:
        raise ExecutionError("request_conflict", "Pinned runtime identity does not match this store and Caller", 409)
