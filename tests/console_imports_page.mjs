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

const { renderImports } = await load("pages/imports.js");
const calls = [];
globalThis.fetch = async (path, init) => {
  calls.push([path, init.method, init.body && JSON.parse(init.body)]);
  return { ok: true, text: async () => "{}" };
};
await api.importSources();
await api.runImport({ source_kind: "all", dry_run: true });
await api.importRuns();
await api.importRuns("5");
await api.importChildren();
await api.setChildOriginHint("a/b", null);
await api.collectChildSessions("a/b", { source_kind: "all", dry_run: true });
assert.deepEqual(calls, [
  ["/admin/imports/sources", "GET", undefined],
  ["/admin/imports/run", "POST", { source_kind: "all", dry_run: true }],
  ["/admin/imports/runs?limit=20", "GET", undefined],
  ["/admin/imports/runs?limit=5", "GET", undefined],
  ["/admin/imports/children", "GET", undefined],
  ["/admin/imports/children/a%2Fb/origin-hint", "PUT", { origin_hint: null }],
  ["/admin/imports/children/a%2Fb/collect", "POST", { source_kind: "all", dry_run: true }],
]);
const run = {
  source_kind: "claude", dry_run: true, started_at: "2026-11-26T15:00:00Z",
  completed_at: "2026-11-26T15:00:02Z", files_scanned: 5, sessions_detected: 3,
  turns_extracted: 12, turns_imported: 10, turns_skipped_duplicate: 2,
  redactions_count: 1, truncations_count: 4, errors: [],
};
api.importChildren = async () => ({ children: [] });
let sourceLoads = 0;
let runLoads = 0;
api.importSources = async () => {
  sourceLoads++;
  return { sources: [
    { source_kind: "claude", default_path: "/local/<unsafe>", exists: true, file_count: 335, imported_turns: 42, last_imported_at: run.started_at },
    { source_kind: "extra", default_path: "/missing", exists: false, file_count: 0, imported_turns: 0 },
  ] };
};
api.importRuns = async (limit) => {
  assert.equal(limit, 20);
  runLoads++;
  return { runs: [run, { ...run, dry_run: false, errors: ["bad file"] }] };
};
await renderImports(main, {});
const tables = main.querySelectorAll("table");
assert.equal(tables.length, 2);
for (const text of ["claude", "/local/<unsafe>", "available", "not found", "335", "42"]) assert.ok(tables[0].textContent.includes(text), text);
for (const text of ["dry-run", "import", "10 / 2", "Completed", "Errors (1)"]) assert.ok(tables[1].textContent.includes(text), text);
assert.equal(main.querySelectorAll("unsafe").length, 0);
const input = (name) => [...main.querySelectorAll("input"), ...main.querySelectorAll("select")].find((node) => node.getAttribute("name") === name);
assert.deepEqual(input("source_kind").children.map((node) => node.value), ["all", "claude", "codex", "antigravity", "cursor", "grok", "extra"]);
assert.equal(input("dry_run").checked, true);
const form = main.querySelectorAll("form")[0];
const submit = button("Dry Run");
let complete;
const payloads = [];
api.runImport = (body) => {
  payloads.push(body);
  return new Promise((resolve) => { complete = resolve; });
};
input("source_kind").value = "extra";
input("since").value = "2026-01-01";
input("origin_hint").value = "personal";
const event = { preventDefault() {} };
const pending = form.fire("submit", event);
assert.equal(submit.disabled, true);
await form.fire("submit", event);
assert.equal(payloads.length, 1);
assert.deepEqual(payloads[0], { source_kind: "extra", dry_run: true, since: "2026-01-01", origin_hint: "personal" });
complete(run);
await pending;
assert.equal(submit.disabled, false);
assert.equal(sourceLoads, 2);
assert.equal(runLoads, 2);
const result = () => main.querySelectorAll("section").find((node) => node.getAttribute("aria-label") === "Import result");
for (const text of ["Sessions detected3", "Turns extracted12", "Would import10", "Duplicates skipped2", "Redactions1", "Truncations4"]) assert.ok(result().textContent.includes(text), text);
input("dry_run").checked = false;
await input("dry_run").fire("change");
assert.equal(submit.textContent, "Run Import");
input("source_kind").value = "codex";
input("origin_hint").value = "work";
api.runImport = async (body) => {
  payloads.push(body);
  return { ...run, dry_run: false, errors: ["<bad file>"] };
};
await form.fire("submit", event);
assert.deepEqual(payloads[1], { source_kind: "codex", dry_run: false, since: "2026-01-01", origin_hint: "work" });
assert.ok(result().textContent.includes("Imported10"));
assert.ok(result().textContent.includes("<bad file>"));
assert.equal(main.querySelectorAll("bad").length, 0);
assert.equal(sourceLoads, 3);
assert.equal(runLoads, 3);
input("since").value = " ";
input("origin_hint").value = "";
await form.fire("submit", event);
assert.deepEqual(payloads[2], { source_kind: "codex", dry_run: false });
const beforeError = [sourceLoads, runLoads];
api.runImport = async () => { throw new Error("permission denied"); };
await form.fire("submit", event);
assert.match(main.textContent, /Could not run import: permission denied/);
assert.equal(result(), undefined);
assert.equal(submit.disabled, false);
assert.deepEqual([sourceLoads, runLoads], beforeError);
await button("Refresh").fire("click");
assert.deepEqual([sourceLoads, runLoads], beforeError.map((n) => n + 1));
main.replaceChildren();
api.importSources = async () => ({ sources: [] });
api.importRuns = async () => ({ runs: [] });
await renderImports(main, {});
assert.match(main.textContent, /No local sources found/);
assert.match(main.textContent, /No import runs yet/);
main.replaceChildren();
api.importSources = async () => { throw new Error("sources offline"); };
api.importRuns = async () => { throw new Error("runs offline"); };
await renderImports(main, {});
assert.match(main.textContent, /Could not load sources: sources offline/);
assert.match(main.textContent, /Could not load recent runs: runs offline/);
assert.equal(button("Refresh").disabled, false);
main.replaceChildren();
let childLoads = 0;
let remoteRunLoads = 0;
let hostHint = "personal";
api.importSources = async () => ({ sources: [] });
api.importRuns = async () => { remoteRunLoads++; return { runs: [] }; };
api.importChildren = async () => {
  childLoads++;
  return { children: [
    { name: "<laptop>", platform: "linux", connected: true, supports_session_collect: true,
      session_import_sources: ["claude", "codex"], host_origin_hint: hostHint,
      effective_origin_hint: "work", imported_turns: 42, last_imported_at: run.started_at },
    { name: "offline", connected: false, supports_session_collect: true, session_import_sources: ["claude"] },
    { name: "disabled", connected: true, supports_session_collect: false, session_import_sources: ["claude"] },
    { name: "no-sources", connected: true, supports_session_collect: true, session_import_sources: [] },
  ] };
};
const saved = [];
api.setChildOriginHint = async (name, hint) => { saved.push([name, hint]); hostHint = hint; };
const collected = [];
api.collectChildSessions = async (name, body) => {
  collected.push([name, body]);
  return { ...run, dry_run: body.dry_run };
};
await renderImports(main, {});
const remote = () => main.querySelectorAll("section").find((node) => node.getAttribute("aria-label") === "Remote Machines (Children)");
for (const text of ["<laptop>", "linux", "connected", "session-collect", "offline", "disabled", "none", "claude, codex", "work", "42"]) assert.ok(remote().textContent.includes(text), text);
assert.equal(main.querySelectorAll("laptop").length, 0);
const machineRows = () => remote().querySelectorAll("tbody")[0].children;
for (const row of [...machineRows()].slice(1)) {
  assert.equal(button("Dry Run", row).disabled, true);
  assert.equal(button("Collect", row).disabled, true);
}
machineRows()[0].querySelectorAll("select")[0].value = "work";
await button("Save", machineRows()[0]).fire("click");
assert.deepEqual(saved, [["<laptop>", "work"]]);
assert.equal(childLoads, 2);
assert.equal(machineRows()[0].querySelectorAll("select")[0].value, "work");
machineRows()[0].querySelectorAll("select")[0].value = "";
await button("Save", machineRows()[0]).fire("click");
assert.deepEqual(saved[1], ["<laptop>", null]);
await button("Dry Run", machineRows()[0]).fire("click");
assert.deepEqual(collected[0], ["<laptop>", { source_kind: "all", dry_run: true }]);
assert.match(machineRows()[0].textContent, /Would import10/);
await button("Collect", machineRows()[0]).fire("click");
assert.deepEqual(collected[1], ["<laptop>", { source_kind: "all", dry_run: false }]);
assert.match(machineRows()[0].textContent, /Imported10/);
assert.equal(childLoads, 5);
assert.equal(remoteRunLoads, 3);
api.collectChildSessions = async () => { throw new Error("offline"); };
await button("Collect", machineRows()[0]).fire("click");
assert.match(remote().textContent, /Could not collect sessions: offline/);
assert.equal(button("Collect", machineRows()[0]).disabled, false);
api.setChildOriginHint = async () => { throw new Error("save failed"); };
await button("Save", machineRows()[0]).fire("click");
assert.match(remote().textContent, /Could not save origin hint: save failed/);
main.replaceChildren();
api.importChildren = undefined;
await renderImports(main, {});
assert.match(remote().textContent, /No enrolled child machines/);
main.replaceChildren();
api.importChildren = async () => { throw new Error("hub offline"); };
await renderImports(main, {});
assert.match(remote().textContent, /Could not load children: hub offline/);
console.log("Console imports page tests passed");
