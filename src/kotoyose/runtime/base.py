"""runtime adapter 共通型。"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias, runtime_checkable


@dataclass(frozen=True, slots=True)
class RuntimeTextInput:
    """provider へ渡すユーザーテキスト。"""

    text: str


@dataclass(frozen=True, slots=True)
class RuntimeImageInput:
    """provider へ渡す OpenAI 互換の画像 URL。"""

    url: str
    detail: str | None = None


RuntimeInputPart: TypeAlias = RuntimeTextInput | RuntimeImageInput
RuntimePrompt: TypeAlias = str | list[RuntimeInputPart]


def prompt_text(prompt: RuntimePrompt) -> str:
    """runtime prompt からテキスト部分だけを順序どおり連結して返す。"""
    if isinstance(prompt, str):
        return prompt
    return "\n".join(
        part.text for part in prompt if isinstance(part, RuntimeTextInput)
    )


def prompt_has_images(prompt: RuntimePrompt) -> bool:
    """runtime prompt に画像が 1 件以上含まれるかを返す。"""
    return not isinstance(prompt, str) and any(
        isinstance(part, RuntimeImageInput) for part in prompt
    )


def prompt_image_urls(prompt: RuntimePrompt) -> list[str]:
    """runtime prompt に含まれる画像 URL を入力順に返す。"""
    if isinstance(prompt, str):
        return []
    return [
        part.url for part in prompt if isinstance(part, RuntimeImageInput)
    ]


def prompt_audit_text(prompt: RuntimePrompt) -> str:
    """画像本体や URL を保存せず、監査ログ向けの入力概要を返す。"""
    if isinstance(prompt, str):
        return prompt
    return "\n".join(
        part.text if isinstance(part, RuntimeTextInput) else "[image]"
        for part in prompt
    )


def image_data_url_parts(url: str) -> tuple[str, str] | None:
    """base64 image data URL を media type とデータへ分解する。"""
    if not url.startswith("data:"):
        return None
    header, data = url.split(",", 1)
    return header.removeprefix("data:").removesuffix(";base64"), data


@dataclass(slots=True)
class RuntimeEvent:
    """runtime 内の進捗を route 層へ伝える構造化イベント。"""

    kind: str
    tool_name: str | None = None
    summary: str | None = None
    payload: dict | None = None


@dataclass(slots=True)
class RuntimeResult:
    """provider 実行結果を route 層向けに正規化した値。"""

    text: str
    usage: dict | None = None
    runtime_session_id: str | None = None
    provider: str = "claude"
    structured_output: object | None = None


class RuntimeTurnError(RuntimeError):
    """Failed turn with recoverable partial output, never a successful result.

    ``message`` and ``diagnostics`` must contain only sanitized, public-safe
    classification, not a provider's raw error, prompt, or credentials. Partial
    text/usage stay available to the failure recorder without finalizing success.
    """

    def __init__(
        self,
        message: str,
        *,
        partial_result: RuntimeResult,
        diagnostics: dict[str, str | int | bool] | None = None,
        retryable: bool | None = None,
    ) -> None:
        super().__init__(message)
        self.partial_result = partial_result
        self.diagnostics = dict(diagnostics or {})
        self.terminal_reason = self.diagnostics.get("terminal_reason")
        self.subtype = self.diagnostics.get("subtype")
        self.api_error_status = self.diagnostics.get("api_error_status")
        self.retryable = retryable


@runtime_checkable
class RuntimeSession(Protocol):
    """provider 差分を隠して route / session 層が扱う実行セッション。"""

    provider: str

    async def disconnect(self) -> None:
        """セッションの接続・子プロセス等を解放する。"""
        ...

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """非ストリーミング実行結果を正規化して返す。"""
        ...

    def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeEvent | RuntimeResult]:
        """テキスト chunk、進捗イベント、最後の実行結果を順に返す。"""
        ...


# (preset, cwd_override, existing_session_id, request_headers) -> session。preset は ModelPreset。
RuntimeSessionBuilder = Callable[[Any, "str | None", "str | None", "dict[str, str] | None"], RuntimeSession]


@dataclass(frozen=True, slots=True)
class RuntimeProviderSpec:
    """host runtime の 1 provider 分。生成関数と resume lookup 名を対応づける。"""

    builder: RuntimeSessionBuilder
    session_provider: str
    stateless: bool = False
