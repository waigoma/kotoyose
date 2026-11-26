"""OpenAI-compatible Chat Completions for host endpoints and child loopback."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, aclosing
import re
from sys import float_info

import httpx
import structlog

from .. import process_env
from ..config import HarnessConfig, ModelPreset
from ..secret_store import SecretStoreError, resolve_secret_sync
from .base import RuntimeChatHistory, RuntimePrompt, RuntimeResult, RuntimeTurnError, prompt_text
from .translation import translate_runtime_config

_DEFAULT_STREAM_IDLE_SECONDS = 60.0
_DEFAULT_ASK_TIMEOUT_SECONDS = 300.0
_LOOPBACK_BODY_LIMIT = 8 * 1024 * 1024
_LOOPBACK_LINE_LIMIT = 1024 * 1024
_SSE_NEWLINE = re.compile(rb"\r\n|\r|\n")
_PROVIDER = "openai_compat"
_FINISH_REASONS = frozenset({"stop", "length", "content_filter", "tool_calls", "function_call"})
log = structlog.get_logger()


def _turn_error(code: str, message: str, status: int, **diagnostics: str | bool) -> RuntimeTurnError:
    return RuntimeTurnError(
        message,
        partial_result=RuntimeResult(text="", provider=_PROVIDER),
        diagnostics={"terminal_reason": code, "api_error_status": status, **diagnostics},
        retryable=False,
    )


def _check_status(response: httpx.Response) -> None:
    status = response.status_code
    if response.is_success:
        return
    code, message = {
        401: ("authentication_failed", "Chat endpoint authentication failed"),
        403: ("authentication_failed", "Chat endpoint authentication failed"),
        429: ("provider_usage_limit", "Chat endpoint rate limit exceeded"),
    }.get(status, ("provider_error", "Chat endpoint rejected the request"))
    # 上流の本文やヘッダには鍵や元のプロンプトが入り得る。
    raise _turn_error(code, message, status)


def _invalid_response() -> RuntimeTurnError:
    return _turn_error("provider_error", "Chat endpoint returned an invalid response", 502)


def _decode(payload: str | bytes | bytearray) -> dict:
    try:
        value = json.loads(payload)
    except ValueError:
        raise _invalid_response() from None
    if not isinstance(value, dict) or value.get("error") is not None:
        raise _invalid_response()
    return value


def _check_finish_reason(payload: dict) -> bool:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return False
    reason = choices[0].get("finish_reason")
    if reason is None:
        return False
    # 互換 endpoint の任意の文字列をログや失敗記録に残さない。
    reason = reason if isinstance(reason, str) and reason in _FINISH_REASONS else "unknown"
    log.info("openai_chat.finish", finish_reason=reason, truncated=reason == "length")
    if reason == "content_filter":
        raise _turn_error("provider_error", "Chat endpoint filtered the response", 502, finish_reason=reason)
    if reason not in {"stop", "length"}:
        raise _turn_error("provider_error", "Chat endpoint returned an unsupported finish reason", 502,
                          finish_reason=reason)
    return True


def _text(payload: dict, field: str) -> str:
    choices = payload.get("choices")
    if choices == [] and field == "delta":  # usage 専用の chunk
        return ""
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise _invalid_response()
    choice = choices[0].get(field)
    if not isinstance(choice, dict) or choice.get("tool_calls") or choice.get("function_call"):
        raise _invalid_response()
    content = choice.get("content")
    if content is None and field == "delta":
        return ""
    if not isinstance(content, str):
        raise _invalid_response()
    return content


def _usage(payload: dict) -> dict | None:
    raw = payload.get("usage")
    if not isinstance(raw, dict):
        return None
    values: dict[str, int | float] = {}
    for source, target in (("prompt_tokens", "input_tokens"), ("completion_tokens", "output_tokens")):
        value = raw.get(source)
        if type(value) is int and value >= 0:
            values[target] = value
    for details, source, target in (
        ("prompt_tokens_details", "cached_tokens", "cache_read_tokens"),
        ("completion_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        detail = raw.get(details)
        value = detail.get(source) if isinstance(detail, dict) else None
        if type(value) is int and value >= 0:
            values[target] = value
    for key in ("cost_usd", "cost"):
        value = raw.get(key)
        if type(value) in (int, float) and -float_info.max <= value <= float_info.max:
            values["cost_usd"] = value
            break
    return values or None


async def _bounded_body(response: httpx.Response) -> AsyncIterator[bytes]:
    if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
        raise _turn_error("provider_error", "Local LLM response must use identity encoding", 502)
    received = 0
    async for chunk in response.aiter_bytes():
        received += len(chunk)
        if received > _LOOPBACK_BODY_LIMIT:
            raise _turn_error("provider_error", "Local LLM response exceeds the 8 MiB limit", 502)
        if chunk:
            yield chunk


async def _bounded_sse_lines(response: httpx.Response) -> AsyncIterator[str]:
    line = bytearray()
    trailing_cr = False
    async for chunk in _bounded_body(response):
        # A CRLF pair can straddle transport chunks. The CR already ended the line.
        start = int(trailing_cr and chunk.startswith(b"\n"))
        trailing_cr = chunk.endswith(b"\r")
        for ending in _SSE_NEWLINE.finditer(chunk, start):
            _append_sse_line(line, chunk[start:ending.start()])
            yield line.decode("utf-8", errors="replace")
            line.clear()
            start = ending.end()
        _append_sse_line(line, chunk[start:])
    if line:
        yield line.decode("utf-8", errors="replace")


def _append_sse_line(line: bytearray, part: bytes) -> None:
    if len(line) + len(part) > _LOOPBACK_LINE_LIMIT:
        raise _turn_error("provider_error", "Local LLM SSE line exceeds the 1 MiB limit", 502)
    line.extend(part)


async def _sse_data(lines: AsyncIterator[str], idle_timeout: float) -> AsyncIterator[str]:
    data: list[str] = []
    while True:
        async with asyncio.timeout(idle_timeout):
            line = await anext(lines, None)
        if line is None:
            break
        if not line and data:
            yield "\n".join(data)
            data.clear()
        elif line.startswith("data:"):
            data.append(line[5:].removeprefix(" "))
    if data:
        yield "\n".join(data)


class OpenAIChatSession:
    """上流セッションを持たず、リクエストごとに指定された履歴を送る。"""

    provider = _PROVIDER
    _loopback = False

    def __init__(self, url: str, token: str | None, model: str, instructions: str | None, timeout: float | None) -> None:
        self._url = url
        self._token = token
        self._model = model
        self._instructions = instructions
        self._timeout = timeout

    def _client(self, timeout: float) -> httpx.AsyncClient:
        headers = {"Authorization": f"Bearer {self._token}"} if self._token is not None else {}
        if self._loopback:
            headers["Accept-Encoding"] = "identity"
        return httpx.AsyncClient(
            headers=headers,
            timeout=timeout, trust_env=False, follow_redirects=False,
        )

    def _body(self, prompt: RuntimePrompt) -> dict:
        messages = [{"role": "system", "content": self._instructions}] if self._instructions else []
        history = prompt.messages if isinstance(prompt, RuntimeChatHistory) else (("user", prompt_text(prompt)),)
        messages.extend({"role": role, "content": text} for role, text in history)
        return {"model": self._model, "messages": messages}

    async def disconnect(self) -> None:
        """各 turn が HTTP 接続を閉じるため、解放するセッションはない。"""

    async def ask(self, prompt: RuntimePrompt) -> RuntimeResult:
        """同期形式で上流のテキストと usage を取得する。"""
        timeout = self._timeout or _DEFAULT_ASK_TIMEOUT_SECONDS
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        try:
            async with asyncio.timeout_at(deadline), self._client(timeout) as client:
                body = {**self._body(prompt), "stream": False}
                if self._loopback:
                    content = bytearray()
                    async with client.stream("POST", self._url, json=body) as response:
                        _check_status(response)
                        async for chunk in _bounded_body(response):
                            content.extend(chunk)
                else:
                    response = await client.post(self._url, json=body)
                    _check_status(response)
                    content = response.content
                payload = _decode(content)
                _check_finish_reason(payload)
                answer = _text(payload, "message")
                if self._token is not None:
                    answer = answer.replace(self._token, "[REDACTED]")
                result = RuntimeResult(text=answer, usage=_usage(payload), provider=self.provider)
                if self._loopback and loop.time() >= deadline:
                    raise TimeoutError
                return result
        except (httpx.TimeoutException, TimeoutError):
            raise _turn_error("timeout", "Chat endpoint timed out", 504) from None
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
            raise _turn_error("service_unavailable", "Chat endpoint connection failed", 503) from None

    async def stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeResult]:
        """SSE のテキスト delta を順に渡し、最後に使用量を渡す。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + (self._timeout or _DEFAULT_STREAM_IDLE_SECONDS) if self._loopback else None
        try:
            async with aclosing(self._stream(prompt)) as items:
                while True:
                    # Keep a single deadline without cancelling the caller between yields.
                    async with asyncio.timeout_at(deadline):
                        if deadline is not None and loop.time() >= deadline:
                            raise TimeoutError
                        item = await anext(items, None)
                        if deadline is not None and loop.time() >= deadline:
                            raise TimeoutError
                    if item is None:
                        return
                    if isinstance(item, RuntimeResult):
                        break
                    yield item
            yield item
        except (httpx.TimeoutException, TimeoutError):
            raise _turn_error("timeout", "Chat endpoint timed out", 504) from None
        except (httpx.HTTPError, httpx.InvalidURL, UnicodeError):
            raise _turn_error("service_unavailable", "Chat endpoint connection failed", 503) from None

    async def _stream(self, prompt: RuntimePrompt) -> AsyncIterator[str | RuntimeResult]:
        body = {**self._body(prompt), "stream": True, "stream_options": {"include_usage": True}}
        pending = ""
        usage = None
        finished = False
        idle_timeout = self._timeout or _DEFAULT_STREAM_IDLE_SECONDS
        async with self._client(idle_timeout) as client, AsyncExitStack() as stack:
            async with asyncio.timeout(idle_timeout):
                response = await stack.enter_async_context(client.stream("POST", self._url, json=body))
            _check_status(response)
            lines = _bounded_sse_lines(response) if self._loopback else response.aiter_lines()
            async for data in _sse_data(lines, idle_timeout):
                if data == "[DONE]":
                    finished = True
                    break
                payload = _decode(data)
                usage = _usage(payload) or usage
                finished = _check_finish_reason(payload) or finished
                released, pending = self._redact_delta(pending + _text(payload, "delta"))
                if released:
                    yield released
        if not finished:
            raise _invalid_response()
        if pending:
            yield pending
        yield RuntimeResult(text="", usage=usage, provider=self.provider)

    def _redact_delta(self, text: str) -> tuple[str, str]:
        if self._token is None:
            return text, ""
        # SSE の delta 境界で鍵が分割されても、続きが判明するまで候補の接頭辞を保留する。
        *complete, tail = text.split(self._token)
        released = "".join(part + "[REDACTED]" for part in complete)
        for size in range(min(len(tail), len(self._token) - 1), 0, -1):
            if tail.endswith(self._token[:size]):
                return released + tail[:-size], tail[-size:]
        return released + tail, ""


def build_openai_chat_session(
    preset: ModelPreset,
    cwd_override: str | None,
    existing_session_id: str | None,
    request_headers: dict[str, str] | None,
    *,
    config: HarnessConfig,
) -> OpenAIChatSession:
    """検証済み route と解決済み secret から、使い捨ての chat runtime を作る。"""
    assert preset.route is not None and preset.model is not None
    conn = config.connections[config.routes[preset.route].connection]
    assert conn.endpoint is not None
    if conn.env_name is not None:
        token = process_env.get(conn.env_name) or ""
    else:
        assert conn.secret_name is not None
        token = resolve_secret_sync(conn.secret_name)
    token = token.strip()
    if not token or not token.isascii() or not token.isprintable() or " " in token or token in conn.endpoint:
        raise SecretStoreError("Chat endpoint credential is unavailable or invalid")
    instructions = translate_runtime_config(config, preset).instructions
    return OpenAIChatSession(
        f"{conn.endpoint.rstrip('/')}/chat/completions", token, preset.model,
        instructions, preset.timeout_seconds,
    )


class _LoopbackChatSession(OpenAIChatSession):
    _loopback = True


def build_loopback_chat_session(
    preset: ModelPreset, *, config: HarnessConfig, loopback_url: str,
) -> OpenAIChatSession:
    """Share Chat Completions with a literal, policy-validated loopback URL."""
    assert preset.model is not None
    instructions = translate_runtime_config(config, preset).instructions
    return _LoopbackChatSession(loopback_url, None, preset.model, instructions, preset.timeout_seconds)
