"""ACP の更新と許可応答を Kotoyose の turn に変換する。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

from .acp_client import AcpClient, AcpError
from .runtime.base import RuntimeEvent

_READ_KINDS = frozenset({"read", "search", "think"})
_WRITE_KINDS = frozenset({"edit", "delete", "move"})
_TOOL_KINDS = _READ_KINDS | _WRITE_KINDS | {"execute", "fetch", "switch_mode", "other"}
_TOOL_STATUS = frozenset({"pending", "in_progress", "completed", "failed"})


@dataclass(frozen=True, slots=True)
class AcpPermissionPolicy:
    """alias の tool 名を ACP の title と照合し、surface と place で承認を絞る。"""

    surface: str
    place: str
    # None: the alias did not restrict tools, so surface and place decide (as for the other
    # drivers). A tuple (even empty) is an allowlist matched against the tool kind or title.
    tools: tuple[str, ...] | None = None
    deny: tuple[str, ...] = ()

    def decide(self, params: dict) -> tuple[dict, RuntimeEvent]:
        """1 回限りの選択を返す。判断材料が不足した場合は拒否する。"""
        call = params.get("toolCall")
        call = call if isinstance(call, dict) else {}
        title, kind = call.get("title"), call.get("kind")
        named = {value for value in (title, kind) if isinstance(value, str)}
        explicit = (self.tools is None or bool(named & set(self.tools))) and not (named & set(self.deny))
        allowed_kinds = _READ_KINDS if self.surface in {"read", "write"} else frozenset()
        if self.surface == "write" and self.place == "isolated":
            allowed_kinds = allowed_kinds | _WRITE_KINDS | {"execute"}
        allow = explicit and isinstance(kind, str) and kind in allowed_kinds and kind not in self.deny
        option_kind = "allow_once" if allow else "reject_once"
        options = params.get("options")
        options = options if isinstance(options, list) else []
        option_id = next((
            option["optionId"] for option in options if isinstance(option, dict)
            and option.get("kind") == option_kind
            and isinstance(option.get("optionId"), str) and option["optionId"]
        ), None)
        decision = "allowed" if allow and option_id is not None else "denied"
        outcome = (
            {"outcome": "selected", "optionId": option_id} if option_id is not None
            else {"outcome": "cancelled"}
        )
        return {"outcome": outcome}, RuntimeEvent(
            kind="permission", tool_name=kind if isinstance(kind, str) and kind in _TOOL_KINDS else None,
            summary=decision, payload={"surface": self.surface, "place": self.place},
        )


def acp_mcp_servers(servers: Mapping[str, dict]) -> list[dict]:
    """解決済みの MCP 設定を ACP の stdio / HTTP / SSE union に変換する。"""
    result = []
    for name, server in servers.items():
        transport = server.get("type", "stdio")
        if transport == "stdio":
            result.append({
                "name": name, "command": server["command"], "args": server.get("args") or [],
                "env": [{"name": k, "value": v} for k, v in (server.get("env") or {}).items()],
            })
        elif transport in {"http", "sse"}:
            result.append({
                "type": transport, "name": name, "url": server["url"],
                "headers": [{"name": k, "value": v} for k, v in (server.get("headers") or {}).items()],
            })
        else:
            raise AcpError("unsupported_mcp_transport")
    return result


TurnEmitter = Callable[[str | RuntimeEvent], Awaitable[None]]


_USAGE_KEYS = (
    ("inputTokens", "input_tokens"), ("outputTokens", "output_tokens"), ("cachedReadTokens", "cache_read_tokens"),
)


def acp_usage_parser(profile) -> Callable[[dict], dict] | None:
    """profile の ACP 宣言が名指す使用量の hook を返す。"""
    from .print_cli_hooks import get_acp_usage_parser

    name = profile.acp.usage_parser if profile.acp is not None else None
    return get_acp_usage_parser(name) if name else None


def acp_usage(usage: object) -> dict[str, int]:
    """ACP の camelCase の turn 使用量を、Kotoyose の usage のキーへ写す。"""
    if not isinstance(usage, dict):
        return {}
    return {
        target: value for key, target in _USAGE_KEYS
        if type(value := usage.get(key)) is int and 0 <= value < 2**64
    }


@dataclass(slots=True)
class AcpTurn:
    """turn の出力上限、イベント、usage と部分応答を管理する。"""

    policy: AcpPermissionPolicy
    output_limit: int
    emit: TurnEmitter
    content: bytearray = field(default_factory=bytearray)
    usage: dict = field(default_factory=lambda: {
        "source": "unknown", "input_tokens": None, "output_tokens": None, "cost": None,
    })
    usage_parser: Callable[[dict], dict] | None = None
    _updates: int = 0
    _tool_calls: dict[str, dict] = field(default_factory=dict)

    async def _update(self, update: dict) -> None:
        self._count_update()
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text":
                return
            text = content.get("text")
            if not isinstance(text, str) or not text:
                return
            try:
                encoded = text.encode("utf-8")
            except UnicodeError:
                raise AcpError("invalid_acp_message") from None
            remaining = self.output_limit - len(self.content)
            if len(encoded) > remaining:
                self.content.extend(encoded[:remaining])
                raise AcpError("output_limit_exceeded")
            self.content.extend(encoded)
            await self.emit(text)
        elif kind in ("tool_call", "tool_call_update"):
            tool_id = update.get("toolCallId")
            call = self._merge_tool_call(update)
            tool_kind, status = call.get("kind"), update.get("status")
            await self.emit(RuntimeEvent(
                kind=kind,
                tool_name=tool_kind if isinstance(tool_kind, str) and tool_kind in _TOOL_KINDS else None,
                summary=status if isinstance(status, str) and status in _TOOL_STATUS else None,
                payload={"tool_call_id": tool_id[:256]} if isinstance(tool_id, str) else None,
            ))
        elif kind == "usage_update":
            # ACP v1 の usage_update は会話全体のコンテキスト使用量。
            for key, target in (("used", "context_tokens"), ("size", "context_window_tokens")):
                value = update.get(key)
                if type(value) is int and 0 <= value < 2**64:
                    self.usage[target] = value
            cost = update.get("cost")
            if isinstance(cost, dict) and cost.get("currency") == "USD":
                amount = cost.get("amount")
                if type(amount) in (int, float) and 0 <= amount < 10**12:
                    self.usage["session_cost_usd"] = amount
        elif kind in ("agent_thought_chunk", "plan"):
            await self.emit(RuntimeEvent(kind=kind))

    def _merge_tool_call(self, update: dict) -> dict:
        # permission の toolCall も部分更新なので、すでに届いた同じ ID の項目を補う。
        tool_id = update.get("toolCallId")
        if not isinstance(tool_id, str) or not tool_id or len(tool_id) > 256:
            return {}
        call = self._tool_calls.setdefault(tool_id, {})
        for key in ("title", "kind"):
            if key in update:
                value = update[key]
                call[key] = value if isinstance(value, str) and len(value) <= 1024 else None
        return call

    def _count_update(self) -> None:
        self._updates += 1
        if self._updates > 10_000:
            raise AcpError("output_limit_exceeded")

    async def _permission(self, params: dict) -> dict:
        self._count_update()
        update = params.get("toolCall")
        if isinstance(update, dict):
            params = {**params, "toolCall": self._merge_tool_call(update)}
        outcome, event = self.policy.decide(params)
        await self.emit(event)
        return outcome

    async def run(self, client: AcpClient, prompt: str, *, timeout: float) -> None:
        """ACP の正常終了を確認する。失敗時もバッファ済みの出力と usage を残す。"""
        result = await client.prompt(
            prompt, timeout=timeout, update=self._update, permission=self._permission,
        )
        # 標準の PromptResponse.usage が無い agent は、profile の hook が拡張から取り出す。
        found = acp_usage(result.get("usage"))
        if not found and self.usage_parser is not None:
            found = self.usage_parser(result)
        if found:
            self.usage.update(found)
            self.usage["source"] = "provider"
        stop = result.get("stopReason")
        if stop != "end_turn":
            reason = {
                "cancelled": "cancelled", "max_tokens": "max_tokens",
                "max_turn_requests": "max_turn_requests", "refusal": "provider_refusal",
            }.get(stop, "invalid_acp_message") if isinstance(stop, str) else "invalid_acp_message"
            raise AcpError(reason)
