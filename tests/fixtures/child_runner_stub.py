"""Fixture launched in a separate interpreter by the Rust/Python conformance test."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

from kotoyose import child_ops, child_runner
from kotoyose.child_catalog import catalog_document, component_rows
from kotoyose.registry import registry
from kotoyose.runtime.base import RuntimeEvent, RuntimeProviderSpec, RuntimeResult


COMPONENT_PACKAGES = {
    "claude": ("claude-agent-sdk",),
    "codex": ("openai-codex", "openai-codex-cli-bin"),
}


async def exercise_http_mcp(server, *values):
    import httpx

    async with httpx.AsyncClient(trust_env=False) as http:
        async def post(method, params, request_id=None):
            body = {"jsonrpc": "2.0", "method": method, "params": params}
            if request_id is not None:
                body["id"] = request_id
            response = await http.post(server["url"], headers=server["headers"], json=body)
            response.raise_for_status()
            assert "mcp-session-id" not in response.headers
            if request_id is None:
                assert response.status_code == 202
                return None
            assert response.json()["id"] == request_id
            return response.json()

        initialized = await post("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                "clientInfo": {"name": "fake-cli", "version": "1"}}, 1)
        assert initialized["result"]["serverInfo"]["name"] == "fixture"
        await post("notifications/initialized", {})
        tools = await post("tools/list", {}, 2)
        assert [tool["name"] for tool in tools["result"]["tools"]] == ["echo"]
        return await asyncio.gather(*(
            post("tools/call", {"name": "echo", "arguments": {"value": value}}, index)
            for index, value in enumerate(values, start=3)
        ))


def fake_pip(home):
    assert sys.argv[1:5] == ["-I", "-u", "-m", "pip"]
    args = sys.argv[5:]
    if "install" in args:
        assert {"--isolated", "--require-hashes", "--no-deps", "--only-binary=:all:", "--force-reinstall"} <= set(args)
        assert args[args.index("--index-url") + 1] == "https://pypi.org/simple"
        requirements = Path(args[args.index("-r") + 1])
        provider = requirements.stem.removeprefix("requirements-")
        assert requirements.read_text() == "".join(
            f"{package}==1 --hash=sha256:fake\n" for package in COMPONENT_PACKAGES[provider]
        )
        print("Downloading the fake SDK", flush=True)
        deadline = time.monotonic() + 10
        while not (home / "continue-pip").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert (home / "continue-pip").is_file()
        if (home / "fail-pip").is_file():
            invoked = Path(os.environ.get("KOTOYOSE_STUB_PYTHON") or sys.argv[0])
            print(f"Could not replace '{invoked.parent.parent / 'locked file'}'", file=sys.stderr)
            return 1
        marker = home / f"fake-sdk-{provider}"
        marker.write_text(str(int(marker.read_text()) + 1) if marker.exists() else "1")
        if (home / "flood-pip").is_file():
            for index in range(256):
                print(f"progress {index}", file=sys.stdout)
                print(f"warning {index}", file=sys.stderr)
    else:
        assert "uninstall" in args and "--yes" in args
        packages = tuple(args[args.index("--yes") + 1:])
        provider = next(provider for provider, expected in COMPONENT_PACKAGES.items() if expected == packages)
        print("Removing the fake SDK", flush=True)
        (home / f"fake-sdk-{provider}").unlink()
    return 0


def _provider_sites():
    """The link app's Python lives in `<root>/bin`; provider files live in `<root>/lib/python*/site-packages`."""
    stub = os.environ.get("KOTOYOSE_STUB_PYTHON")
    if not stub:
        return []
    lib = Path(stub).resolve().parent.parent / "lib"
    if not lib.is_dir():
        return []
    return sorted(
        entry / "site-packages"
        for entry in lib.iterdir()
        if entry.is_dir() and entry.name.startswith("python") and (entry / "site-packages").is_dir()
    )


def _codex_sdk_present(sites):
    for site in sites:
        try:
            names = [entry.name for entry in site.iterdir()]
        except OSError:
            continue
        if any(
            (name.startswith("openai_codex-") and name.endswith((".dist-info", ".egg-info")))
            or name == "openai_codex.egg-info"
            for name in names
        ):
            return True
    return False


def _codex_bundle_present(sites):
    for site in sites:
        package = site / "codex_cli_bin"
        if not (package / "codex-package.json").is_file():
            continue
        for name in ("codex", "codex.exe"):
            binary = package / "bin" / name
            if binary.is_file() and os.access(binary, os.X_OK):
                return True
    return False


def fake_probe(home):
    with (home / "component-probes").open("a") as log:
        print(sys.argv[-1], file=log)
    if sys.argv[-1] == "availability" and (home / "fail-probe").exists():
        return 1
    settings = json.loads((home / "app.json").read_text()) if (home / "app.json").is_file() else {}
    managed = (home / "fake-components").is_file()
    sites = _provider_sites()
    codex_sdk = _codex_sdk_present(sites)
    codex_bundle = _codex_bundle_present(sites)
    codex_system = shutil.which("codex") is not None
    rows = []
    for provider, packages in COMPONENT_PACKAGES.items():
        marker = home / f"fake-sdk-{provider}"
        # Unmanaged hellos keep advertising Claude. Codex stays absent until its SDK and selected CLI exist.
        version = marker.read_text() if marker.exists() else "1" if not managed and provider == "claude" else None
        bundled_cli = version is not None and (provider != "claude" or not (home / "no-bundled-cli").exists())
        sdk_importable = version is not None
        system_cli = bool(managed)
        if provider == "codex":
            bundled_cli = bundled_cli or codex_bundle
            sdk_importable = sdk_importable or codex_sdk
            system_cli = system_cli or codex_system
        choice = settings.get(f"{provider}_cli", "bundled")
        if choice != "system":
            choice = "bundled"
        rows.append({
            "id": provider,
            "packages": dict.fromkeys(packages, version),
            "system_cli": system_cli,
            "system_version": f"{provider.title()} fixture 9.9" if managed and sys.argv[-1] == "check" else None,
            "bundled_cli": bundled_cli,
            "choice": choice,
            "turns_supported": True,
            "sdk_importable": sdk_importable,
        })
    catalog = catalog_document(component_rows())
    sdk_rows = {row["id"]: row for row in rows}
    rows = [
        {
            **sdk_rows.get(provider["id"], {
                "id": provider["id"], "packages": {}, "system_cli": False, "system_version": None,
                "bundled_cli": False, "choice": "bundled", "turns_supported": False, "sdk_importable": False,
            }),
            "catalog_version": catalog["version"],
            "managed_available": False,
        }
        for provider in catalog["providers"]
    ]
    print(json.dumps(rows))
    return 0


def _expand_cli_env(servers, env):
    """Stand in for the CLI expanding ${VAR} MCP headers from its process environment."""
    expanded = {}
    for name, server in servers.items():
        headers = {}
        for key, value in (server.get("headers") or {}).items():
            if isinstance(value, str):
                for var, token in env.items():
                    value = value.replace("${" + var + "}", token)
            headers[key] = value
        expanded[name] = {**server, "headers": headers}
    return expanded


def main():
    if sys.argv[1:5] == ["-I", "-u", "-m", "pip"]:
        raise SystemExit(fake_pip(Path(os.environ["KOTOYOSE_CHILD_HOME"])))
    assert sys.argv[1:4] == ["-I", "-m", "kotoyose.child_runner"]
    home = Path(sys.argv[sys.argv.index("--home") + 1])
    if "--components" in sys.argv:
        raise SystemExit(fake_probe(home))
    with (home / "starts").open("a") as log:
        print(os.getpid(), file=log)
    child_ops.resolve_child_binary = lambda *_: home / "fake-cli"

    send = child_runner.Runner.send

    async def send_with_stuck_delta(runner, stream, kind, payload):
        await send(runner, stream, kind, payload)
        if kind == "delta" and payload["text"] == "stuck partial":
            # Let the host receive the delta while the runtime is still suspended at yield.
            await asyncio.Event().wait()

    child_runner.Runner.send = send_with_stuck_delta

    class Runtime:
        def __init__(self, servers):
            self.servers = servers

        async def stream(self, prompt):
            if prompt in {"host-mcp", "host-mcp-block"}:
                reply, = await exercise_http_mcp(self.servers["remote"], "block" if prompt.endswith("block") else "rust-relay")
                if "error" in reply:
                    (home / "relay-result").write_text(reply["error"]["message"])
                    yield RuntimeResult(text=reply["error"]["message"], provider="claude")
                else:
                    yield RuntimeResult(text=reply["result"]["content"][0]["text"], provider="claude")
                return
            # Both a Python print and an inherited native stdout must stay off the IPC pipe.
            print("ordinary runner stdout", flush=True)
            subprocess.run([
                sys.executable, "-I", "-c",
                "import os; assert os.read(0, 1) == b''; os.write(1, b'child stdout\\n')",
            ], check=True, close_fds=False)
            if prompt in {"cancel", "tree", "crash", "stuck"}:
                # The stub creates a descendant in another session too.
                argv = [
                    sys.executable, "-I", "-c",
                    "import subprocess, sys, time; from pathlib import Path; "
                    "child = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(30)'], "
                    "start_new_session=True); Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)",
                    str(home / f"{prompt}.grandchild"),
                ]
                process = subprocess.Popen(argv, start_new_session=True)
                (home / f"{prompt}.pid").write_text(str(process.pid))
                try:
                    yield "stuck partial" if prompt == "stuck" else "partial"
                    if prompt == "crash":
                        await asyncio.sleep(0.05)
                        os._exit(23)
                    await asyncio.Event().wait()
                finally:
                    await asyncio.to_thread(process.wait, 2)
                    (home / "cancelled").touch()
                    if prompt == "stuck":
                        await asyncio.Event().wait()
                return
            yield "こん"
            yield "にちは"
            yield RuntimeEvent(kind="tool", tool_name="fixture")
            yield RuntimeResult(text="こんにちは", usage={"output_tokens": 2}, provider="claude")

        async def disconnect(self):
            (home / "released").touch()

    def build(preset, _cwd, _session_id, _headers):
        if preset.plugins:
            asset = Path(preset.plugins[0]) / "skills/demo/large-fixture.bin"
            if asset.exists():
                assert asset.read_bytes() == b"\xff" * (800 * 1024)
                (home / "skill-fixture-read").touch()
        with (home / "builds").open("a") as log:
            print(os.getpid(), file=log)
        from kotoyose.child_mcp_proxy import relay_process_env
        from kotoyose.config import get_config

        servers = get_config().resolve_resources(preset)[2] if "remote" in preset.mcp_refs else {}
        return Runtime(_expand_cli_env(servers, relay_process_env()))

    registry.runtime_providers["claude"] = RuntimeProviderSpec(build, "claude")
    # The child path prefers the child builder; replace it too so turns use this stub.
    registry.override_child_builder("claude", lambda _alias, preset, cwd, **_options: build(preset, cwd, None, None))
    raise SystemExit(child_runner.main(sys.argv[4:]))


if __name__ == "__main__":
    main()
