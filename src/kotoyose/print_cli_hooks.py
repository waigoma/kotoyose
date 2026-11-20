"""Named output hooks for trusted print CLI profiles."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, NotRequired, Protocol, TypedDict, TypeVar

from .print_cli_events import PrintCliStreamParser


if TYPE_CHECKING:
    from .cli_login import PtyLoginSession
    from .print_cli_profiles import PrintCliLoginSpec, PrintCliProfile


LoginFlowKind = Literal["paste_code", "browser_wait", "device_code"]


@dataclass(frozen=True)
class LoginFlow:
    kind: LoginFlowKind
    start: Callable[[PtyLoginSession, PrintCliProfile, PrintCliLoginSpec, float], Awaitable[str]]
    succeeded: Callable[[PrintCliLoginSpec, int | None, str], bool]


class ParsedQuota(TypedDict):
    auth: str
    quota: list[dict[str, Any]]
    last_error: str | None
    quota_source: NotRequired[str]
    plan: NotRequired[str | None]


class QuotaParser(Protocol):
    def __call__(self, stdout: bytes, *, stderr: bytes) -> ParsedQuota: ...


ParserFactory = Callable[[], PrintCliStreamParser]
StderrClassifier = Callable[[str], dict[str, str | int | bool]]
ModelsParser = Callable[[bytes], list[dict[str, Any]]]

_parsers: dict[str, ParserFactory] = {}
_stderr: dict[str, StderrClassifier] = {}
_models: dict[str, ModelsParser] = {}
_quota: dict[str, QuotaParser] = {}
_login: dict[str, LoginFlow] = {}

_Hook = TypeVar("_Hook")


def _register_hook(registry: dict[str, _Hook], name: str, hook: _Hook) -> None:
    if name in registry:
        raise ValueError(f"Print CLI hook already registered: {name!r}")
    registry[name] = hook


def register_parser(name: str, hook: ParserFactory) -> None:
    _register_hook(_parsers, name, hook)


def register_stderr_classifier(name: str, hook: StderrClassifier) -> None:
    _register_hook(_stderr, name, hook)


def register_models_parser(name: str, hook: ModelsParser) -> None:
    _register_hook(_models, name, hook)


def register_quota_parser(name: str, hook: QuotaParser) -> None:
    _register_hook(_quota, name, hook)


def register_login_flow(name: str, hook: LoginFlow) -> None:
    _register_hook(_login, name, hook)


def get_login_flow(name: str) -> LoginFlow:
    return _login[name]


def get_parser(name: str) -> ParserFactory:
    return _parsers[name]


def classify_stderr(name: str | None, stderr: str) -> dict[str, str | int | bool]:
    return _stderr[name](stderr) if name is not None else {}


def get_models_parser(name: str) -> ModelsParser:
    return _models[name]


def get_quota_parser(name: str) -> QuotaParser:
    return _quota[name]


def _register_builtins() -> None:
    from .print_cli_agy import (
        AgyStreamJsonParser,
        _antigravity_cli_model_details,
        parse_agy_error,
        parse_antigravity_usage_output,
    )

    from .print_cli_cursor import CursorStreamJsonParser, parse_cursor_error, parse_cursor_models

    from .print_cli_grok import GrokStreamingJsonParser, parse_grok_billing, parse_grok_error, parse_grok_models

    register_parser("grok_streaming_json_v1", GrokStreamingJsonParser)
    register_stderr_classifier("grok_error_v1", parse_grok_error)
    register_models_parser("grok_models_v1", parse_grok_models)
    register_quota_parser("grok_billing_v1", parse_grok_billing)
    register_parser("cursor_stream_json_v1", CursorStreamJsonParser)
    register_stderr_classifier("cursor_error_v1", parse_cursor_error)
    register_models_parser("cursor_models_v1", parse_cursor_models)
    register_parser("agy_stream_json_v1", AgyStreamJsonParser)
    register_stderr_classifier("agy_error_v1", parse_agy_error)
    register_models_parser("agy_models_v1", _antigravity_cli_model_details)
    register_quota_parser("agy_usage_v1", parse_antigravity_usage_output)


_register_builtins()
