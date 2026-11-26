"""組み込み SDK と CLI profile が共有するログイン宣言。"""

from dataclasses import dataclass
import re
from typing import Any, Literal

LoginFlowKind = Literal["paste_code", "browser_wait", "device_code"]


@dataclass(frozen=True)
class PrintCliLoginSpec:
    """Trusted PTY login declaration.

    URL candidates must match the pattern and an HTTPS host in url_hosts.
    Device code patterns capture the code in group 1. Browser and device flows
    run status_command after login and apply the success fields to its result.
    An unset success_json_field checks the exit code alone.
    """

    command: tuple[str, ...]
    url_pattern: str
    url_hosts: tuple[str, ...]
    cli_timeout_seconds: int
    code_prompt_pattern: str | None = None
    success_exit_code: int = 0
    success_json_field: str | None = "status"
    success_json_value: str | bool | int | None = "SUCCESS"
    user_code_pattern: str | None = None
    status_command: tuple[str, ...] = ()
    public_error_lines: tuple[str, ...] = ()
    # Some CLIs exit 0 from their status command even when signed out (Grok's `models`
    # prints "You are not authenticated." and still lists defaults). This regex marks that.
    signed_out_pattern: str | None = None

    def matches_status(
        self, returncode: int | None, payload: dict[str, Any] | None, output: str = "",
    ) -> bool:
        """Check the declared exit code, the signed-out marker and optional JSON status field."""
        if returncode != self.success_exit_code:
            return False
        if self.signed_out_pattern and re.search(self.signed_out_pattern, output):
            return False
        if self.success_json_field is None:
            return True
        return payload is not None and payload.get(self.success_json_field) == self.success_json_value

    def to_canonical_dict(self) -> dict[str, Any]:
        return {
            "command": list(self.command),
            "url_pattern": self.url_pattern,
            "url_hosts": list(self.url_hosts),
            "user_code_pattern": self.user_code_pattern,
            "status_command": list(self.status_command),
            "public_error_lines": list(self.public_error_lines),
            "signed_out_pattern": self.signed_out_pattern,
            "code_prompt_pattern": self.code_prompt_pattern,
            "cli_timeout_seconds": self.cli_timeout_seconds,
            "success_exit_code": self.success_exit_code,
            "success_json_field": self.success_json_field,
            "success_json_value": self.success_json_value,
        }


