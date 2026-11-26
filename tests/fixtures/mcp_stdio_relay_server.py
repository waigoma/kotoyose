"""Newline JSON-RPC stdio MCP used by host-relay tests. Stdlib only; no model calls."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


role = os.environ.get("MCP_STDIO_ROLE", "")
marker = os.environ.get("MCP_STDIO_MARKER", "")
secret = os.environ.get("MCP_STDIO_SECRET", "")
mode = os.environ.get("MCP_STDIO_MODE", "normal")
pid_path = os.environ.get("MCP_STDIO_PIDS", "")
spawn_log = os.environ.get("MCP_STDIO_SPAWNS", "")
started = os.environ.get("MCP_STDIO_STARTED", "")

if spawn_log:
    with open(spawn_log, "a", encoding="utf-8") as spawns:
        spawns.write(f"{os.getpid()}\n")

if mode == "stall":
    import fcntl

    try:
        fcntl.fcntl(sys.stdin.fileno(), fcntl.F_SETPIPE_SZ, 4096)
    except OSError:
        pass

report_path = os.environ.get("MCP_STDIO_REPORT", "")
if report_path:
    Path(report_path).write_text(
        json.dumps(
            {
                "cwd": os.getcwd(),
                "home": os.environ.get("HOME"),
                "env": dict(os.environ),
                "entries": sorted(os.listdir()),
            }
        ),
        encoding="utf-8",
    )

if pid_path:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)",
        ]
    )
    Path(pid_path).write_text(
        json.dumps(
            {
                "server": os.getpid(),
                "child": child.pid,
                "group": os.getpgrp(),
            }
        ),
        encoding="utf-8",
    )

def reply(body, result):
    print(json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result}), flush=True)


for line in sys.stdin:
    body = json.loads(line)
    method = body.get("method")
    if isinstance(method, str) and method.startswith("notifications/"):
        if mode == "stall":
            if started:
                Path(started).write_text("1", encoding="utf-8")
            time.sleep(300)
        continue
    if method == "initialize":
        reply(
            body,
            {
                "protocolVersion": body["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stdio-fixture", "version": "1"},
            },
        )
        continue
    if method == "tools/list":
        reply(
            body,
            {
                "tools": [
                    {"name": "write", "inputSchema": {"type": "object"}},
                    {"name": "leak", "inputSchema": {"type": "object"}},
                    {"name": "block", "inputSchema": {"type": "object"}},
                    {"name": "private", "inputSchema": {"type": "object"}},
                ]
            },
        )
        continue
    if method == "ping":
        reply(body, {})
        continue
    if method != "tools/call":
        reply(body, {"content": [{"type": "text", "text": "unsupported"}], "isError": True})
        continue
    name = body.get("params", {}).get("name")
    if name == "write":
        path = Path(body["params"]["arguments"]["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{role}\n{marker}\n", encoding="utf-8")
        reply(body, {"content": [{"type": "text", "text": "wrote"}], "isError": False})
    elif name == "leak":
        reply(body, {"content": [{"type": "text", "text": secret}], "isError": False})
    elif name == "block":
        if started:
            Path(started).write_text("1", encoding="utf-8")
        time.sleep(300)
        reply(body, {"content": [{"type": "text", "text": "late"}], "isError": False})
    else:
        reply(body, {"content": [{"type": "text", "text": "no"}], "isError": True})

if pid_path:
    time.sleep(300)
