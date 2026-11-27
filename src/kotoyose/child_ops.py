"""Execute approved login, status and quota requests on a child link."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from time import monotonic
from typing import TYPE_CHECKING

from .child_identity import load_policy
from .child_pinned import (
    PinnedCommandRejected,
    adapter_matches_provider,
    login_spec,
    normalize_child_provider,
    pinned_profile,
    quota_command,
    status_spec,
)
from .child_process_tree import TURN_PIDS, kill_pids, kill_process_group, new_turn_pids
from .child_protocol import (
    SESSION_COLLECT_POLICY_REFUSAL,
    Frame, LOGIN_CAP_SECONDS, LOGIN_MESSAGE_LIMIT, QUOTA_POLICY_REFUSAL, QUOTA_WINDOW_LIMIT, decode_frame, encode_frame,
)
from .host_place import kill_session
from .print_cli_hooks import LoginFlowKind
from .print_cli_profiles import PrintCliLoginSpec, build_print_cli_env, resolve_print_cli_binary
from .provider_spec import ChildLaunch, sdk_provider
from .runtime.claude_auth import _default_claude_config_dir, claude_dir_is_default

if TYPE_CHECKING:
    from .cli_login import PtyLoginSession

_REQUEST_LIMIT = 16
_QUOTA_TIMEOUT = 25
_STATUS_TIMEOUT = 8
_STATUS_TTL = 60
_SIGNED_OUT_VALUES = frozenset({
    "logged_out", "signed_out", "not_logged_in", "not_authenticated", "unauthenticated", "login_required",
})
_ENV_KEYS = ("PATH", "HOME", "LANG", "TERM", "USERPROFILE", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA")


def login_pty_supported() -> bool:
    return sys.platform == "linux"


def child_cli_env(provider: str, profile_id: str | None, *, login: bool = False) -> dict[str, str]:
    env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
    if sdk := sdk_provider(provider):
        home = sdk.child_turn.login_home
        if home and (explicit := os.environ.get(home.env)):
            if home.inject_config_dir:
                explicit = explicit.strip()
                if claude_dir_is_default(explicit, str(_default_claude_config_dir()), windows=sys.platform == "win32"):
                    explicit = ""
            if explicit:
                env[home.env] = explicit
    else:
        profile = pinned_profile(provider, profile_id)
        if profile.data_declaration is not None:
            from .provider_data_runtime import data_env

            return data_env()
        env.update(build_print_cli_env(profile, None))
        if login:
            env.update(profile.login_env)
    if login:
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("BROWSER", "true")
    return env


def _windows_exe(binary: Path) -> Path:
    """Use an ``.exe``. A Windows shim must resolve to one before login or quota starts it."""
    if sys.platform != "win32" or binary.suffix.lower() not in {".cmd", ".bat"}:
        return binary
    from .child_windows import resolve_windows_shim

    executable, extra = resolve_windows_shim(str(binary), [])
    resolved = Path(executable)
    if extra or not resolved.is_absolute() or resolved.suffix.lower() != ".exe":
        raise FileNotFoundError(f"{binary.name} did not resolve to an .exe")
    return resolved


def resolve_child_binary(provider: str, profile_id: str | None, home: Path) -> Path:
    sdk = sdk_provider(provider)
    launch = sdk.child_turn.launch if sdk else None
    if launch is ChildLaunch.CLAUDE_AGENT_SDK_PATH:
        from .claude_cli import child_claude_cli

        binary: Path | None = child_claude_cli(home)
    elif launch is ChildLaunch.CODEX_EFFECTIVE_CONFIG_CHECK:
        from .child_components import providers, selected_cli

        row = next(row for row in providers() if row["id"] == sdk.id)
        default = "bundled" if Path(sys.executable).with_name("kotoyose-ready").is_file() else "system"
        binary = selected_cli(home, row, default=default)
    else:
        profile = pinned_profile(provider, profile_id)
        if profile.provider == "grok" and sys.platform == "win32":
            raise PinnedCommandRejected("Windows Grok requires an approved unrestricted declaration")
        if profile.data_declaration is not None:
            from .provider_data_runtime import managed_binary

            return managed_binary(profile.data_declaration, home)
        binary = resolve_print_cli_binary(profile)
    if binary is None:
        raise FileNotFoundError(provider)
    binary = Path(binary)
    if sdk:
        binary = _windows_exe(binary)
    return binary


def _probe_argv(binary: Path, command: tuple[str, ...]) -> tuple[str, ...]:
    if sys.platform == "win32":
        from .child_windows import resolve_windows_shim

        executable, args = resolve_windows_shim(str(binary), list(command))
        return (executable, *args)
    return (str(binary), *command)


@asynccontextmanager
async def _probe_scope(timeout: float):
    pids = new_turn_pids()
    tracking = TURN_PIDS.set(pids)
    try:
        async with asyncio.timeout(timeout):
            yield
    finally:
        TURN_PIDS.reset(tracking)
        kill_pids(pids)


class ChildOps:
    def __init__(self, home: Path, send: Callable[[int, str, dict], Awaitable[None]]) -> None:
        self._home = home
        self._send_frame = send
        self._tasks: dict[int, asyncio.Task] = {}
        self._inputs: dict[int, asyncio.Future[str]] = {}
        self._login_homes: set[str] = set()
        self._status_cache: dict[tuple[str, str | None], tuple[float, str]] = {}
        self._status_tasks: dict[tuple[str, str | None], asyncio.Task[str]] = {}
        self._down = False

    def begin_shutdown(self) -> None:
        if self._down:
            return
        self._down = True
        for task in (*self._tasks.values(), *self._status_tasks.values()):
            if not task.cancelling():
                task.cancel()

    async def finish(self) -> None:
        self.begin_shutdown()
        await asyncio.gather(*self._tasks.values(), *self._status_tasks.values(), return_exceptions=True)

    async def handle(self, frame: Frame) -> asyncio.Task | None:
        """Handle a validated operation frame and reply immediately when at capacity."""
        if self._down:
            return
        stream = frame.stream
        task = self._tasks.get(stream)
        if frame.kind in {"login_cancel", "status_cancel", "quota_cancel", "session_collect_cancel"}:
            if task is not None and not task.cancelling():
                task.cancel()
            return
        if frame.kind == "login_input":
            code = self._inputs.get(stream)
            if code is not None and not code.done():
                code.set_result(frame.payload["code"])
            return
        if task is not None:
            return
        if len(self._tasks) >= _REQUEST_LIMIT:
            if frame.kind == "session_collect":
                await self._send(stream, "session_collect_error", {"error": "child busy"})
            else:
                await self._send(stream, f"{frame.kind}_error", {"message": "child busy"})
            return
        task = asyncio.create_task(self._run(frame))
        self._tasks[stream] = task
        task.add_done_callback(lambda _: self._tasks.pop(stream, None))
        return task

    async def _send(self, stream: int, kind: str, payload: dict) -> bool:
        try:
            decode_frame(encode_frame(stream, kind, payload))
        except ValueError:
            if kind.startswith("session_collect_"):
                await self._send_frame(stream, "session_collect_error", {"error": "session_collect failed"})
            else:
                operation = kind.split("_", 1)[0]
                await self._send_frame(stream, f"{operation}_error", {"message": f"{operation} failed"})
            return False
        await self._send_frame(stream, kind, payload)
        return True

    def _provider_allowed(self, provider: str, *, status_only: bool = False) -> bool:
        try:
            policy = load_policy(self._home / "policy.json")
        except ValueError:
            return False
        if status_only:
            folded = provider.casefold()
            if any(name.casefold() == folded for name in policy.adapters):
                return True
            return any(adapter_matches_provider(adapter, provider) for adapter in policy.adapters)
        return any(normalize_child_provider(name) == provider for name in policy.logins)

    async def _run(self, frame: Frame) -> None:
        from .provider_data_child import use_approved

        stream, payload = frame.stream, frame.payload
        kind = frame.kind
        if kind == "session_collect":
            await self._session_collect(stream, payload)
            return
        try:
            with use_approved(self._home):
                await self._dispatch_operation(stream, payload, kind)
        except PinnedCommandRejected:
            await self._send(stream, f"{kind}_error", {"message": f"{kind} command does not match the pinned profile"})
        except TimeoutError:
            await self._send(stream, f"{kind}_error", {"message": f"{kind} timed out"})
        except FileNotFoundError:
            await self._send(stream, f"{kind}_error", {"message": "official CLI is unavailable"})
        except Exception:
            await self._send(stream, f"{kind}_error", {"message": f"{kind} failed"})

    async def _session_collect(self, stream: int, payload: dict) -> None:
        from .session_import.remote import collect_local_batches
        from .session_import.importer import parse_since

        try:
            policy = load_policy(self._home / "policy.json")
            if not policy.session_import_sources:
                raise PermissionError(SESSION_COLLECT_POLICY_REFUSAL)
            batches, stats = await asyncio.to_thread(
                collect_local_batches, allowed_sources=policy.session_import_sources,
                source_kind=payload.get("source_kind", "all"), since=parse_since(payload.get("since")),
                max_files=payload.get("max_files", 200), max_turns=payload.get("max_turns", 2000),
                known_files=payload.get("known_files"),
            )
            for batch in batches:
                if not await self._send(stream, "session_collect_batch", batch):
                    return
            await self._send(stream, "session_collect_done", dict(stats, child_origin_hint=policy.session_import_origin_hint))
        except Exception as exc:
            await self._send(stream, "session_collect_error", {"error": str(exc)})

    async def _dispatch_operation(self, stream: int, payload: dict, kind: str) -> None:
        if kind == "login":
            async with asyncio.timeout(LOGIN_CAP_SECONDS):
                await self._login(stream, payload)
        elif kind == "status":
            await self._status(stream, payload)
        else:
            async with _probe_scope(_QUOTA_TIMEOUT):
                await self._quota(stream, payload)

    async def _login(self, stream: int, payload: dict) -> None:
        if not login_pty_supported():
            message = (
                "Windows child does not support login relay yet"
                if sys.platform == "win32" else "Login relay requires a Linux child"
            )
            await self._send(stream, "login_error", {"message": message})
            return
        provider, profile_id = normalize_child_provider(payload["provider"]), payload.get("profile")
        if not self._provider_allowed(provider):
            await self._send(stream, "login_error", {"message": "login refused by child policy"})
            return
        spec, flow = login_spec(provider, profile_id)
        if tuple(payload["command"]) != spec.command or payload["flow"] != flow:
            raise PinnedCommandRejected
        if provider in self._login_homes:
            await self._send(stream, "login_error", {"message": "login already in progress for this child provider"})
            return
        self._login_homes.add(provider)
        try:
            await self._relay_login(stream, provider, profile_id, spec, flow)
        finally:
            self._login_homes.discard(provider)
            self._inputs.pop(stream, None)

    async def _relay_login(
        self, stream: int, provider: str, profile_id: str | None, spec: PrintCliLoginSpec, flow: LoginFlowKind,
    ) -> None:
        from .cli_login import (
            CliLoginStartupError, PtyLoginSession, _check_login_success, _wait_for_login_command, wait_for_pty_url,
        )

        binary = resolve_child_binary(provider, profile_id, self._home)
        prefix_args = ()
        if sdk_provider(provider) is None:
            declared = pinned_profile(provider, profile_id).data_declaration
            if declared is not None:
                from .provider_data_runtime import managed_command

                prefix = managed_command(declared, self._home)
                binary, prefix_args = Path(prefix[0]), prefix[1:]
                if sys.platform == "win32" and (not binary.is_absolute() or binary.suffix.lower() != ".exe"):
                    raise PinnedCommandRejected("declared command requires an absolute EXE")
        env = child_cli_env(provider, profile_id, login=True)
        with tempfile.TemporaryDirectory(prefix="kotoyose-child-login-") as cwd:
            session = PtyLoginSession("", "", "", datetime.now(timezone.utc), binary, temp_cwd=Path(cwd))
            async with self._pty(session, (*prefix_args, *spec.command), env):
                try:
                    url = await wait_for_pty_url(
                        session, spec.url_pattern, timeout_seconds=min(20, LOGIN_CAP_SECONDS), login_spec=spec,
                        code_prompt_pattern=spec.code_prompt_pattern if flow == "paste_code" else None,
                        user_code_pattern=spec.user_code_pattern if flow == "device_code" else None,
                    )
                except CliLoginStartupError as exc:
                    await self._send(stream, "login_error", {"message": str(exc)})
                    return
                if flow == "paste_code":
                    self._inputs[stream] = asyncio.get_running_loop().create_future()
                output = {"url": url, "user_code": session.user_code}
                if not await self._send(stream, "login_output", output):
                    return
                session.wipe_buffers()
                if flow == "paste_code":
                    await self._paste(stream, session)
                returncode = await _wait_for_login_command(session)
                success = not spec.status_command and _check_login_success(spec, returncode, session._output_buf)
            if sdk_provider(provider) is None:
                if pinned_profile(provider, profile_id).classify_exit_code(returncode) == "timeout":
                    raise TimeoutError
            if spec.status_command and (flow != "paste_code" or returncode == 0):
                session.wipe_buffers()
                status_env = child_cli_env(provider, profile_id)
                async with self._pty(session, (*prefix_args, *spec.status_command), status_env):
                    status = await _wait_for_login_command(session)
                    success = _check_login_success(spec, status, session._output_buf)
            await self._send(stream, "login_exit", {"returncode": returncode, "success": success})

    async def _paste(self, stream: int, session: PtyLoginSession) -> None:
        from .cli_login import write_pty_line

        code = self._inputs[stream]
        eof = asyncio.create_task(session._eof_event.wait())
        try:
            done, _ = await asyncio.wait((code, eof), return_when=asyncio.FIRST_COMPLETED)
            if code in done and not session._eof_event.is_set():
                await write_pty_line(session, code.result())
        finally:
            self._inputs.pop(stream, None)
            eof.cancel()
            await asyncio.gather(eof, return_exceptions=True)

    @staticmethod
    @asynccontextmanager
    async def _pty(session: PtyLoginSession, command: tuple[str, ...], env: dict[str, str]):
        from .cli_login import attach_pty_reader, close_master_fd, spawn_pty_process

        spawn = asyncio.create_task(spawn_pty_process(session.cli_binary, command, env=env, cwd=session.temp_cwd))
        cancelled = None
        # 起動が完了するまで、後始末に必要なプロセスと fd を受け取れるようにする。
        while True:
            try:
                proc, fd = await asyncio.shield(spawn)
                break
            except asyncio.CancelledError as exc:
                if spawn.cancelled():
                    raise
                cancelled = exc
            except Exception as exc:
                if cancelled is not None:
                    raise cancelled from exc
                raise
        if cancelled is not None:
            try:
                ChildOps._kill_process_session(proc)
            finally:
                os.close(fd)
            await proc.wait()
            raise cancelled
        session._process = proc
        try:
            attach_pty_reader(session, fd)
            yield
        finally:
            close_master_fd(session)
            ChildOps._kill_process_session(proc)
            await proc.wait()

    @staticmethod
    def _kill_process_session(process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            return
        kill_process_group(process.pid)
        kill_session(process.pid)

    async def _quota(self, stream: int, payload: dict) -> None:
        provider, profile_id = normalize_child_provider(payload["provider"]), payload.get("profile")
        if not self._provider_allowed(provider):
            await self._send(stream, "quota_error", {"message": QUOTA_POLICY_REFUSAL})
            return
        command = quota_command(provider, profile_id)
        if command != tuple(payload["command"]):
            raise PinnedCommandRejected
        binary = resolve_child_binary(provider, profile_id, self._home)
        sdk = sdk_provider(provider)
        quota_probe = sdk.login.quota_probe if sdk else None
        if quota_probe == "claude_cli_usage":
            result = await self._claude_quota(binary, provider, command)
        elif quota_probe == "codex_app_server":
            result = await self._codex_quota(binary)
        else:
            result = await self._print_quota(binary, provider, profile_id, command)
        await self._send(stream, "quota_result", _public_quota(result))

    async def _status(self, stream: int, payload: dict) -> None:
        provider, profile_id = normalize_child_provider(payload["provider"]), payload.get("profile")
        if not self._provider_allowed(provider, status_only=True):
            await self._send(stream, "status_error", {"message": "status refused by child policy"})
            return
        spec = status_spec(provider, profile_id)
        row = pinned_profile(provider, profile_id).data_declaration if spec and not sdk_provider(provider) else None
        if row is None:
            state = "unknown" if spec is None else await self._cached_login_state(provider, profile_id, spec)
        else:
            try:
                if sys.platform == "win32":
                    from .child_process_tree import windows_job_scope

                    with windows_job_scope():
                        async with asyncio.timeout(row.status_timeout):
                            state = await self._read_login_state(provider, profile_id, spec)
                else:
                    async with _probe_scope(row.status_timeout):
                        state = await self._read_login_state(provider, profile_id, spec)
            except (OSError, ValueError, TimeoutError):
                state = "unknown"
        await self._send(stream, "status_result", {"state": state})

    async def _cached_login_state(self, provider: str, profile_id: str | None, spec: PrintCliLoginSpec) -> str:
        key = (provider, profile_id)
        cached = self._status_cache.get(key)
        if cached is not None and monotonic() < cached[0]:
            return cached[1]
        task = self._status_tasks.get(key)
        if task is None:
            async def probe() -> str:
                async with _probe_scope(_STATUS_TIMEOUT):
                    state = await self._read_login_state(provider, profile_id, spec)
                self._status_cache[key] = (monotonic() + _STATUS_TTL, state)
                return state

            def forget(done: asyncio.Task[str]) -> None:
                self._status_tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task = asyncio.create_task(probe())
            self._status_tasks[key] = task
            task.add_done_callback(forget)
        return await asyncio.shield(task)

    async def _read_login_state(self, provider: str, profile_id: str | None, spec: PrintCliLoginSpec) -> str:
        sdk = sdk_provider(provider)
        if sdk and sdk.child_turn.launch is ChildLaunch.CODEX_EFFECTIVE_CONFIG_CHECK:
            from .child_launch import sdk_cli_prefix

            argv = (*sdk_cli_prefix(self._home, sdk.child_turn.launch), *spec.status_command)
        else:
            profile = pinned_profile(provider, profile_id) if not sdk else None
            if profile is not None and profile.data_declaration is not None:
                from .provider_data_runtime import managed_command

                prefix = managed_command(profile.data_declaration, self._home)
                if sys.platform == "win32" and (not Path(prefix[0]).is_absolute() or Path(prefix[0]).suffix.lower() != ".exe"):
                    raise PinnedCommandRejected("declared command requires an absolute EXE")
                argv = (*prefix, *spec.status_command)
            else:
                binary = resolve_child_binary(provider, profile_id, self._home)
                argv = _probe_argv(binary, spec.status_command)
        stdout, stderr, code = await self._exec(argv, child_cli_env(provider, profile_id))
        return _login_state(provider, spec, stdout, stderr, code)

    @staticmethod
    async def _exec(argv: tuple[str, ...], env: dict[str, str]) -> tuple[bytes, bytes, int]:
        from .connections_probe import _drain_probe_output, _stop_probe_process

        separate_group = sys.platform != "win32"
        options = {"start_new_session": separate_group}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        with tempfile.TemporaryDirectory(prefix="kotoyose-child-probe-") as cwd:
            # POSIX cleanup here owns the live process rather than retaining its PID in TURN_PIDS.
            tracking = TURN_PIDS.set(None) if separate_group else None
            try:
                proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=cwd, env=env,
                    stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    **options,
                )
            finally:
                if tracking is not None:
                    TURN_PIDS.reset(tracking)
            pgid = proc.pid if separate_group else None
            stdout, stderr = bytearray(), bytearray()
            try:
                await asyncio.gather(
                    _drain_probe_output(proc.stdout, stdout, 1024 * 1024),
                    _drain_probe_output(proc.stderr, stderr, 16 * 1024),
                )
                return bytes(stdout), bytes(stderr), await proc.wait()
            finally:
                if proc.returncode is None:
                    if pgid is not None:
                        kill_process_group(pgid)
                        kill_session(pgid)
                    await _stop_probe_process(proc)

    async def _claude_quota(self, binary: Path, provider: str, command: tuple[str, ...]) -> dict:
        from .connections_probe import classify_expiry_auth, parse_claude_auth_status, parse_claude_usage_output

        env = child_cli_env(provider, None)
        stdout, _, _ = await self._exec(_probe_argv(binary, login_spec(provider, None)[0].status_command), env)
        status = parse_claude_auth_status(stdout)
        if status is not None and not status["logged_in"]:
            result = {"auth": "logged_out", "quota": [], "quota_source": "claude_cli_usage"}
        else:
            stdout, stderr, _ = await self._exec(_probe_argv(binary, command), env)
            result = parse_claude_usage_output(stdout, stderr=stderr)
        if status is not None:
            if result["auth"] != "login_required":
                result["auth"] = classify_expiry_auth(
                    status["expires_dt"], logged_in=status["logged_in"], now=datetime.now(timezone.utc),
                )
            result["plan"] = status["plan"]
            result["expires_at"] = status["expires_dt"]
        return result

    async def _print_quota(self, binary: Path, provider: str, profile_id: str | None, command: tuple[str, ...]) -> dict:
        from .connections_probe import _QUOTA_RPC_LINE_LIMIT, _exchange_quota_rpc
        from .print_cli_hooks import get_quota_parser

        profile = pinned_profile(provider, profile_id)
        env = child_cli_env(provider, profile_id)
        rpc = profile.quota_rpc
        if rpc is not None:
            with tempfile.TemporaryDirectory(prefix="kotoyose-child-quota-") as cwd:
                proc = await asyncio.create_subprocess_exec(
                    *_probe_argv(binary, command), cwd=cwd, env=env,
                    stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    start_new_session=sys.platform != "win32", limit=_QUOTA_RPC_LINE_LIMIT,
                )
                result = await _exchange_quota_rpc(proc, rpc, rpc.timeout_seconds)
        else:
            stdout, stderr, code = await self._exec(_probe_argv(binary, command), env)
            result = get_quota_parser(profile.parse_quota)(stdout, stderr=stderr)
            if code != 0 and result["auth"] == "ok":
                result = {"auth": "unknown", "quota": [], "last_error": "quota command failed"}
        result.setdefault("quota_source", profile.quota_source or "print_cli_usage")
        return result

    @staticmethod
    async def _codex_quota(binary: Path) -> dict:
        from .config import Connection
        from .connection_state_store import ConnectionStateStore
        from .connections_probe import probe_codex_connection

        conn = Connection(provider="codex", kind="subscription", data_policy="no_training")
        return await probe_codex_connection(
            "child", conn, cli_binary=binary, store=ConnectionStateStore(), inspect_expiry=False,
            fixed_env=child_cli_env("codex", None),
        )


def _login_state(
    provider: str, spec: PrintCliLoginSpec, stdout: bytes, stderr: bytes, code: int,
) -> str:
    from .cli_login import _extract_json_dict, strip_ansi

    output = stdout.decode("utf-8", "replace")
    sdk = sdk_provider(provider)
    parser = sdk.login.status_parser if sdk else None
    if parser == "codex_exit_code":
        diagnostic = strip_ansi(stderr.decode("utf-8", "replace"))
        if code == 1 and "Not logged in" in (strip_ansi(output).strip(), diagnostic.strip()):
            return "logged_out"
        return "logged_in" if spec.matches_status(code, None, output) else "unknown"

    payload = _extract_json_dict(output) if spec.success_json_field is not None else None
    if parser == "strict_json_bool":
        logged_in = payload.get(spec.success_json_field) if payload else None
        if logged_in is False:
            return "logged_out"
        return "logged_in" if logged_in is True and code == spec.success_exit_code else "unknown"
    if spec.signed_out_pattern and any(
        re.search(spec.signed_out_pattern, strip_ansi(text)) for text in (output, stderr.decode("utf-8", "replace"))
    ):
        return "logged_out"
    if spec.matches_status(code, payload, output):
        return "logged_in"
    value = payload.get(spec.success_json_field) if payload is not None else None
    if value is False or (isinstance(value, str) and value.strip().casefold() in _SIGNED_OUT_VALUES):
        return "logged_out"
    return "unknown"


def _public_quota(result: dict) -> dict:
    from .connection_state_store import normalize_quota_list

    expires = result.get("expires_at")
    quota = normalize_quota_list(result.get("quota") or [], default_source=result.get("quota_source"))
    for item in quota:
        item["window"] = item["window"][:QUOTA_WINDOW_LIMIT]
        if "label" in item:
            item["label"] = item["label"][:LOGIN_MESSAGE_LIMIT]
    return {
        "auth": result["auth"],
        "quota": quota,
        "quota_source": result.get("quota_source"),
        "plan": result.get("plan"),
        "expires_at": expires.isoformat() if isinstance(expires, datetime) else expires,
        "last_error": "quota unavailable" if result.get("last_error") else None,
    }
