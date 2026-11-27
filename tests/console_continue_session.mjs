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
    this.classList = { add() {}, remove() {} };
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
    if (name === "checked" || name === "hidden" || name === "disabled") this[name] = true;
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

globalThis.location = { hash: "#/executions", pathname: "/admin/console", search: "" };
globalThis.requestAnimationFrame = (callback) => callback();
const { renderExecutions } = await load("pages/executions.js");
const { destroyPeek } = await load("peek.js");
const calls = [];
globalThis.fetch = async (path, init) => {
  calls.push([path, init.method, JSON.parse(init.body)]);
  return { ok: true, text: async () => "{}" };
};
const source = { source_conversation_key: "import:claude:sess-1", source_provider: "claude", source_runtime_session_id: "sess-1" };
await api.handoffPreview({ ...source, model: "codex-sol" });
await api.continueSession({ ...source, model: "codex-sol", message: "Continue" });
assert.deepEqual(calls, [
  ["/admin/sessions/handoff-preview", "POST", { ...source, model: "codex-sol" }],
  ["/admin/sessions/continue", "POST", { ...source, model: "codex-sol", message: "Continue" }],
]);
const parent = "parent:<unsafe>/1";
let turns = [{ id: 1, status: "completed", metadata: "{broken" },
  { id: 2, status: "completed", metadata: JSON.stringify({ parent_conversation_key: parent }) }];
api.sessions = async () => ({ items: [] });
api.sessionDetail = async () => ({ turns });
const previews = [];
const allowed = { handoff_turns_included: 2, handoff_turns_total: 3, preview_bytes: 4200, handoff_truncated: true, policy_blocked_reason: null };
api.handoffPreview = async (body) => {
  previews.push(body);
  return { ...allowed, policy_blocked_reason: body.model === "blocked" ? "Policy <denied>" : null };
};
const config = { models: { "codex-sol": {}, blocked: {} }, pools: { pool: {}, "codex-sol": {} } };
const render = async (cfg = config, rest = ["session", source.source_conversation_key, "claude", "sess-1"]) => {
  main.replaceChildren();
  const cleanup = await renderExecutions(main, { config: cfg, rest });
  await flush();
  return cleanup;
};
let cleanup = await render();
const root = document.body;
const links = () => root.querySelectorAll("a");
assert.ok(root.textContent.includes("Forked from " + parent));
assert.ok(links().some((a) => a.textContent === parent && a.getAttribute("href") === `#/executions/session/${encodeURIComponent(parent)}`));
assert.equal(root.querySelectorAll("unsafe").length, 0);
const model = root.querySelectorAll("select")[0];
const message = root.querySelectorAll("textarea")[0];
const form = root.querySelectorAll("form")[0];
const submit = button("Continue (Fork)", root);
assert.deepEqual(model.children.map((n) => n.value), ["blocked", "codex-sol", "pool"]);
assert.deepEqual(previews[0], { ...source, model: "blocked" });
assert.ok(root.textContent.includes("Policy <denied>"));
assert.equal(submit.disabled, true);
let complete;
const payloads = [];
api.continueSession = (body) => {
  payloads.push(body);
  return new Promise((resolve) => { complete = resolve; });
};
const event = { preventDefault() {} };
message.value = "Continue <please>";
await form.fire("submit", event);
assert.equal(payloads.length, 0);
model.value = "codex-sol";
await model.fire("change");
assert.equal(submit.disabled, false);
assert.ok(root.textContent.includes("2 / 3 turns · 4200 bytes · truncated"));
const pending = form.fire("submit", event);
assert.equal(submit.disabled, true);
await form.fire("submit", event);
assert.deepEqual(payloads, [{ ...source, model: "codex-sol", message: "Continue <please>" }]);
const fork = "fork:0123456789abcdef";
complete({ conversation_key: fork, response: "Reply <safe>" });
await pending;
assert.equal(submit.disabled, false);
assert.ok(root.textContent.includes("Reply <safe>"));
assert.ok(links().some((a) => a.getAttribute("href") === `#/executions/session/${encodeURIComponent(fork)}` && a.textContent === `Open forked session (${fork})`));
assert.equal(root.querySelectorAll("safe").length, 0);
let resolveOld;
api.handoffPreview = () => new Promise((resolve) => { resolveOld = resolve; });
model.value = "blocked";
const old = model.fire("change");
api.handoffPreview = async () => allowed;
model.value = "pool";
await model.fire("change");
resolveOld({ ...allowed, policy_blocked_reason: "Stale denial" });
await old;
assert.equal(submit.disabled, false);
assert.ok(!root.textContent.includes("Stale denial"));
api.handoffPreview = async () => { throw new Error("offline"); };
await model.fire("change");
assert.equal(submit.disabled, false);
api.continueSession = async () => { throw new Error("Continue failed"); };
await form.fire("submit", event);
assert.ok(root.textContent.includes("Continue failed"));
assert.equal(submit.disabled, false);
cleanup();
destroyPeek();
turns = [{ id: 1, status: "completed", metadata: { forked_from: parent } }];
api.handoffPreview = undefined;
cleanup = await render();
assert.ok(root.textContent.includes("Forked from " + parent));
assert.equal(button("Continue (Fork)", root).disabled, false);
cleanup();
destroyPeek();
cleanup = await render(null);
assert.equal(root.querySelectorAll("form").length, 0);
cleanup();
destroyPeek();
turns = [];
cleanup = await render();
assert.equal(root.querySelectorAll("form").length, 0);
cleanup();
destroyPeek();
console.log("Console continue session tests passed");
