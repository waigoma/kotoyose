"""汎用会話 API v1 の入力と管理者設定。既存 Execution API とは独立する。"""

import json
from typing import Any, Literal

from pydantic.json_schema import SkipJsonSchema
from pydantic import Field, field_validator, model_validator

from ..execution_contract import ContractModel


class Message(ContractModel):
    """context はアプリケーション由来の注記。user / assistant の本文と区別する。"""

    role: Literal["user", "assistant", "context"]
    content: str | SkipJsonSchema[list[dict[str, Any]]] = Field(max_length=262144)
    attachments: SkipJsonSchema[object] = Field(default=None, exclude=True)
    files: SkipJsonSchema[object] = Field(default=None, exclude=True)
    images: SkipJsonSchema[object] = Field(default=None, exclude=True)


class Tool(ContractModel):
    """実行するのは Caller。Kotoyose は宣言と結果を provider へ渡すだけ。"""

    name: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    description: str = Field(max_length=8192)
    input_schema: dict[str, Any]


class Start(ContractModel):
    """各要求は新規の隔離実行。instructions と順序付き履歴を必ず明示する。"""

    schema_version: Literal["1"]
    model: str = Field(min_length=1, max_length=128)
    instructions: str = Field(max_length=65536)
    messages: list[Message] = Field(min_length=1, max_length=1024)
    tools: list[Tool] = Field(default_factory=list, max_length=64)
    tool_choice: SkipJsonSchema[object] = Field(default=None, exclude=True)
    attachments: SkipJsonSchema[object] = Field(default=None, exclude=True)
    files: SkipJsonSchema[object] = Field(default=None, exclude=True)
    images: SkipJsonSchema[object] = Field(default=None, exclude=True)
    max_output_tokens: int = Field(gt=0, le=65536)

    @model_validator(mode="after")
    def valid_history(self):
        """現在の入力の欠落と、tool 名の衝突を provider 接続前に拒否する。"""
        if self.messages[-1].role != "user" or not self.messages[-1].content:
            raise ValueError("The last message must be a nonempty user message")
        if len({tool.name for tool in self.tools}) != len(self.tools):
            raise ValueError("Tool names must be unique")
        return self


class ToolResult(ContractModel):
    """未確定の Call ID に一度だけ対応する JSON 結果。"""

    call_id: str = Field(min_length=1, max_length=128)
    output: Any

    @field_validator("output")
    @classmethod
    def finite_json(cls, value):
        """非有限数など、provider へ有効な JSON として送れない値を入口で拒否する。"""
        try:
            json.dumps(value, allow_nan=False)
        except (ValueError, TypeError) as error:
            raise ValueError("Tool output must be finite JSON") from error
        return value


class Continue(ContractModel):
    """全文履歴は送らない。同じ実行の現在の round に新しい結果だけ追加する。"""

    schema_version: Literal["1"]
    round: int = Field(ge=1)
    results: list[ToolResult] = Field(min_length=1, max_length=64)


class Model(ContractModel):
    """検証した Messages API モデルだけを明示的に公開する。"""

    input_token_budget: int = Field(gt=0)
    max_output_tokens: int = Field(gt=0, le=65536)
    # 非標準 gateway が返す既知の接尾辞だけを明示設定で正規化する。未知名の推測はしない。
    response_tool_name_suffix: str = Field(default="", max_length=32, pattern=r"^(?:_[a-zA-Z0-9_-]+)?$")


class Limits(ContractModel):
    """運用上限。終端でも TTL までは状態を保持し、遅延結果を拒否する。"""

    max_executions: int = Field(default=4, ge=1, le=100)
    ttl_seconds: int = Field(default=300, ge=1, le=3600)
    stream_timeout_seconds: float = Field(default=300, gt=0, le=3600)
    max_input_bytes: int = Field(default=1048576, ge=1024, le=8388608)
    max_output_bytes: int = Field(default=8388608, ge=1024, le=8388608)
    max_tool_result_bytes: int = Field(default=65536, ge=1024, le=1048576)
    max_rounds: int = Field(default=5, ge=1, le=100)


class Config(ContractModel):
    """秘密を含まない設定。接続と credential は既存 runtime alias から解決する。"""

    schema_version: Literal["1"]
    models: dict[str, Model]
    # Native Caller の既存 Grant 経路とは別の capability。管理者が明示した主体だけに公開する。
    caller_ids: list[str] = Field(default_factory=list, max_length=256)
    limits: Limits = Field(default_factory=Limits)
