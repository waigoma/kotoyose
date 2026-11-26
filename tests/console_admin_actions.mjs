import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";
import { join } from "node:path";

class Element {
  constructor(tag) {
    this.tag = tag;
    this.attributes = {};
    this.children = [];
    this.listeners = {};
    this.text = "";
    this.value = "";
    this.hidden = false;
    this.disabled = false;
    this.classList = { add() {} };
  }

  get parentNode() { return this.parent; }
  get isConnected() { return this === document.body || Boolean(this.parent?.isConnected); }
  get textContent() { return this.text + this.children.map((child) => child.textContent).join(""); }
  set textContent(text) { this.text = text; this.replaceChildren(); }

  append(...children) {
    for (const child of children) child.parent = this;
    this.children.push(...children);
  }

  replaceChildren(...children) {
    for (const child of this.children) child.parent = null;
    this.children = [];
    this.append(...children);
  }

  remove() {
    if (!this.parent) return;
    this.parent.children = this.parent.children.filter((child) => child !== this);
    this.parent = null;
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
    if (name === "value") this.value = value;
    if (name === "hidden" || name === "disabled") this[name] = true;
  }

  getAttribute(name) { return this.attributes[name] ?? null; }
  focus() { document.activeElement = this; }
  addEventListener(name, callback) { (this.listeners[name] ??= []).push(callback); }
  dispatchEvent(event) { return this.fire(event.type, event); }

  async fire(name, event = {}) {
    await Promise.all((this.listeners[name] || []).map((callback) => callback(event)));
    if (event.bubbles) await this.parent?.fire(name, event);
  }

  querySelectorAll(tag) {
    return this.children.flatMap((child) => [
      ...(child.tag === tag ? [child] : []), ...child.querySelectorAll(tag),
    ]);
  }
}

globalThis.Node = Element;
globalThis.document = {
  body: new Element("body"),
  createElement: (tag) => new Element(tag),
  createElementNS: (_ns, tag) => new Element(tag),
  createTextNode: (text) => Object.assign(new Element("#text"), { text }),
  addEventListener() {},
  removeEventListener() {},
  querySelector: () => null,
};
globalThis.window = {};
const load = (module) => import(pathToFileURL(join(process.argv[2], "js", module)));
const { api } = await load("api.js");
const { el } = await load("dom.js");
const main = el("main");
document.body.append(main);
const button = (label, root = main) => {
  const result = root.querySelectorAll("button").find((item) => item.textContent === label);
  assert.ok(result, `Missing button: ${label}`);
  return result;
};
const flush = () => new Promise((resolve) => setImmediate(resolve));

const cases = {
  async childConnections() {
    const { renderProviders } = await load("pages/providers.js");
    const { renderChildren } = await load("pages/children.js");
    const { renderDashboard } = await load("pages/dashboard.js");
    const { renderRoutes } = await load("pages/routes.js");
    const { renderAliases } = await load("pages/aliases.js");
    const { renderCombos } = await load("pages/combos.js");
    const { destroyPeek } = await load("peek.js");
    const lastSeen = new Date(Date.now() - 7 * 60_000).toISOString();
    const roots = [String.raw`C:\programming\tmp\work`, "/path/<example>"];
    const workspace = { workspace_roots: roots, codex_sandbox_max: "read-only" };
    const oldVersion = "Not reported by this child version";
    const folderWarning = String.raw`Alias alias-a allows D:\Programming\ai\tmp but child disconnected's workspace has ${roots.join(", ")}.`;
    const sandboxWarning = "Alias alias-a requests Codex sandbox workspace-write but child disconnected's limit is read-only.";
    const entries = [
      ["waiting", { child_state: "waiting" }, "Waiting for child"],
      ["disconnected", { child_state: "disconnected", last_heartbeat: lastSeen, workspace_policy: workspace }, "Disconnected"],
      ["revoked", { child_state: "revoked" }, "Revoked"],
      ["disabled", { child_state: "not_enabled" }, "Not enabled on child"],
      ["signed-out", { child_state: "online", auth: "logged_out" }, "Logged out"],
      ["signed-in", { child_state: "online", auth: "ok", child_quota_shared: false }, "ok"],
      ["failed", { child_state: "online", last_error: "child login status timed out" }, "unknown"],
      ["unchecked", { child_state: "online" }, "Login not checked"],
    ];
    const connections = entries.map(([id, state]) => ({
      id, on: `child:${id}`, provider: "claude", kind: "subscription", state: { auth: "unknown", ...state },
    }));
    const config = {
      connections: Object.fromEntries(connections.map((conn) => [conn.id, conn])),
      routes: { "route-a": { driver: "claude.agent-sdk", model: "fixture", connection: "disconnected" } },
      models: { "alias-a": { route: "route-a", on: "child:disconnected" } },
      pools: { "pool-a": { candidates: ["alias-a"] } },
    };
    api.connections = async () => ({ connections });
    api.children = async () => ({ children: [
      { name: "child-a", child_state: "waiting" },
      { name: "disconnected", child_state: "disconnected", last_heartbeat: lastSeen, workspace_policy: workspace,
        alias_warnings: [folderWarning, sandboxWarning].map((message) => ({ alias: "alias-a", message })) },
      { name: "child-c", child_state: "revoked", revoked: true },
      { name: "child-d", child_state: "online", online: true,
        workspace_policy: { workspace_roots: [], codex_sandbox_max: "workspace-write" } },
    ] });
    api.machine = api.tools = api.secrets = api.providerDeclarations = async () => ({});
    api.interactions = async () => ({ items: [] });
    api.poolState = async () => ({ pools: {} });
    globalThis.location = { hash: "#/providers/connection/disconnected" };
    globalThis.requestAnimationFrame = (callback) => callback();
    const context = { config, rest: [], query: new URLSearchParams(), refresh: async () => {}, counts: {} };
    await renderProviders(main, { ...context, rest: ["connection", "disconnected"] });
    for (const [id, , label] of entries) {
      const row = main.querySelectorAll("a").find((a) => a.className === "connection-row" &&
        a.getAttribute("href").includes(`/connection/${id}`));
      const chip = row?.querySelectorAll("span").find((item) => item.className?.includes("state-chip"));
      assert.ok(chip?.textContent.startsWith(label), `${id} should show ${label}`);
    }
    assert.match(main.textContent, /last seen 7m ago/);
    assert.match(main.textContent, /child login status timed out/);
    const detail = document.body.querySelectorAll("aside")[0];
    assert.match(detail.textContent, /Disconnected · last seen 7m ago/);
    for (const text of ["Workspace folders", ...roots, "Codex sandbox limit", "read-only"]) {
      assert.ok(detail.textContent.includes(text));
    }
    destroyPeek();
    main.replaceChildren();
    await renderProviders(main, { ...context, rest: ["connection", "waiting"] });
    assert.ok(document.body.querySelectorAll("aside")[0].textContent.includes(oldVersion));
    destroyPeek();

    for (const render of [renderChildren, renderDashboard, renderRoutes, renderAliases, renderCombos]) {
      main.replaceChildren();
      await render(main, context);
      assert.match(main.textContent, /Disconnected/);
      assert.match(main.textContent, /last seen 7m ago/);
      assert.doesNotMatch(main.textContent, /Quota unknown/);
      if (render === renderChildren) {
        for (const label of ["Waiting for child", "Revoked", "Online"]) assert.ok(main.textContent.includes(label));
        for (const text of ["Workspace folders", ...roots, "Codex sandbox limit", "read-only", oldVersion,
          "No workspace folders configured", folderWarning, sandboxWarning]) assert.ok(main.textContent.includes(text));
      }
      if (render === renderAliases) {
        for (const text of [folderWarning, sandboxWarning]) assert.ok(main.textContent.includes(text));
      }
      if (render === renderDashboard) assert.match(main.textContent, /Quota not shared/);
    }
    main.replaceChildren();
    await renderAliases(main, { ...context, rest: ["alias-a"] });
    const aliasDetail = document.body.querySelectorAll("aside")[0];
    for (const text of [folderWarning, sandboxWarning]) assert.ok(aliasDetail.textContent.includes(text));
  },

  async reload() {
    const { renderMachine } = await load("pages/machine.js");
    api.machine = async () => ({});
    let complete;
    let requests = 0;
    api.reloadConfig = () => {
      requests++;
      return new Promise((resolve) => { complete = resolve; });
    };
    const refreshes = [];
    const refresh = async (result) => {
      refreshes.push(result);
      main.replaceChildren();
      await renderMachine(main, { refresh, ...result });
    };
    await renderMachine(main, { refresh });
    const reload = button("Reload config.yml");
    window.confirm = () => false;
    await reload.fire("click");
    assert.equal(requests, 0);
    window.confirm = () => true;
    const pending = reload.fire("click");
    assert.equal(reload.disabled, true);
    assert.equal(reload.textContent, "Reloading…");
    await reload.fire("click");
    assert.equal(requests, 1);
    complete();
    await pending;
    assert.equal(refreshes.length, 1);
    assert.match(main.textContent, /Configuration reloaded/);

    api.reloadConfig = async () => { throw new Error("invalid YAML"); };
    const retry = button("Reload config.yml");
    await retry.fire("click");
    assert.equal(retry.disabled, false);
    assert.equal(refreshes.length, 1);
    assert.match(main.textContent, /Could not reload config.yml: invalid YAML/);
  },

  async picker() {
    const { pathPicker } = await load("path-picker.js");
    const calls = [];
    api.browse = async (path) => {
      calls.push(path);
      if (path === "/fixture/sub") {
        return { current: path, parent: "/fixture", entries: [
          { name: "prompt.md", path: "/fixture/sub/prompt.md", is_dir: false },
        ] };
      }
      assert.equal(path, "/fixture");
      return { current: path, entries: [{ name: "sub", path: "/fixture/sub", is_dir: true }] };
    };
    const input = el("input", { type: "text" });
    const picker = pathPicker(input, { start: "/fixture" });
    main.append(picker);
    let changes = 0;
    main.addEventListener("input", () => { changes++; });
    const key = (name) => ({ key: name, preventDefault() {}, stopPropagation() {} });
    const browse = button("Browse…");
    await browse.fire("click");
    await flush();
    assert.equal(browse.getAttribute("aria-expanded"), "true");
    assert.equal(document.activeElement, button("subDirectory"));
    await document.activeElement.fire("click");
    const panel = picker.querySelectorAll("section")[0];
    assert.equal(document.activeElement, button("Parent directory"));
    await panel.fire("keydown", key("ArrowDown"));
    assert.equal(document.activeElement, button("prompt.mdFile"));
    await panel.fire("keydown", key("ArrowUp"));
    assert.equal(document.activeElement, button("Parent directory"));
    await document.activeElement.fire("click");
    assert.equal(document.activeElement, button("subDirectory"));
    await document.activeElement.fire("click");
    await panel.fire("keydown", key("ArrowDown"));
    await document.activeElement.fire("click");
    assert.equal(input.value, "/fixture/sub/prompt.md");
    assert.equal(changes, 1);
    assert.equal(document.activeElement, input);
    assert.equal(panel.hidden, true);
    assert.deepEqual(calls, ["/fixture", "/fixture/sub", "/fixture", "/fixture/sub"]);

    await browse.fire("click");
    await flush();
    await panel.fire("keydown", key("Escape"));
    assert.equal(browse.getAttribute("aria-expanded"), "false");
    assert.equal(document.activeElement, browse);

    const directory = el("input", { type: "text", value: "/fixture/sub" });
    const dirPicker = pathPicker(directory, { directories: true });
    main.append(dirPicker);
    await button("Browse…", dirPicker).fire("click");
    await flush();
    assert.doesNotMatch(dirPicker.textContent, /prompt\.md/);
    await button("Choose this directory", dirPicker).fire("click");
    assert.equal(directory.value, "/fixture/sub");
  },

  async settings() {
    const { renderSecrets } = await load("pages/secrets.js");
    const current = "fixture-token-should-never-render";
    const writes = [];
    globalThis.fetch = async (path, request) => {
      if (path === "/admin/settings" && request.method === "PUT") {
        writes.push(JSON.parse(request.body));
        return { ok: true, text: async () => "{}" };
      }
      assert.equal(path, "/admin/settings");
      assert.equal(request.method, "GET");
      return { ok: true, text: async () => JSON.stringify({
        anthropic_auth_token: { set: true, hint: current },
        anthropic_base_url: "https://fixture.invalid",
      }) };
    };
    api.secrets = async () => ({});
    await renderSecrets(main, { config: {}, refresh: async () => {} });
    const inputs = main.querySelectorAll("input");
    const token = inputs.find((input) => input.getAttribute("aria-label") === "Anthropic auth token");
    const baseUrl = inputs.find((input) => input.getAttribute("aria-label") === "Anthropic base URL");
    assert.equal(token.getAttribute("type"), "password");
    assert.equal(token.value, "");
    assert.equal(token.getAttribute("value"), null);
    assert.equal(baseUrl.value, "https://fixture.invalid");
    assert.doesNotMatch(main.textContent, new RegExp(current));
    assert.ok(inputs.every((input) => !Object.values(input.attributes).includes(current)));

    await button("Clear token").fire("click");
    assert.deepEqual(writes, [{ anthropic_auth_token: "" }]);
    baseUrl.value = "https://new.fixture.invalid";
    const form = main.querySelectorAll("form")[0];
    await form.fire("submit", { preventDefault() {} });
    assert.deepEqual(writes[1], { anthropic_base_url: "https://new.fixture.invalid" });
    token.value = "fixture-replacement";
    await form.fire("submit", { preventDefault() {} });
    assert.equal(writes[2].anthropic_auth_token, "fixture-replacement");
    assert.equal(token.value, "");
  },
};

await cases[process.argv[3]]();
