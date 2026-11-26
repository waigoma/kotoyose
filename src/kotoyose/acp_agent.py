"""ACP v1 stdio agent backed by Kotoyose's chat API."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import uuid
from pathlib import Path
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path

import httpx

from kotoyose.acp_sessions import InvalidCursor, InvalidSessionId, SessionDataError, SessionStore, StoredSession, acp_state_dir, same_cwd


log = logging.getLogger(__name__)
_TOOL_KINDS = {"read", "edit", "delete", "move", "search", "execute", "think", "fetch"}
_TOOL_STATUS = {"pending", "in_progress", "completed", "failed"}
_SESSION_METHODS = {"session/set_model", "session/set_config_option", "session/prompt", "session/cancel"}
_PERMISSION_NAMES = {"allow_once": "Allow once", "reject_once": "Reject once", "allow_always": "Allow always"}


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
    fast: dict[str, dict] = field(default_factory=dict)
    fast_mode: str = "off"
    messages: list[dict] = field(default_factory=list)
    seen_tools: set[str] = field(default_factory=set)
    prompt_task: asyncio.Task | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

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
        info = self.fast.get(self.model)
        if info:
            enabled = {"value": "on", "name": info.get("name") or "Fast"}
            description = info.get("description")
            if isinstance(description, str) and description:
                enabled["description"] = description
            options.append({
                "id": "fast",
                "name": "Fast",
                "category": "other",
                "type": "select",
                "currentValue": self.fast_mode if self.fast_mode in {"off", "on"} else "off",
                "options": [{"value": "off", "name": "Off"}, enabled],
            })
        return options

    def keep_effort(self) -> None:
        """Drop an effort the newly selected alias does not list."""
        supported = self.efforts.get(self.model)
        if self.effort != "default" and (not supported or self.effort not in supported):
            self.effort = "default"

    def keep_fast(self) -> None:
        """Fast を出さない alias へ移ったときは Off に戻す。"""
        if self.fast_mode != "on" or self.model not in self.fast:
            self.fast_mode = "off"

    def selection(self) -> dict:
        return {
            "configOptions": self.config_options(),
            "models": {
                "availableModels": [{"modelId": alias, "name": alias} for alias in self.aliases],
                "currentModelId": self.model,
            },
        }

    def conversation_key(self, session_id: str) -> str:
        key = f"acp:{session_id}:{self.model}"
        if self.model in self.efforts:
            key = f"{key}:{self.effort}"
        if self.model in self.fast:
            key = f"{key}:{self.fast_mode}"
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


def _fast_catalog(rows: object) -> dict[str, dict]:
    """Keep Fast labels for aliases that published a priority fast tier."""
    if not isinstance(rows, list):
        return {}
    catalog: dict[str, dict] = {}
    for item in rows:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        info = item.get("fast")
        if not isinstance(info, dict) or info.get("service_tier") != "priority":
            continue
        name = info.get("name")
        description = info.get("description")
        catalog[item["id"]] = {
            "name": name if isinstance(name, str) and name else "Fast",
            "description": description if isinstance(description, str) and description else None,
        }
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


def _replay_content(content: str | list[dict]) -> Iterator[dict]:
    if isinstance(content, str):
        yield {"type": "text", "text": content}
        return
    for part in content:
        if part["type"] == "text":
            yield {"type": "text", "text": part["text"]}
        else:
            mime, data = part["image_url"]["url"].removeprefix("data:").split(";base64,", 1)
            yield {"type": "image", "mimeType": mime, "data": data}


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
    summary = event.get("summary")
    if isinstance(summary, str) and summary:
        # T3 は tool call の content を進捗行として描く。summary だけでは行が出ない。
        update["content"] = [{
            "type": "content",
            "content": {"type": "text", "text": summary[:500]},
        }]
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
        self.session_store = SessionStore(acp_state_dir())
        self._permission_replies: dict[str, asyncio.Future] = {}

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
        if isinstance(request, dict) and request.get("jsonrpc") == "2.0" and "method" not in request:
            reply_id = request.get("id")
            if isinstance(reply_id, str) and reply_id.startswith("permission:"):
                reply = self._permission_replies.get(reply_id)
                if reply is not None and not reply.done():
                    reply.set_result(request.get("result") if "error" not in request else None)
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
                    "loadSession": True,
                    "sessionCapabilities": {"list": {}},
                    "promptCapabilities": {"image": True, "audio": False, "embeddedContext": True},
                    "mcpCapabilities": {"http": False, "sse": False},
                },
                "authMethods": [],
                "agentInfo": {"name": "kotoyose", "title": "Kotoyose", "version": version("kotoyose")},
            }
        if method == "session/new":
            return await self._new_session(params)
        if method == "session/load":
            return await self._load_session(params)
        if method == "session/list":
            return self._list_sessions(params)
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
        if method == "session/set_config_option" and params.get("configId") == "fast":
            return self._set_fast(session, params.get("value"))
        if method == "session/set_config_option" and params.get("configId") != "model":
            raise _invalid("Unknown configId")
        alias = params.get("modelId" if method == "session/set_model" else "value")
        if alias not in session.aliases:
            raise _invalid("Unknown model alias")
        previous = session.model
        session.model = alias
        session.keep_effort()
        session.keep_fast()
        if method == "session/set_model":
            # set_model の戻り値は空なので、effort か Fast の選択肢が変わるときは update で届ける。
            if previous in session.efforts or alias in session.efforts or previous in session.fast or alias in session.fast:
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

    def _set_fast(self, session: _Session, value: object) -> dict:
        if session.model not in session.fast:
            raise _invalid("Unknown configId")
        if value not in {"off", "on"}:
            raise _invalid("Unknown fast setting")
        session.fast_mode = value
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
        aliases, efforts, fast = await self._model_catalog(params)
        model = self.model if self.model is not None else aliases[0]
        if model not in aliases:
            raise _invalid("The initial model alias is unavailable")
        session_id = str(uuid.uuid4())
        session = self.sessions[session_id] = _Session(cwd, aliases, model, efforts, fast=fast)
        return {"sessionId": session_id, **session.selection()}

    async def _model_catalog(self, params: dict) -> tuple[list[str], dict[str, list[str]], dict[str, dict]]:
        if params.get("mcpServers"):
            log.info("Ignoring client MCP servers; Kotoyose aliases configure MCP on the server")
        response = await self.http.get("/v1/models", headers=self._auth_headers)
        await self._check_response(response, "/v1/models")
        try:
            body = response.json()
            aliases = [item["id"] for item in body["data"]]
            efforts = _effort_catalog(body["data"])
            fast = _fast_catalog(body["data"])
        except (ValueError, KeyError, TypeError):
            raise RpcError(-32000, "GET /v1/models returned an invalid model list") from None
        if not aliases or any(not isinstance(alias, str) or not alias for alias in aliases):
            raise RpcError(-32000, "GET /v1/models returned no usable model aliases")
        return aliases, efforts, fast

    def _saved_session(self, session_id: object, cwd: str) -> StoredSession:
        try:
            saved = self.session_store.load(session_id)
        except InvalidSessionId as exc:
            raise _invalid(str(exc)) from None
        except SessionDataError as exc:
            raise RpcError(-32000, str(exc)) from None
        if saved is None:
            raise _invalid("Unknown or expired sessionId")
        if not same_cwd(saved.cwd, cwd):
            raise _invalid("Saved session cwd differs from the requested cwd")
        return saved

    async def _load_session(self, params: dict) -> dict:
        cwd = params.get("cwd")
        if not isinstance(cwd, str):
            raise _invalid("cwd must be a string")
        saved = self._saved_session(params.get("sessionId"), cwd)
        aliases, efforts, fast = await self._model_catalog(params)
        session_id = saved.session_id
        if (active := self.sessions.get(session_id)) is not None:
            if active.prompt_task is not None:
                raise RpcError(-32000, "A prompt is already in progress for this session")
            # A prompt may have finished while the catalog request was in flight.
            saved = self._saved_session(session_id, cwd)
        default_model = self.model if self.model in aliases else aliases[0]
        model_available = saved.model in aliases
        session = _Session(
            cwd=saved.cwd, aliases=aliases, model=saved.model if model_available else default_model,
            efforts=efforts, effort=saved.effort if model_available else "default",
            fast=fast, fast_mode=saved.fast if model_available else "off", messages=saved.messages,
            created_at=datetime.fromisoformat(saved.created_at),
        )
        session.keep_effort()
        session.keep_fast()
        self.sessions[session_id] = session
        for message in session.messages:
            kind = "user_message_chunk" if message["role"] == "user" else "agent_message_chunk"
            for content in _replay_content(message["content"]):
                self._update(session_id, {"sessionUpdate": kind, "content": content})

        changes = [
            f"{name} → {current}" for name, previous, current in (
                ("Model", saved.model, session.model), ("Effort", saved.effort, session.effort),
                ("Fast", saved.fast, session.fast_mode),
            ) if previous != current
        ]
        if changes:
            self._update(session_id, {
                "sessionUpdate": "agent_message_chunk",
                "content": {"type": "text", "text": f"Saved options are unavailable. Using defaults ({', '.join(changes)})."},
            })
        return session.selection()

    def _list_sessions(self, params: dict) -> dict:
        cwd, cursor = params.get("cwd"), params.get("cursor")
        if cwd is not None and not isinstance(cwd, str):
            raise _invalid("cwd must be a string")
        if cursor is not None and not isinstance(cursor, str):
            raise _invalid("cursor must be a string")
        try:
            sessions, next_cursor = self.session_store.list_sessions(cwd=cwd, cursor=cursor)
        except InvalidCursor as exc:
            raise _invalid(str(exc)) from None
        except OSError:
            raise RpcError(-32000, "Saved sessions could not be listed") from None
        result = {"sessions": [
            {"sessionId": item.session_id, "cwd": item.cwd, "title": item.title, "updatedAt": item.updated_at}
            for item in sessions
        ]}
        if next_cursor is not None:
            result["nextCursor"] = next_cursor
        return result

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
        if session.fast_mode == "on" and session.model in session.fast:
            body["service_tier"] = "priority"
        if self.forward_cwd:
            body["cwd"] = session.cwd
        headers = {
            **self._auth_headers,
            "X-Conversation-Key": session.conversation_key(session_id),
            "X-Kotoyose-Events": "1",
            "X-Kotoyose-Approvals": "1",
        }
        session.prompt_task = asyncio.current_task()
        try:
            result = await self._stream_prompt(session_id, session, body, headers)
        except asyncio.CancelledError:
            # 取消した発言は server 側の会話にも届いているので、記録に残す (途中の応答は残さない)。
            result = {"stopReason": "cancelled"}
        except Exception:
            # 失敗した turn の発言は記録から外し、次の prompt に未回答の発言を重ねて送らない。
            session.messages.pop()
            raise
        finally:
            session.prompt_task = None
        try:
            self.session_store.save(
                session_id=session_id, cwd=session.cwd, model=session.model, effort=session.effort,
                fast=session.fast_mode, messages=session.messages, created_at=session.created_at,
            )
        except (OSError, SessionDataError):
            log.error("Could not save ACP session history")
        return result

    async def _request_permission(self, session_id: str, session: _Session, event: dict) -> None:
        request_id = event.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", request_id):
            return
        offered = event.get("options")
        if not isinstance(offered, list):
            return
        options = [
            {"optionId": option, "kind": option, "name": name}
            for option, name in _PERMISSION_NAMES.items() if option in offered
        ]
        tool_call = _tool_update({**event, "status": "pending"}, set())
        decision = "reject_once"
        if tool_call is not None and options:
            if tool_call["toolCallId"] not in session.seen_tools:
                session.seen_tools.add(tool_call["toolCallId"])
                self._update(session_id, tool_call)
            rpc_id = f"permission:{uuid.uuid4().hex}"
            reply = asyncio.get_running_loop().create_future()
            self._permission_replies[rpc_id] = reply
            try:
                self.send({
                    "jsonrpc": "2.0", "id": rpc_id, "method": "session/request_permission",
                    "params": {"sessionId": session_id, "options": options, "toolCall": {
                        key: value for key, value in tool_call.items() if key != "sessionUpdate"
                    }},
                })
                async with asyncio.timeout(300):
                    result = await reply
                outcome = result.get("outcome") if isinstance(result, dict) else None
                if isinstance(outcome, dict) and outcome.get("outcome") == "selected":
                    option_id = outcome.get("optionId")
                    if any(option["optionId"] == option_id for option in options):
                        decision = option_id
            except TimeoutError:
                pass
            finally:
                self._permission_replies.pop(rpc_id, None)
        path = f"/v1/chat/approvals/{request_id}"
        response = await self.http.post(path, json={"decision": decision}, headers=self._auth_headers)
        if response.status_code != 404:
            await self._check_response(response, path)

    async def _stream_prompt(self, session_id: str, session: _Session, body: dict, headers: dict) -> dict:
        path = "/v1/chat/completions"
        text = []
        usage = None
        cost = None
        approvals: set[asyncio.Task] = set()
        def approval_finished(task: asyncio.Task) -> None:
            approvals.discard(task)
            if not task.cancelled() and task.exception() is not None:
                log.warning("Could not deliver ACP tool permission decision")

        async def cancel_approvals() -> None:
            pending = list(approvals)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

        async with self.http.stream("POST", path, json=body, headers=headers) as response, AsyncExitStack() as cleanup:
            cleanup.push_async_callback(cancel_approvals)
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
                    if chunk.get("kind") == "permission_request":
                        task = asyncio.create_task(self._request_permission(session_id, session, chunk))
                        approvals.add(task)
                        task.add_done_callback(approval_finished)
                    elif chunk.get("kind") in ("tool_call", "tool_call_update"):
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
    Unix では group と others に読めないことを st_mode で確かめる。Windows の st_mode は
    ACL を表さず、自分だけのファイルでも 0o666 に見えることがある。そこでは検査せず、
    ファイルが user profile の ACL の下にあることに頼る。
    """
    if key := env.get("KOTOYOSE_API_KEY"):
        return key
    path = env.get("KOTOYOSE_API_KEY_FILE")
    if not path:
        raise ValueError("KOTOYOSE_API_KEY or KOTOYOSE_API_KEY_FILE is required")
    file = Path(path).expanduser()
    try:
        mode = file.stat().st_mode
        if sys.platform != "win32" and mode & 0o077:
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
