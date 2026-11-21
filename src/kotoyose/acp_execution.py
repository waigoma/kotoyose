"""1 attempt の ACP turn を CLI worker の結果と進捗へ変換する。"""

from __future__ import annotations

from dataclasses import asdict

from .acp_client import AcpClient, AcpError
from .acp_turn import AcpPermissionPolicy, AcpTurn, acp_usage_parser
from .native_claude import _extract_json_object
from .print_cli_agent import PrintCliJob, compose_print_cli_prompt
from .print_cli_profiles import PrintCliProfile
from .runtime.base import RuntimeEvent


class AcpExecution:
    """CLI worker が起動・監視・削除するプロセス上で ACP を実行する。"""

    def __init__(self, process, job: PrintCliJob, profile: PrintCliProfile, on_event, *, cwd: str) -> None:
        self.client = AcpClient(process)
        self.job = job
        self.profile = profile
        self.cwd = cwd

        async def emit(item: str | RuntimeEvent) -> None:
            event = RuntimeEvent(kind="text", summary=item) if isinstance(item, str) else item
            await on_event("provider_progress", asdict(event))

        self.turn = AcpTurn(
            AcpPermissionPolicy(job.surface, job.execution_environment, job.acp_tools, job.acp_deny),
            job.output_limit_bytes, emit, usage_parser=acp_usage_parser(profile),
        )

    async def run(self) -> tuple[str, str, bytes]:
        """結果の status、reason、本文を、失敗時は部分応答とともに返す。"""
        spec = self.profile.acp
        assert spec is not None
        try:
            await self.client.start_session(spec, cwd=self.cwd, mcp_servers=[], model=self.job.model)
            prompt = compose_print_cli_prompt(
                self.job.instructions, self.job.prompt,
                output_contract=self.job.output_contract, output_schema=self.job.output_schema,
            )
            await self.turn.run(self.client, prompt, timeout=self.job.timeout_seconds)
        except AcpError as exc:
            if exc.reason != "cancelled":
                await self.client.cancel()
            status = "cancelled" if exc.reason == "cancelled" else "failed"
            return status, exc.reason, bytes(self.turn.content)
        content = bytes(self.turn.content)
        if self.job.output_contract == "json":
            extracted = _extract_json_object(content)
            if extracted is None:
                return "failed", "invalid_json_output", content
            content = extracted
        return "succeeded", "completed", content
