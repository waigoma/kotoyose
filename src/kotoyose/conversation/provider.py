"""Anthropic Messages API を明示履歴 / tool round の port へ変換する。"""

from collections.abc import AsyncIterator
from copy import deepcopy
import json

import httpx

from ..execution_resources import ExecutionError
from .ports import Delta, Message, Round, ToolCall, ToolResult


class AnthropicProvider:
    """一つの execution が所有する設定 snapshot と provider 固有の一時履歴。"""

    def __init__(self, *, url: str, headers: dict, model: str, instructions: str,
                 messages: tuple[Message, ...],
                 tools: list[dict], timeout: float, max_bytes: int, input_token_budget: int,
                 response_tool_name_suffix: str = "",
                 transport: httpx.AsyncBaseTransport | None = None):
        self.url, self.headers = url.rstrip("/"), headers
        self.model, self.instructions = model, instructions
        # provider の名前制約を公開 tool 名へ漏らさない (例: avatar.set_expression)。
        self.tool_names = {tool["name"]: "caller_tool_" + str(index) for index, tool in enumerate(tools)}
        self.public_names = {value: key for key, value in self.tool_names.items()}
        if response_tool_name_suffix:
            self.public_names.update({value + response_tool_name_suffix: key for key, value in self.tool_names.items()})
        self.tools = [{**deepcopy(tool), "name": self.tool_names[tool["name"]]} for tool in tools]
        # context は独立した注記へ変換し、user / assistant の順序は保つ。
        self._messages = [{"role": "user" if item.role == "context" else item.role,
                           "content": [{"type": "text", "text": "[Application context]\n" + item.content}]
                           if item.role == "context" else item.content} for item in messages]
        self.timeout, self.max_bytes, self.transport = timeout, max_bytes, transport
        self.input_token_budget = input_token_budget

    async def _lines(self, response: httpx.Response) -> AsyncIterator[str]:
        """改行のない巨大 chunk も、文字列へ蓄積する前に byte 上限で止める。"""
        pending = b""
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self.max_bytes:
                raise ExecutionError("output_limit_exceeded", "Provider stream exceeded byte limit", 502,
                                     safe_to_retry=False)
            pending += chunk
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                yield line.rstrip(b"\r").decode("utf-8")
        if pending:
            yield pending.rstrip(b"\r").decode("utf-8")

    async def stream(self, max_tokens: int, tool_results: tuple[ToolResult, ...] = ()) -> AsyncIterator[Delta | Round]:
        """不完全な SSE、未知の tool、token 上限終了は成功に変換しない。"""
        if tool_results:
            self._messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": result.call_id,
                 "content": json.dumps(result.output, ensure_ascii=False, allow_nan=False)} for result in tool_results]})
        body = {"model": self.model, "system": self.instructions, "messages": self._messages,
                "tools": self.tools, "max_tokens": max_tokens, "stream": True}
        # configured gateway への credential を redirect や環境 proxy へ流さない。
        async with httpx.AsyncClient(timeout=self.timeout, transport=self.transport,
                                     trust_env=False, follow_redirects=False) as client:
            async with client.stream("POST", self.url + "/messages/count_tokens", headers=self.headers,
                                     json={key: value for key, value in body.items() if key not in {"stream", "max_tokens"}}) as count:
                if count.status_code != 200:
                    raise ExecutionError("agent_contract_unsupported", "Provider token counting is unavailable", 502)
                count_data = b""
                async for chunk in count.aiter_bytes():
                    if len(count_data) + len(chunk) > self.max_bytes:
                        raise ExecutionError("agent_contract_unsupported", "Provider token count exceeds byte limit", 502)
                    count_data += chunk
                value = json.loads(count_data)
                tokens = value.get("input_tokens") if isinstance(value, dict) else None
            if type(tokens) is not int or tokens < 0:
                raise ExecutionError("agent_contract_unsupported", "Provider token count is invalid", 502)
            if tokens + max_tokens > self.input_token_budget:
                raise ExecutionError("context_input_too_large", "Conversation input exceeds the model budget", 422)
            async with client.stream("POST", self.url + "/messages", headers=self.headers, json=body) as response:
                if response.status_code != 200:
                    raise ExecutionError("upstream_rejected", "Conversation provider rejected the request", 502,
                                         safe_to_retry=False)
                blocks, fragments = {}, {}
                open_blocks = set()
                stop_reason = None
                output_tokens = None
                data = []
                async for line in self._lines(response):
                    if line.startswith("data:"):
                        data.append(line[5:].lstrip())
                        continue
                    if line or not data:
                        continue
                    event = json.loads("\n".join(data))
                    data = []
                    kind = event["type"]
                    if kind == "error":
                        raise ExecutionError("upstream_error", "Conversation provider stream failed", 502,
                                             safe_to_retry=False)
                    if kind == "content_block_start":
                        index = event["index"]
                        block = dict(event["content_block"])
                        if index in blocks or block["type"] not in {"text", "tool_use", "thinking", "redacted_thinking"}:
                            raise ExecutionError("agent_contract_unsupported", "Unsupported provider content block", 502)
                        if block["type"] == "tool_use":
                            if block.get("name") not in self.public_names:
                                raise ExecutionError("upstream_protocol_error", "Unknown provider tool", 502)
                            block["name"] = self.tool_names[self.public_names[block["name"]]]
                        blocks[index] = block
                        fragments[index] = ""
                        open_blocks.add(index)
                        if block["type"] == "text" and block.get("text"):
                            yield Delta(block["text"])
                    elif kind == "content_block_delta":
                        index, delta = event["index"], event["delta"]
                        if index not in open_blocks:
                            raise ExecutionError("upstream_protocol_error", "Provider delta is outside a content block", 502)
                        if delta["type"] == "text_delta" and blocks[index]["type"] == "text":
                            blocks[index]["text"] += delta["text"]
                            yield Delta(delta["text"])
                        elif delta["type"] == "input_json_delta" and blocks[index]["type"] == "tool_use":
                            fragments[index] += delta["partial_json"]
                        elif delta["type"] == "thinking_delta" and blocks[index]["type"] == "thinking":
                            # 署名付き block は同じ tool loop へだけ保持する。本文 SSE / ログへ出さない。
                            blocks[index]["thinking"] = blocks[index].get("thinking", "") + delta["thinking"]
                        elif delta["type"] == "signature_delta" and blocks[index]["type"] == "thinking":
                            blocks[index]["signature"] = blocks[index].get("signature", "") + delta["signature"]
                        else:
                            raise ExecutionError("upstream_protocol_error", "Invalid provider delta", 502)
                    elif kind == "content_block_stop":
                        index = event["index"]
                        if index not in open_blocks:
                            raise ExecutionError("upstream_protocol_error", "Provider block stop is invalid", 502)
                        open_blocks.remove(index)
                        if blocks[index]["type"] == "tool_use" and fragments[index]:
                            blocks[index]["input"] = json.loads(fragments[index])
                    elif kind == "message_delta":
                        stop_reason = event["delta"].get("stop_reason")
                        output_tokens = event.get("usage", {}).get("output_tokens")
                    elif kind == "message_stop":
                        if open_blocks:
                            raise ExecutionError("upstream_protocol_error", "Provider content block is incomplete", 502)
                        if stop_reason not in {"end_turn", "tool_use"}:
                            raise ExecutionError("output_limit_exceeded" if stop_reason == "max_tokens" else
                                                 "upstream_protocol_error", "Provider did not complete the round", 502)
                        if type(output_tokens) is not int or not 0 <= output_tokens <= max_tokens:
                            raise ExecutionError("upstream_protocol_error", "Provider output usage is invalid", 502)
                        completed_blocks = [blocks[index] for index in sorted(blocks)]
                        calls = []
                        for block in completed_blocks:
                            if block["type"] != "tool_use":
                                continue
                            if (not isinstance(block.get("id"), str) or not 1 <= len(block["id"]) <= 128
                                    or not isinstance(block.get("input"), dict)):
                                raise ExecutionError("upstream_protocol_error", "Provider tool call is invalid", 502)
                            calls.append(ToolCall(block["id"], self.public_names[block["name"]],
                                                  deepcopy(block["input"])))
                        if (stop_reason == "tool_use") != bool(calls):
                            raise ExecutionError("upstream_protocol_error", "Provider tool round is inconsistent", 502)
                        # 生の block (thinking の署名を含む) は、この provider だけが保持する。
                        self._messages.append({"role": "assistant", "content": completed_blocks})
                        yield Round(tuple(calls), output_tokens)
                        return
                raise ExecutionError("upstream_disconnected", "Provider stream ended without message_stop", 502,
                                     safe_to_retry=False)
