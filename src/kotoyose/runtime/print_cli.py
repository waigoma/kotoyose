"""Fresh host print-CLI turns for chat."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from pathlib import Path
import tempfile

from anyio import CancelScope

from ..agent_deadlines import AgentStartRejected
from ..config import HarnessConfig, ModelPreset, get_config
from ..execution_resources import NativeExecutionConfig
from ..print_cli_agent import PrintCliJob, PrintCliWorker, compose_print_cli_prompt
from ..print_cli_profiles import PrintCliProfile, get_print_cli_profile, resolve_print_cli_binary
from .base import RuntimePrompt, RuntimeResult, RuntimeTurnError, prompt_has_images, prompt_text


class PrintCliRuntimeSession:
    """The provider session ID is observational. Every turn starts a new process."""

    provider = "print_cli"

    def __init__(
        self, profile: PrintCliProfile, binary: Path, model: str, login_home: Path,
        timeout: float, instructions: str,
    ) -> None:
        self.profile = profile
        self.binary = binary
        self.model = model
        self.login_home = login_home
        self.timeout = timeout
        self.instructions = instructions
        self.worker = PrintCliWorker(chat=True)

    async def disconnect(self) -> None:
        """Each turn owns and closes its own process."""

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        async with aclosing(self.stream(prompt)) as events:
            async for item in events:
                if isinstance(item, RuntimeResult):
                    return item
        raise self._error("missing_completion")

    async def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeResult]:
        if prompt_has_images(prompt):
            raise self._error("unsupported_input", "cli.print does not support image input")
        text = prompt_text(prompt)
        if len(compose_print_cli_prompt(self.instructions, text).encode()) > self.profile.max_prompt_bytes:
            raise self._error("unsupported_input", "cli.print prompt exceeds the profile size limit")

        cancel = asyncio.Event()
        queue: asyncio.Queue[str | None] = asyncio.Queue()

        async def process_started(pid: int) -> None:
            pass

        async def on_event(kind: str, payload: dict) -> None:
            if kind == "provider_progress" and payload["kind"] == "text" and payload["summary"]:
                queue.put_nowait(payload["summary"])

        with tempfile.TemporaryDirectory(prefix="kotoyose-print-chat-") as cwd:
            job = PrintCliJob(
                profile_id=self.profile.id, cli_binary=self.binary, model=self.model,
                workspace=Path(cwd), login_home=self.login_home, surface="read",
                instructions=self.instructions, prompt=text, output_contract="text",
                timeout_seconds=self.timeout, grace_seconds=0.5, output_limit_bytes=1024 * 1024,
                memory_bytes=2 * 1024**3, pids_limit=128, cpus=2,
            )

            async def run():
                try:
                    return await self.worker.execute(
                        job, cancel, on_process_started=process_started, on_event=on_event,
                    )
                finally:
                    queue.put_nowait(None)

            task = asyncio.create_task(run())
            streamed = False
            try:
                while (chunk := await queue.get()) is not None:
                    streamed = True
                    yield chunk
                outcome = task.result()
            except AgentStartRejected as exc:
                raise self._error(exc.code) from None
            finally:
                cancel.set()
                # Keep the cwd and login-home lock alive until the worker reaps the group.
                interrupted = False
                with CancelScope(shield=True):
                    while not task.done():
                        try:
                            await asyncio.shield(task)
                        except asyncio.CancelledError:
                            interrupted = True
                        except Exception:
                            break
                if not task.cancelled():
                    task.exception()
                if interrupted:
                    raise asyncio.CancelledError

        result = RuntimeResult(
            text=outcome.content.decode("utf-8", errors="replace"),
            usage=outcome.usage, runtime_session_id=outcome.provider_session_id, provider=self.provider,
        )
        if outcome.status != "succeeded":
            raise self._error(outcome.reason, result=result)
        if result.text and not streamed:
            yield result.text
        yield result

    def _error(
        self, reason: str, message: str | None = None, *, result: RuntimeResult | None = None,
    ) -> RuntimeTurnError:
        return RuntimeTurnError(
            message or f"cli.print turn failed ({reason})",
            partial_result=result or RuntimeResult(text="", provider=self.provider),
            diagnostics={"terminal_reason": reason},
            retryable=False if reason in {"authentication_failed", "provider_usage_limit", "unsupported_input"} else None,
        )


def build_print_cli_session(
    preset: ModelPreset, _cwd: str | None, _session_id: str | None, _headers: dict[str, str] | None,
    *, config: HarnessConfig | None = None, settings: NativeExecutionConfig | None = None,
) -> PrintCliRuntimeSession:
    from ..config import resolve_prompt_texts
    from ..session import SDKConnectionError

    config = config or get_config()
    route = config.routes.get(preset.route or "")
    if route is None or route.driver != "cli.print" or route.profile is None:
        raise SDKConnectionError("cli.print chat requires a configured print CLI route")
    if preset.place == "jail" or (preset.agent is not None and preset.agent.place != "host"):
        raise SDKConnectionError("cli.print chat requires host placement")
    connection = config.connections[route.connection]
    if connection.config_dir is None or connection.kind != "subscription":
        raise SDKConnectionError("cli.print chat requires a CLI login home")
    profile = get_print_cli_profile(route.profile)
    binary = resolve_print_cli_binary(profile, settings)
    if binary is None:
        raise SDKConnectionError(f"Set drivers.cli.print.profile_binaries.{profile.id} to an available executable")
    instructions = "\n".join(resolve_prompt_texts(config.resolve_prompt_files(preset)))
    return PrintCliRuntimeSession(
        profile, binary.absolute(), route.model, connection.config_dir.expanduser().absolute(),
        preset.timeout_seconds or 600.0, instructions,
    )
