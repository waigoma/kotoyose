"""CLI profile が宣言する ACP の起動設定。"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AcpSpec:
    """CLI profile の ACP 起動引数とログイン済み home を扱える認証方式。"""

    argv: tuple[str, ...]
    home_auth_methods: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    http_mcp_args: tuple[str, ...] = ()
    # 標準の PromptResponse.usage を返さず、拡張に置く agent 向けの名前付き hook。
    usage_parser: str | None = None

    def arguments(self, model: str, *, mcp_servers: Sequence[Mapping] = ()) -> tuple[str, ...]:
        """モデル ID を展開し、HTTP MCP に必要な引数を追加する。"""
        if any("{model}" in arg for arg in self.argv):
            from .provider_data import reject_option_value

            reject_option_value(model, "model")
        args = tuple(model if arg == "{model}" else arg for arg in self.argv)
        return args + self.http_mcp_args if any(server.get("type") == "http" for server in mcp_servers) else args

    @property
    def model_at_launch(self) -> bool:
        """モデル選択を CLI の起動オプションに委ねるかを返す。"""
        return "{model}" in self.argv
