"""管理 console (/admin/ui)・pool 状態 API の配信と認証。"""
from datetime import datetime, timezone
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from kotoyose.app import app
from kotoyose.config import Client, get_config, reload_config, save_config
from kotoyose.execution_resources import (
    AgentContainerConfig,
    CodexEgressLimits,
    DriverCell,
    DriverInstall,
    NativeExecutionConfig,
)

client = TestClient(app)
AUTH = ("kotoyose-service", "secret-token")
VERSION_RE = re.compile(r"/admin/ui/console/([0-9a-f]{12})/")


@pytest.fixture(autouse=True)
def secured_config(tmp_path, monkeypatch):
    path = tmp_path / "config.yml"
    path.write_text("models: {}\n")
    monkeypatch.setenv("KOTOYOSE_CONFIG", str(path))
    monkeypatch.setattr("kotoyose.auth.API_KEY", "secret-token")
    monkeypatch.setattr("kotoyose.auth.BASIC_AUTH_USER", "kotoyose-service")
    reload_config()
    yield
    reload_config()


def _version() -> str:
    html = client.get("/admin/ui", auth=AUTH).text
    match = VERSION_RE.search(html)
    assert match, "console の HTML が内容ハッシュ付きの URL を含まない"
    return match.group(1)


@pytest.mark.parametrize("path", ["/admin/pools/state", "/admin/machine", "/admin/browse", "/admin/settings"])
def test_console_pages_require_basic_auth(path):
    res = client.get(path)
    assert res.status_code == 401
    assert res.headers.get("WWW-Authenticate", "").lower().startswith("basic")


def test_console_ui_redirects_unauthenticated_to_login():
    res = client.get("/admin/ui", follow_redirects=False)
    assert res.status_code == 302
    assert res.headers["location"] == "/admin/login?next=/admin/ui"


def test_console_shell_references_hashed_assets():
    res = client.get("/admin/ui", auth=AUTH)
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "Kotoyose Console" in res.text
    assert "__ASSET_BASE__" not in res.text
    version = _version()
    assert f"/admin/ui/console/{version}/console.css" in res.text
    assert f"/admin/ui/console/{version}/js/main.js" in res.text


def test_console_assets_are_served_by_hash():
    version = _version()
    css = client.get(f"/admin/ui/console/{version}/console.css", auth=AUTH)
    assert css.status_code == 200
    assert "text/css" in css.headers["content-type"]
    assert "immutable" in css.headers["cache-control"]
    assert "--accent: #3fb9a6" in css.text

    module = client.get(f"/admin/ui/console/{version}/js/main.js", auth=AUTH)
    assert module.status_code == 200
    assert "parseHash" in module.text


def test_console_assets_with_a_stale_hash_are_not_found():
    res = client.get("/admin/ui/console/000000000000/console.css", auth=AUTH)
    assert res.status_code == 404


def test_console_index_template_is_not_served_as_an_asset():
    version = _version()
    res = client.get(f"/admin/ui/console/{version}/index.html", auth=AUTH)
    assert res.status_code == 404


def test_console_asset_path_cannot_leave_the_console_folder():
    version = _version()
    # 同じ版の下から、console の外にある実在のファイルを指す。ガードが無ければ配ってしまう。
    res = client.get(f"/admin/ui/console/{version}/%2e%2e/%2e%2e/routes/admin_ui.py", auth=AUTH)
    assert res.status_code == 404


def test_console_assets_require_basic_auth():
    version = _version()
    assert client.get(f"/admin/ui/console/{version}/console.css").status_code == 401


def test_console_sidebar_has_the_fixed_order():
    version = _version()
    nav = client.get(f"/admin/ui/console/{version}/js/nav.js", auth=AUTH).text
    labels = ["Dashboard", "Providers", "Endpoint", "Routes", "Combos", "Aliases",
              "Prompts", "Skills", "MCPs", "Agents", "Usage", "Executions", "Machine", "Secrets"]
    positions = [nav.index(f'label: "{label}"') for label in labels]
    assert positions == sorted(positions)


def test_console_js_does_not_assign_innerhtml():
    version = _version()
    for name in ("main.js", "api.js", "dom.js", "format.js", "nav.js", "library.js", "peek.js",
                 "pages/dashboard.js", "pages/providers.js", "pages/endpoint.js",
                 "pages/routes.js", "pages/combos.js", "pages/aliases.js",
                 "pages/prompts.js", "pages/skills.js", "pages/mcps.js",
                 "pages/agents.js", "pages/usage.js", "pages/executions.js",
                 "pages/machine.js", "pages/secrets.js", "path-picker.js"):
        body = client.get(f"/admin/ui/console/{version}/js/{name}", auth=AUTH).text
        assert not re.search(r"\.innerHTML\s*=", body), name


def test_console_combos_and_aliases_modules_are_referenced_and_served():
    html = client.get("/admin/ui", auth=AUTH).text
    version = _version()
    for module_path, symbol in (("js/pages/combos.js", "renderCombos"),
                                ("js/pages/aliases.js", "renderAliases")):
        url = f"/admin/ui/console/{version}/{module_path}"
        assert url in html
        res = client.get(url, auth=AUTH)
        assert res.status_code == 200
        assert "immutable" in res.headers["cache-control"]
        assert symbol in res.text

    main_js = client.get(f"/admin/ui/console/{version}/js/main.js", auth=AUTH).text
    assert "./pages/combos.js" in main_js
    assert "./pages/aliases.js" in main_js

    nav_js = client.get(f"/admin/ui/console/{version}/js/nav.js", auth=AUTH).text
    assert 'key: "combos", label: "Combos", count: "pools"' in nav_js
    assert 'key: "aliases", label: "Aliases", count: "models"' in nav_js


def test_console_library_modules_are_referenced_and_served():
    html = client.get("/admin/ui", auth=AUTH).text
    version = _version()
    for module_path, symbol in (("js/pages/prompts.js", "renderPrompts"),
                                ("js/pages/skills.js", "renderSkills"),
                                ("js/pages/mcps.js", "renderMcps"),
                                ("js/pages/agents.js", "renderAgents")):
        url = f"/admin/ui/console/{version}/{module_path}"
        assert url in html
        res = client.get(url, auth=AUTH)
        assert res.status_code == 200
        assert "immutable" in res.headers["cache-control"]
        assert symbol in res.text

    helper_res = client.get(f"/admin/ui/console/{version}/js/library.js", auth=AUTH)
    assert helper_res.status_code == 200
    assert "immutable" in helper_res.headers["cache-control"]

    main_js = client.get(f"/admin/ui/console/{version}/js/main.js", auth=AUTH).text
    for page_file in ("./pages/prompts.js", "./pages/skills.js", "./pages/mcps.js", "./pages/agents.js"):
        assert page_file in main_js

    nav_js = client.get(f"/admin/ui/console/{version}/js/nav.js", auth=AUTH).text
    assert 'key: "prompts", label: "Prompts", count: "prompt_files"' in nav_js
    assert 'key: "skills", label: "Skills", count: "skill_definitions"' in nav_js
    assert 'key: "mcps", label: "MCPs", count: "mcps"' in nav_js
    assert 'key: "agents", label: "Agents", count: "agent_definitions"' in nav_js


def test_pool_state_returns_empty_without_an_execution_service():
    res = client.get("/admin/pools/state", auth=AUTH)
    assert res.status_code == 200
    assert res.json() == {"pools": {}}


def test_console_monitor_modules_are_referenced_and_served():
    html = client.get("/admin/ui", auth=AUTH).text
    version = _version()
    for module_path, symbol in (("js/pages/usage.js", "renderUsage"),
                                ("js/pages/executions.js", "renderExecutions")):
        url = f"/admin/ui/console/{version}/{module_path}"
        assert url in html
        res = client.get(url, auth=AUTH)
        assert res.status_code == 200
        assert "immutable" in res.headers["cache-control"]
        assert symbol in res.text

    main_js = client.get(f"/admin/ui/console/{version}/js/main.js", auth=AUTH).text
    assert "./pages/usage.js" in main_js
    assert "./pages/executions.js" in main_js

    nav_js = client.get(f"/admin/ui/console/{version}/js/nav.js", auth=AUTH).text
    assert 'key: "usage", label: "Usage"' in nav_js
    assert 'key: "executions", label: "Executions"' in nav_js


def test_hourly_stats_requires_basic_auth():
    unauth = client.get("/admin/stats/hourly")
    assert unauth.status_code == 401
    assert unauth.headers.get("WWW-Authenticate", "").lower().startswith("basic")

    bearer = client.get("/admin/stats/hourly", headers={"Authorization": "Bearer secret-token"})
    assert bearer.status_code == 401

    wrong = client.get("/admin/stats/hourly", auth=("kotoyose-service", "wrong"))
    assert wrong.status_code == 401


def test_hourly_stats_aggregates_24h_buckets():
    current_hour = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    fake_pool = MagicMock()
    fake_pool.fetch = AsyncMock(return_value=[
        {
            "hour": current_hour,
            "requests": 4,
            "successful": 3,
            "failed": 1,
            "tokens_in": 1200,
            "tokens_out": 450,
        },
    ])

    with patch("kotoyose.interactions._get_pool", return_value=fake_pool):
        res = client.get("/admin/stats/hourly?hours=24", auth=AUTH)

    assert res.status_code == 200
    body = res.json()
    assert body["hours"] == 24
    assert len(body["items"]) == 24
    assert body["items"][-1]["hour"] == current_hour.isoformat()
    assert body["items"][-1]["requests"] == 4
    assert body["items"][-1]["successful"] == 3
    assert body["items"][-1]["failed"] == 1
    assert body["items"][-1]["tokens_in"] == 1200
    assert body["items"][-1]["tokens_out"] == 450
    assert body["items"][0]["requests"] == 0
    sql = fake_pool.fetch.await_args.args[0]
    assert "deleted_at IS NULL" in sql
    assert client.get("/admin/stats/hourly?hours=0", auth=AUTH).status_code == 422


def test_console_connect_and_settings_modules_are_referenced_and_served():
    html = client.get("/admin/ui", auth=AUTH).text
    version = _version()
    for module_path, symbol in (("js/pages/providers.js", "renderProviders"),
                                ("js/pages/endpoint.js", "renderEndpoint"),
                                ("js/pages/machine.js", "renderMachine"),
                                ("js/pages/secrets.js", "renderSecrets")):
        url = f"/admin/ui/console/{version}/{module_path}"
        assert url in html
        res = client.get(url, auth=AUTH)
        assert res.status_code == 200
        assert "immutable" in res.headers["cache-control"]
        assert symbol in res.text

    main_js = client.get(f"/admin/ui/console/{version}/js/main.js", auth=AUTH).text
    for page_file in ("./pages/providers.js", "./pages/endpoint.js", "./pages/machine.js", "./pages/secrets.js"):
        assert page_file in main_js
    assert "placeholder" not in main_js

    nav_js = client.get(f"/admin/ui/console/{version}/js/nav.js", auth=AUTH).text
    assert 'key: "providers", label: "Providers"' in nav_js
    assert 'key: "endpoint", label: "Endpoint"' in nav_js
    assert 'key: "machine", label: "Machine"' in nav_js
    assert 'key: "secrets", label: "Secrets"' in nav_js


def test_machine_endpoint_requires_basic_auth():
    unauth = client.get("/admin/machine")
    assert unauth.status_code == 401
    assert unauth.headers.get("WWW-Authenticate", "").lower().startswith("basic")

    bearer = client.get("/admin/machine", headers={"Authorization": "Bearer secret-token"})
    assert bearer.status_code == 401

    wrong = client.get("/admin/machine", auth=("kotoyose-service", "wrong"))
    assert wrong.status_code == 401


def test_machine_endpoint_returns_empty_without_execution_service():
    res = client.get("/admin/machine", auth=AUTH)
    assert res.status_code == 200
    body = res.json()
    assert body["enabled"] is False
    assert body["drivers"] == {}
    assert body["clients"] == []
    assert body["sanction_enforcement"] == "off"


def test_machine_endpoint_never_leaks_tokens_or_token_env(tmp_path, monkeypatch):
    fake_secret = "super-secret-caller-token-9999"
    auth_secret = "never-read-auth-file-secret"
    monkeypatch.setenv("FAKE_CALLER_TOKEN_ENV", fake_secret)

    auth_file = tmp_path / "codex_auth.json"
    auth_file.write_text(f'{{"token": "{auth_secret}"}}\n', encoding="utf-8")

    fake_cli = tmp_path / "fake-claude"
    fake_cli.write_text("#!/bin/sh\necho 'fake-cli 1.2.3'\n", encoding="utf-8")
    fake_cli.chmod(0o755)

    cfg = get_config().model_copy(deep=True)
    cfg.clients["hub"] = Client(

        workspace_roots=[tmp_path],
        places=["isolated"],
        max_surface="write",
        models=["chat-a"],
        max_run_seconds=60.0,
        max_memory_bytes=64 * 1024 * 1024,
        max_processes=32,
        max_cpus=1.0,
        max_concurrent_runs=3,
    )
    save_config(cfg)

    exec_config = NativeExecutionConfig.model_construct(
        schema_version="1",

        image_id="sha256:" + "0" * 64,
        sanction_enforcement="agent",
        drivers={
            "claude.agent-sdk": DriverInstall(cli_binary=fake_cli),
            "codex.app-server": DriverInstall(
                cli_binary=tmp_path / "missing-codex",
                cells={
                    "coding": DriverCell(
                        rootfs=tmp_path / "rootfs",
                        rootfs_digest="a" * 64,
                        codex_egress_limits=CodexEgressLimits(
                            version=1,
                            total_bytes=1024,
                            total_connections=4,
                            request_bytes=512,
                            response_bytes=512,
                        ),
                    ),
                },
            ),
        },
        agent_container=AgentContainerConfig(
            agent_root=str(tmp_path / "agent"),
            home_root=str(tmp_path / "home"),
            max_containers=4,
        ),
    )
    monkeypatch.setattr(app.state, "execution_service", SimpleNamespace(config=exec_config), raising=False)

    res = client.get("/admin/machine", auth=AUTH)
    assert res.status_code == 200
    assert fake_secret not in res.text
    assert "FAKE_CALLER_TOKEN_ENV" not in res.text
    assert auth_secret not in res.text
    assert "token_env" not in res.text

    body = res.json()
    assert body["enabled"] is True
    assert body["sanction_enforcement"] == "agent"
    assert body["native_worker"] is None
    assert body["drivers"]["claude.agent-sdk"]["cli_version"] == "fake-cli 1.2.3"
    assert body["drivers"]["codex.app-server"]["cli_version"] is None
    assert body["drivers"]["codex.app-server"]["cli_version_note"]
    assert "credentials" not in body["drivers"]["claude.agent-sdk"]
    assert "credentials" not in body["drivers"]["codex.app-server"]
    assert body["drivers"]["codex.app-server"]["cells"]["coding"]["rootfs_digest_prefix"] == "a" * 12
    assert body["clients"] == [
        {
            "name": "hub",
            "places": ["isolated"],
            "max_surface": "write",
            "models": ["chat-a"],
            "max_concurrent_runs": 3,
            "max_run_seconds": 60.0,
            "max_memory_bytes": 64 * 1024 * 1024,
            "max_processes": 32,
            "max_cpus": 1.0,
        },
    ]
    assert body["agent_container"]["max_containers"] == 4


def test_console_side_peek_centered_main_and_vertical_quota_assets():
    html = client.get("/admin/ui", auth=AUTH).text
    version = _version()
    assert f"/admin/ui/console/{version}/js/peek.js" in html

    css = client.get(f"/admin/ui/console/{version}/console.css", auth=AUTH).text
    assert "max-width: 1240px" in css
    assert ".main--wide" in css
    assert "max-width: 1440px" in css
    assert "min(var(--peek-width, 640px), 100vw - 24px)" in css  # 幅は把手で変えられる (既定 640px)
    assert "prefers-reduced-motion" in css
    assert "max-width: 760px" in css
    assert "12px 200px minmax(160px, 1fr) 72px 96px" in css
    assert ".split" not in css
    assert ".exec-split" not in css

    peek_js = client.get(f"/admin/ui/console/{version}/js/peek.js", auth=AUTH).text
    assert "openPeek" in peek_js
    assert 'aria-modal": "true"' in peek_js
    assert '"aria-labelledby"' in peek_js

    providers_js = client.get(f"/admin/ui/console/{version}/js/pages/providers.js", auth=AUTH).text
    assert "renderQuotaRows" in providers_js
    assert "renderQuotaBars" not in providers_js
    assert "token set" in providers_js
    assert "token missing" in providers_js

    dashboard_js = client.get(f"/admin/ui/console/{version}/js/pages/dashboard.js", auth=AUTH).text
    assert "renderQuotaRows" in dashboard_js
    assert "renderQuotaBars" not in dashboard_js


def test_console_peek_and_quota_behavior_in_node(tmp_path):
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")

    from pathlib import Path

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.style = {{}};
    this.className = '';
    this.hidden = false;
    this.scrollTop = 0;
    this._text = '';
    this.classList = {{
      contains: (v) => this.className.split(/\\s+/).includes(v),
      add: (...vs) => {{ this.className = [...new Set(this.className.split(/\\s+/).filter(Boolean).concat(vs))].join(' '); }},
      remove: (...vs) => {{ this.className = this.className.split(/\\s+/).filter((v) => !vs.includes(v)).join(' '); }},
    }};
  }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{ this.attributes[k] = String(v); }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children) {{
        if (sel === 'a[href]' && c.tagName === 'A' && c.getAttribute('href') !== null) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
}}

const docListeners = {{}};
const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: (id) => (id === 'main' ? mainEl : null),
  addEventListener: (name, fn) => {{ (docListeners[name] ||= []).push(fn); }},
  removeEventListener: (name, fn) => {{
    if (docListeners[name]) docListeners[name] = docListeners[name].filter((f) => f !== fn);
  }},
}};
const mainEl = new FakeNode('main');
mainEl.setAttribute('id', 'main');
doc.body.append(mainEl);

let confirmAnswer = true;
const win = {{
  matchMedia: () => ({{ matches: true }}),
  confirm: () => confirmAnswer,
}};
globalThis.document = doc;
globalThis.window = win;
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers', origin: 'http://localhost' }};
globalThis.requestAnimationFrame = (cb) => cb();
globalThis.confirm = (...args) => win.confirm(...args);

const {{ openPeek, closePeek, createDirtyTracker }} = await import({repr((console_dir / "js/peek.js").as_uri())});
const {{ renderQuotaRows, renderProviders }} = await import({repr((console_dir / "js/pages/providers.js").as_uri())});

// 1. Trigger focus -> openPeek -> Esc dirty guard -> close -> focus restored
const triggerRow = new FakeNode('a');
triggerRow.setAttribute('href', '#/providers/connection/c1');
mainEl.append(triggerRow);
triggerRow.focus();

const tracker = createDirtyTracker();
const bodyInput = new FakeNode('input');
tracker.attach(bodyInput);

let closedCount = 0;
openPeek({{
  title: 'c1',
  subtitle: 'claude · subscription',
  body: bodyInput,
  onClose: () => {{ closedCount += 1; }},
  isDirty: tracker.isDirty,
}});

const peekRoot = doc.body.children.find((n) => n.classList.contains('peek-root'));
assert.ok(peekRoot, 'peek-root appended to body');
const panel = peekRoot.children[1];
assert.equal(panel.getAttribute('role'), 'dialog');
assert.equal(panel.getAttribute('aria-modal'), 'true');
assert.equal(panel.getAttribute('aria-labelledby'), 'peek-dialog-title');
assert.equal(doc.activeElement, panel, 'focus moves into peek panel');

// Dirty guard cancels close when confirm returns false
bodyInput.dispatch('input');
confirmAnswer = false;
for (const fn of [...(docListeners.keydown || [])]) fn({{ key: 'Escape', preventDefault() {{}}, stopPropagation() {{}} }});
assert.equal(closedCount, 0, 'Esc aborted when user declines discard');
assert.equal(peekRoot.isConnected, true);

// Dirty guard closes when confirm returns true and restores focus
confirmAnswer = true;
for (const fn of [...(docListeners.keydown || [])]) fn({{ key: 'Escape', preventDefault() {{}}, stopPropagation() {{}} }});
assert.equal(closedCount, 1, 'Esc closed peek when confirmed');
assert.equal(peekRoot.isConnected, false);
assert.equal(doc.activeElement, triggerRow, 'focus restored to triggering row');

// 2. renderQuotaRows thresholds, used %, relative reset with absolute title, via caption
const inTwoHours = new Date(Date.now() + 2 * 3600 * 1000 + 25 * 60 * 1000).toISOString();
const qNode = renderQuotaRows([
  {{ label: 'Current session', used_percent: 14, resets_at: inTwoHours }},
  {{ label: 'Weekly all', used_percent: 75, resets_at: inTwoHours }},
  {{ label: 'Weekly fable', used_percent: 92, resets_at: inTwoHours }},
], {{ via: 'claude-host' }});
assert.equal(qNode.children[0].textContent, 'via claude-host');
const rows = qNode.children.slice(1);
assert.equal(rows.length, 3);
assert.ok(rows[0].children[0].className.includes('positive'));
assert.ok(rows[0].children[2].children[0].className.includes('ok'));
assert.equal(rows[0].children[3].textContent, '14% used');
assert.match(rows[0].children[4].textContent, /^in /);
assert.ok(rows[0].children[4].getAttribute('title'), 'absolute time tooltip present');
assert.ok(rows[1].children[0].className.includes('warning'));
assert.ok(rows[1].children[2].children[0].className.includes('warning'));
assert.ok(rows[2].children[0].className.includes('negative'));
assert.ok(rows[2].children[2].children[0].className.includes('danger'));
"""
    script_path = tmp_path / "verify_console.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_console_peek_locks_page_scroll_and_is_resizable():
    """side peek: 開いている間は背後をスクロールさせず、左端の把手で幅を変えられる。"""
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    js = (root / "js/peek.js").read_text(encoding="utf-8")
    css = (root / "console.css").read_text(encoding="utf-8")
    assert "lockPageScroll()" in js and "unlockPageScroll()" in js
    assert 'role: "separator"' in js and "pointerdown" in js and "localStorage" in js
    assert "html.peek-scroll-lock" in css and "overflow: hidden" in css
    assert ".peek-resize" in css and "col-resize" in css
    assert "var(--peek-width, 640px)" in css


def test_console_routes_model_suggestions_send_connection_in_node(tmp_path):
    """routes.js sends ?connection=... on model focus and connection change, never endpoint/credential."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")

    from pathlib import Path

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.style = {{}};
    this.className = '';
    this.hidden = false;
    this.scrollTop = 0;
    this._text = '';
    this._value = '';
    this.checked = false;
    this.disabled = false;
    this.elements = [];
    this.classList = {{
      contains: (v) => this.className.split(/\\s+/).includes(v),
      add: (...vs) => {{ this.className = [...new Set(this.className.split(/\\s+/).filter(Boolean).concat(vs))].join(' '); }},
      remove: (...vs) => {{ this.className = this.className.split(/\\s+/).filter((v) => !vs.includes(v)).join(' '); }},
    }};
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'value') this._value = String(v);
    if (k === 'disabled') this.disabled = true;
    if (k === 'checked') this.checked = true;
  }}
  removeAttribute(k) {{
    delete this.attributes[k];
    if (k === 'disabled') this.disabled = false;
    if (k === 'checked') this.checked = false;
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll() {{ return []; }}
}}

const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: () => null,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
const mainEl = new FakeNode('main');
doc.body.append(mainEl);

globalThis.document = doc;
globalThis.window = {{ matchMedia: () => ({{ matches: true }}), confirm: () => true }};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/routes/new', origin: 'http://localhost' }};
globalThis.requestAnimationFrame = (cb) => cb();

const fetchedUrls = [];
const cannedByConn = {{
  'claude-oauth': {{
    provider: 'claude',
    models: ['claude-opus-5-5', 'claude-sonnet-5-0'],
    source: 'claude_cli_initialize',
    warning: null,
  }},
  'claude-backup': {{
    provider: 'claude',
    models: ['claude-opus-5'],
    source: 'bundled_catalog',
    warning: 'Claude CLI にログインしていないため bundled catalog を返しました。',
  }},
}};

globalThis.fetch = async (url) => {{
  fetchedUrls.push(String(url));
  const parsed = new URL(String(url), 'http://localhost');
  const conn = parsed.searchParams.get('connection');
  const payload = cannedByConn[conn] || {{ provider: 'claude', models: [], source: 'bundled_catalog', warning: null }};
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(payload),
  }};
}};

const {{ renderRoutes, suggestionsFor }} = await import({repr((console_dir / "js/pages/routes.js").as_uri())});

// Direct suggestionsFor call
const direct = await suggestionsFor('claude', 'claude-oauth');
assert.equal(direct.source, 'claude_cli_initialize');
assert.equal(fetchedUrls[0], '/admin/providers/claude/models?connection=claude-oauth');
assert.ok(!fetchedUrls[0].includes('endpoint_base_url'));
assert.ok(!fetchedUrls[0].includes('endpoint_api_key_env'));

// Full routeForm interaction: focus and connection select change
const config = {{
  catalog: {{ drivers: {{ 'claude.agent-sdk': {{ provider: 'claude' }} }} }},
  connections: {{
    'claude-oauth': {{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', secret: 'claude-oauth-secret' }},
    'claude-backup': {{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', config_dir: '/tmp/claude-backup' }},
  }},
  routes: {{}},
  models: {{}},
}};

await renderRoutes(mainEl, {{ config, rest: ['new'], refresh: async () => {{}} }});

function findByAttr(root, attr, val) {{
  if (root.getAttribute && root.getAttribute(attr) === val) return root;
  for (const child of root.children || []) {{
    const found = findByAttr(child, attr, val);
    if (found) return found;
  }}
  return null;
}}
function findById(root, id) {{ return findByAttr(root, 'id', id); }}

const modelInput = findByAttr(doc.body, 'aria-label', 'Model');
const connSelect = findByAttr(doc.body, 'aria-label', 'Connection');
const modelList = findById(doc.body, 'route-model-options');
const modelHint = modelInput.parentNode.children[2];

connSelect.value = 'claude-oauth';
modelInput.dispatch('focus');
await new Promise((r) => setTimeout(r, 0));

assert.deepEqual(modelList.children.map((o) => o.value), ['claude-opus-5-5', 'claude-sonnet-5-0']);
assert.equal(modelHint.textContent, 'from Claude CLI (claude-oauth)');

// Changing connection select re-fetches for the newly selected connection and displays source + warning
connSelect.value = 'claude-backup';
connSelect.dispatch('change');
await new Promise((r) => setTimeout(r, 0));

assert.equal(fetchedUrls[fetchedUrls.length - 1], '/admin/providers/claude/models?connection=claude-backup');
assert.deepEqual(modelList.children.map((o) => o.value), ['claude-opus-5']);
assert.ok(modelHint.textContent.includes('from bundled_catalog (claude-backup)'));
assert.ok(modelHint.textContent.includes('Claude CLI にログインしていないため'));
"""
    script_path = tmp_path / "verify_routes.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_console_providers_families_and_attention_in_node(tmp_path):
    """Provider accordion rows, install, filters, persisted collapse and order."""
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")

    from pathlib import Path

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.style = {{}};
    this.className = '';
    this.hidden = false;
    this.open = false;
    this.disabled = false;
    this._text = '';
    this._value = '';
    this.classList = {{
      contains: (v) => this.className.split(/\\s+/).includes(v),
      add: (...vs) => {{ this.className = [...new Set(this.className.split(/\\s+/).filter(Boolean).concat(vs))].join(' '); }},
      remove: (...vs) => {{ this.className = this.className.split(/\\s+/).filter((v) => !vs.includes(v)).join(' '); }},
      toggle: (v, force) => {{
        const has = this.classList.contains(v);
        const on = force === undefined ? !has : Boolean(force);
        if (on) this.classList.add(v);
        else this.classList.remove(v);
        return on;
      }},
    }};
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = "";
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'open') this.open = true;
    if (k === 'hidden') this.hidden = true;
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ type: name, target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  setPointerCapture(id) {{ this.capturedPointer = id; }}
  closest(sel) {{
    const tags = sel.split(',').map((s) => s.trim().toUpperCase());
    let cur = this;
    while (cur) {{
      if (tags.includes(cur.tagName) || (sel === '[data-provider]' && cur.getAttribute('data-provider'))) return cur;
      cur = cur.parentNode;
    }}
    return null;
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children) {{
        if (sel === 'a[href]' && c.tagName === 'A' && c.getAttribute('href') !== null) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
}}

const storageMap = new Map();
const appEl = new FakeNode('div');
const mainEl = new FakeNode('main');
mainEl.setAttribute('id', 'main');
const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: (id) => (id === 'main' ? mainEl : id === 'app' ? appEl : null),
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
doc.body.append(appEl, mainEl);

globalThis.document = doc;
globalThis.window = {{
  matchMedia: () => ({{ matches: true }}),
  confirm: () => true,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  localStorage: {{
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => {{ storageMap.set(k, String(v)); }},
  }},
}};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers', origin: 'http://localhost', pathname: '/admin/ui', search: '' }};
globalThis.history = {{ replaceState: () => {{}} }};
globalThis.requestAnimationFrame = (cb) => cb();

const inFuture = new Date(Date.now() + 4 * 86400 * 1000 + 3600 * 1000).toISOString();
const inPast = new Date(Date.now() - 60 * 1000).toISOString();
const config = {{
  settings: {{ provider_order: ['codex', 'claude', 'print_cli', 'cli.print'] }},
  catalog: {{
    drivers: {{
      'claude.agent-sdk': {{ provider: 'claude', places: {{ isolated: ['read', 'write'] }} }},
      'codex.app-server': {{ provider: 'codex', places: {{ isolated: ['write'] }} }},
      'plugin.driver': {{ provider: 'plugin-provider' }},
      'cli.print': {{ provider: 'print_cli', places: {{ isolated: ['write'], host: ['write'] }} }},
    }},
  }},
  connections: {{}},
  routes: {{
    'r-claude-1': {{ driver: 'claude.agent-sdk', connection: 'c-ok' }},
    'r-claude-2': {{ driver: 'claude.agent-sdk', connection: 'c-ok' }},
    'r-print-1': {{ driver: 'cli.print', connection: 'agy-good', profile: 'agy' }},
    'r-print-2': {{ driver: 'cli.print', connection: 'agy-good', profile: 'agy' }},
    'r-print-3': {{ driver: 'cli.print', connection: 'cursor-good', profile: 'cursor' }},
    'r-claude-3': {{ driver: 'claude.agent-sdk', connection: 'c-hot' }},
  }},
  route_sanctions: {{}},
}};

const apiResponses = {{
  '/admin/config': config,
  '/admin/session': {{ user: 'admin' }},
  '/admin/machine': {{ enabled: false, drivers: {{}} }},
  '/admin/connections': {{
    connections: [
      {{
        id: 'c-ok',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        config_dir: '/tmp/claude-ok',
        routes: ['r-claude-1', 'r-claude-2'],
        state: {{ auth: 'ok', plan: 'max', cli_version: 'CLI 2.1.325 (Claude Code)', expires_at: inFuture, quota: [{{ label: 'Weekly', used_percent: 42, resets_at: inFuture }}] }},
      }},
      {{
        id: 'c-hot',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        quota_from: 'c-ok',
        routes: ['r-claude-3'],
        state: {{ auth: 'ok', quota_via: 'c-ok', quota: [{{ label: 'Weekly', used_percent: 91, resets_at: inFuture }}] }},
      }},
      {{
        id: 'o-good',
        provider: 'codex',
        kind: 'subscription', data_policy: 'no_training',
        routes: [],
        state: {{ auth: 'ok', plan: 'pro', cli_version: 'CLI codex-cli 0.170.0', quota: [{{ label: '5h', used_percent: 76, resets_at: inFuture }}] }},
      }},
      {{
        id: 'agy-good', provider: 'antigravity', profile: 'agy', kind: 'subscription',
        data_policy: 'no_training', routes: ['r-print-1', 'r-print-2'], state: {{ auth: 'ok' }},
      }},
      {{
        id: 'cursor-good', provider: 'cursor', profile: 'cursor', kind: 'subscription',
        data_policy: 'no_training', routes: ['r-print-3'], state: {{ auth: 'ok' }},
      }},
      {{
        id: 'o-out',
        provider: 'openai',
        kind: 'subscription', data_policy: 'no_training',
        routes: [],
        state: {{ auth: 'logged_out', quota: [] }},
      }},
    ],
  }},
  '/admin/secrets': {{ secrets: [{{ name: 'my-secret', set: true }}] }},
  '/admin/tools': {{ tools: [{{ profile: 'grok', provider: 'grok', version: '1.0.89', installed: false }}] }},
}};

let finishInstall;
let rejectOrder = false;
const savedOrders = [];
globalThis.fetch = async (url, init) => {{
  const key = new URL(String(url), 'http://localhost').pathname;
  if (key === '/admin/settings/provider-order') {{
    assert.equal(init.method, 'PUT');
    const body = JSON.parse(init.body);
    if (rejectOrder) return {{ ok: false, status: 500, text: async () => 'save failed' }};
    savedOrders.push(body.provider_order);
    config.settings.provider_order = body.provider_order;
  }}
  if (key === '/admin/tools/grok/install') {{
    assert.equal(init.method, 'POST');
    assert.equal(init.headers['X-Kotoyose-Admin'], '1');
    await new Promise((resolve) => {{ finishInstall = resolve; }});
    return {{ ok: true, status: 200, text: async () => JSON.stringify({{
      profile: 'grok', version: '1.0.89', installed: true, binary_version: 'grok 1.0.89 (28be5bb1bf65)',
    }}) }};
  }}
  const payload = apiResponses[key] || {{}};
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(payload),
  }};
}};

const {{ parseHash }} = await import({repr((console_dir / "js/main.js").as_uri())});
const {{
  PROVIDER_FAMILIES,
  PROVIDER_TO_FAMILY,
  connectionAttention,
  connectionHash,
  familyForProvider,
  providersHash,
  renderProviders,
}} = await import({repr((console_dir / "js/pages/providers.js").as_uri())});

// 1. parseHash preserves path and query parameters in URL hash
const parsedList = parseHash('#/providers?family=claude&attention=1');
assert.equal(parsedList.page, 'providers');
assert.deepEqual(parsedList.rest, []);
assert.equal(parsedList.query.get('family'), 'claude');
assert.equal(parsedList.query.get('attention'), '1');

const parsedPeek = parseHash('#/providers/connection/c-hot?family=claude&attention=1');
assert.equal(parsedPeek.page, 'providers');
assert.deepEqual(parsedPeek.rest, ['connection', 'c-hot']);
assert.equal(parsedPeek.query.get('family'), 'claude');

// 2. Family mapping table covers all required providers + fallback to other
assert.equal(familyForProvider('claude'), 'claude');
assert.equal(familyForProvider('anthropic'), 'claude');
assert.equal(familyForProvider('codex'), 'openai');
assert.equal(familyForProvider('openai'), 'openai');
assert.equal(familyForProvider('antigravity'), 'google');
assert.equal(familyForProvider('cursor'), 'cursor');
assert.equal(familyForProvider('custom-llm'), 'other');
assert.equal(familyForProvider(''), 'other');
assert.deepEqual(PROVIDER_FAMILIES.map((f) => f.key), ['claude', 'openai', 'google', 'cursor', 'grok', 'other']);
assert.equal(PROVIDER_TO_FAMILY.anthropic, 'claude');

// 3. Attention rule covers all conditions
const setSecrets = new Set(['existing-secret']);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', config_dir: '/d', state: {{ auth: 'ok', expires_at: inFuture, quota: [{{ used_percent: 89 }}] }} }}, setSecrets).needsAttention, false);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', state: {{ auth: 'expired' }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'codex', kind: 'subscription', data_policy: 'no_training', state: {{ auth: 'logged_out' }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'codex', kind: 'subscription', data_policy: 'no_training', state: {{ auth: 'login_required' }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', state: {{ auth: 'ok', expires_at: inPast }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', secret: 'missing-secret', state: {{ auth: 'unknown' }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', secret: 'existing-secret', state: {{ auth: 'unknown' }} }}, setSecrets).needsAttention, false);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', secret: 'env:CLAUDE_TOKEN', state: {{ auth: 'unknown' }} }}, setSecrets).needsAttention, false);
assert.equal(connectionAttention({{ provider: 'codex', kind: 'subscription', data_policy: 'no_training', state: {{ auth: 'ok', last_error: 'boom' }} }}, setSecrets).needsAttention, true);
assert.equal(connectionAttention({{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', config_dir: '/d', state: {{ auth: 'ok', quota: [{{ used_percent: 90 }}] }} }}, setSecrets).needsAttention, true);

// 4. Full renderProviders output check
const findAll = (root, pred, acc = []) => {{
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}};

mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams(''), refresh: async () => {{}} }});

const sections = () => findAll(mainEl, (n) => n.classList?.contains('family-section'));
const ids = () => sections().map((s) => s.getAttribute('data-provider'));
assert.deepEqual(ids(), ['codex', 'claude', 'antigravity', 'cursor', 'grok', 'openai', 'plugin-provider']);
const [codex, claude, antigravity, cursor, grok, openai, plugin] = sections();
const byClass = (root, cls) => findAll(root, (n) => n.classList?.contains(cls));
const assertVersion = (section, raw, version) => {{
  const [label] = findAll(byClass(section, 'family-head')[0], (n) => n.getAttribute('title') === raw);
  assert.ok(label, `Missing version tooltip: ${{raw}}`);
  assert.equal(label.textContent, `CLI ${{version}}`);
  assert.equal(label.className, 'small mono muted');
}};
assertVersion(claude, 'CLI 2.1.325 (Claude Code)', '2.1.325');
assertVersion(codex, 'CLI codex-cli 0.170.0', '0.170.0');
assert.deepEqual(byClass(antigravity, 'driver-chip').map((chip) => chip.getAttribute('aria-label')), ['cli.print, host, isolated, 2 routes']);
// Drivers are a level-2 list: each item carries name, place tags and the route count as separate parts.
const [agyDriver] = byClass(antigravity, 'driver-chip');
assert.equal(agyDriver.parentNode.tagName, 'LI');
assert.equal(byClass(agyDriver, 'driver-item-name')[0].textContent, 'cli.print');
assert.deepEqual(byClass(agyDriver, 'place-tag').map((tag) => tag.textContent), ['host', 'isolated']);
assert.equal(byClass(agyDriver, 'driver-item-routes')[0].textContent, '2 routes');
assert.deepEqual(byClass(cursor, 'driver-chip').map((chip) => chip.getAttribute('aria-label')), ['cli.print, host, isolated, 1 route']);
assert.ok(!byClass(grok, 'driver-chip').some((chip) => chip.getAttribute('aria-label').startsWith('cli.print')));
assert.equal(byClass(mainEl, 'providers-seg').length, 0);
assert.equal(findAll(mainEl, (n) => n.getAttribute('aria-label') === 'Filter by family').length, 0);
const [toolbar] = byClass(mainEl, 'providers-toolbar');
const [attention] = byClass(toolbar, 'providers-attention-toggle');
assert.deepEqual(toolbar.children, [attention]);
assert.equal(attention.getAttribute('aria-pressed'), 'false');
attention.dispatch('click');
assert.equal(location.hash, '#/providers?attention=1');

assert.match(codex.textContent, /OpenAICodex1 connectionCLI 0.170.0/);
assert.ok(byClass(codex, 'dot').some((n) => n.classList.contains('positive')));
assert.match(claude.textContent, /AnthropicClaude2 connections/);
assert.ok(byClass(byClass(claude, 'family-toggle')[0], 'dot')[0].classList.contains('negative'));
assert.ok(byClass(byClass(openai, 'family-toggle')[0], 'dot')[0].classList.contains('negative'));
assert.match(antigravity.textContent, /GoogleAntigravity/);
assert.match(plugin.textContent, /plugin-provider0 connections/);

const [install] = findAll(byClass(grok, 'family-head')[0], (n) => n.tagName === 'BUTTON' && n.textContent === 'Install');
assert.match(grok.textContent, /xAIGrok Build0 connectionsgrok 1.0.89Install/);
const installing = install.listeners.click[0]();
assert.equal(install.disabled, true);
assert.ok(grok.textContent.includes('Installing v1.0.89'));
finishInstall();
await installing;
assert.equal(install.hidden, true);
assertVersion(grok, 'grok 1.0.89 (28be5bb1bf65)', '1.0.89');

for (const section of sections()) {{
  const empty = [grok, plugin].includes(section);
  assert.equal(byClass(section, 'family-toggle')[0].getAttribute('aria-expanded'), String(!empty));
  assert.equal(byClass(section, 'family-body')[0].hidden, empty);
}}
const claudeRows = byClass(byClass(claude, 'family-body')[0], 'connection-row');
assert.deepEqual(claudeRows.map((row) => byClass(row, 'connection-name')[0].textContent), ['c-hot', 'c-ok']);
assert.equal(claudeRows[0].getAttribute('href'), '#/providers/connection/c-hot');
assert.equal(byClass(claudeRows[1], 'connection-kind')[0].textContent, 'subscription');
// Drivers start closed (secondary detail); opening one is remembered per browser.
const [agyDrivers] = byClass(antigravity, 'family-drivers');
assert.equal(agyDrivers.getAttribute('open'), null);
agyDrivers.open = true;
agyDrivers.dispatch('toggle');
assert.deepEqual(JSON.parse(storageMap.get('kotoyose.console.providers.drivers-open')), ['antigravity']);
agyDrivers.open = false;
agyDrivers.dispatch('toggle');
assert.deepEqual(JSON.parse(storageMap.get('kotoyose.console.providers.drivers-open')), []);
assert.equal(byClass(mainEl, 'quota-win').length, 0);

const [handle] = byClass(claude, 'provider-handle');
handle.focus();
handle.dispatch('keydown', {{ key: 'ArrowUp' }});
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(savedOrders, [['claude', 'codex', 'antigravity', 'cursor', 'grok', 'openai', 'plugin-provider']]);
assert.deepEqual(ids(), savedOrders[0]);
assert.equal(doc.activeElement, handle);

doc.elementFromPoint = () => byClass(antigravity, 'family-toggle')[0];
handle.dispatch('pointerdown', {{ pointerId: 1, button: 0 }});
assert.equal(handle.capturedPointer, 1);
handle.dispatch('pointermove', {{ pointerId: 1 }});
assert.ok(antigravity.classList.contains('is-drop-target'));
handle.dispatch('pointercancel', {{ pointerId: 1 }});
assert.ok(!antigravity.classList.contains('is-drop-target'));
assert.equal(savedOrders.length, 1);

handle.dispatch('pointerdown', {{ pointerId: 2, button: 0 }});
handle.dispatch('pointerup', {{ pointerId: 2 }});
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(savedOrders[1], ['codex', 'antigravity', 'claude', 'cursor', 'grok', 'openai', 'plugin-provider']);
assert.deepEqual(ids(), savedOrders[1]);

rejectOrder = true;
handle.dispatch('keydown', {{ key: 'ArrowUp' }});
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(ids(), savedOrders[1]);
assert.match(mainEl.textContent, /Could not save provider order/);
rejectOrder = false;

const [claudeToggle] = byClass(claude, 'family-toggle');
claudeToggle.dispatch('click');
assert.equal(claudeToggle.getAttribute('aria-expanded'), 'false');
assert.equal(byClass(claude, 'family-body')[0].hidden, true);
const [pluginToggle] = byClass(plugin, 'family-toggle');
pluginToggle.dispatch('click');
assert.equal(pluginToggle.getAttribute('aria-expanded'), 'true');
assert.equal(byClass(plugin, 'family-body')[0].hidden, false);
assert.deepEqual(JSON.parse(storageMap.get('kotoyose.console.providers.collapsed')), {{ claude: true, 'plugin-provider': false }});

const [add] = byClass(plugin, 'provider-add');
assert.equal(byClass(add, 'provider-add-label')[0].textContent, 'New connection');
// The add row closes the connection list, above the Drivers disclosure.
const [pluginConnections] = byClass(plugin, 'family-connections');
assert.equal(pluginConnections.children.at(-1), add);
assert.equal(byClass(plugin, 'family-body')[0].children[0], pluginConnections);
const newRoute = parseHash(add.getAttribute('href'));
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, ...newRoute, refresh: async () => {{}} }});
assert.deepEqual(ids(), savedOrders[1]);
assert.equal(byClass(sections().find((section) => section.getAttribute('data-provider') === 'claude'), 'family-toggle')[0].getAttribute('aria-expanded'), 'false');
assert.equal(byClass(sections().at(-1), 'family-toggle')[0].getAttribute('aria-expanded'), 'true');
const [providerSelect] = findAll(doc.body, (n) => n.tagName === 'SELECT' && n.getAttribute('aria-label') === 'Provider');
assert.equal(providerSelect.value, 'plugin-provider');

mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams('attention=1'), refresh: async () => {{}} }});
assert.deepEqual(ids(), ['claude', 'openai']);
assert.deepEqual(byClass(mainEl, 'connection-name').map((n) => n.textContent), ['c-hot', 'o-out']);
assert.match(sections()[0].textContent, /2 connections/);

mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams('family=openai&attention=1'), refresh: async () => {{}} }});
assert.deepEqual(ids(), ['openai']);
assert.match(byClass(mainEl, 'connection-row')[0].textContent, /logged out/);
assert.equal(providersHash({{ family: 'openai', attention: true }}), '#/providers?family=openai&attention=1');
assert.equal(connectionHash('o-out', {{ family: 'openai', attention: true }}), '#/providers/connection/o-out?family=openai&attention=1');
"""
    script_path = tmp_path / "verify_providers.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_console_quota_labels_are_normalized_across_providers(tmp_path):
    """Claude CLI と Codex の窓の名前を同じ言い方で表示する (元の名前は title に残す)。"""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console/js/pages/providers.js"
    script = tmp_path / "labels.mjs"
    script.write_text(
        f"import {{ quotaDisplayLabel }} from {json.dumps(module.as_uri())};\n"
        "const cases = [\n"
        "  'Current session', '5h', 'Current week (all models)', '7d', 'Current week (Fable)', 'other',\n"
        "  'Gemini · Weekly', 'Gemini · Session (5h)', 'Claude & GPT · Weekly', 'Argon · Weekly',\n"
        "  'gemini-weekly', 'gemini-5h', '3p-weekly', '3p-5h', 'gemini-argon-wtu-weekly',\n"
        "];\n"
        "console.log(JSON.stringify(cases.map(quotaDisplayLabel)));\n",
        encoding="utf-8",
    )
    out = subprocess.run([node, str(script)], capture_output=True, text=True, check=True, timeout=30)
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        "Session (5h)", "Session (5h)", "Weekly (7d)", "Weekly (7d)", "Weekly · Fable", "other",
        "Gemini · Weekly", "Gemini · Session (5h)", "Claude & GPT · Weekly", "Argon · Weekly",
        "Gemini · Weekly", "Gemini · Session (5h)", "Claude & GPT · Weekly", "Claude & GPT · Session (5h)", "Argon · Weekly",
    ]


def test_console_providers_compact_duplicate_quota_and_google_family_in_node(tmp_path):
    """Dashboard deduplicates quota rows and Providers shows full quota in the peek."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeClassList {{
  constructor(node) {{ this.node = node; this.set = new Set(); }}
  sync() {{ this.node.attributes.class = [...this.set].join(' '); }}
  add(...names) {{ for (const n of names) if (n) this.set.add(n); this.sync(); }}
  remove(...names) {{ for (const n of names) this.set.delete(n); this.sync(); }}
  toggle(name, force) {{
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }}
  contains(name) {{ return this.set.has(name); }}
}}

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.classList = new FakeClassList(this);
    this._text = '';
    this.disabled = false;
    this.hidden = false;
    this.open = false;
  }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  set className(v) {{
    this.classList.set = new Set(String(v || '').split(/\\s+/).filter(Boolean));
    this.classList.sync();
  }}
  get className() {{ return this.attributes.class || ''; }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (Array.isArray(item)) {{ this.append(...item); continue; }}
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'open') this.open = true;
    if (k === 'hidden') this.hidden = true;
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  closest(sel) {{
    const tags = sel.split(',').map((s) => s.trim().toUpperCase());
    let cur = this;
    while (cur) {{
      if (tags.includes(cur.tagName)) return cur;
      cur = cur.parentNode;
    }}
    return null;
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children) {{
        if (sel === 'a[href]' && c.tagName === 'A' && c.getAttribute('href') !== null) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
}}

const storageMap = new Map();
const appEl = new FakeNode('div');
const mainEl = new FakeNode('main');
mainEl.setAttribute('id', 'main');
const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: (id) => (id === 'main' ? mainEl : id === 'app' ? appEl : null),
  querySelector: () => null,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
doc.body.append(appEl, mainEl);

globalThis.document = doc;
globalThis.window = {{
  matchMedia: () => ({{ matches: true }}),
  confirm: () => true,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  localStorage: {{
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => {{ storageMap.set(k, String(v)); }},
  }},
}};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers', origin: 'http://localhost', pathname: '/admin/ui', search: '' }};
globalThis.history = {{ replaceState: () => {{}} }};
globalThis.requestAnimationFrame = (cb) => cb();

const inThreeHours = new Date(Date.now() + 3 * 3600 * 1000 + 15 * 60 * 1000).toISOString();
const sharedClaudeQuota = [
  {{ window: 'Current session', label: 'Current session', used_percent: 12, resets_at: inThreeHours }},
  {{ window: 'Current week (all models)', label: 'Current week (all models)', used_percent: 78, resets_at: inThreeHours }},
];
const sharedAgyQuota = [
  {{ window: 'gemini-weekly', label: 'Gemini · Weekly', used_percent: 28.1, resets_at: inThreeHours }},
  {{ window: 'gemini-5h', label: 'Gemini · Session (5h)', used_percent: 93.4, resets_at: inThreeHours }},
];

const config = {{
  catalog: {{
    drivers: {{
      'claude.agent-sdk': {{ provider: 'claude', places: {{ isolated: ['read', 'write'] }} }},
      'antigravity.sdk': {{ provider: 'antigravity', places: {{ host: [] }} }},
    }},
  }},
  connections: {{}},
  routes: {{}},
  route_sanctions: {{}},
}};

const apiResponses = {{
  '/admin/config': config,
  '/admin/session': {{ user: 'admin' }},
  '/admin/machine': {{ enabled: false, drivers: {{}} }},
  '/admin/connections': {{
    connections: [
      {{
        id: 'claude-host',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        config_dir: '/tmp/claude-host',
        routes: [],
        state: {{ auth: 'ok', quota: sharedClaudeQuota }},
      }},
      {{
        id: 'claude-oauth',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        secret: 'claude-oauth-sec',
        quota_from: 'claude-host',
        routes: [],
        state: {{ auth: 'ok', quota_via: 'claude-host', quota_source: 'claude_cli_usage', quota: sharedClaudeQuota }},
      }},
      {{
        id: 'agy-alpha',
        provider: 'antigravity',
        kind: 'subscription', data_policy: 'no_training',
        routes: [],
        state: {{ auth: 'ok', quota_source: 'agy_cli_usage', quota: sharedAgyQuota }},
      }},
      {{
        id: 'agy-beta',
        provider: 'antigravity',
        kind: 'subscription', data_policy: 'no_training',
        routes: [],
        state: {{ auth: 'ok', quota_source: 'agy_cli_usage', quota: JSON.parse(JSON.stringify(sharedAgyQuota)) }},
      }},
    ],
  }},
  '/admin/secrets': {{ secrets: [{{ name: 'claude-oauth-sec', set: true }}] }},
}};

globalThis.fetch = async (url) => {{
  const key = new URL(String(url), 'http://localhost').pathname;
  const payload = apiResponses[key] || {{}};
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(payload),
  }};
}};

const {{ providerMark }} = await import({repr((console_dir / "js/provider-ui.js").as_uri())});
const {{ renderRoutes }} = await import({repr((console_dir / "js/pages/routes.js").as_uri())});
const {{ renderAliases }} = await import({repr((console_dir / "js/pages/aliases.js").as_uri())});
const {{ renderDashboard }} = await import({repr((console_dir / "js/pages/dashboard.js").as_uri())});
const {{ renderProviders }} = await import({repr((console_dir / "js/pages/providers.js").as_uri())});

const findAll = (root, pred, acc = []) => {{
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}};

mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams(''), refresh: async () => {{}} }});

assert.equal(findAll(mainEl, (n) => n.classList?.contains('quota-win')).length, 0);
assert.equal(findAll(mainEl, (n) => n.classList?.contains('quota-compact')).length, 0);
assert.equal(findAll(mainEl, (n) => n.getAttribute?.('data-provider') === 'antigravity')[0]
  .textContent.includes('2 connections'), true);

mainEl.replaceChildren();
const orderedConfig = {{ ...config, settings: {{ provider_order: ['other', 'antigravity', 'claude'] }} }};
await renderDashboard(mainEl, {{ config: orderedConfig, counts: {{}} }});
const byClass = (root, name) => findAll(root, (n) => n.classList?.contains(name));
const panels = () => byClass(mainEl, 'grouped-provider');
const panelIds = () => panels().map((n) => n.getAttribute('data-provider'));
assert.deepEqual(panelIds(), ['antigravity', 'claude']);
assert.deepEqual(byClass(panels()[0], 'family-name').map((n) => n.textContent), ['Google']);
assert.equal(byClass(panels()[0], 'provider-product')[0].textContent, 'Antigravity');
assert.equal(byClass(panels()[0], 'family-counts')[0].textContent, '2 connections');
const attention = byClass(mainEl, 'attention-item');
assert.equal(attention.length, 1);
assert.equal(attention[0].textContent, 'Nothing needs attention');
assert.equal(byClass(attention[0], 'positive').length, 1);
const mark = providerMark('antigravity', 'warning', 'Needs attention');
assert.ok(mark.classList.contains('is-google'));
assert.equal(byClass(mark, 'provider-mark-status')[0].getAttribute('aria-label'), 'Needs attention');
const quotaRows = findAll(mainEl, (n) => n.classList?.contains('quota-row'));
const quotaById = (id) => quotaRows.find((row) => findAll(row, (n) => n.classList?.contains('row-title'))[0].textContent === id);
const hostCard = quotaById('claude-host');
const oauthCard = quotaById('claude-oauth');

// A quota reference on Dashboard begins with a compact line.
assert.equal(findAll(hostCard, (n) => n.classList && n.classList.contains('quota-win')).length, 2);
assert.equal(findAll(oauthCard, (n) => n.classList && n.classList.contains('quota-win')).length, 0);
const oauthCompact = findAll(oauthCard, (n) => n.classList && n.classList.contains('quota-compact'))[0];
assert.ok(oauthCompact, 'quota_from card renders compact button');
assert.ok(oauthCompact.classList.contains('is-warning'), '78% tightest window -> warning tone');
assert.match(oauthCompact.textContent, /^Quota via claude-host · tightest 78% · resets in /);
assert.equal(oauthCompact.getAttribute('aria-expanded'), 'false');

// Clicking compact line toggles full rows (not persisted)
oauthCompact.dispatch('click');
assert.equal(oauthCompact.getAttribute('aria-expanded'), 'true');
assert.equal(findAll(oauthCard, (n) => n.classList && n.classList.contains('quota-win')).length, 2);
oauthCompact.dispatch('click');
assert.equal(oauthCompact.getAttribute('aria-expanded'), 'false');
assert.equal(findAll(oauthCard, (n) => n.classList && n.classList.contains('quota-win')).length, 0);

// Equal quota windows are grouped on Dashboard.
const googleCards = ['agy-alpha', 'agy-beta'].map(quotaById);
assert.equal(findAll(googleCards[0], (n) => n.classList && n.classList.contains('quota-win')).length, 2);
assert.ok(googleCards[0].textContent.includes('Gemini · Weekly'));
assert.ok(googleCards[0].textContent.includes('Gemini · Session (5h)'));

assert.equal(findAll(googleCards[1], (n) => n.classList && n.classList.contains('quota-win')).length, 0);
const betaCompact = findAll(googleCards[1], (n) => n.classList && n.classList.contains('quota-compact'))[0];
assert.ok(betaCompact, 'second identical-quota card renders compact button');
assert.ok(betaCompact.classList.contains('is-negative'), '93.4% tightest window -> negative tone');
assert.match(betaCompact.textContent, /^Same quota as agy-alpha · tightest 93% · resets in /);
betaCompact.dispatch('click');
assert.equal(findAll(googleCards[1], (n) => n.classList && n.classList.contains('quota-win')).length, 2);

// Grouped route and alias rows keep their links and quiet official status under filtering.
const groupedConfig = {{
  ...orderedConfig,
  connections: {{ 'agy-link': {{ provider: 'antigravity' }} }},
  routes: {{
    fallback: {{ driver: 'claude.agent-sdk', model: 'sonnet' }},
    official: {{ driver: 'cli.print', connection: 'agy-link', model: 'gemini' }},
    unknown: {{ driver: 'plugin.custom', model: 'custom' }},
  }},
  route_sanctions: {{
    fallback: {{ sanction: 'acknowledged' }},
    official: {{ sanction: 'official' }},
    unknown: {{ sanction: 'needs_ack' }},
  }},
  models: Object.fromEntries([
    ...Array.from({{ length: 6 }}, (_, i) => [
      `agy-${{i}}`, {{ route: 'official', prompt_file_refs: ['p1', 'p2', 'p3'], mcp_refs: ['m1', 'm2'] }},
    ]),
    ['claude', {{ route: 'fallback' }}],
    ['orphan', {{ route: 'missing' }}],
  ]),
}};
const namedRow = (name) => byClass(mainEl, 'row-title').find((n) => n.textContent === name).parentNode.parentNode;
const tags = (row) => byClass(row, 'place-tag').map((n) => n.textContent);
const assertQuietIdentifiers = () => {{
  for (const title of byClass(mainEl, 'row-title')) assert.ok(!title.classList.contains('mono'));
}};
const ctx = {{ config: groupedConfig, rest: [], refresh: async () => {{}} }};
mainEl.replaceChildren();
await renderRoutes(mainEl, ctx);
assert.deepEqual(panelIds(), ['antigravity', 'claude', 'other']);
assert.equal(byClass(panels()[2], 'family-name')[0].textContent, 'Other');
assert.equal(namedRow('official').getAttribute('href'), '#/routes/official');
assert.deepEqual(tags(namedRow('official')), ['agy-0', 'agy-1', 'agy-2', 'agy-3', '+2']);
assert.equal(byClass(namedRow('official'), 'state-chip').length, 0);
assert.equal(byClass(namedRow('fallback'), 'is-warning')[0].textContent, 'Acknowledged');
assert.equal(byClass(namedRow('unknown'), 'is-warning')[0].textContent, 'Needs acknowledgement');
assert.deepEqual(byClass(namedRow('official'), 'row-sub')[0].children.map((n) => n.textContent), ['cli.print', 'gemini']);
assertQuietIdentifiers();

mainEl.replaceChildren();
await renderAliases(mainEl, ctx);
assert.deepEqual(panelIds(), ['antigravity', 'claude', 'other']);
assert.equal(namedRow('agy-0').getAttribute('href'), '#/aliases/agy-0');
assert.deepEqual(tags(namedRow('agy-0')), ['p1', 'p2', 'p3', 'm1', '+1']);
assert.equal(byClass(namedRow('agy-0'), 'state-chip').length, 0);
assert.equal(byClass(namedRow('claude'), 'is-warning')[0].textContent, 'Acknowledged');
assertQuietIdentifiers();
const search = findAll(mainEl, (n) => n.getAttribute?.('aria-label') === 'Search aliases')[0];
search.value = 'orphan';
search.dispatch('input');
assert.deepEqual(panelIds(), ['other']);
assert.deepEqual(byClass(mainEl, 'row-title').map((n) => n.textContent), ['orphan']);

// 4. Peek still shows full rows for quota_from connection
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: ['connection', 'claude-oauth'], query: new URLSearchParams(''), refresh: async () => {{}} }});
const peekPanel = doc.body.children.find((n) => n.classList && n.classList.contains('peek-root'));
assert.ok(peekPanel);
assert.equal(findAll(peekPanel, (n) => n.classList && n.classList.contains('quota-win')).length, 2);
assert.ok(peekPanel.textContent.includes('via claude-host'));
"""
    script_path = tmp_path / "verify_compact_quota.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_console_providers_show_quota_hiding_header_and_form_in_node(tmp_path):
    """Providers respects show_quota in attention, keeps the peek quota, and submits the setting."""
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeClassList {{
  constructor(node) {{ this.node = node; this.set = new Set(); }}
  sync() {{ this.node.attributes.class = [...this.set].join(' '); }}
  add(...names) {{ for (const n of names) if (n) this.set.add(n); this.sync(); }}
  remove(...names) {{ for (const n of names) this.set.delete(n); this.sync(); }}
  toggle(name, force) {{
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }}
  contains(name) {{ return this.set.has(name); }}
}}

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.classList = new FakeClassList(this);
    this._text = '';
    this._value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.required = false;
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  set className(v) {{
    this.classList.set = new Set(String(v || '').split(/\\s+/).filter(Boolean));
    this.classList.sync();
  }}
  get className() {{ return this.attributes.class || ''; }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (Array.isArray(item)) {{ this.append(...item); continue; }}
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'open') this.open = true;
    if (k === 'hidden') this.hidden = true;
    if (k === 'checked') this.checked = true;
    if (k === 'value') this._value = String(v ?? '');
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  closest(sel) {{
    const tags = sel.split(',').map((s) => s.trim().toUpperCase());
    let cur = this;
    while (cur) {{
      if (tags.includes(cur.tagName)) return cur;
      cur = cur.parentNode;
    }}
    return null;
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children) {{
        if (sel === 'a[href]' && c.tagName === 'A' && c.getAttribute('href') !== null) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
}}

const storageMap = new Map();
const appEl = new FakeNode('div');
const mainEl = new FakeNode('main');
mainEl.setAttribute('id', 'main');
const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: (id) => (id === 'main' ? mainEl : id === 'app' ? appEl : null),
  querySelector: () => null,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
doc.body.append(appEl, mainEl);

globalThis.document = doc;
globalThis.window = {{
  matchMedia: () => ({{ matches: true }}),
  confirm: () => true,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  localStorage: {{
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => {{ storageMap.set(k, String(v)); }},
  }},
}};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers', origin: 'http://localhost', pathname: '/admin/ui', search: '' }};
globalThis.history = {{ replaceState: () => {{}} }};
globalThis.requestAnimationFrame = (cb) => cb();

const inFuture = new Date(Date.now() + 4 * 86400 * 1000 + 3600 * 1000).toISOString();
const sharedAgyQuota = [
  {{ window: 'gemini-weekly', label: 'Gemini · Weekly', used_percent: 31, resets_at: inFuture }},
  {{ window: 'gemini-5h', label: 'Gemini · Session (5h)', used_percent: 62, resets_at: inFuture }},
];

const config = {{
  catalog: {{ drivers: {{}} }},
  connections: {{}},
  routes: {{}},
  route_sanctions: {{}},
}};

const apiResponses = {{
  '/admin/config': config,
  '/admin/session': {{ user: 'admin' }},
  '/admin/machine': {{ enabled: false, drivers: {{}} }},
  '/admin/connections': {{
    connections: [
      // Claude family: c-hidden has 95% quota + show_quota:false, c-shown has 42% + show_quota:true
      {{
        id: 'c-hidden',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        config_dir: '/tmp/claude-hidden',
        show_quota: false,
        routes: [],
        state: {{ auth: 'ok', quota: [{{ label: 'Weekly', used_percent: 95, resets_at: inFuture }}] }},
      }},
      {{
        id: 'c-shown',
        provider: 'claude',
        kind: 'subscription', data_policy: 'no_training',
        config_dir: '/tmp/claude-shown',
        show_quota: true,
        routes: [],
        state: {{ auth: 'ok', quota: [{{ label: 'Weekly', used_percent: 42, resets_at: inFuture }}] }},
      }},
      {{
        id: 'c-hidden-via',
        provider: 'claude',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'env:CLAUDE_KEY',
        quota_from: 'c-shown',
        show_quota: false,
        routes: [],
        state: {{ auth: 'unknown', quota_via: 'c-shown', quota: [{{ label: 'Weekly', used_percent: 42, resets_at: inFuture }}] }},
      }},
      {{
        id: 'c-hidden-empty',
        provider: 'claude',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'env:CLAUDE_KEY_2',
        show_quota: false,
        routes: [],
        state: {{ auth: 'unknown', quota: [] }},
      }},
      // OpenAI family: o-expired has show_quota:false AND auth:expired -> still counts toward attention!
      {{
        id: 'o-expired',
        provider: 'codex',
        kind: 'subscription', data_policy: 'no_training',
        show_quota: false,
        routes: [],
        state: {{ auth: 'expired', quota: [{{ label: '5h', used_percent: 96, resets_at: inFuture }}] }},
      }},
      {{
        id: 'o-ok',
        provider: 'codex',
        kind: 'subscription', data_policy: 'no_training',
        show_quota: true,
        routes: [],
        state: {{ auth: 'ok', quota: [{{ label: '5h', used_percent: 25, resets_at: inFuture }}] }},
      }},
      // Antigravity: identical quota where first card has show_quota:false -> second card shows full rows, third shows Same quota as second
      {{
        id: 'agy-1-hidden',
        provider: 'antigravity',
        kind: 'subscription', data_policy: 'no_training',
        show_quota: false,
        routes: [],
        state: {{ auth: 'ok', quota: JSON.parse(JSON.stringify(sharedAgyQuota)) }},
      }},
      {{
        id: 'agy-2-shown',
        provider: 'antigravity',
        kind: 'subscription', data_policy: 'no_training',
        show_quota: true,
        routes: [],
        state: {{ auth: 'ok', quota: JSON.parse(JSON.stringify(sharedAgyQuota)) }},
      }},
      {{
        id: 'agy-3-shown',
        provider: 'antigravity',
        kind: 'subscription', data_policy: 'no_training',
        show_quota: true,
        routes: [],
        state: {{ auth: 'ok', quota: JSON.parse(JSON.stringify(sharedAgyQuota)) }},
      }},
      // Cursor family: only connection has show_quota:false and auth:ok -> header has no tightest% summary
      {{
        id: 'cur-hidden',
        provider: 'cursor',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'env:CURSOR_KEY',
        show_quota: false,
        routes: [],
        state: {{ auth: 'unknown', quota: [{{ label: '5h', used_percent: 88, resets_at: inFuture }}] }},
      }},
    ],
  }},
  '/admin/secrets': {{ secrets: [] }},
}};

const requests = [];
globalThis.fetch = async (url, init = {{}}) => {{
  const key = new URL(String(url), 'http://localhost').pathname;
  const method = (init && init.method) || 'GET';
  if (method !== 'GET') {{
    requests.push({{ method, path: key, body: init.body ? JSON.parse(init.body) : null }});
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => 'application/json' }},
      text: async () => JSON.stringify({{ status: 'ok', connection: {{ id: 'saved' }} }}),
    }};
  }}
  const payload = apiResponses[key] || {{}};
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(payload),
  }};
}};

const {{ connectionAttention, connectionBody, renderProviders }} =
  await import({repr((console_dir / "js/pages/providers.js").as_uri())});
const {{ renderDashboard }} = await import({repr((console_dir / "js/pages/dashboard.js").as_uri())});
const {{ routeBody }} = await import({repr((console_dir / "js/pages/routes.js").as_uri())});
const cursorConn = connectionBody({{
  provider: 'cursor', kind: 'subscription', configDir: '/tmp/cursor-home', dataPolicy: 'no_training',
}});
assert.deepEqual(cursorConn, {{
  provider: 'cursor', kind: 'subscription', profile: 'cursor', config_dir: '/tmp/cursor-home', data_policy: 'no_training',
}});
assert.deepEqual(routeBody({{}}, {{
  driver: 'cli.print', profile: cursorConn.profile, model: 'auto', connection: 'cursor-cli',
}}), {{
  driver: 'cli.print', profile: 'cursor', model: 'auto', connection: 'cursor-cli',
}});

const findAll = (root, pred, acc = []) => {{
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}};
const cardById = (root, id) => findAll(root, (n) => n.classList && n.classList.contains('connection-row'))
  .find((c) => findAll(c, (n) => n.classList && n.classList.contains('connection-name'))[0].textContent === id);

// 1. Attention ignores hot quota when show_quota:false, but still flags auth problems & last_error
const hiddenHot = {{ provider: 'claude', kind: 'subscription', data_policy: 'no_training', config_dir: '/d', show_quota: false, state: {{ auth: 'ok', quota: [{{ used_percent: 98 }}] }} }};
assert.equal(connectionAttention(hiddenHot).needsAttention, false);

const hiddenExpired = {{ provider: 'codex', kind: 'subscription', data_policy: 'no_training', show_quota: false, state: {{ auth: 'expired', quota: [{{ used_percent: 98 }}] }} }};
const expAtt = connectionAttention(hiddenExpired);
assert.equal(expAtt.needsAttention, true);
assert.ok(!expAtt.reasons.includes('quota >= 90%'));

const hiddenError = {{ provider: 'cursor', kind: 'api_key', data_policy: 'no_training', secret: 'env:K', show_quota: false, state: {{ auth: 'unknown', last_error: 'bad' }} }};
assert.equal(connectionAttention(hiddenError).needsAttention, true);

// 2. Render providers page
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams(''), refresh: async () => {{}} }});

assert.equal(findAll(mainEl, (n) => n.classList?.contains('quota-win')).length, 0);
assert.equal(findAll(mainEl, (n) => n.classList?.contains('quota-compact')).length, 0);
assert.ok(!mainEl.textContent.includes('tightest'));
assert.ok(cardById(mainEl, 'c-shown'));
assert.ok(cardById(mainEl, 'c-hidden'));
assert.ok(cardById(mainEl, 'cur-hidden'));

mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams('attention=1'), refresh: async () => {{}} }});
assert.deepEqual(findAll(mainEl, (n) => n.classList?.contains('connection-name')).map((n) => n.textContent), ['o-expired']);

mainEl.replaceChildren();
await renderDashboard(mainEl, {{ config, counts: {{}} }});
const dashboardRows = findAll(mainEl, (n) => n.classList?.contains('quota-row'));
const dashboardRow = (id) => dashboardRows.find((row) =>
  findAll(row, (n) => n.classList?.contains('row-title'))[0].textContent === id);
assert.equal(dashboardRow('c-hidden'), undefined);
assert.equal(dashboardRow('agy-1-hidden'), undefined);
assert.equal(findAll(dashboardRow('c-shown'), (n) => n.classList?.contains('quota-win')).length, 1);
assert.equal(findAll(dashboardRow('agy-2-shown'), (n) => n.classList?.contains('quota-win')).length, 2);
assert.match(dashboardRow('agy-3-shown').textContent, /Same quota as agy-2-shown · tightest 62%/);

// Peek still shows quota when the Dashboard checkbox is unchecked.
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: ['connection', 'c-hidden'], query: new URLSearchParams(''), refresh: async () => {{}} }});
let peekPanel = doc.body.children.find((n) => n.classList && n.classList.contains('peek-root'));
assert.ok(peekPanel);
assert.equal(findAll(peekPanel, (n) => n.classList && n.classList.contains('quota-win')).length, 1);
const editCheck = findAll(peekPanel, (n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Show quota on Dashboard')[0];
assert.ok(editCheck, 'Show quota on Dashboard checkbox present on edit form');
assert.equal(editCheck.checked, false);
assert.ok(peekPanel.textContent.includes('Show quota on Dashboard'));
assert.ok(peekPanel.textContent.includes('The panel always shows quota.'));

editCheck.checked = true;
const editForm = findAll(peekPanel, (n) => n.tagName === 'FORM')[0];
editForm.dispatch('submit');
await new Promise((r) => setTimeout(r, 0));
assert.equal(requests.length, 1);
assert.equal(requests[0].method, 'PUT');
assert.equal(requests[0].path, '/admin/connections/c-hidden');
assert.equal(requests[0].body.show_quota, true);
assert.equal(requests[0].body.data_policy, 'no_training');

// 4. New connection form: checkbox defaults to checked, unchecking submits show_quota=false
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: ['connection', 'new'], query: new URLSearchParams(''), refresh: async () => {{}} }});
peekPanel = doc.body.children.find((n) => n.classList && n.classList.contains('peek-root'));
const newCheck = findAll(peekPanel, (n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Show quota on Dashboard')[0];
assert.ok(newCheck, 'Show quota on Dashboard checkbox present on new form');
assert.equal(newCheck.checked, true);
assert.ok(peekPanel.textContent.includes('The panel always shows quota.'));

const idInput = findAll(peekPanel, (n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Connection ID')[0];
const configDirInput = findAll(peekPanel, (n) => n.tagName === 'INPUT' && n.getAttribute('aria-label') === 'Config dir')[0];
idInput.value = 'claude-new-quiet';
configDirInput.value = '/tmp/claude-new-quiet';
newCheck.checked = false;
const [policySelect] = findAll(peekPanel, (n) => n.getAttribute?.('name') === 'data_policy');
assert.equal(policySelect.value, '');
assert.equal(policySelect.getAttribute('required'), '');
assert.equal(policySelect.children[0].textContent, 'Choose…');
const [telemetryHint] = findAll(peekPanel, (n) => n.classList?.contains('hint') &&
  n.textContent === 'Antigravity also has its own telemetry setting (enable telemetry) in agy; check it matches this choice.');
assert.ok(telemetryHint);
assert.equal(telemetryHint.hidden, true);
const [providerSelect] = findAll(peekPanel, (n) => n.tagName === 'SELECT' && n.getAttribute('aria-label') === 'Provider');
providerSelect.value = 'antigravity';
providerSelect.dispatch('change');
assert.equal(telemetryHint.hidden, false);
providerSelect.value = 'claude';
providerSelect.dispatch('change');
policySelect.value = 'no_training';
const newForm = findAll(peekPanel, (n) => n.tagName === 'FORM')[0];
newForm.dispatch('submit');
await new Promise((r) => setTimeout(r, 0));
assert.equal(requests.length, 2);
assert.equal(requests[1].method, 'POST');
assert.equal(requests[1].path, '/admin/connections/claude-new-quiet');
assert.equal(requests[1].body.show_quota, false);
assert.equal(requests[1].body.data_policy, 'no_training');
"""
    script_path = tmp_path / "verify_show_quota.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_dashboard_quota_respects_show_quota(tmp_path):
    """Providers で残量を隠した connection は Dashboard の残量にも出さない。"""
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console/js/pages/dashboard.js"
    script = tmp_path / "dash.mjs"
    script.write_text(
        "globalThis.document = { getElementById: () => null };\n"
        f"const {{ quotaConnections }} = await import({json.dumps(module.as_uri())});\n"
        "const conns = [\n"
        "  { id: 'a', kind: 'subscription', data_policy: 'no_training' },\n"
        "  { id: 'b', kind: 'subscription', data_policy: 'no_training', show_quota: false },\n"
        "  { id: 'c', kind: 'api_key', data_policy: 'no_training' },\n"
        "];\n"
        "const r = quotaConnections(conns);\n"
        "console.log(JSON.stringify({ shown: r.shown.map((c) => c.id), hidden: r.hidden }));\n",
        encoding="utf-8",
    )
    out = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == {"shown": ["a"], "hidden": 1}


def test_console_providers_api_key_and_endpoint_states_and_check_now_in_node(tmp_path):
    """API key and endpoint rows show status, and peeks show diagnostics and Check now."""
    import shutil
    import subprocess
    from pathlib import Path

    version = _version()
    providers_asset = client.get(f"/admin/ui/console/{version}/js/pages/providers.js", auth=AUTH)
    assert providers_asset.status_code == 200
    for expected_str in ("key ok", "key rejected", "unreachable", "not checked", "Check now"):
        assert expected_str in providers_asset.text

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeClassList {{
  constructor(node) {{ this.node = node; this.set = new Set(); }}
  sync() {{ this.node.attributes.class = [...this.set].join(' '); }}
  add(...names) {{ for (const n of names) if (n) this.set.add(n); this.sync(); }}
  remove(...names) {{ for (const n of names) this.set.delete(n); this.sync(); }}
  toggle(name, force) {{
    const on = force === undefined ? !this.set.has(name) : Boolean(force);
    if (on) this.set.add(name); else this.set.delete(name);
    this.sync();
    return on;
  }}
  contains(name) {{ return this.set.has(name); }}
}}

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.classList = new FakeClassList(this);
    this._text = '';
    this._value = '';
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.open = false;
    this.required = false;
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  set className(v) {{
    this.classList.set = new Set(String(v || '').split(/\\s+/).filter(Boolean));
    this.classList.sync();
  }}
  get className() {{ return this.attributes.class || ''; }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (Array.isArray(item)) {{ this.append(...item); continue; }}
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        if (item.parentNode) item.parentNode.removeChild(item);
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    const idx = this.children.indexOf(child);
    if (idx >= 0) this.children.splice(idx, 1);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'open') this.open = true;
    if (k === 'hidden') this.hidden = true;
    if (k === 'checked') this.checked = true;
    if (k === 'value') this._value = String(v ?? '');
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  closest(sel) {{
    const tags = sel.split(',').map((s) => s.trim().toUpperCase());
    let cur = this;
    while (cur) {{
      if (tags.includes(cur.tagName)) return cur;
      cur = cur.parentNode;
    }}
    return null;
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll(sel) {{
    const out = [];
    const walk = (n) => {{
      for (const c of n.children) {{
        if (sel === 'a[href]' && c.tagName === 'A' && c.getAttribute('href') !== null) out.push(c);
        walk(c);
      }}
    }};
    walk(this);
    return out;
  }}
}}

const storageMap = new Map();
const appEl = new FakeNode('div');
const mainEl = new FakeNode('main');
mainEl.setAttribute('id', 'main');
const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (_ns, tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: (id) => (id === 'main' ? mainEl : id === 'app' ? appEl : null),
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
doc.body.append(appEl, mainEl);

globalThis.document = doc;
globalThis.window = {{
  matchMedia: () => ({{ matches: true }}),
  confirm: () => true,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
  localStorage: {{
    getItem: (k) => (storageMap.has(k) ? storageMap.get(k) : null),
    setItem: (k, v) => {{ storageMap.set(k, String(v)); }},
  }},
}};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/providers', origin: 'http://localhost', pathname: '/admin/ui', search: '' }};
globalThis.history = {{ replaceState: () => {{}} }};
globalThis.requestAnimationFrame = (cb) => cb();

const checkedNow = '2026-11-15T12:00:00Z';
const config = {{ catalog: {{ drivers: {{}} }}, connections: {{}}, routes: {{}}, route_sanctions: {{}} }};
const apiResponses = {{
  '/admin/config': config,
  '/admin/session': {{ user: 'admin' }},
  '/admin/machine': {{ enabled: false, drivers: {{}} }},
  '/admin/secrets': {{ secrets: [] }},
  '/admin/connections': {{
    connections: [
      {{
        id: 'claude-key-ok',
        provider: 'claude',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'claude-sec',
        routes: [],
        state: {{ auth: 'ok', plan: '12 models', checked_at: checkedNow, last_error: null, quota: [] }},
      }},
      {{
        id: 'openai-key-bad',
        provider: 'openai',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'openai-sec',
        routes: [],
        state: {{ auth: 'logged_out', plan: null, checked_at: checkedNow, last_error: 'API key rejected (HTTP 401)', quota: [] }},
      }},
      {{
        id: 'gw-unreachable',
        provider: 'codex',
        kind: 'endpoint', data_policy: 'no_training',
        endpoint: 'https://gw.example/v1',
        secret: 'gw-sec',
        routes: [],
        state: {{ auth: 'unknown', plan: null, checked_at: checkedNow, last_error: 'endpoint unreachable', quota: [] }},
      }},
      {{
        id: 'cursor-unchecked',
        provider: 'cursor',
        kind: 'api_key', data_policy: 'no_training',
        secret: 'cursor-sec',
        routes: [],
        state: {{ auth: 'unknown', plan: null, checked_at: null, last_error: null, quota: [] }},
      }},
    ],
  }},
}};

const probePosts = [];
globalThis.fetch = async (url, init = {{}}) => {{
  const path = new URL(String(url), 'http://localhost').pathname;
  const method = (init && init.method) || 'GET';
  if (method === 'POST') {{
    probePosts.push(path);
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => 'application/json' }},
      text: async () => JSON.stringify({{ status: 'ok', state: {{ auth: 'ok' }} }}),
    }};
  }}
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(apiResponses[path] || {{}}),
  }};
}};

const {{ connectionAuthBadge, renderProviders }} = await import({repr((console_dir / "js/pages/providers.js").as_uri())});

const findAll = (root, pred, acc = []) => {{
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}};
const cardById = (root, id) => findAll(root, (n) => n.classList && n.classList.contains('connection-row'))
  .find((c) => findAll(c, (n) => n.classList && n.classList.contains('connection-name'))[0].textContent === id);

assert.equal(connectionAuthBadge({{ kind: 'api_key', data_policy: 'no_training', state: {{ auth: 'ok' }} }}).label, 'key ok');
assert.equal(connectionAuthBadge({{ kind: 'endpoint', data_policy: 'no_training', state: {{ auth: 'logged_out' }} }}).label, 'key rejected');
assert.equal(connectionAuthBadge({{ kind: 'endpoint', data_policy: 'no_training', state: {{ auth: 'unknown', checked_at: checkedNow }} }}).label, 'unreachable');
assert.equal(connectionAuthBadge({{ kind: 'api_key', data_policy: 'no_training', state: {{ auth: 'unknown', checked_at: null, last_error: null }} }}).label, 'not checked');

let refreshCount = 0;
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: [], query: new URLSearchParams(''), refresh: async () => {{ refreshCount += 1; }} }});

const okCard = cardById(mainEl, 'claude-key-ok');
assert.equal(findAll(okCard, (n) => n.classList && n.classList.contains('state-chip'))[0].textContent, 'key ok');
assert.match(okCard.textContent, /^api key/);
assert.ok(!okCard.textContent.includes('Quota not available'));

const badCard = cardById(mainEl, 'openai-key-bad');
assert.equal(findAll(badCard, (n) => n.classList && n.classList.contains('state-chip'))[0].textContent, 'key rejected');
assert.ok(!badCard.textContent.includes('API key rejected (HTTP 401)'));
assert.ok(!badCard.textContent.includes('Not logged in'));

const unreachCard = cardById(mainEl, 'gw-unreachable');
assert.equal(findAll(unreachCard, (n) => n.classList && n.classList.contains('state-chip'))[0].textContent, 'unreachable');
assert.match(unreachCard.textContent, /^endpoint/);

const uncheckedCard = cardById(mainEl, 'cursor-unchecked');
assert.equal(findAll(uncheckedCard, (n) => n.classList && n.classList.contains('state-chip'))[0].textContent, 'not checked');

// API key probe and plan are available in the connection peek.
const openConnection = async (id) => {{
  mainEl.replaceChildren();
  await renderProviders(mainEl, {{ config, rest: ['connection', id], query: new URLSearchParams(''), refresh: async () => {{ refreshCount += 1; }} }});
  return doc.body.children.find((n) => n.classList?.contains('peek-root'));
}};
const okPeek = await openConnection('claude-key-ok');
assert.ok(okPeek.textContent.includes('12 models'));
assert.ok(okPeek.textContent.includes('Data policy'));
const rejectedPeek = await openConnection('openai-key-bad');
assert.ok(rejectedPeek.textContent.includes('API key rejected (HTTP 401)'));
const uncheckedPeek = await openConnection('cursor-unchecked');
const keyCheck = findAll(uncheckedPeek, (n) => n.tagName === 'BUTTON' && n.textContent === 'Check now')[0];
assert.ok(keyCheck);
keyCheck.dispatch('click');
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(probePosts, ['/admin/connections/cursor-unchecked/probe']);
assert.equal(refreshCount, 1);

// Peek view for endpoint shows chip, last_error, and Check now button
mainEl.replaceChildren();
await renderProviders(mainEl, {{ config, rest: ['connection', 'gw-unreachable'], query: new URLSearchParams(''), refresh: async () => {{ refreshCount += 1; }} }});
const peekRoot = doc.body.children.find((n) => n.classList && n.classList.contains('peek-root'));
assert.ok(peekRoot);
assert.ok(peekRoot.textContent.includes('unreachable'));
assert.ok(peekRoot.textContent.includes('endpoint unreachable'));
const peekCheckBtn = findAll(peekRoot, (n) => n.tagName === 'BUTTON' && n.textContent === 'Check now')[0];
assert.ok(peekCheckBtn, 'Check now button present in endpoint peek');
peekCheckBtn.dispatch('click');
await new Promise((r) => setTimeout(r, 0));
assert.deepEqual(probePosts, ['/admin/connections/cursor-unchecked/probe', '/admin/connections/gw-unreachable/probe']);
assert.equal(refreshCount, 2);
"""
    script_path = tmp_path / "verify_api_key_probe_ui.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr


def test_console_endpoint_client_strings():
    """Endpoint/machine/aliases console pages use user-facing Client wording instead of Caller."""
    from pathlib import Path

    console_js = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console/js/pages"
    endpoint_js = (console_js / "endpoint.js").read_text(encoding="utf-8")
    machine_js = (console_js / "machine.js").read_text(encoding="utf-8")
    aliases_js = (console_js / "aliases.js").read_text(encoding="utf-8")

    for src_name, text in (("endpoint.js", endpoint_js), ("machine.js", machine_js), ("aliases.js", aliases_js)):
        assert "caller" not in text.lower(), f"unexpected 'caller' wording in {src_name}"

    for expected in (
        "Clients",
        "New client",
        "Client",
        "All models",
        "every configured alias, including ones added later",
        "Host",
        "Jail",
        "Max permission",
        "Run commands",
        "Workspace directories",
        "empty = chat only",
        "Bound API keys",
    ):
        assert expected in endpoint_js


def test_console_endpoint_clients_peek_dom_in_node(tmp_path):
    """endpoint.js: Clients card, API key Client select, and side peek fields/labels/PUT."""
    from pathlib import Path
    import shutil
    import subprocess

    if shutil.which("node") is None:
        pytest.skip("node not installed")

    console_dir = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    script = f"""
import assert from 'node:assert/strict';

class FakeNode {{
  constructor(tag) {{
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parentNode = null;
    this.listeners = {{}};
    this.attributes = {{}};
    this.style = {{}};
    this.className = '';
    this.hidden = false;
    this.open = false;
    this.disabled = false;
    this.checked = false;
    this.type = '';
    this.required = false;
    this._text = '';
    this._value = '';
    this.classList = {{
      contains: (v) => this.className.split(/\\s+/).includes(v),
      add: (...vs) => {{ this.className = [...new Set(this.className.split(/\\s+/).filter(Boolean).concat(vs))].join(' '); }},
      remove: (...vs) => {{ this.className = this.className.split(/\\s+/).filter((v) => !vs.includes(v)).join(' '); }},
      toggle: (v, force) => {{
        const has = this.classList.contains(v);
        const on = force === undefined ? !has : Boolean(force);
        if (on) this.classList.add(v);
        else this.classList.remove(v);
        return on;
      }},
    }};
  }}
  get value() {{ return this._value; }}
  set value(v) {{ this._value = String(v ?? ''); }}
  get isConnected() {{
    let cur = this;
    while (cur) {{
      if (cur === doc.body) return true;
      cur = cur.parentNode;
    }}
    return false;
  }}
  append(...items) {{
    for (const item of items) {{
      if (item === null || item === undefined || item === false) continue;
      if (typeof item === 'string' || typeof item === 'number') {{
        const tn = new FakeNode('#text');
        tn._text = String(item);
        tn.parentNode = this;
        this.children.push(tn);
      }} else {{
        item.parentNode = this;
        this.children.push(item);
      }}
    }}
  }}
  removeChild(child) {{
    this.children = this.children.filter((c) => c !== child);
    child.parentNode = null;
  }}
  remove() {{
    if (this.parentNode) this.parentNode.removeChild(this);
  }}
  replaceChildren(...items) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this.append(...items);
  }}
  set textContent(v) {{
    for (const c of this.children) c.parentNode = null;
    this.children = [];
    this._text = String(v ?? '');
  }}
  get textContent() {{
    return this._text + this.children.map((c) => c.textContent).join('');
  }}
  setAttribute(k, v) {{
    this.attributes[k] = String(v);
    if (k === 'value') this._value = String(v);
    if (k === 'disabled') this.disabled = true;
    if (k === 'checked') this.checked = true;
  }}
  removeAttribute(k) {{
    delete this.attributes[k];
    if (k === 'disabled') this.disabled = false;
    if (k === 'checked') this.checked = false;
  }}
  getAttribute(k) {{ return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }}
  addEventListener(name, fn) {{ (this.listeners[name] ||= []).push(fn); }}
  removeEventListener(name, fn) {{
    if (!this.listeners[name]) return;
    this.listeners[name] = this.listeners[name].filter((f) => f !== fn);
  }}
  dispatch(name, ev = {{}}) {{
    const event = {{ target: this, preventDefault() {{}}, stopPropagation() {{}}, ...ev }};
    for (const fn of [...(this.listeners[name] || [])]) fn(event);
  }}
  focus() {{ doc.activeElement = this; }}
  querySelectorAll() {{ return []; }}
}}

const doc = {{
  body: new FakeNode('body'),
  documentElement: new FakeNode('html'),
  activeElement: null,
  createElement: (tag) => new FakeNode(tag),
  createTextNode: (t) => {{ const n = new FakeNode('#text'); n._text = String(t); return n; }},
  getElementById: () => null,
  addEventListener: () => {{}},
  removeEventListener: () => {{}},
}};
const mainEl = new FakeNode('main');
doc.body.append(mainEl);

globalThis.document = doc;
globalThis.window = {{ matchMedia: () => ({{ matches: true }}), confirm: () => true }};
globalThis.Node = FakeNode;
globalThis.location = {{ hash: '#/endpoint', origin: 'http://localhost' }};
globalThis.requestAnimationFrame = (cb) => cb();

const putRequests = [];
const apiResponses = {{
  '/admin/api-keys': {{
    keys: [{{ name: 'k-prod', client: 'taskhub', prefix: 'kys_', last4: '9999', created_at: null, last_used_at: null, revoked_at: null }}],
  }},
  '/admin/clients': {{
    clients: [{{
      name: 'taskhub',
      models: ['sonnet'],
      places: ['isolated'],
      max_surface: 'exec',
      workspace_roots: ['/srv/work'],
      active_keys: ['k-prod'],
      keys: [{{ name: 'k-prod', prefix: 'kys_', last4: '9999', revoked_at: null }}],
    }}],
  }},
}};

globalThis.fetch = async (url, init = {{}}) => {{
  const path = new URL(String(url), 'http://localhost').pathname;
  const method = (init && init.method) || 'GET';
  if (method === 'PUT') {{
    putRequests.push({{ path, body: JSON.parse(init.body || '{{}}') }});
    return {{
      ok: true,
      status: 200,
      headers: {{ get: () => 'application/json' }},
      text: async () => JSON.stringify({{ status: 'ok', created: true }}),
    }};
  }}
  return {{
    ok: true,
    status: 200,
    headers: {{ get: () => 'application/json' }},
    text: async () => JSON.stringify(apiResponses[path] || {{}}),
  }};
}};

const {{ renderEndpoint }} = await import({repr((console_dir / "js/pages/endpoint.js").as_uri())});

const findAll = (root, pred, acc = []) => {{
  if (pred(root)) acc.push(root);
  for (const c of root.children || []) findAll(c, pred, acc);
  return acc;
}};
const findByAttr = (root, attr, val) => findAll(root, (n) => n.getAttribute && n.getAttribute(attr) === val)[0] || null;

const config = {{
  models: {{
    sonnet: {{}},
    'claude-haiku': {{}},
  }},
}};

// 1. Overview: Client select on API keys form + Clients card
await renderEndpoint(mainEl, {{ config, rest: [], refresh: async () => {{}} }});
const clientSelect = findByAttr(mainEl, 'aria-label', 'Client');
assert.ok(clientSelect, 'API key Client select exists');
assert.deepEqual(clientSelect.children.map((o) => o.value), ['taskhub']);
assert.ok(!mainEl.textContent.toLowerCase().includes('caller'));
const clientsCard = findByAttr(mainEl, 'aria-label', 'Clients');
assert.ok(clientsCard, 'Clients card exists');
assert.ok(clientsCard.textContent.includes('New client'));
assert.ok(clientsCard.textContent.includes('taskhub'));
assert.ok(clientsCard.textContent.includes('Jail'));
assert.ok(clientsCard.textContent.includes('Run commands'));

// 2. Edit peek for existing client 'taskhub'
mainEl.replaceChildren();
await renderEndpoint(mainEl, {{ config, rest: ['clients', 'taskhub'], refresh: async () => {{}} }});
const editPeek = doc.body.children.find((n) => n.classList && n.classList.contains('peek-root'));
assert.ok(editPeek, 'edit peek opened');

const nameInput = findByAttr(editPeek, 'aria-label', 'Client name');
assert.equal(nameInput.value, 'taskhub');
assert.equal(nameInput.disabled, true);

const allModelsBox = findByAttr(editPeek, 'aria-label', 'All models');
const sonnetBox = findByAttr(editPeek, 'aria-label', 'Model sonnet');
const haikuBox = findByAttr(editPeek, 'aria-label', 'Model claude-haiku');
assert.equal(allModelsBox.checked, false);
assert.equal(sonnetBox.checked, true);
assert.equal(haikuBox.checked, false);
assert.equal(sonnetBox.disabled, false);

allModelsBox.checked = true;
allModelsBox.dispatch('change');
assert.equal(sonnetBox.disabled, true);
assert.equal(haikuBox.disabled, true);

const hostBox = findByAttr(editPeek, 'aria-label', 'Host');
const jailBox = findByAttr(editPeek, 'aria-label', 'Jail');
assert.equal(hostBox.checked, false);
assert.equal(jailBox.checked, true);

const surfaceSelect = findByAttr(editPeek, 'aria-label', 'Max permission');
assert.equal(surfaceSelect.value, 'exec');
assert.deepEqual(
  surfaceSelect.children.map((o) => o.textContent),
  ['Unrestricted (alias default)', 'None', 'Read', 'Run commands', 'Write'],
);

const wsInput = findByAttr(editPeek, 'aria-label', 'Workspace directory');
assert.equal(wsInput.value, '/srv/work');
assert.ok(editPeek.textContent.includes('empty = chat only'));


assert.ok(editPeek.textContent.includes('Bound API keys'));
assert.ok(editPeek.textContent.includes('k-prod'));

const deleteBtn = findAll(editPeek, (n) => n.tagName === 'BUTTON' && n.textContent === 'Delete client')[0];
assert.ok(deleteBtn, 'Delete client button present');
assert.equal(deleteBtn.disabled, true);
assert.ok(editPeek.textContent.includes('Delete disabled: bound to active API key (k-prod)'));

// 3. New client peek creates via PUT /admin/clients/<name>
mainEl.replaceChildren();
await renderEndpoint(mainEl, {{ config, rest: ['clients', '__new__'], refresh: async () => {{}} }});
const peeks = doc.body.children.filter((n) => n.classList && n.classList.contains('peek-root'));
const newPeek = peeks[peeks.length - 1];
assert.ok(newPeek, 'new client peek opened');

const newName = findByAttr(newPeek, 'aria-label', 'Client name');
assert.equal(newName.disabled, false);
newName.value = 'new-chat';
const newAllModels = findByAttr(newPeek, 'aria-label', 'All models');
newAllModels.checked = true;
newAllModels.dispatch('change');
const newHost = findByAttr(newPeek, 'aria-label', 'Host');
const newJail = findByAttr(newPeek, 'aria-label', 'Jail');
newHost.checked = true;
newJail.checked = false;
const newSurface = findByAttr(newPeek, 'aria-label', 'Max permission');
newSurface.value = 'read';

const form = findAll(newPeek, (n) => n.tagName === 'FORM')[0];
form.dispatch('submit');
await new Promise((r) => setTimeout(r, 0));

assert.equal(putRequests.length, 1);
assert.equal(putRequests[0].path, '/admin/clients/new-chat');
assert.deepEqual(putRequests[0].body, {{
  models: ['*'],
  places: ['host'],
  max_surface: 'read',
  workspace_roots: [],
}});
"""
    script_path = tmp_path / "verify_endpoint_clients_ui.mjs"
    script_path.write_text(script, encoding="utf-8")
    res = subprocess.run(["node", str(script_path)], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, res.stdout + res.stderr



def test_console_shows_training_risk_and_saves_owner_acknowledgement():
    from tests.test_data_policy import config

    save_config(config())
    version = _version()
    assets = {
        name: client.get(f"/admin/ui/console/{version}/js/{name}.js", auth=AUTH).text
        for name in ["format", "pages/aliases", "pages/providers", "pages/routes"]
    }
    assert "Trains on input" in assets["format"]
    assert "Unknown — treated as training" in assets["format"]
    assert "Not used for training" in assets["pages/providers"]
    assert "Used for training" in assets["pages/providers"]
    assert "data_policy" in assets["pages/routes"]
    assert 'type: "checkbox"' in assets["pages/aliases"]
    assert "Training risk acknowledgement" in assets["pages/aliases"]

    path = "/admin/models/demo"
    body = {"route": "gateway", "tools": ["WebSearch"]}
    rejected = client.put(path, json=body, auth=AUTH)
    assert rejected.status_code == 422
    assert "Acknowledge tools" in rejected.json()["error"]["message"]
    body["training_acknowledgement"] = {"capabilities": ["tools"], "note": " "}
    assert client.put(path, json=body, auth=AUTH).status_code == 422
    body["training_acknowledgement"]["note"] = "Public repository only"
    assert client.put(path, json=body, auth=AUTH).status_code == 200

    config_data = client.get("/admin/config", auth=AUTH).json()
    assert config_data["connections"]["gateway"]["effective_data_policy"] == "unknown"
    assert config_data["route_sanctions"]["gateway"]["data_policy"] == "unknown"
    assert config_data["alias_data_policies"]["demo"] == {"data_policy": "unknown", "training_ack": True}
    (ack,) = reload_config().training_acks
    assert ack.acknowledged_by == AUTH[0]
    assert ack.note == "Public repository only"
    assert ack.capabilities == ["tools"]


def test_peek_settings_sections_start_expanded():
    """右の peek の Settings 欄は、新規でも既存でも最初から開いている (所有者の希望 2026-11-19)。"""
    console_js = Path(__file__).resolve().parents[1] / "src" / "kotoyose" / "static" / "console" / "js"
    peek = (console_js / "peek.js").read_text(encoding="utf-8")
    assert 'createSettingsDisclosure({ title = "Settings", open = true,' in peek
    for page in ("routes.js", "mcps.js", "providers.js"):
        text = (console_js / "pages" / page).read_text(encoding="utf-8")
        assert "open: isNew" not in text, page


def test_suggested_connection_id_follows_provider_method_rule(tmp_path):
    """新しい接続の ID の初期値は「provider-認証の方法」(cli / token / apikey / endpoint)。"""
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")
    module = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console/js/pages/providers.js"
    script = tmp_path / "suggest.mjs"
    script.write_text(
        "globalThis.document = { getElementById: () => null, createElement: () => ({}) };\n"
        f"const {{ suggestConnectionId }} = await import({json.dumps(module.as_uri())});\n"
        "console.log(JSON.stringify([\n"
        "  suggestConnectionId('cursor', 'subscription', 'config_dir', []),\n"
        "  suggestConnectionId('claude', 'subscription', 'secret', []),\n"
        "  suggestConnectionId('claude', 'subscription', 'config_dir', [{ id: 'claude-cli' }]),\n"
        "  suggestConnectionId('openai', 'api_key', 'config_dir', []),\n"
        "  suggestConnectionId('My Provider', 'endpoint', 'config_dir', ['my-provider-endpoint', 'my-provider-endpoint-2']),\n"
        "]));\n",
        encoding="utf-8",
    )
    out = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [
        "cursor-cli", "claude-token", "claude-cli-2", "openai-apikey", "my-provider-endpoint-3",
    ]


def test_managed_cli_admin_routes_require_owner_and_csrf(monkeypatch, tmp_path):
    from kotoyose.auth import ADMIN_SESSION_COOKIE, create_admin_session
    from kotoyose.cli_installer import CliInstallConflict, ToolStatus

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    endpoint = "/admin/tools/grok/install"
    headers = {"Origin": "http://testserver", "X-Kotoyose-Admin": "1"}
    assert client.get("/admin/tools").status_code == 401
    assert client.post(endpoint, headers=headers).status_code == 401
    tools = client.get("/admin/tools", auth=AUTH).json()["tools"]
    assert {tool["profile"]: tool for tool in tools} == {
        profile: {
            "profile": profile, "provider": provider, "version": version, "installed": False,
            "binary_version": None, "path": None,
        }
        for profile, provider, version in (
            ("grok", "grok", "1.0.89"),
            ("agy", "antigravity", "1.5.13"),
            ("cursor", "cursor", "2026.11.19-f005e9d"),
        )
    }
    assert client.post(endpoint, auth=AUTH).status_code == 403
    assert client.post("/admin/tools/unknown/install", auth=AUTH, headers=headers).status_code == 404

    session = create_admin_session(AUTH[0])
    cookie_client = TestClient(app, cookies={ADMIN_SESSION_COOKIE: session.session_id})
    assert cookie_client.post(endpoint).status_code == 403
    assert cookie_client.post(endpoint, headers={**headers, "Origin": "https://wrong.invalid"}).status_code == 403
    installed = ToolStatus("grok", "grok", "1.0.89", True, "/fixture/grok", "grok 1.0.89")
    with patch("kotoyose.routes.admin_settings.install_tool", return_value=installed) as install:
        response = cookie_client.post(endpoint, headers=headers)
        assert response.status_code == 200
        assert response.json()["binary_version"] == "grok 1.0.89"
        assert install.call_args.args[0].id == "grok"
    with patch("kotoyose.routes.admin_settings.install_tool", side_effect=CliInstallConflict("version in use")):
        assert client.post(endpoint, auth=AUTH, headers=headers).status_code == 409
    monkeypatch.setattr("kotoyose.auth.API_KEY", None)
    assert client.post(endpoint, headers=headers).status_code == 403

def test_console_provider_resolution_and_order_in_node(tmp_path):
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is not installed")

    module = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console/js/provider-ui.js"
    script = tmp_path / "verify_provider_ui.mjs"
    script.write_text(
        f"""
import assert from 'node:assert/strict';
import {{ groupProviderItems, orderedProviderIds, providerOf }} from {module.as_uri()!r};

const config = {{
  settings: {{ provider_order: ['other', 'codex', 'claude', 'codex', 'unused'] }},
  connections: {{ org: {{ provider: 'codex' }}, custom: {{ provider: 'unknown-provider' }} }},
  catalog: {{ drivers: {{ 'sdk.default': {{ provider: 'claude' }} }} }},
}};
const routes = [
  {{ connection: 'org', driver: 'sdk.default' }},
  {{ connection: 'missing', driver: 'sdk.default' }},
  {{ driver: 'antigravity.sdk' }},
  {{ connection: 'custom', driver: 'sdk.default' }},
  {{ driver: 'unknown.driver' }},
];
const resolve = (item) => providerOf(item, config);
assert.deepEqual(routes.map(resolve), ['codex', 'claude', 'antigravity', 'other', 'other']);
assert.equal(resolve({{ provider: 'openai' }}), 'openai');
assert.equal(resolve(undefined), 'other');
assert.deepEqual(orderedProviderIds(['claude', 'antigravity'], config), ['other', 'codex', 'claude', 'unused', 'antigravity']);
assert.deepEqual(groupProviderItems(routes, resolve, config), [
  ['codex', [routes[0]]], ['claude', [routes[1]]], ['antigravity', [routes[2]]], ['other', routes.slice(3)],
]);
""",
        encoding="utf-8",
    )
    result = subprocess.run([node, str(script)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("case", ["reload", "picker", "settings"])
def test_console_admin_actions_in_node(tmp_path, case):
    import shutil
    import subprocess

    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    source = Path(__file__).resolve().parents[1] / "src/kotoyose/static/console"
    console = tmp_path / "console"
    shutil.copytree(source, console)
    (console / "package.json").write_text('{"type":"module"}', encoding="utf-8")
    script = Path(__file__).with_name("console_admin_actions.mjs")
    result = subprocess.run([node, str(script), str(console), case], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
