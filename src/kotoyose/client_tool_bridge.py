"""Native provider の client tool call を durable に仲介する。"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Any, Literal, Mapping, Callable, Awaitable
from uuid import uuid4

import asyncpg

from .client_tool_digest import client_tool_arguments_canonical, normalize_client_tool_arguments
from .execution_resources import ExecutionError, canonical_json
from .agent_execution_authority import agent_execution_lease_guard
from .agent_execution_contract import AgentExecutionError
from .execution_contract import ContractModel
from pydantic import Field
from jsonschema import Draft202012Validator, ValidationError


CLIENT_TOOL_MAX_ARGUMENT_BYTES = 1024 * 1024
ClientCallState = Literal["pending", "sent", "succeeded", "failed", "unknown", "client_unavailable", "cancelled"]
_APPROVAL_REASONS = frozenset({
    "approved_within_grant", "blocking_wait_elapsed", "new_grant_required", "approval_rejected",
    "approval_expired", "approval_invalidated", "approval_cancelled", "approval_consumed",
})


@dataclass(frozen=True, slots=True)
class ClientToolCallOutcome:
    """provider callback が扱う durable call の結果。"""

    state: ClientCallState
    result: dict[str, Any] | list[dict] | None = None
    required_action: dict[str, str] | None = None
    call_id: str | None = None


class ClientConnectionRequest(ContractModel):
    """外部 client が claim する attempt-bound connection。"""

    schema_version: Literal["1"]
    client_key: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31-1, strict=True)


class ClientToolResultRequest(ContractModel):
    """client call の一回限りの結果 envelope。"""

    schema_version: Literal["1"]
    client_key: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=0, le=2**31-1, strict=True)
    tool_ref: str = Field(min_length=1, max_length=512)
    arguments_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    disposition: Literal["applied", "not_applied", "unknown"]
    result: dict[str, Any]


class ClientToolReconciliationRequest(ClientToolResultRequest):
    """現接続世代の Caller が、過去 call の結果を再実行せず照合した証拠。"""

    disposition: Literal["applied", "not_applied"]
    evidence_ref: str = Field(min_length=1, max_length=2048)


_CLIENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS kotoyose.execution_client_connections (
    id text PRIMARY KEY, attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    principal text NOT NULL, client_key text NOT NULL, generation bigint NOT NULL,
    state text NOT NULL DEFAULT 'active', created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (attempt_id, principal, client_key, generation)
);
CREATE UNIQUE INDEX IF NOT EXISTS execution_client_active_key
    ON kotoyose.execution_client_connections(attempt_id, principal, client_key)
    WHERE state='active';
CREATE TABLE IF NOT EXISTS kotoyose.execution_client_calls (
    id text PRIMARY KEY, attempt_id text NOT NULL REFERENCES kotoyose.execution_attempts(id),
    principal text NOT NULL, connection_id text NOT NULL REFERENCES kotoyose.execution_client_connections(id),
    client_key text NOT NULL, generation bigint NOT NULL, tool_ref text NOT NULL,
    arguments jsonb NOT NULL, arguments_digest text NOT NULL,
    operation_id text NOT NULL UNIQUE REFERENCES kotoyose.execution_operations(id) DEFERRABLE INITIALLY DEFERRED,
    state text NOT NULL, deadline timestamptz NOT NULL, result jsonb,
    created_at timestamptz NOT NULL DEFAULT now(), sent_at timestamptz, finished_at timestamptz,
    UNIQUE (attempt_id, id)
);
ALTER TABLE kotoyose.execution_client_calls ADD COLUMN IF NOT EXISTS reconciliation_ref text;
"""


class ClientToolRepository:
    """connection claim と call / operation の同時 durable 更新を担当する。"""

    def __init__(self, pool: asyncpg.Pool, event_sink: Callable[..., Awaitable[None]] | None = None):
        self.pool = pool
        self.event_sink = event_sink
        self.agent_execution_policy = None

    async def initialize(self) -> None:
        await self.pool.execute(_CLIENT_SCHEMA)

    async def register_connection(
        self, principal: str, attempt_id: str, client_key: str, generation: int,
    ) -> dict:
        self._validate_key(client_key, generation)
        is_agent_exec = bool(self.agent_execution_policy and await self.agent_execution_policy.is_agent_exec(attempt_id))
        async with self.pool.acquire() as conn, conn.transaction():
            if is_agent_exec:
                current = await conn.fetchval('''SELECT a.attempt_id FROM kotoyose.agent_executions e
                    JOIN kotoyose.agent_execution_activations a ON a.execution_id=e.id AND a.nonce=e.current_nonce
                    WHERE e.principal=$1 AND a.attempt_id=$2 FOR SHARE OF e''', principal, attempt_id)
                if current is None:
                    raise ExecutionError("request_conflict", "Client activation changed", 409)
                minimum = await self.agent_execution_policy.min_generation_in_transaction(conn, attempt_id, client_key)
                if generation < minimum:
                    raise ExecutionError("request_conflict", "Client connection generation is stale", 409)
            attempt = await conn.fetchrow("SELECT * FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE", attempt_id, principal)
            if attempt is None:
                raise ExecutionError("invalid_request", "Unknown attempt", 404)
            if attempt["resumed_by"] is not None:
                raise ExecutionError("request_conflict", "Attempt was fenced by resume", 409)
            if json.loads(attempt["snapshot"]).get("client_key") != client_key:
                raise ExecutionError("permission_denied", "Client key is not bound to this Attempt", 403)
            prior = await conn.fetchrow(
                "SELECT * FROM kotoyose.execution_client_connections WHERE attempt_id=$1 AND principal=$2 "
                "AND client_key=$3 ORDER BY generation DESC LIMIT 1 FOR UPDATE",
                attempt_id, principal, client_key,
            )
            if prior is not None:
                if generation < prior["generation"]:
                    raise ExecutionError("request_conflict", "Client connection generation is stale", 409)
                if generation == prior["generation"]:
                    if prior["state"] != "active":
                        raise ExecutionError("request_conflict", "Client connection is revoked", 403)
                    return self._connection(prior)
                if not is_agent_exec and generation != prior["generation"] + 1:
                    raise ExecutionError("request_conflict", "Client connection generation skipped", 409)
                await conn.execute(
                    "UPDATE kotoyose.execution_client_connections SET state='revoked' "
                    "WHERE attempt_id=$1 AND principal=$2 AND client_key=$3 AND state='active'",
                    attempt_id, principal, client_key,
                )
                # 未送信のみ次世代へ移せる。送信済みは適用有無を推測しない。
                await conn.execute(
                    "UPDATE kotoyose.execution_client_calls SET state='unknown',finished_at=now() "
                    "WHERE connection_id=$1 AND state='sent'", prior["id"],
                )
                await conn.execute(
                    "UPDATE kotoyose.execution_operations SET state='unknown',updated_at=now() WHERE id IN "
                    "(SELECT operation_id FROM kotoyose.execution_client_calls WHERE connection_id=$1 AND state='unknown')",
                    prior["id"],
                )
            row = await conn.fetchrow(
                "INSERT INTO kotoyose.execution_client_connections(id,attempt_id,principal,client_key,generation) "
                "VALUES($1,$2,$3,$4,$5) RETURNING *", str(uuid4()), attempt_id, principal, client_key, generation,
            )
            if prior is not None:
                await conn.execute(
                    "UPDATE kotoyose.execution_client_calls SET connection_id=$2,generation=$3 "
                    "WHERE connection_id=$1 AND state='pending'", prior["id"], row["id"], generation,
                )
            return self._connection(row)

    async def pending(
        self, principal: str, attempt_id: str, client_key: str, generation: int, limit: int = 32,
    ) -> list[dict]:
        self._validate_key(client_key, generation)
        if not 0 < limit <= 100:
            raise ValueError("client call limit must be between 1 and 100")
        async with self.pool.acquire() as conn, conn.transaction():
            await self._authorized_attempt(conn, principal, attempt_id)
            connection = await self._active_connection(conn, principal, attempt_id, client_key, generation)
            rows = await conn.fetch(
                "SELECT * FROM kotoyose.execution_client_calls WHERE connection_id=$1 AND state IN ('pending','sent') "
                "AND deadline > now() ORDER BY created_at LIMIT $2 FOR UPDATE SKIP LOCKED",
                connection["id"], limit,
            )
            if rows:
                await conn.execute(
                    "UPDATE kotoyose.execution_client_calls SET state='sent',sent_at=now() "
                    "WHERE id=ANY($1::text[]) AND state='pending'", [row["id"] for row in rows],
                )
                await conn.execute(
                    "UPDATE kotoyose.execution_operations SET state='sent',updated_at=now() WHERE id=ANY($1::text[])",
                    [row["operation_id"] for row in rows],
                )
                if self.event_sink:
                    for row in rows:
                        if row["state"] == "pending":
                            await self.event_sink(conn, attempt_id, "client_tool_sent", {
                                "call_id": row["id"], "operation_id": row["operation_id"], "tool_ref": row["tool_ref"],
                                "arguments_digest": row["arguments_digest"], "generation": generation,
                            })
            await conn.execute(
                "UPDATE kotoyose.execution_client_connections SET last_seen_at=now() WHERE id=$1", connection["id"],
            )
            return [self._call(row, state="sent" if row["state"] == "pending" else row["state"]) for row in rows]

    async def create_call(
        self, principal: str, attempt_id: str, client_key: str, generation: int,
        tool_ref: str, arguments: Mapping[str, Any], deadline: datetime,
    ) -> dict | ClientToolCallOutcome:
        self._validate_key(client_key, generation)
        if not isinstance(arguments, Mapping):
            raise ExecutionError("invalid_request", "Client tool arguments must be an object", 400)
        try:
            arguments = normalize_client_tool_arguments(dict(arguments))
            canonical = client_tool_arguments_canonical(arguments)
            encoded = canonical.encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ExecutionError("invalid_request", "Client tool arguments are not JSON", 400) from exc
        if not encoded or len(encoded) > CLIENT_TOOL_MAX_ARGUMENT_BYTES:
            raise ExecutionError("invalid_request", "Client tool arguments exceed the limit", 400)
        digest = hashlib.sha256(encoded).hexdigest()
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await self._authorized_attempt(conn, principal, attempt_id)
            connection = await self._active_connection(conn, principal, attempt_id, client_key, generation)
            snapshot = attempt["snapshot"]
            if isinstance(snapshot, str):
                snapshot = json.loads(snapshot)
            allowed = {item.get("resource_ref") for item in snapshot.get("client_tools", [])}
            allowed.update(snapshot.get("tool_refs", []))
            if tool_ref not in allowed:
                raise ExecutionError("permission_denied", "Tool is not pinned in the Attempt snapshot", 403)
            if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM kotoyose.execution_operations WHERE attempt_id=$1 AND state='unknown')", attempt_id):
                raise ExecutionError("request_conflict", "Prior operation is unresolved", 409)
            if await conn.fetchval("SELECT count(*) FROM kotoyose.execution_client_calls WHERE attempt_id=$1", attempt_id) >= 1000:
                raise ExecutionError("request_conflict", "Attempt client call limit reached", 409)
            if snapshot.get("client_key") != client_key:
                raise ExecutionError("permission_denied", "Client key does not match the pinned request", 403)
            spec = next((item for item in snapshot.get("client_tools", []) if item["resource_ref"] == tool_ref), None)
            if spec:
                if len(encoded) > spec["max_payload_bytes"]:
                    raise ExecutionError("invalid_request", "Tool arguments exceed pinned limit", 400)
                try:
                    Draft202012Validator(spec["input_schema"]).validate(arguments)
                except ValidationError as exc:
                    raise ExecutionError("invalid_request", "Tool arguments do not match pinned schema", 400) from exc
                deadline = min(deadline, datetime.now(timezone.utc) + timedelta(seconds=spec["timeout_seconds"]))
            if tool_ref == "builtin:request-approval@1":
                if set(arguments) != {"approval_id", "subject_digest"}:
                    raise ExecutionError("invalid_request", "Approval arguments have an invalid shape", 400)
                binding = next((item for item in snapshot.get("approval_bindings", [])
                                if item.get("approval_id") == arguments["approval_id"]), None)
                if binding is None or binding.get("subject_digest") != arguments["subject_digest"]:
                    raise ExecutionError("permission_denied", "Approval arguments are outside the pinned binding", 403)
            is_agent_exec = attempt['api_operation'] == 'agent_activation'
            if is_agent_exec and self.agent_execution_policy:
                prior = await self.agent_execution_policy.replay_in_transaction(conn, attempt_id, tool_ref, digest)
                if prior is not None:
                    return prior
            call_id = str(uuid4())
            operation_id = "client-tool:" + call_id
            row = await conn.fetchrow(
                "INSERT INTO kotoyose.execution_client_calls(id,attempt_id,principal,connection_id,client_key,generation,"
                "tool_ref,arguments,arguments_digest,operation_id,state,deadline) VALUES($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,$10,'pending',$11) RETURNING *",
                call_id, attempt_id, principal, connection["id"], client_key, generation, tool_ref,
                canonical, digest, operation_id, deadline,
            )
            await conn.execute(
                "INSERT INTO kotoyose.execution_operations(id,attempt_id,kind,state) VALUES($1,$2,'client_tool','prepared')",
                operation_id, attempt_id,
            )
            if is_agent_exec and tool_ref == "builtin:request-approval@1":
                result = {**arguments, "outcome": "approval_required", "reason": "new_grant_required"}
                row = await conn.fetchrow("UPDATE kotoyose.execution_client_calls SET state='failed',result=$2::jsonb,finished_at=now() WHERE id=$1 RETURNING *", call_id, canonical_json(result))
                await conn.execute("UPDATE kotoyose.execution_operations SET state='confirmed',result=$2::jsonb,updated_at=now() WHERE id=$1", operation_id, canonical_json(result))
            if self.event_sink:
                await self.event_sink(conn, attempt_id, "operation_prepared", {
                    "operation_id": operation_id, "kind": "client_tool", "call_id": call_id,
                    "tool_ref": tool_ref, "arguments_digest": digest,
                })
            return self._call(row)

    async def call(
        self, principal: str, attempt_id: str, tool_ref: str, arguments: Mapping[str, Any],
        deadline: datetime, cancel: asyncio.Event | None = None, *, approval_clock=None,
    ) -> ClientToolCallOutcome:
        """provider callback 用の current connection 選択・永続化・bounded wait。"""
        attempt = await self.pool.fetchrow("SELECT snapshot FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2", attempt_id, principal)
        snapshot = json.loads(attempt["snapshot"]) if attempt else {}
        client_key = snapshot.get("client_key")
        if not client_key:
            return ClientToolCallOutcome("client_unavailable")
        connection = await self.pool.fetchrow(
            "SELECT client_key,generation FROM kotoyose.execution_client_connections "
            "WHERE attempt_id=$1 AND principal=$2 AND client_key=$3 AND state='active' "
            "ORDER BY generation DESC LIMIT 1", attempt_id, principal, client_key,
        )
        if connection is None:
            generation = await self.agent_execution_policy.min_generation(attempt_id, client_key) if self.agent_execution_policy and await self.agent_execution_policy.is_agent_exec(attempt_id) else 0
            connection = await self.register_connection(principal, attempt_id, client_key, generation)
        call = await self.create_call(
            principal, attempt_id, connection["client_key"], connection["generation"],
            tool_ref, arguments, deadline,
        )
        if isinstance(call, ClientToolCallOutcome):
            return call
        if call['state'] == 'failed':
            return await self.wait(principal, attempt_id, call['id'], cancel)
        if tool_ref != "builtin:request-approval@1" or approval_clock is None:
            return await self.wait(principal, attempt_id, call["id"], cancel)
        approval_clock.approval_wait_started()
        try:
            return await self.wait(principal, attempt_id, call["id"], cancel)
        finally:
            approval_clock.approval_wait_finished()

    async def wait(self, principal: str, attempt_id: str, call_id: str, cancel: asyncio.Event | None = None) -> ClientToolCallOutcome:
        while True:
            row = await self.pool.fetchrow(
                "SELECT * FROM kotoyose.execution_client_calls WHERE id=$1 AND attempt_id=$2 AND principal=$3",
                call_id, attempt_id, principal,
            )
            if row is None:
                raise ExecutionError("invalid_request", "Unknown client call", 404)
            state = row["state"]
            if state in {"succeeded", "failed", "unknown", "client_unavailable", "cancelled"}:
                result = row["result"]
                if isinstance(result, str):
                    result = json.loads(result)
                action = self._required_action(result) if row["tool_ref"] == "builtin:request-approval@1" else None
                if action is not None:
                    action["call_id"] = call_id
                return ClientToolCallOutcome(state, result, action, call_id)
            if cancel is not None and cancel.is_set():
                await self.cancel(principal, attempt_id, call_id)
                continue
            if row["tool_ref"] == "builtin:request-approval@1" and await self._expire_approval_wait(row):
                continue
            if row["deadline"] <= datetime.now(timezone.utc):
                await self._settle(principal, attempt_id, call_id, "client_unavailable")
                continue
            await asyncio.sleep(0.05)

    async def save_result(
        self, principal: str, attempt_id: str, call_id: str, client_key: str, generation: int,
        result: Mapping[str, Any],
        *, tool_ref: str, arguments_digest: str, disposition: str, reconciliation_ref: str | None = None,
        agent_execution_context: tuple | None = None,
    ) -> dict:
        self._validate_key(client_key, generation)
        try:
            encoded = canonical_json(dict(result)).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise ExecutionError("invalid_request", "Client result is not JSON", 400) from exc
        if len(encoded) > CLIENT_TOOL_MAX_ARGUMENT_BYTES:
            raise ExecutionError("invalid_request", "Client result exceeds the limit", 400)
        async with self.pool.acquire() as conn, conn.transaction():
            connection_attempt = attempt_id
            if agent_execution_context is not None:
                execution_id, nonce, current_attempt, reconciliation = agent_execution_context
                current = await conn.fetchrow('''SELECT e.current_nonce,a.attempt_id FROM kotoyose.agent_executions e
                    JOIN kotoyose.agent_execution_activations a ON a.execution_id=e.id AND a.nonce=e.current_nonce
                    WHERE e.id=$1 AND e.principal=$2 FOR UPDATE OF e''', execution_id, principal)
                if not reconciliation and current and current['current_nonce'] != nonce:
                    raise AgentExecutionError("stale_activation", "Client activation changed", 409)
                if not current or current['attempt_id'] != current_attempt:
                    raise ExecutionError("request_conflict", "Client activation changed", 409)
                connection_attempt = current_attempt
            attempt = await conn.fetchrow("SELECT snapshot,status FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE", attempt_id, principal)
            await self._active_connection(conn, principal, connection_attempt, client_key, generation)
            row = await conn.fetchrow(
                "SELECT * FROM kotoyose.execution_client_calls WHERE id=$1 AND attempt_id=$2 AND principal=$3 FOR UPDATE",
                call_id, attempt_id, principal,
            )
            if row is None:
                raise ExecutionError("invalid_request", "Unknown client call", 404)
            if row["client_key"] != client_key or (row["generation"] != generation and reconciliation_ref is None):
                raise ExecutionError("request_conflict", "Client connection generation is stale", 409)
            if reconciliation_ref is not None and (row["state"] not in {"unknown", "succeeded", "failed"} or generation < row["generation"]):
                raise ExecutionError("request_conflict", "Only a dispatched unknown call can be reconciled", 409)
            if (row["tool_ref"], row["arguments_digest"]) != (tool_ref, arguments_digest):
                raise ExecutionError("request_conflict", "Client result identifies another tool invocation", 409)
            if disposition not in {"applied", "not_applied", "unknown"}:
                raise ExecutionError("invalid_request", "Invalid result disposition", 400)
            snapshot = json.loads(attempt["snapshot"])
            spec = next((item for item in snapshot.get("client_tools", []) if item["resource_ref"] == tool_ref), None)
            if spec and len(encoded) > spec["max_payload_bytes"]:
                raise ExecutionError("invalid_request", "Tool result exceeds pinned limit", 400)
            if row["tool_ref"] == "builtin:request-approval@1":
                if disposition != "applied":
                    raise ExecutionError("invalid_request", "Approval control result must be definitive", 400)
                if set(result) != {"outcome", "approval_id", "subject_digest", "reason"}:
                    raise ExecutionError("invalid_request", "Approval result has an invalid shape", 400)
                if result["outcome"] not in {"approved", "approval_required"} or result["reason"] not in _APPROVAL_REASONS:
                    raise ExecutionError("invalid_request", "Approval result has an invalid outcome", 400)
                args = row["arguments"] if isinstance(row["arguments"], dict) else json.loads(row["arguments"])
                if (result["approval_id"], result["subject_digest"]) != (args["approval_id"], args["subject_digest"]):
                    raise ExecutionError("request_conflict", "Approval result does not match the call", 409)
                if result["outcome"] == "approved" and result["reason"] != "approved_within_grant":
                    raise ExecutionError("invalid_request", "Approved result has an invalid reason", 400)
                if result["outcome"] == "approval_required" and result["reason"] == "approved_within_grant":
                    raise ExecutionError("invalid_request", "Suspended result has an invalid reason", 400)
            if row["state"] in {"succeeded", "failed"}:
                if row["reconciliation_ref"] != reconciliation_ref:
                    raise ExecutionError("request_conflict", "Reconciliation evidence changed", 409)
                expected_state = ("failed" if result.get("outcome") == "approval_required" else "succeeded") if tool_ref == "builtin:request-approval@1" else {"applied": "succeeded", "not_applied": "failed", "unknown": "unknown"}[disposition]
                if expected_state != row["state"]:
                    raise ExecutionError("request_conflict", "Result disposition contradicts the prior result", 409)
                old = row["result"]
                if canonical_json(old if isinstance(old, dict) else json.loads(old)) != canonical_json(dict(result)):
                    raise ExecutionError("request_conflict", "Client result contradicts the terminal result", 409)
                return self._call(row)
            if row["state"] not in {"sent", "unknown"}:
                raise ExecutionError("request_conflict", "Client call was not sent", 409)
            if agent_execution_context is not None and reconciliation_ref is not None and row['state'] == 'unknown':
                # settlement と照合が競合しても、元の不明な call を再開時に識別できるようにする。
                await conn.execute('''INSERT INTO kotoyose.agent_execution_client_replays
                    (execution_id,call_id,tool_ref,arguments_digest) VALUES($1,$2,$3,$4)
                    ON CONFLICT DO NOTHING''', agent_execution_context[0], call_id, tool_ref, arguments_digest)
            if tool_ref == "builtin:request-approval@1" and result["outcome"] == "approved":
                binding = next(item for item in snapshot["approval_bindings"] if item["approval_id"] == result["approval_id"])
                wait_seconds = 0 if binding.get("mode") == "asynchronous" else binding["wait_seconds"]
                if attempt["status"] != "running" or datetime.now(timezone.utc) >= row["created_at"] + timedelta(seconds=wait_seconds):
                    raise ExecutionError("request_conflict", "An expired Approval wait cannot continue the Attempt", 409)
            terminal_state = {"applied": "succeeded", "not_applied": "failed", "unknown": "unknown"}[disposition]
            if row["tool_ref"] == "builtin:request-approval@1" and result.get("outcome") == "approval_required":
                terminal_state = "failed"
            row = await conn.fetchrow(
                "UPDATE kotoyose.execution_client_calls SET state=$2,result=$3::jsonb,reconciliation_ref=$4,finished_at=now() "
                "WHERE id=$1 AND state IN ('sent','unknown') RETURNING *", call_id, terminal_state,
                canonical_json(dict(result)), reconciliation_ref,
            )
            await conn.execute(
                "UPDATE kotoyose.execution_operations SET state=$3,result=$2::jsonb,updated_at=now() WHERE id=$1",
                row["operation_id"], canonical_json(dict(result)), "unknown" if terminal_state == "unknown" else "confirmed",
            )
            if self.event_sink:
                await self.event_sink(conn, attempt_id, "client_tool_result", {
                    "call_id": call_id, "operation_id": row["operation_id"], "tool_ref": tool_ref,
                    "arguments_digest": arguments_digest, "state": terminal_state,
                    "reconciliation_ref": reconciliation_ref,
                    "control": dict(result) if tool_ref == "builtin:request-approval@1" else None,
                })
            return self._call(row)

    async def _expire_approval_wait(self, call: asyncpg.Record) -> bool:
        """Caller が切断しても、固定した待機期限を provider の判断に委ねない。"""
        async with self.pool.acquire() as conn, conn.transaction():
            attempt = await conn.fetchrow("SELECT snapshot FROM kotoyose.execution_attempts WHERE id=$1 FOR UPDATE", call["attempt_id"])
            snapshot = json.loads(attempt["snapshot"])
            args = json.loads(call["arguments"]) if isinstance(call["arguments"], str) else call["arguments"]
            binding = next(item for item in snapshot["approval_bindings"] if item["approval_id"] == args["approval_id"])
            seconds = 0 if binding.get("mode") == "asynchronous" else binding["wait_seconds"]
            if datetime.now(timezone.utc) < min(call["deadline"], call["created_at"] + timedelta(seconds=seconds)):
                return False
            result = {**args, "outcome": "approval_required", "reason": "blocking_wait_elapsed"}
            row = await conn.fetchrow(
                "UPDATE kotoyose.execution_client_calls SET state='failed',result=$2::jsonb,finished_at=now() "
                "WHERE id=$1 AND state IN ('pending','sent') RETURNING operation_id", call["id"], canonical_json(result),
            )
            if row:
                await conn.execute("UPDATE kotoyose.execution_operations SET state='confirmed',result=$2::jsonb,updated_at=now() WHERE id=$1", row["operation_id"], canonical_json(result))
                if self.event_sink:
                    await self.event_sink(conn, call["attempt_id"], "client_tool_result", {
                        "call_id": call["id"], "operation_id": row["operation_id"], "tool_ref": call["tool_ref"],
                        "arguments_digest": call["arguments_digest"], "state": "failed", "control": result,
                    })
            return True

    async def cancel(self, principal: str, attempt_id: str, call_id: str) -> None:
        await self._settle(principal, attempt_id, call_id, "cancelled")

    async def _settle(self, principal: str, attempt_id: str, call_id: str, state: str) -> None:
        async with self.pool.acquire() as conn, conn.transaction():
            await conn.fetchrow("SELECT id FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2 FOR UPDATE", attempt_id, principal)
            row = await conn.fetchrow(
                "UPDATE kotoyose.execution_client_calls SET state=CASE WHEN state='sent' THEN 'unknown' ELSE $4 END,finished_at=now() "
                "WHERE id=$1 AND attempt_id=$2 AND principal=$3 AND state IN ('pending','sent') RETURNING operation_id,state",
                call_id, attempt_id, principal, state,
            )
            if row is not None:
                await conn.execute(
                    "UPDATE kotoyose.execution_operations SET state=$2,updated_at=now() WHERE id=$1",
                    row["operation_id"], "unknown" if row["state"] == "unknown" else "confirmed",
                )

    async def cancel_attempt(self, principal: str, attempt_id: str) -> None:
        rows = await self.pool.fetch(
            "SELECT id FROM kotoyose.execution_client_calls WHERE attempt_id=$1 AND principal=$2 AND state IN ('pending','sent')",
            attempt_id, principal,
        )
        for row in rows:
            await self.cancel(principal, attempt_id, row["id"])

    async def _active_connection(self, conn: asyncpg.Connection, principal: str, attempt_id: str, client_key: str, generation: int) -> asyncpg.Record:
        row = await conn.fetchrow(
            "SELECT * FROM kotoyose.execution_client_connections WHERE attempt_id=$1 AND principal=$2 "
            "AND client_key=$3 AND generation=$4 AND state='active' FOR UPDATE",
            attempt_id, principal, client_key, generation,
        )
        if row is None:
            raise ExecutionError("authentication_failed", "Client connection is not active", 401)
        return row

    async def _authorized_attempt(self, conn: asyncpg.Connection, principal: str, attempt_id: str, *, now=None) -> asyncpg.Record:
        initial = await conn.fetchrow("SELECT request,api_operation FROM kotoyose.execution_attempts WHERE id=$1 AND principal=$2", attempt_id, principal)
        if initial is None:
            raise ExecutionError("invalid_request", "Unknown attempt", 404)
        grant_ref = json.loads(initial["request"])["execution_grant_ref"]
        is_agent_exec = initial['api_operation'] == 'agent_activation'
        if is_agent_exec:
            await agent_execution_lease_guard(conn, attempt_id, now or datetime.now(timezone.utc))
        grant = await conn.fetchrow(
            "SELECT revoked_at,(payload->>'expires_at')::timestamptz > now() AS valid "
            "FROM kotoyose.execution_resources WHERE principal=$1 AND ref=$2 FOR UPDATE", principal, grant_ref,
        )
        attempt = await conn.fetchrow("SELECT * FROM kotoyose.execution_attempts WHERE id=$1 FOR UPDATE", attempt_id)
        if not grant or grant["revoked_at"] or (not is_agent_exec and not grant["valid"]) or attempt["status"] not in {"dispatched", "running"}:
            raise ExecutionError("permission_denied", "Attempt no longer accepts client activity", 403)
        return attempt

    @staticmethod
    def _validate_key(client_key: str, generation: int) -> None:
        if not isinstance(client_key, str) or not 1 <= len(client_key) <= 128 or "\x00" in client_key:
            raise ExecutionError("invalid_request", "Invalid client key", 400)
        if isinstance(generation, bool) or not isinstance(generation, int) or not 0 <= generation <= 2**31-1:
            raise ExecutionError("invalid_request", "Invalid client connection generation", 400)

    @staticmethod
    def _connection(row: asyncpg.Record) -> dict:
        return {key: row[key] for key in ("id", "attempt_id", "client_key", "generation", "state")}

    @staticmethod
    def _call(row: asyncpg.Record, *, state: str | None = None) -> dict:
        result = dict(row)
        result["state"] = state or result["state"]
        for key in ("arguments", "result"):
            if isinstance(result.get(key), str):
                result[key] = json.loads(result[key])
        return {key: result[key] for key in (
            "id", "attempt_id", "client_key", "generation", "tool_ref", "arguments", "arguments_digest",
            "state", "deadline", "created_at", "operation_id", "result",
        )}

    @staticmethod
    def _required_action(result: dict | None) -> dict[str, str] | None:
        if isinstance(result, dict) and result.get("outcome") == "approval_required":
            return {"kind": "approval", **{key: str(result[key]) for key in ("approval_id", "subject_digest") if key in result}}
        return None
