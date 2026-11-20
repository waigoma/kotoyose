"""mTLS Remote Worker client と response-loss recovery。"""

from __future__ import annotations

import asyncio
import base64
import http.client
import json
import hashlib
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from .execution_jobs import CommandJob, WorkerResult
from .remote_worker_contract import (
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteOperationState,
    RemoteStopRequest,
    RemoteStopResult,
    RemoteWorkerRef,
    request_digest,
)
from .remote_worker_contract import read_workspace_challenge
from .remote_worker_identity import certificate_fingerprint, create_client_context
from .remote_worker_journal import RemoteWorkerJournal

_MAX_RESPONSE = 2 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Endpoint:
    host: str
    port: int


class RemoteWorkerClient:
    """同一 operation を再送せず、response-loss は GET で回復する client。"""

    def __init__(
        self,
        *,
        base_url: str,
        worker: RemoteWorkerRef,
        ca_file,
        certificate_file,
        key_file,
        journal: RemoteWorkerJournal,
        workspace_ref: str,
        workspace_generation: int,
        timeout_seconds: float = 20.0,
    ) -> None:
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or parsed.path not in ("", "/") or parsed.query or parsed.fragment:
            raise ValueError("base_url must be an https origin without path or query")
        if not parsed.hostname or parsed.port is None:
            raise ValueError("base_url must include host and port")
        self.endpoint = _Endpoint(parsed.hostname, parsed.port)
        self.worker = worker
        self.context = create_client_context(ca_file, certificate_file, key_file)
        self.journal = journal
        self.workspace_ref = workspace_ref
        self.workspace_generation = workspace_generation
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.timeout_seconds = timeout_seconds

    async def probe(self) -> bool:
        try:
            status, payload = await asyncio.to_thread(self._request, "GET", "/v1/remote/health", None)
            return status == 200 and payload.get("status") == "ok"
        except (OSError, ValueError, json.JSONDecodeError):
            return False

    async def verify_shared_binding(self, local_path, remote_workspace_ref: str, generation: int, challenge_relative: str) -> bool:
        """Kotoyose 側と remote 側が同じ challenge bytes を読めることを確認する。"""
        try:
            local_canonical = local_path.resolve(strict=True)
            challenge_bytes = read_workspace_challenge(local_canonical, challenge_relative)
            local_digest = hashlib.sha256(challenge_bytes).hexdigest()
            status, payload = await asyncio.to_thread(
                self._request,
                "GET",
                f"/v1/remote/workspaces/{remote_workspace_ref}/binding",
                None,
                {"X-Kotoyose-Workspace-Generation": str(generation)},
            )
            return (
                status == 200
                and payload.get("workspace_ref") == remote_workspace_ref
                and payload.get("workspace_generation") == generation
                and payload.get("challenge_digest") == local_digest
            )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError):
            return False

    async def execute(self, job: CommandJob, cancel: asyncio.Event) -> WorkerResult:
        operation_id = f"remote-{job.attempt_id}-{job.generation}"
        draft = RemoteCommandRequest(
            operation_id=operation_id,
            attempt_id=job.attempt_id,
            generation=job.generation,
            request_digest="0" * 64,
            worker=self.worker,
            workspace_ref=self.workspace_ref,
            workspace_generation=self.workspace_generation,
            argv=job.argv,
            cwd=job.cwd,
            env=job.env,
            timeout_seconds=job.timeout_seconds,
            grace_seconds=job.grace_seconds,
            output_limit_bytes=job.output_limit_bytes,
            memory_bytes=job.memory_bytes,
            pids_limit=job.pids_limit,
            cpus=job.cpus,
        )
        request = draft.model_copy(update={"request_digest": request_digest(draft)})
        state = self.journal.prepare_operation(
            operation_id,
            job.attempt_id,
            job.generation,
            request.request_digest,
            self.worker,
            self.workspace_ref,
            self.workspace_generation,
        )
        if state.result is not None and state.state == "confirmed":
            return _result_from_wire(state.result)
        if state.state != "prepared" or not self.journal.mark_running(operation_id):
            recovered = await self._recover(operation_id, request.request_digest)
            if recovered is not None:
                return recovered
            return WorkerResult("worker_lost", None, b"", b"", "unconfirmed", "operation is already in flight")

        request_task = asyncio.create_task(asyncio.to_thread(self._request, "POST", "/v1/remote/execute", request.model_dump(mode="json")))
        cancel_task = asyncio.create_task(cancel.wait())
        try:
            done, _ = await asyncio.wait((request_task, cancel_task), return_when=asyncio.FIRST_COMPLETED)
            if cancel_task in done and cancel.is_set() and request_task not in done:
                stop = await self.stop(job.attempt_id, job.generation, job.grace_seconds)
                if stop == "confirmed":
                    try:
                        await asyncio.wait_for(
                            asyncio.shield(request_task),
                            timeout=job.grace_seconds + 10,
                        )
                    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError):
                        self.journal.mark_unknown(operation_id)
                    await self._recover_until_confirmed(operation_id, request.request_digest)
                    return WorkerResult("cancelled", None, b"", b"", "confirmed", "remote stop confirmed")
                self.journal.mark_unknown(operation_id)
                if not request_task.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(request_task), timeout=job.grace_seconds + 2)
                    except (OSError, ValueError, TimeoutError, asyncio.TimeoutError):
                        self.journal.mark_unknown(operation_id)
                return WorkerResult("worker_lost", None, b"", b"", "unconfirmed", "remote stop was not confirmed")
            status, payload = await request_task
            if status != 200:
                raise OSError(f"remote execute returned HTTP {status}")
            wire_state = RemoteOperationState.model_validate(payload)
            if (
                wire_state.operation_id != operation_id
                or wire_state.request_digest != request.request_digest
                or wire_state.result is None
                or wire_state.result.operation_id != operation_id
                or wire_state.state != ("confirmed" if wire_state.result.stop_confirmation == "confirmed" else "unknown")
            ):
                raise OSError("remote execute response identity is invalid")
            self.journal.save_result(wire_state.result)
            return _result_from_wire(wire_state.result)
        except (OSError, ValueError, json.JSONDecodeError, socket.timeout, TimeoutError):
            self.journal.mark_unknown(operation_id)
            recovered = await self._recover_until_confirmed(operation_id, request.request_digest)
            if recovered is not None:
                return recovered
            return WorkerResult("worker_lost", None, b"", b"", "unconfirmed", "remote response is unknown")
        finally:
            if not cancel_task.done():
                cancel_task.cancel()
            await asyncio.gather(cancel_task, return_exceptions=True)
            if not request_task.done():
                request_task.add_done_callback(_consume_task_exception)

    async def _recover(self, operation_id: str, digest: str) -> WorkerResult | None:
        try:
            status, payload = await asyncio.to_thread(
                self._request, "GET", f"/v1/remote/operations/{operation_id}", None
            )
            if status != 200:
                return None
            state = RemoteOperationState.model_validate(payload)
            if (
                state.operation_id != operation_id
                or state.request_digest != digest
                or state.result is None
                or state.result.operation_id != operation_id
                or state.state != ("confirmed" if state.result.stop_confirmation == "confirmed" else "unknown")
            ):
                return None
            self.journal.save_result(state.result)
            return _result_from_wire(state.result)
        except (OSError, ValueError, json.JSONDecodeError, socket.timeout, TimeoutError):
            return None

    async def _recover_until_confirmed(self, operation_id: str, digest: str) -> WorkerResult | None:
        """再起動直後の短い通信断を待つが、同じ operation の GET だけを行う。"""
        for attempt in range(30):
            recovered = await self._recover(operation_id, digest)
            if recovered is not None:
                return recovered
            if attempt < 29:
                await asyncio.sleep(0.1)
        return None

    async def stop(self, attempt_id: str, generation: int, grace_seconds: float) -> str:
        operation_id = f"remote-{attempt_id}-{generation}"
        request = RemoteStopRequest(
            operation_id=operation_id,
            attempt_id=attempt_id,
            generation=generation,
            worker=self.worker,
            grace_seconds=grace_seconds,
        )
        # Docker の stop 応答と inspect の反映には短い競合窓があるため、同じ
        # operation の stop 確認だけを bounded に再照会する。execute は再送しない。
        for attempt_number in range(3):
            try:
                status, payload = await asyncio.to_thread(
                    self._request, "POST", "/v1/remote/stop", request.model_dump(mode="json")
                )
                if status == 200:
                    result = RemoteStopResult.model_validate(payload)
                    if result.operation_id == operation_id and result.stop_confirmation == "confirmed":
                        return "confirmed"
            except (OSError, ValueError, json.JSONDecodeError, socket.timeout, TimeoutError):
                pass
            if attempt_number < 2:
                await asyncio.sleep(0.05)
        return "unconfirmed"

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if not path.startswith("/v1/remote/") or "?" in path or "#" in path or ".." in path:
            raise ValueError("invalid remote path")
        connection = http.client.HTTPSConnection(
            self.endpoint.host, self.endpoint.port, context=self.context, timeout=self.timeout_seconds
        )
        headers = {
            "X-Kotoyose-Worker-Id": self.worker.worker_id,
            "X-Kotoyose-Worker-Generation": str(self.worker.generation),
            "Connection": "close",
        }
        if extra_headers:
            headers.update(extra_headers)
        body = None
        if payload is not None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.connect()
            peer = connection.sock.getpeercert(binary_form=True) if connection.sock else None
            expected = self.worker.server_certificate_fingerprint
            if expected is None or not peer or certificate_fingerprint(peer) != expected:
                raise OSError("remote server certificate fingerprint mismatch")
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            raw = response.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise ValueError("remote response exceeded size limit")
            decoded = json.loads(raw)
            if not isinstance(decoded, dict):
                raise ValueError("remote response must be an object")
            return response.status, decoded
        finally:
            connection.close()


def _result_from_wire(result: RemoteCommandResult) -> WorkerResult:
    try:
        stdout = base64.b64decode(result.stdout, validate=True)
        stderr = base64.b64decode(result.stderr, validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("remote result output is not valid base64") from exc
    return WorkerResult(result.status, result.exit_code, stdout, stderr, result.stop_confirmation, result.reason)


def _consume_task_exception(task: asyncio.Task) -> None:
    """cancel で待ち切れない HTTP thread の例外を未回収にしない。"""
    try:
        task.exception()
    except BaseException:
        return
