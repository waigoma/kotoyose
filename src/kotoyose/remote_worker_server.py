"""mTLS で接続する Remote Command Worker server。"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import hashlib
import json
import sqlite3
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from pathlib import PurePosixPath

from .execution_jobs import CommandJob, WorkerResult
from .local_worker import DockerLocalWorker
from .remote_worker_contract import (
    RemoteCommandRequest,
    RemoteCommandResult,
    RemoteOperationState,
    RemoteStopRequest,
    RemoteStopResult,
    RemoteWorkerRef,
    read_workspace_challenge,
    request_digest,
)
from .remote_worker_identity import certificate_file_fingerprint, certificate_fingerprint, create_server_context
from .remote_worker_journal import RemoteWorkerJournal, RemoteWorkspace

_MAX_BODY = 2 * 1024 * 1024
_REQUEST_TIMEOUT = 15.0


class RemoteWorkerServer:
    """登録済み workspace だけを DockerLocalWorker で処理する mTLS server。"""

    def __init__(
        self,
        *,
        journal: RemoteWorkerJournal,
        worker: DockerLocalWorker,
        workspace_root: Path,
        ca_file: Path,
        certificate_file: Path,
        key_file: Path,
        host: str = "127.0.0.1",
        port: int = 0,
    ) -> None:
        root = workspace_root.resolve(strict=True)
        if not root.is_dir() or root == Path("/"):
            raise ValueError("worker workspace root must be a non-root directory")
        self.journal = journal
        self.worker = worker
        self.workspace_root = root
        root_stat = root.stat()
        self._root_identity = (root_stat.st_dev, root_stat.st_ino)
        self._server_certificate_fingerprint = certificate_file_fingerprint(certificate_file)
        self._context = create_server_context(ca_file, certificate_file, key_file)
        self._listen_host = host
        self._listen_port = port
        self._httpd = self._new_httpd()
        self._thread: threading.Thread | None = None
        self._loop = asyncio.new_event_loop()
        self._loop_thread: threading.Thread | None = None
        self._active: dict[str, concurrent.futures.Future] = {}
        self._active_lock = threading.Lock()
        self._shutdown_operations: set[str] = set()
        self._drop_responses: set[str] = set()
        self._drop_lock = threading.Lock()

    def _new_httpd(self) -> _WorkerHTTPServer:
        """再起動時にも同じ listen port で新しい TLS socket を作る。"""
        httpd = _WorkerHTTPServer((self._listen_host, self._listen_port), _RequestHandler, self)
        httpd.socket = self._context.wrap_socket(httpd.socket, server_side=True)
        self._listen_port = int(httpd.server_address[1])
        return httpd

    @property
    def address(self) -> tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        if self._httpd.fileno() < 0:
            self._loop = asyncio.new_event_loop()
            self._httpd = self._new_httpd()
        self.recover_unknown()
        self._loop_thread = threading.Thread(target=self._run_loop, name="kotoyose-remote-worker-loop", daemon=False)
        self._loop_thread.start()
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="kotoyose-remote-worker", daemon=True)
        self._thread.start()

    def stop_server(self) -> None:
        # HTTP shutdown が request thread の完了を待つ前に対象を固定する。
        # 先に snapshot しないと execute が exit 137 を通常 failure として保存できる。
        with self._active_lock:
            active = list(self._active.items())
            self._shutdown_operations.update(operation_id for operation_id, _ in active)
        known = {operation_id for operation_id, _ in active}
        try:
            unsettled = self.journal.unsettled_operations()
        except sqlite3.ProgrammingError:
            # caller が journal を先に close した場合も server.close() を冪等にする。
            unsettled = []
        for state in unsettled:
            if state.operation_id not in known:
                active.append((state.operation_id, None))
                known.add(state.operation_id)
        if self._thread and self._thread.is_alive():
            self._httpd.shutdown()
            self._thread.join(timeout=5)
        for operation_id, future in active:
            identity = self.journal.operation_identity(operation_id)
            if identity:
                try:
                    confirmation = "unconfirmed"
                    if self._loop_thread is not None and self._loop_thread.is_alive() and not self._loop.is_closed():
                        for stop_attempt in range(3):
                            stop_future = asyncio.run_coroutine_threadsafe(
                                self.worker.stop(identity[0], identity[1], 1.0), self._loop
                            )
                            confirmation = stop_future.result(timeout=10)
                            if confirmation == "confirmed":
                                break
                            if stop_attempt < 2:
                                threading.Event().wait(0.05)
                    if confirmation == "confirmed":
                        # server shutdown が command を停止した場合は、exit 137 などの
                        # worker 側終了値を通常の command failure として記録しない。
                        self.journal.save_result(RemoteCommandResult(
                            operation_id=operation_id, status="worker_lost", exit_code=None,
                            stdout="", stderr="", stop_confirmation="confirmed",
                            reason="server stopped; existing operation reconciled",
                        ))
                    else:
                        self.journal.mark_unknown(operation_id)
                except (OSError, RuntimeError, TimeoutError, concurrent.futures.TimeoutError):
                    self.journal.mark_unknown(operation_id)
            if future is not None and not future.done():
                future.cancel()
        if self._loop_thread and self._loop_thread.is_alive():
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._loop_thread.join(timeout=10)
        elif not self._loop.is_closed():
            self._loop.close()
        port = int(self._httpd.server_address[1])
        self._httpd.server_close()
        self._listen_port = port
        self._thread = None
        self._loop_thread = None

    def close(self) -> None:
        self.stop_server()

    def enroll_worker(self, ref: RemoteWorkerRef) -> None:
        if ref.server_certificate_fingerprint != self._server_certificate_fingerprint:
            raise ValueError("worker enrollment is bound to this server certificate")
        self.journal.enroll(ref)

    def revoke_worker(self, worker_id: str, generation: int) -> None:
        self.journal.revoke(worker_id, generation)

    def register_workspace(self, ref: str, path: Path, generation: int, challenge_relative: str | None = None) -> None:
        if not ref or generation < 0:
            raise ValueError("invalid workspace registration")
        canonical = path.resolve(strict=True)
        if not canonical.is_dir() or canonical == self.workspace_root:
            raise ValueError("workspace must be a child directory")
        if not canonical.is_relative_to(self.workspace_root):
            raise ValueError("workspace is outside worker root")
        if path != canonical:
            raise ValueError("workspace path must not be a symlink")
        challenge = ""
        challenge_digest = ""
        if challenge_relative is not None:
            relative = PurePosixPath(challenge_relative)
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("workspace challenge must be relative")
            challenge_bytes = read_workspace_challenge(canonical, relative.as_posix())
            challenge = relative.as_posix()
            challenge_digest = hashlib.sha256(challenge_bytes).hexdigest()
        stat = canonical.stat()
        self.journal.register_workspace(
            RemoteWorkspace(ref, canonical, generation, stat.st_dev, stat.st_ino, challenge, challenge_digest)
        )

    def binding(self, ref: str, generation: int, handler: BaseHTTPRequestHandler) -> dict[str, str | int]:
        self._authenticate_headers(handler)
        workspace = self._workspace(ref, generation)
        if not workspace.challenge_relative:
            raise PermissionError("workspace has no challenge binding")
        try:
            challenge_bytes = read_workspace_challenge(workspace.path, workspace.challenge_relative)
        except (OSError, RuntimeError, ValueError) as exc:
            raise PermissionError("workspace challenge changed") from exc
        if hashlib.sha256(challenge_bytes).hexdigest() != workspace.challenge_digest:
            raise PermissionError("workspace challenge changed")
        return {"workspace_ref": ref, "workspace_generation": generation, "challenge_digest": workspace.challenge_digest}

    def drop_next_response(self, operation_id: str) -> None:
        """テストおよび transport fault injection 用に一度だけ応答を落とす。"""
        with self._drop_lock:
            self._drop_responses.add(operation_id)

    def _should_drop(self, operation_id: str) -> bool:
        with self._drop_lock:
            if operation_id not in self._drop_responses:
                return False
            self._drop_responses.remove(operation_id)
            return True

    def _peer_fingerprint(self, handler: BaseHTTPRequestHandler) -> str:
        certificate = handler.connection.getpeercert(binary_form=True)
        if not certificate:
            raise PermissionError("peer certificate is required")
        return certificate_fingerprint(certificate)

    def _authenticate(self, handler: BaseHTTPRequestHandler, worker: RemoteWorkerRef) -> RemoteWorkerRef:
        actual = self.journal.active_worker(worker.worker_id, worker.generation, self._peer_fingerprint(handler))
        if actual is None or actual != worker:
            raise PermissionError("worker certificate or generation is not enrolled")
        return actual

    def _authenticate_headers(self, handler: BaseHTTPRequestHandler) -> RemoteWorkerRef:
        worker_id = handler.headers.get("X-Kotoyose-Worker-Id", "")
        generation = handler.headers.get("X-Kotoyose-Worker-Generation", "")
        try:
            parsed_generation = int(generation)
        except ValueError as exc:
            raise PermissionError("worker identity headers are required") from exc
        actual = self.journal.active_worker_by_fingerprint(worker_id, parsed_generation, self._peer_fingerprint(handler))
        if actual is None:
            raise PermissionError("worker certificate or generation is not enrolled")
        return actual

    def _workspace(self, ref: str, generation: int) -> RemoteWorkspace:
        workspace = self.journal.workspace(ref, generation)
        if workspace is None:
            raise PermissionError("workspace is not registered")
        try:
            root_stat = self.workspace_root.stat()
            canonical = workspace.path.resolve(strict=True)
            stat = workspace.path.stat()
        except (OSError, RuntimeError) as exc:
            raise PermissionError("registered workspace is unavailable") from exc
        if (
            (root_stat.st_dev, root_stat.st_ino) != self._root_identity
            or canonical != workspace.path
            or not canonical.is_relative_to(self.workspace_root)
            or stat.st_dev != workspace.device
            or stat.st_ino != workspace.inode
            or not workspace.path.is_dir()
        ):
            raise PermissionError("registered workspace identity changed")
        return workspace

    async def execute(self, request: RemoteCommandRequest, handler: BaseHTTPRequestHandler) -> RemoteOperationState:
        self._authenticate(handler, request.worker)
        if request_digest(request) != request.request_digest:
            raise ValueError("request digest mismatch")
        workspace = self._workspace(request.workspace_ref, request.workspace_generation)
        state = self.journal.prepare_operation(
            request.operation_id,
            request.attempt_id,
            request.generation,
            request.request_digest,
            request.worker,
            request.workspace_ref,
            request.workspace_generation,
        )
        if state.state != "prepared":
            return state
        if not self.journal.mark_running(request.operation_id):
            return self.journal.operation(request.operation_id) or state
        job = CommandJob(
            attempt_id=request.attempt_id,
            generation=request.generation,
            workspace=workspace.path,
            argv=request.argv,
            cwd=request.cwd,
            env=request.env,
            timeout_seconds=request.timeout_seconds,
            grace_seconds=request.grace_seconds,
            output_limit_bytes=request.output_limit_bytes,
            memory_bytes=request.memory_bytes,
            pids_limit=request.pids_limit,
            cpus=request.cpus,
        )
        result = await self.worker.execute(job, asyncio.Event())
        with self._active_lock:
            stopped_by_shutdown = request.operation_id in self._shutdown_operations
        if stopped_by_shutdown and result.stop_confirmation == "confirmed":
            result = WorkerResult(
                status="worker_lost", exit_code=None, stdout=b"", stderr=b"",
                stop_confirmation="confirmed", reason="server stopped; existing operation reconciled",
            )
        wire = _result_to_wire(request.operation_id, result)
        return self.journal.save_result(wire)

    def execute_request(self, request: RemoteCommandRequest, handler: BaseHTTPRequestHandler) -> RemoteOperationState:
        future = self._submit(self.execute(request, handler), request.operation_id)
        try:
            return future.result(timeout=request.timeout_seconds + request.grace_seconds + 20)
        except concurrent.futures.TimeoutError as exc:
            self.journal.mark_unknown(request.operation_id)
            raise TimeoutError("remote execution handler exceeded deadline") from exc

    async def stop(self, request: RemoteStopRequest, handler: BaseHTTPRequestHandler) -> RemoteStopResult:
        worker = self._authenticate(handler, request.worker)
        state = self.journal.operation_for_worker(request.operation_id, worker)
        identity = self.journal.operation_identity(request.operation_id)
        if state is None or identity != (request.attempt_id, request.generation):
            return RemoteStopResult(operation_id=request.operation_id, stop_confirmation="unconfirmed")
        confirmation = await self.worker.stop(request.attempt_id, request.generation, request.grace_seconds)
        if confirmation == "confirmed" and state.result is None:
            wire = RemoteCommandResult(
                operation_id=request.operation_id, status="cancelled", exit_code=None,
                stdout="", stderr="", stop_confirmation="confirmed", reason="stop confirmed",
            )
            self.journal.save_result(wire)
        return RemoteStopResult(operation_id=request.operation_id, stop_confirmation=confirmation)

    def stop_request(self, request: RemoteStopRequest, handler: BaseHTTPRequestHandler) -> RemoteStopResult:
        future = self._submit(self.stop(request, handler), None)
        try:
            return future.result(timeout=request.grace_seconds + 15)
        except concurrent.futures.TimeoutError:
            return RemoteStopResult(operation_id=request.operation_id, stop_confirmation="unconfirmed")

    def _submit(self, coroutine, operation_id: str | None):
        future = asyncio.run_coroutine_threadsafe(coroutine, self._loop)
        if operation_id is not None:
            with self._active_lock:
                self._active[operation_id] = future
            future.add_done_callback(lambda _: self._remove_active(operation_id, future))
        return future

    def _remove_active(self, operation_id: str, future) -> None:
        with self._active_lock:
            if self._active.get(operation_id) is future:
                self._active.pop(operation_id, None)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def recover_unknown(self) -> None:
        """再起動時に operation を再 dispatch せず、既存 container の停止確認だけ行う。"""
        for state in self.journal.unsettled_operations():
            if state.state == "prepared":
                self.journal.save_result(RemoteCommandResult(
                    operation_id=state.operation_id, status="worker_lost", exit_code=None,
                    stdout="", stderr="", stop_confirmation="confirmed", reason="server restarted before start",
                ))
                continue
            identity = self.journal.operation_identity(state.operation_id)
            if identity is None:
                continue
            attempt_id, generation = identity
            self.journal.mark_unknown(state.operation_id)
            confirmation = "unconfirmed"
            for _ in range(3):
                try:
                    confirmation = _run_async_blocking(self.worker.stop(attempt_id, generation, 0.5))
                except (OSError, RuntimeError, ValueError, asyncio.TimeoutError):
                    confirmation = "unconfirmed"
                if confirmation == "confirmed":
                    break
            if confirmation == "confirmed":
                self.journal.save_result(RemoteCommandResult(
                    operation_id=state.operation_id, status="worker_lost", exit_code=None,
                    stdout="", stderr="", stop_confirmation="confirmed", reason="server restarted; existing operation reconciled",
                ))


class _WorkerHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, owner: RemoteWorkerServer):
        self.owner = owner
        super().__init__(address, handler)


class _RequestHandler(BaseHTTPRequestHandler):
    server: _WorkerHTTPServer
    protocol_version = "HTTP/1.1"

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(_REQUEST_TIMEOUT)

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:
        path = urlsplit(self.path)
        if path.query:
            self._error(HTTPStatus.BAD_REQUEST, "query is not allowed")
            return
        if path.path == "/v1/remote/health":
            try:
                self.server.owner._authenticate_headers(self)
            except (PermissionError, ValueError) as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc))
                return
            self._write(HTTPStatus.OK, {"status": "ok"})
            return
        if path.path.startswith("/v1/remote/operations/"):
            try:
                worker = self.server.owner._authenticate_headers(self)
            except (PermissionError, ValueError) as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc))
                return
            operation_id = path.path.removeprefix("/v1/remote/operations/")
            if not operation_id or "/" in operation_id:
                self._error(HTTPStatus.NOT_FOUND, "operation not found")
                return
            state = self.server.owner.journal.operation_for_worker(operation_id, worker)
            if state is None:
                self._error(HTTPStatus.NOT_FOUND, "operation not found")
                return
            self._write(HTTPStatus.OK, state.model_dump(mode="json"))
            return
        if path.path.startswith("/v1/remote/workspaces/"):
            try:
                parts = path.path.removeprefix("/v1/remote/workspaces/").split("/")
                if len(parts) != 2 or parts[1] != "binding":
                    raise ValueError("unknown endpoint")
                response = self.server.owner.binding(parts[0], int(self.headers.get("X-Kotoyose-Workspace-Generation", "-1")), self)
            except (PermissionError, ValueError) as exc:
                self._error(HTTPStatus.FORBIDDEN, str(exc))
                return
            self._write(HTTPStatus.OK, response)
            return
        self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def do_POST(self) -> None:
        path = urlsplit(self.path)
        if path.query:
            self._error(HTTPStatus.BAD_REQUEST, "query is not allowed")
            return
        body = self._read_body()
        if body is None:
            return
        try:
            payload = json.loads(body)
            if path.path == "/v1/remote/execute":
                request = RemoteCommandRequest.model_validate(payload)
                state = self.server.owner.execute_request(request, self)
                response = state.model_dump(mode="json")
                if self.server.owner._should_drop(request.operation_id):
                    self.close_connection = True
                    return
                self._write(HTTPStatus.OK, response)
                return
            if path.path == "/v1/remote/stop":
                request = RemoteStopRequest.model_validate(payload)
                response = self.server.owner.stop_request(request, self)
                self._write(HTTPStatus.OK, response.model_dump(mode="json"))
                return
            self._error(HTTPStatus.NOT_FOUND, "unknown endpoint")
        except PermissionError as exc:
            self._error(HTTPStatus.FORBIDDEN, str(exc))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "worker request failed")

    def _read_body(self) -> bytes | None:
        if self.headers.get("Transfer-Encoding"):
            self._error(HTTPStatus.BAD_REQUEST, "chunked requests are not allowed")
            return None
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._error(HTTPStatus.BAD_REQUEST, "invalid content length")
            return None
        if length <= 0 or length > _MAX_BODY:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request body limit exceeded")
            return None
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application/json required")
            return None
        try:
            body = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._error(HTTPStatus.REQUEST_TIMEOUT, "request read timed out")
            return None
        if len(body) != length:
            self._error(HTTPStatus.BAD_REQUEST, "short request body")
            return None
        return body

    def _write(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError:
            # 応答先の切断は実行失敗ではない。確定結果は journal から照合する。
            self.close_connection = True

    def _error(self, status: HTTPStatus, message: str) -> None:
        self._write(status, {"error": message})


def _result_to_wire(operation_id: str, result: WorkerResult) -> RemoteCommandResult:
    return RemoteCommandResult(
        operation_id=operation_id,
        status=result.status,
        exit_code=result.exit_code,
        stdout=base64.b64encode(result.stdout).decode("ascii"),
        stderr=base64.b64encode(result.stderr).decode("ascii"),
        stop_confirmation=result.stop_confirmation,
        reason=result.reason,
    )


def _run_async_blocking(awaitable):
    """既存 event loop の thread からも recovery を同期的に実行する。"""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)
    result: list[object] = []
    error: list[BaseException] = []

    def runner() -> None:
        try:
            result.append(asyncio.run(awaitable))
        except BaseException as exc:  # recovery caller が分類する
            error.append(exc)

    thread = threading.Thread(target=runner, name="kotoyose-remote-recovery", daemon=True)
    thread.start()
    thread.join(timeout=10)
    if thread.is_alive():
        raise TimeoutError("remote recovery exceeded deadline")
    if error:
        raise error[0]
    return result[0]
