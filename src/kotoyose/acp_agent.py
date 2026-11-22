"""ACP v1 stdio agent backed by Kotoyose's chat API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from importlib.metadata import version

import httpx


log = logging.getLogger(__name__)
_TOOL_KINDS = {"read", "edit", "delete", "move", "search", "execute", "think", "fetch"}
_TOOL_STATUS = {"pending", "in_progress", "completed", "failed"}
_SESSION_METHODS = {"session/set_model", "session/set_config_option", "session/prompt", "session/cancel"}


class RpcError(Exception):
    def __init__(self, code: int, message: str):
        super().__init__(message)
        self.code = code


def _invalid(message: str) -> RpcError:
    return RpcError(-32602, message)


@dataclass
class _Session:
    cwd: str
    aliases: list[str]
    model: str
    efforts: dict[str, list[str]] = field(default_factory=dict)
    effort: str = "default"
    messages: list[dict] = field(default_factory=list)
    seen_tools: set[str] = field(default_factory=set)
    prompt_task: asyncio.Task | None = None

    def config_options(self) -> list[dict]:
        options = [{
            "id": "model",
            "name": "Model",
            "category": "model",
            "type": "select",
            "currentValue": self.model,
            "options": [{"value": alias, "name": alias} for alias in self.aliases],
        }]
        supported = self.efforts.get(self.model)
        if supported:
            current = self.effort if self.effort == "default" or self.effort in supported else "default"
            options.append({
                "id": "effort",
                "name": "Effort",
                "category": "thought_level",
                "type": "select",
                "currentValue": current,
                "options": [
                    {"value": "default", "name": "Default"},
                    *[{"value": value, "name": value} for value in supported],
                ],
            })
        return options

    def keep_effort(self) -> None:
        """Drop an effort the newly selected alias does not list."""
        supported = self.efforts.get(self.model)
        if self.effort != "default" and (not supported or self.effort not in supported):
            self.effort = "default"

    def conversation_key(self, session_id: str) -> str:
        key = f"acp:{session_id}:{self.model}"
        if self.model in self.efforts:
            return f"{key}:{self.effort}"
        return key


def _effort_catalog(rows: object) -> dict[str, list[str]]:
    """Keep supported efforts for aliases that published reasoning_efforts."""
    if not isinstance(rows, list):
        return {}
    catalog: dict[str, list[str]] = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        info = item.get("reasoning_efforts")
        if not isinstance(info, dict) or not isinstance(info.get("supported"), list):
            continue
        supported = [value for value in info["supported"] if isinstance(value, str) and value]
        if supported:
            catalog[item["id"]] = supported
    return catalog


def _content(blocks: object) -> str | list[dict]:
    if not isinstance(blocks, list):
        raise _invalid("prompt must be an array of content blocks")
    parts = []
    for block in blocks:
        if not isinstance(block, dict):
            raise _invalid("Invalid prompt content block")
        match block:
            case {"type": "text", "text": str(value)}:
                pass
            case {"type": "image", "data": str(data), "mimeType": str(mime)}:
                parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
                continue
            case {"type": "resource", "resource": {"uri": str(uri), "text": str(body)}}:
                value = f"{uri}\n{body}"
            case {"type": "resource_link", "uri": str(uri), "name": str(name)}:
                value = f"{name}: {uri}"
            case _:
                raise _invalid("Unsupported or malformed prompt content block")
        parts.append({"type": "text", "text": value})
    if all(part["type"] == "text" for part in parts):
        return "\n".join(part["text"] for part in parts)
    return parts


async def _sse_data(response: httpx.Response) -> AsyncIterator[str]:
    lines = []
    async for line in response.aiter_lines():
        if not line:
            if lines:
                yield "\n".join(lines)
                lines.clear()
        elif line.startswith("data:"):
            lines.append(line[5:].removeprefix(" "))
    if lines:
        yield "\n".join(lines)


def _server_message(body: object, fallback: str) -> str:
    if isinstance(body, dict):
        for detail in (body.get("error"), body.get("detail"), body.get("message")):
            if isinstance(detail, dict):
                detail = detail.get("message")
            if isinstance(detail, str) and detail:
                return detail
    return fallback


def _tool_update(event: dict, seen: set[str]) -> dict | None:
    """Kotoyose が揃えた道具の進捗を、ACP の tool_call / tool_call_update にする。"""
    tool_id = event.get("tool_call_id")
    if not isinstance(tool_id, str) or not tool_id:
        return None
    update: dict = {"toolCallId": tool_id}
    status = event.get("status")
    if isinstance(status, str) and status in _TOOL_STATUS:
        update["status"] = status
    if tool_id in seen:
        return {"sessionUpdate": "tool_call_update", **update}
    seen.add(tool_id)
    title, kind = event.get("title"), event.get("tool_kind")
    return {
        "sessionUpdate": "tool_call", "status": "pending", **update,
        "title": title if isinstance(title, str) and title else "tool",
        "kind": kind if isinstance(kind, str) and kind in _TOOL_KINDS else "other",
    }


def _prompt_result(usage: dict | None, cost: object) -> dict:
    result: dict = {"stopReason": "end_turn"}
    if usage is not None:
        input_tokens = usage.get("prompt_tokens") or 0
        output_tokens = usage.get("completion_tokens") or 0
        result["usage"] = {
            "inputTokens": input_tokens,
            "outputTokens": output_tokens,
            "totalTokens": usage.get("total_tokens", input_tokens + output_tokens),
        }
    if type(cost) in (int, float):
        result["_meta"] = {"kotoyose": {"cost_usd": cost}}
    return result


class AcpAgent:
    def __init__(
        self,
        http: httpx.AsyncClient,
        api_key: str,
        send: Callable[[dict], None],
        *,
        model: str | None = None,
        forward_cwd: bool = False,
    ):
        self.http = http
        self.api_key = api_key
        self.send = send
        self.model = model
        self.forward_cwd = forward_cwd
        self.sessions: dict[str, _Session] = {}

    async def serve(self, reader: asyncio.StreamReader) -> None:
        tasks: set[asyncio.Task] = set()
        try:
            while line := await reader.readline():
                task = asyncio.create_task(self._handle_line(line))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
                # Start handling each message in arrival order, even when stdin is buffered.
                await asyncio.sleep(0)
        finally:
            for task in tasks:
                if not task.cancelling():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    def _error(self, request_id: object, code: int, message: str) -> None:
        self.send({
            "jsonrpc": "2.0", "id": request_id,
            "error": {"code": code, "message": message.replace(self.api_key, "[redacted]")[:500]},
        })

    async def _handle_line(self, line: bytes) -> None:
        try:
            request = json.loads(line)
        except ValueError:
            self._error(None, -32700, "Parse error")
            return
        if (
            not isinstance(request, dict)
            or request.get("jsonrpc") != "2.0"
            or not isinstance(request.get("method"), str)
            or type(request.get("id")) not in (str, int, float, type(None))
        ):
            self._error(None, -32600, "Invalid request")
            return
        notification = "id" not in request
        request_id = request.get("id")
        try:
            params = request.get("params", {})
            if not isinstance(params, dict):
                raise _invalid("params must be an object")
            result = await self._dispatch(request["method"], params)
        except RpcError as exc:
            if not notification:
                self._error(request_id, exc.code, str(exc))
        except httpx.HTTPError as exc:
            if not notification:
                self._error(request_id, -32000, f"Kotoyose HTTP request failed ({type(exc).__name__})")
        except Exception:
            log.error("Unexpected ACP request failure")
            if not notification:
                self._error(request_id, -32603, "Internal error")
        else:
            if not notification:
                self.send({"jsonrpc": "2.0", "id": request_id, "result": result})

    async def _dispatch(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": False,
                    "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                    "mcpCapabilities": {"http": False, "sse": False},
                },
                "authMethods": [],
                "agentInfo": {"name": "kotoyose", "title": "Kotoyose", "version": version("kotoyose")},
            }
        if method == "session/new":
            return await self._new_session(params)
        if method not in _SESSION_METHODS:
            raise RpcError(-32601, "Method not found")

        session_id = params.get("sessionId")
        session = self.sessions.get(session_id) if isinstance(session_id, str) else None
        if session is None:
            raise _invalid("Unknown sessionId")
        if method == "session/cancel":
            task = session.prompt_task
            if task is not None and not task.cancelling():
                task.cancel()
            return {}
        if method == "session/prompt":
            return await self._prompt(session_id, session, params.get("prompt"))
        if method == "session/set_config_option" and params.get("configId") == "effort":
            return self._set_effort(session, params.get("value"))
        if method == "session/set_config_option" and params.get("configId") != "model":
            raise _invalid("Unknown configId")
        alias = params.get("modelId" if method == "session/set_model" else "value")
        if alias not in session.aliases:
            raise _invalid("Unknown model alias")
        previous = session.model
        session.model = alias
        session.keep_effort()
        if method == "session/set_model":
            # set_model の戻り値は空なので、effort の選択肢が変わるときは update で届ける。
            if previous in session.efforts or alias in session.efforts:
                self._update(session_id, {
                    "sessionUpdate": "config_option_update",
                    "configOptions": session.config_options(),
                })
            return {}
        return {"configOptions": session.config_options()}

    def _set_effort(self, session: _Session, value: object) -> dict:
        supported = session.efforts.get(session.model)
        if not supported:
            raise _invalid("Unknown configId")
        if not isinstance(value, str) or (value != "default" and value not in supported):
            raise _invalid("Unknown effort")
        session.effort = value
        return {"configOptions": session.config_options()}

    @property
    def _auth_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def _check_response(self, response: httpx.Response, path: str) -> None:
        if response.is_success:
            return
        if path == "/v1/models" and response.status_code == 401:
            raise RpcError(-32000, "GET /v1/models returned HTTP 401: the Kotoyose API key was rejected")
        await response.aread()
        try:
            body = response.json()
        except ValueError:
            body = None
        message = _server_message(body, response.text.strip() or response.reason_phrase)
        raise RpcError(-32000, f"{path} returned HTTP {response.status_code}: {message}")

    async def _new_session(self, params: dict) -> dict:
        cwd = params.get("cwd")
        if not isinstance(cwd, str):
            raise _invalid("cwd must be a string")
        if params.get("mcpServers"):
            log.info("Ignoring client MCP servers; Kotoyose aliases configure MCP on the server")
        response = await self.http.get("/v1/models", headers=self._auth_headers)
        await self._check_response(response, "/v1/models")
        try:
            body = response.json()
            aliases = [item["id"] for item in body["data"]]
            efforts = _effort_catalog(body["data"])
        except (ValueError, KeyError, TypeError):
            raise RpcError(-32000, "GET /v1/models returned an invalid model list") from None
        if not aliases or any(not isinstance(alias, str) or not alias for alias in aliases):
            raise RpcError(-32000, "GET /v1/models returned no usable model aliases")
        model = self.model if self.model is not None else aliases[0]
        if model not in aliases:
            raise _invalid("The initial model alias is unavailable")
        session_id = str(uuid.uuid4())
        session = self.sessions[session_id] = _Session(cwd, aliases, model, efforts)
        return {
            "sessionId": session_id,
            "configOptions": session.config_options(),
            "models": {
                "availableModels": [{"modelId": alias, "name": alias} for alias in aliases],
                "currentModelId": model,
            },
        }

    def _update(self, session_id: str, update: dict) -> None:
        self.send({
            "jsonrpc": "2.0", "method": "session/update",
            "params": {"sessionId": session_id, "update": update},
        })

    async def _prompt(self, session_id: str, session: _Session, blocks: object) -> dict:
        if session.prompt_task is not None:
            raise RpcError(-32000, "A prompt is already in progress for this session")
        content = _content(blocks)
        session.messages.append({"role": "user", "content": content})
        model = session.model
        body = {"model": model, "messages": session.messages, "stream": True}
        if session.effort != "default":
            body["reasoning_effort"] = session.effort
        if self.forward_cwd:
            body["cwd"] = session.cwd
        headers = {
            **self._auth_headers,
            "X-Conversation-Key": session.conversation_key(session_id),
            "X-Kotoyose-Events": "1",
        }
        session.prompt_task = asyncio.current_task()
        try:
            return await self._stream_prompt(session_id, session, body, headers)
        except asyncio.CancelledError:
            # 取消した発言は server 側の会話にも届いているので、記録に残す (途中の応答は残さない)。
            return {"stopReason": "cancelled"}
        except Exception:
            # 失敗した turn の発言は記録から外し、次の prompt に未回答の発言を重ねて送らない。
            session.messages.pop()
            raise
        finally:
            session.prompt_task = None

    async def _stream_prompt(self, session_id: str, session: _Session, body: dict, headers: dict) -> dict:
        path = "/v1/chat/completions"
        text = []
        usage = None
        cost = None
        async with self.http.stream("POST", path, json=body, headers=headers) as response:
            await self._check_response(response, path)
            async for data in _sse_data(response):
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                if not isinstance(chunk, dict):
                    continue
                if chunk.get("error") is not None:
                    raise RpcError(-32000, _server_message(chunk, "Kotoyose reported a stream error"))
                if chunk.get("object") == "kotoyose.event":
                    if chunk.get("kind") in ("tool_call", "tool_call_update"):
                        update = _tool_update(chunk, session.seen_tools)
                        if update is not None:
                            self._update(session_id, update)
                    continue
                if chunk.get("object") not in (None, "chat.completion.chunk"):
                    continue
                if isinstance(chunk.get("usage"), dict):
                    usage = chunk["usage"]
                    cost = usage.get("cost_usd")
                choices = chunk.get("choices") or []
                if choices and isinstance(value := (choices[0].get("delta") or {}).get("content"), str):
                    text.append(value)
                    self._update(session_id, {
                        "sessionUpdate": "agent_message_chunk",
                        "content": {"type": "text", "text": value},
                    })
            else:
                raise RpcError(-32000, "Kotoyose chat stream ended before [DONE]")
        result = _prompt_result(usage, cost)
        session.messages.append({"role": "assistant", "content": "".join(text)})
        return result


async def _run(url: str, key: str, args: argparse.Namespace) -> None:
    reader = asyncio.StreamReader(limit=64 * 1024 * 1024)
    pipe, _ = await asyncio.get_running_loop().connect_read_pipe(
        lambda: asyncio.StreamReaderProtocol(reader), sys.stdin.buffer,
    )

    def send(message: dict) -> None:
        sys.stdout.buffer.write((json.dumps(message) + "\n").encode())
        sys.stdout.buffer.flush()

    try:
        async with httpx.AsyncClient(base_url=url, timeout=httpx.Timeout(30, read=None)) as http:
            await AcpAgent(http, key, send, model=args.model, forward_cwd=args.forward_cwd).serve(reader)
    finally:
        pipe.close()


def _api_key(env: Mapping[str, str]) -> str:
    """API キーを環境変数か、持ち主だけが読めるファイルから取る。

    クライアントの設定画面にキーの値を書かずに済むよう、KOTOYOSE_API_KEY_FILE を受け付ける。
    """
    if key := env.get("KOTOYOSE_API_KEY"):
        return key
    path = env.get("KOTOYOSE_API_KEY_FILE")
    if not path:
        raise ValueError("KOTOYOSE_API_KEY or KOTOYOSE_API_KEY_FILE is required")
    file = Path(path).expanduser()
    try:
        mode = file.stat().st_mode
        if mode & 0o077:
            raise ValueError("KOTOYOSE_API_KEY_FILE must not be readable by group or others (chmod 600)")
        key = file.read_text(encoding="utf-8").strip()
    except OSError:
        raise ValueError("KOTOYOSE_API_KEY_FILE cannot be read") from None
    if not key:
        raise ValueError("KOTOYOSE_API_KEY_FILE is empty")
    return key


def main() -> None:
    parser = argparse.ArgumentParser(description="Expose Kotoyose aliases as an ACP agent")
    parser.add_argument("--model", help="Initial Kotoyose model alias")
    parser.add_argument("--forward-cwd", action="store_true", help="Send the ACP session cwd to Kotoyose")
    args = parser.parse_args()
    try:
        key = _api_key(os.environ)
    except ValueError as exc:
        parser.error(str(exc))
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    asyncio.run(_run(os.environ.get("KOTOYOSE_URL", "http://127.0.0.1:8000"), key, args))


if __name__ == "__main__":
    main()
