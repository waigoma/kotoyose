"""会話実行と provider の境界。外部 API の block や履歴形式を公開しない。"""

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Literal, Protocol, TypeAlias


JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass(frozen=True)
class Message:
    """Caller が確定した順序付き初期入力。provider が実行ごとに保持する。"""

    role: Literal["user", "assistant", "context"]
    content: str


@dataclass(frozen=True)
class ToolResult:
    """現在の round へ一度だけ追加する結果。履歴全体は再送しない。"""

    call_id: str
    output: JsonValue


@dataclass(frozen=True)
class ToolCall:
    """宣言した公開 tool 名と引数。provider 固有の名前を持ち込まない。"""

    call_id: str
    name: str
    arguments: dict[str, JsonValue]


@dataclass(frozen=True)
class Delta:
    """本文の増分。監査ログへは出さない。"""

    text: str


@dataclass(frozen=True)
class Round:
    """確定した生成結果。calls が空なら正常終端、それ以外は結果待ち。"""

    calls: tuple[ToolCall, ...]
    output_tokens: int


class Provider(Protocol):
    """隔離実行が所有する provider。内部履歴を再試行・他実行へ共有しない。"""

    def stream(self, max_tokens: int, tool_results: tuple[ToolResult, ...] = ()) -> AsyncIterator[Delta | Round]:
        """残り出力予算と新しい結果だけを受け、本文と確定 round を返す。"""
        ...
