"""固定 fake Responses transcript。通信先は disposable loopback server のみ。"""

import asyncio
from contextlib import asynccontextmanager
import json

from kotoyose import codex_egress


COMMAND = ['sh', '-c', 'test -z "${OPENAI_API_KEY+x}" && test -z "${GH_TOKEN+x}" && test ! -e /srv/sample-user && test ! -e /etc/codex && ! grep -q eth0 /proc/net/dev && printf ok > marker']


def response(index, tool, *, answer="fixture answer"):
    identifier = f"resp_fixture_{index}"
    start = {"type": "response.created", "response": {"id": identifier, "status": "in_progress", "output": []}}
    if tool and index == 1:
        item = {"id": "fc_fixture", "type": "function_call", "name": "sandbox_exec",
                "call_id": "call_fixture", "arguments": json.dumps({"argv": COMMAND, "cwd": "."})}
        events = [start, {"type": "response.output_item.added", "output_index": 0, "item": {**item, "arguments": ""}},
                  {"type": "response.function_call_arguments.delta", "output_index": 0,
                   "item_id": "fc_fixture", "delta": item["arguments"]},
                  {"type": "response.output_item.done", "output_index": 0, "item": item}]
    else:
        events = [start, {"type": "response.output_item.added", "output_index": 0, "item": {
            "id": "message", "type": "message", "role": "assistant", "status": "in_progress", "content": []}},
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0,
             "item_id": "message", "delta": answer},
            {"type": "response.output_item.done", "output_index": 0, "item": {
                "id": "message", "type": "message", "role": "assistant", "phase": "final_answer", "status": "completed",
                "content": [{"type": "output_text", "text": answer, "annotations": []}]}}]
    events.append({"type": "response.completed", "response": {"id": identifier, "status": "completed",
        "output": [], "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2}}})
    return "".join("event: " + item["type"] + "\ndata: " + json.dumps(item) + "\n\n" for item in events).encode()


@asynccontextmanager
async def fake_api(monkeypatch, *, tool, status=200, reflected_secret=None, answer="fixture answer"):
    calls, tasks, writers = [], set(), set()

    async def serve(reader, writer):
        task = asyncio.current_task()
        tasks.add(task)
        writers.add(writer)
        try:
            async with asyncio.timeout(10):
                header = await reader.readuntil(b"\r\n\r\n")
                fields = dict(line.split(b": ", 1) for line in header.split(b"\r\n")[1:] if line)
                body = json.loads(await reader.readexactly(int(fields[b"content-length"])))
                calls.append((header, body))
                data = response(len(calls), tool, answer=answer)
                if reflected_secret:
                    data = data.replace(b"fixture answer", reflected_secret.encode())
                writer.write(f"HTTP/1.1 {status} Fixture\r\n".encode() + b"Content-Type: text/event-stream\r\nConnection: close\r\nContent-Length: "
                             + str(len(data)).encode() + b"\r\n\r\n" + data)
                await writer.drain()
        except asyncio.IncompleteReadError:
            # broker が接続後の binding gate で拒否した場合、要求は届かない。
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            writers.discard(writer)
            tasks.discard(task)

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    # Production の固定 TLS endpoint 接続だけを置換する。broker/policy/jail は実装を通す。
    monkeypatch.setattr(codex_egress, "connect_endpoint", lambda: asyncio.open_connection("127.0.0.1", port))
    monkeypatch.setattr(codex_egress, "connect_subscription_endpoint", lambda: asyncio.open_connection("127.0.0.1", port))
    try:
        yield calls
    finally:
        server.close()
        await server.wait_closed()
        for writer in tuple(writers):
            writer.close()
        for task in tuple(tasks):
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
