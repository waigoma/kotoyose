"""Playground UI 専用の内部 API。"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..auth import require_basic_auth
from ..config import HarnessConfig, ModelPreset, get_config
from ..runtime.base import RuntimeEvent
from ..session import SDKConnectionError, get_or_resume_client, stream_claude

router = APIRouter(
    prefix="/internal/playground",
    tags=["playground"],
    dependencies=[Depends(require_basic_auth)],
)


class PlaygroundChatRequest(BaseModel):
    """Playground から送る単発メッセージ。"""

    model: str = Field(min_length=1)
    message: str = Field(min_length=1)

    @field_validator("model", "message")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        """空白だけの値を preset / prompt として受け付けない。"""
        value = value.strip()
        if not value:
            raise ValueError("空白だけの値は指定できません")
        return value


def _preset_summary(name: str, cfg: HarnessConfig) -> dict[str, Any]:
    """Playground の選択 UI 用に secret を含まない preset 概要を作る。"""
    resolved = cfg.preset_for(name)
    _, _, mcp_servers = cfg.resolve_resources(resolved)
    return {
        "id": name,
        "provider": resolved.provider,
        "model": resolved.model,
        "mcp_servers": list(mcp_servers),
    }


def _required_headers(request: Request, preset: ModelPreset) -> dict[str, str]:
    """preset が宣言した request header だけを runtime へ渡す。"""
    missing = [name for name in preset.required_request_headers if not request.headers.get(name)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"missing required request header(s): {', '.join(missing)}",
        )
    return dict(request.headers)


def _mapping(value: Any) -> dict[str, Any]:
    """SDK の dict / Pydantic / dataclass 形式を JSON 用 dict に正規化する。"""
    if isinstance(value, dict):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        result = model_dump()
        if isinstance(result, dict):
            return result
    return vars(value) if hasattr(value, "__dict__") else {}


def _tool_summary(tool: Any) -> dict[str, Any]:
    """SDK の MCP tool 情報から UI に必要な公開項目だけを抜き出す。"""
    data = _mapping(tool)
    result: dict[str, Any] = {"name": data.get("name", "")}
    for key in ("description", "annotations"):
        if data.get(key) is not None:
            result[key] = data[key]
    return result


def _server_summary(server: Any) -> dict[str, Any]:
    """SDK の MCP server status を secret を含まない API 形式へ変換する。"""
    data = _mapping(server)
    result: dict[str, Any] = {
        "name": data.get("name", ""),
        "status": data.get("status", "unknown"),
        "tools": [_tool_summary(tool) for tool in (data.get("tools") or [])],
    }
    if data.get("serverInfo") is not None:
        result["server_info"] = _mapping(data["serverInfo"])
    if data.get("error") is not None:
        result["error"] = data["error"]
    return result


def _sse(event: dict[str, Any]) -> str:
    """Playground SSE event を生成する。"""
    return f"data: {json.dumps(event, ensure_ascii=False)}\n\n"


@router.get("/presets", summary="Playground 用 preset / model 一覧")
async def list_playground_presets():
    """設定済み preset と解決済み runtime model を選択 UI へ返す。"""
    cfg = get_config()
    summaries = [
        *(_preset_summary(name, cfg) for name in cfg.models),
    ]
    return {"default": summaries[0]["id"] if summaries else None, "presets": summaries, "models": summaries}


@router.get("/mcp-tools", summary="選択 preset の MCP tool 検出結果")
async def list_playground_mcp_tools(
    model: str,
    request: Request,
):
    """選択 preset を実際に接続し、Claude SDK の MCP status を返す。

    MCP を持たない preset は接続せず空の一覧を返す。Claude 以外の runtime は
    現在 MCP status API を持たないため、設定済み server を unsupported として返す。
    """
    cfg = get_config()
    if model not in cfg.models:
        raise HTTPException(status_code=404, detail="Unknown model")
    preset = cfg.preset_for(model)
    _, _, configured = cfg.resolve_resources(preset)
    if not configured:
        return {"model": model, "provider": preset.provider, "servers": []}
    if preset.provider != "claude":
        return {
            "model": model,
            "provider": preset.provider,
            "servers": [
                {"name": name, "status": "unsupported", "tools": []}
                for name in configured
            ],
        }

    headers = _required_headers(request, preset)
    client = None
    try:
        client, _ = await get_or_resume_client(
            None,
            model,
            request_headers=headers if preset.required_request_headers else None,
        )
        sdk_client = getattr(client, "client", client)
        get_status = getattr(sdk_client, "get_mcp_status", None)
        if not callable(get_status):
            raise HTTPException(status_code=501, detail="MCP tool discovery is unavailable")
        status = _mapping(await get_status())
        servers = [_server_summary(server) for server in (status.get("mcpServers") or [])]
        return {"model": model, "provider": preset.provider, "servers": servers}
    except SDKConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"MCP connection failed: {exc}") from exc
    finally:
        if client is not None:
            await client.disconnect()


@router.post("/chat", summary="Playground 単発 streaming chat")
async def playground_chat(
    body: PlaygroundChatRequest,
    request: Request,
):
    """既存 runtime 経路で単発 prompt を実行し、結果を SSE で返す。"""
    cfg = get_config()
    if body.model not in cfg.models:
        raise HTTPException(status_code=404, detail="Unknown model")
    preset = cfg.preset_for(body.model)
    headers = _required_headers(request, preset)
    try:
        client, _ = await get_or_resume_client(
            None,
            body.model,
            request_headers=headers if preset.required_request_headers else None,
        )
    except SDKConnectionError as exc:
        raise HTTPException(status_code=503, detail=f"runtime connection failed: {exc}") from exc

    async def events() -> AsyncIterator[str]:
        """runtime の chunk を Playground 用 SSE に変換し、終了時に接続を閉じる。"""
        usage: dict | None = None
        try:
            yield _sse({"type": "start", "model": body.model})
            async for item in stream_claude(client, body.message):
                if isinstance(item, tuple):
                    _, usage, _ = item
                    continue
                if isinstance(item, RuntimeEvent):
                    continue
                yield _sse({"type": "text", "text": item})
            yield _sse({"type": "done", "usage": usage})
        except Exception as exc:
            yield _sse({"type": "error", "message": str(exc)})
        finally:
            await client.disconnect()

    return StreamingResponse(events(), media_type="text/event-stream")


ui_router = APIRouter(
    tags=["playground"],
    dependencies=[Depends(require_basic_auth)],
)


_PLAYGROUND_HTML = r'''<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Kotoyose Playground</title>
<style>
:root{color-scheme:dark;--bg:#0d1117;--panel:#161b22;--card:#21262d;--border:#30363d;--text:#f0f6fc;--muted:#8b949e;--blue:#58a6ff;--green:#3fb950;--red:#f85149}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.6 system-ui,sans-serif;height:100vh;display:flex;flex-direction:column}button,select,textarea{font:inherit}button{cursor:pointer}.header{padding:14px 22px;border-bottom:1px solid var(--border);background:var(--panel);display:flex;align-items:center;justify-content:space-between}.brand{font-size:16px;font-weight:600}.muted{color:var(--muted);font-size:12px}.layout{display:grid;grid-template-columns:280px minmax(0,1fr);flex:1;min-height:0}.sidebar{padding:20px 16px;border-right:1px solid var(--border);background:var(--panel);overflow:auto}.main{display:flex;flex-direction:column;min-width:0;min-height:0;padding:24px;gap:18px;max-width:1100px;width:100%;margin:0 auto}.label{display:block;color:var(--muted);font-size:11px;font-weight:600;letter-spacing:.08em;text-transform:uppercase;margin-bottom:7px}.select,.message{width:100%;border:1px solid var(--border);border-radius:7px;background:var(--bg);color:var(--text);padding:10px}.select:focus,.message:focus{outline:2px solid rgba(88,166,255,.35);border-color:var(--blue)}.preset-meta{margin-top:10px;padding:10px;border:1px solid var(--border);border-radius:7px;color:var(--muted);font-size:12px}.tools{margin-top:24px}.tool-server{border:1px solid var(--border);border-radius:7px;margin-bottom:8px;padding:9px}.tool-server summary{cursor:pointer}.tool-server small,.tool-list{color:var(--muted)}.tool-list{margin:7px 0 0;padding-left:19px;font:12px ui-monospace,monospace}.empty{color:var(--muted);font-size:12px}.conversation{flex:1;min-height:180px;overflow:auto;border:1px solid var(--border);border-radius:9px;background:var(--panel);padding:18px}.welcome{color:var(--muted);text-align:center;margin-top:10vh}.bubble{max-width:88%;padding:11px 14px;border-radius:9px;margin:0 0 14px;white-space:pre-wrap;word-break:break-word}.user{margin-left:auto;background:#1f6feb;color:white}.assistant{background:var(--card)}.composer{display:flex;gap:10px;align-items:flex-end}.message{resize:vertical;min-height:86px;max-height:260px}.send{border:0;border-radius:7px;background:var(--blue);color:#07111f;padding:10px 18px;font-weight:700;min-height:44px}.send:disabled{opacity:.5;cursor:wait}.error{display:none;color:var(--red);background:rgba(248,81,73,.1);border:1px solid rgba(248,81,73,.35);border-radius:7px;padding:9px 12px}.error.show{display:block}@media(max-width:760px){.layout{grid-template-columns:1fr}.sidebar{border-right:0;border-bottom:1px solid var(--border);max-height:42vh}.main{padding:16px}.composer{display:block}.send{margin-top:8px;width:100%}}
</style>
</head>
<body>
<header class="header"><div><div class="brand">Kotoyose Playground</div><div class="muted">単発メッセージ実行 / 会話履歴は保存されません</div></div><div class="muted" id="connection">接続中</div></header>
<div class="layout"><aside class="sidebar"><label class="label" for="preset">Preset / Model</label><select class="select" id="preset" disabled><option>読み込み中...</option></select><div class="preset-meta" id="preset-meta">設定を読み込んでいます。</div><div class="tools"><span class="label">Detected MCP Tools</span><div id="tools"><div class="empty">Preset を選択すると検出します。</div></div><div class="muted" id="tool-status"></div></div></aside>
<main class="main"><div class="error" id="error" role="alert"></div><section class="conversation" id="conversation"><div class="welcome" id="welcome">Preset を選択し、メッセージを送信してください。</div></section><div class="muted">Enter で送信、Shift + Enter で改行</div><form class="composer" id="composer"><textarea class="message" id="message" placeholder="メッセージを入力..." required></textarea><button class="send" id="send" type="submit">送信</button></form></main></div>
<script>
(function(){'use strict';const preset=document.getElementById('preset'),meta=document.getElementById('preset-meta'),tools=document.getElementById('tools'),toolStatus=document.getElementById('tool-status'),message=document.getElementById('message'),conversation=document.getElementById('conversation'),send=document.getElementById('send'),error=document.getElementById('error'),connection=document.getElementById('connection');let presets=[];
function fail(value){error.textContent=value;error.classList.add('show')}function clearFail(){error.textContent='';error.classList.remove('show')}function selected(){return presets.find(function(item){return item.id===preset.value})}function renderMeta(item){meta.textContent=item?(item.provider||'unknown')+' / '+(item.model||'provider default'):''}
function renderTools(data){tools.replaceChildren();const servers=data.servers||[];if(!servers.length){tools.innerHTML='<div class="empty">検出された MCP tool はありません。</div>';return}servers.forEach(function(server){const box=document.createElement('details');box.className='tool-server';const title=document.createElement('summary');title.textContent=server.name+' · '+server.status;box.appendChild(title);if(server.error){const detail=document.createElement('small');detail.textContent=server.error;box.appendChild(detail)}const list=document.createElement('ul');list.className='tool-list';(server.tools||[]).forEach(function(tool){const row=document.createElement('li');row.textContent=tool.name+(tool.description?' — '+tool.description:'');list.appendChild(row)});if(list.children.length)box.appendChild(list);else{const empty=document.createElement('small');empty.textContent='tool はありません。';box.appendChild(empty)}tools.appendChild(box)})}
async function loadTools(){const item=selected();if(!item)return;tools.innerHTML='<div class="empty">MCP tool を検出しています...</div>';toolStatus.textContent='';try{const response=await fetch('/internal/playground/mcp-tools?model='+encodeURIComponent(item.id));if(!response.ok)throw new Error(await response.text());renderTools(await response.json())}catch(exc){tools.innerHTML='<div class="empty">検出できませんでした。</div>';toolStatus.textContent=exc.message}}
async function loadPresets(){try{const response=await fetch('/internal/playground/presets');if(!response.ok)throw new Error(await response.text());const data=await response.json();presets=data.presets||[];preset.replaceChildren();presets.forEach(function(item){const option=document.createElement('option');option.value=item.id;option.textContent=item.id+' · '+(item.provider||'unknown');preset.appendChild(option)});preset.disabled=!presets.length;renderMeta(selected());connection.textContent='接続済み';await loadTools()}catch(exc){connection.textContent='接続失敗';fail(exc.message)}}
function bubble(role,text){const node=document.createElement('div');node.className='bubble '+role;node.textContent=text;conversation.appendChild(node);conversation.scrollTop=conversation.scrollHeight;return node}
async function submit(){const text=message.value.trim(),item=selected();if(!text||!item||send.disabled)return;clearFail();const welcome=document.getElementById('welcome');if(welcome)welcome.remove();bubble('user',text);message.value='';send.disabled=true;const answer=bubble('assistant','');try{const response=await fetch('/internal/playground/chat',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({model:item.id,message:text})});if(!response.ok)throw new Error(await response.text());const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='';while(true){const part=await reader.read();if(part.done)break;buffer+=decoder.decode(part.value,{stream:true});const records=buffer.split('\n\n');buffer=records.pop()||'';records.forEach(function(record){const line=record.split('\n').find(function(value){return value.indexOf('data: ')===0});if(!line)return;const event=JSON.parse(line.slice(6));if(event.type==='text')answer.textContent+=event.text;if(event.type==='error')throw new Error(event.message);conversation.scrollTop=conversation.scrollHeight})}}catch(exc){answer.textContent='';fail(exc.message)}finally{send.disabled=false;message.focus()}}
preset.addEventListener('change',function(){clearFail();renderMeta(selected());loadTools()});document.getElementById('composer').addEventListener('submit',function(event){event.preventDefault();submit()});message.addEventListener('keydown',function(event){if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();submit()}});loadPresets()})();
</script>
</body>
</html>'''


@ui_router.get("/playground", response_class=HTMLResponse, include_in_schema=False)
@ui_router.get("/internal/playground/ui", response_class=HTMLResponse, include_in_schema=False)
async def playground_ui() -> str:
    """Playground HTML を返す。認証は Bearer / Basic 認証 dependency が行う。"""
    return _PLAYGROUND_HTML
