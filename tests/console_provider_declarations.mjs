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

  set innerHTML(_value) { throw new Error("innerHTML forbidden"); }

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
const load = (module) => import(pathToFileURL(join(process.argv[2] || "src/kotoyose/static/console", "js", module)));
const { api } = await load("api.js");
const { el } = await load("dom.js");
const main = el("main");
document.body.append(main);
const button = (label, root = main) => {
  const result = root.querySelectorAll("button").find((item) => item.textContent === label);
  assert.ok(result, `Missing button: ${label}`);
  return result;
};
const hasClass = (node, name) => (node.className || "").split(/\s+/).includes(name);
const byClass = (root, name) => root.children.flatMap((child) => [
  ...(hasClass(child, name) ? [child] : []), ...byClass(child, name),
]);


const { readFileSync } = await import("node:fs");
const { spawnSync } = await import("node:child_process");
const { mountDeclarationCatalog, renderDeclarationCatalog } = await load("provider-declarations.js");
const documentText = readFileSync("src/kotoyose/provider_declarations/grok-build.json", "utf8");
const preset = { id: "grok-build", display_name: "<img src=x onerror=alert(1)>", publisher: "<script>bad</script>",
  version: "1.0.89", platforms: [{ platform: "linux-x64", max_capability: "mcp" }], document: documentText };
let catalog = { bundled: [preset], declarations: [] };
const installed = [];
api.installBundledProviderDeclaration = async (id) => {
  installed.push(id);
  catalog = { bundled: [{ ...preset, installed: true, up_to_date: true }],
    declarations: [{ ...preset, document: documentText }] };
  return catalog;
};
main.append(mountDeclarationCatalog(main, catalog));
assert.ok(main.textContent.includes(preset.display_name));
assert.ok(main.textContent.includes("linux-x64 · mcp"));
assert.equal(main.querySelectorAll("img").length, 0);
await button("Offer to children").fire("click");
assert.deepEqual(installed, ["grok-build"]);
assert.ok(main.textContent.includes("Offered"));
assert.equal(byClass(main, "row-title").filter((n) => n.textContent === preset.display_name).length, 1);
assert.equal(byClass(main, "decl-item").length, 1);
assert.equal(byClass(main, "card").length, 1);
const state = byClass(main, "chip-state")[0];
assert.equal(state.textContent, "Offered");
assert.equal(byClass(state, "positive").length, 1);
assert.equal(main.querySelectorAll("script").length, 0);
await button("Edit JSON").fire("click");
const editor = main.querySelectorAll("textarea").find((n) => n.getAttribute("aria-label") === "Declaration JSON");
assert.equal(editor.value, documentText);
assert.equal(byClass(main, "decl-builder")[0].hidden, false);
assert.equal(button("JSON editor").getAttribute("aria-pressed"), "true");
await button("Form builder").fire("click");
await button("Preview JSON").fire("click");
assert.deepEqual(JSON.parse(editor.value).acp, JSON.parse(documentText).acp);
main.replaceChildren(mountDeclarationCatalog(main, { bundled: [preset], declarations: [] }));
const builder = byClass(main, "decl-builder")[0];
assert.equal(builder.hidden, true);
await button("+ Custom provider").fire("click");
assert.equal(builder.hidden, false);
assert.equal(button("+ Custom provider").getAttribute("aria-expanded"), "true");
await button("Close").fire("click");
assert.equal(builder.hidden, true);
await button("+ Custom provider").fire("click");
await button("JSON editor").fire("click");
assert.equal(button("JSON editor").getAttribute("aria-pressed"), "true");
assert.equal(button("Form builder").getAttribute("aria-pressed"), "false");
const emptyJsonEditor = main.querySelectorAll("textarea").find((n) => n.getAttribute("aria-label") === "Declaration JSON");
assert.equal(emptyJsonEditor.parent.hidden, false);
await button("Form builder").fire("click");
const input = (name) => [...main.querySelectorAll("input"), ...main.querySelectorAll("select"), ...main.querySelectorAll("textarea")]
  .find((n) => n.getAttribute("name") === name);
assert.equal(input("print_argv").parentNode.hidden, true);
input("driver").value = "cli.print";
await input("driver").fire("change");
assert.equal(input("print_argv").parentNode.hidden, false);
input("driver").value = "cli.acp";
await input("driver").fire("change");
assert.equal(input("print_argv").parentNode.hidden, true);
assert.equal(input("member").parentNode.hidden, true);
input("format").value = "tar.gz";
await input("format").fire("change");
assert.equal(input("member").parentNode.hidden, false);
input("format").value = "raw";
await input("format").fire("change");
assert.equal(input("member").parentNode.hidden, true);
for (const node of [...main.querySelectorAll("input"), ...main.querySelectorAll("select"), ...main.querySelectorAll("textarea")]) {
  if (node.getAttribute("type") === "file") { assert.equal(node.hidden, true); continue; }
  assert.ok(hasClass(node, "input"));
  if (node.tag === "textarea") assert.ok(hasClass(node, "input-mono") && hasClass(node, "textarea"));
}
for (const label of main.querySelectorAll("label")) assert.ok(hasClass(label.children[0], "field-label"));
assert.equal(main.querySelectorAll("fieldset").length, 0);
input("id").value = "example-cli";
input("display_name").value = "<b>Example</b>";
input("publisher").value = "Example";
input("version").value = "1.0.0";
input("binary").value = "example-cli";
input("urls").value = "https://example.com/cli";
input("sha256").value = "ab".repeat(32);
await button("Preview JSON").fire("click");
const jsonEditor = main.querySelectorAll("textarea").find((n) => n.getAttribute("aria-label") === "Declaration JSON");
const generated = JSON.parse(jsonEditor.value);
assert.deepEqual(generated.network.download_hosts, ["example.com"]);
assert.equal(generated.schema, "kotoyose.provider-data/1");
const validated = spawnSync(".venv/bin/python", ["-c",
  "import sys; from kotoyose.provider_data import parse_declaration; parse_declaration(sys.stdin.buffer.read())"],
  { input: jsonEditor.value, encoding: "utf8" });
assert.equal(validated.status, 0, validated.stderr);
const uploads = [];
api.uploadProviderDeclaration = async (bytes) => { uploads.push(bytes); return { bundled: [preset], declarations: [] }; };
const valid = jsonEditor.value;
jsonEditor.value = "{";
await button("Save declaration").fire("click");
assert.equal(uploads.length, 0);
assert.equal(jsonEditor.value, "{");
assert.ok(byClass(main, "error").length);
jsonEditor.value = valid;
api.uploadProviderDeclaration = async () => { throw new Error("unsupported declaration login flow"); };
await button("Save declaration").fire("click");
assert.ok(main.textContent.includes("unsupported declaration login flow"));
assert.equal(jsonEditor.value, valid);
api.uploadProviderDeclaration = async (bytes) => { uploads.push(bytes); return { bundled: [preset], declarations: [] }; };
await button("Format JSON").fire("click");
await button("Save declaration").fire("click");
assert.equal(uploads.length, 1);
assert.deepEqual(JSON.parse(new TextDecoder().decode(uploads[0])), generated);
const grok = { ...preset, display_name: "Grok Build", installed: true, up_to_date: true };
const offeredGrok = renderDeclarationCatalog({ bundled: [grok], declarations: [grok] });
assert.equal(byClass(offeredGrok, "row-title").filter((n) => n.textContent === "Grok Build").length, 1);
assert.equal(byClass(offeredGrok, "chip-state")[0].textContent, "Offered");
assert.equal(byClass(offeredGrok, "positive").length, 1);
const removed = [];
const standalone = renderDeclarationCatalog({ bundled: [{ ...preset, installed: true, up_to_date: false }],
  declarations: [{ ...preset }, { id: "custom", display_name: "Custom", document: documentText },
    { id: "broken", status: "invalid", reason: "<script>invalid</script>", filename: "broken.json" }] },
  { onRemove: (id) => removed.push(id), onEdit() {}, onOffer() {} });
assert.ok(hasClass(standalone, "candidate-list"));
assert.equal(byClass(standalone, "decl-item").length, 3);
assert.equal(byClass(standalone, "card").length, 0);
assert.equal(byClass(standalone, "warning").length, 1);
assert.ok(standalone.textContent.includes("Update available"));
assert.ok(standalone.textContent.includes("Invalid: <script>invalid</script>"));
const invalidRow = byClass(standalone, "decl-item")[2];
await button("Remove", invalidRow).fire("click");
assert.deepEqual(removed, ["broken.json"]);
assert.equal(standalone.querySelectorAll("script").length, 0);
assert.ok(byClass(main, "success-note").length);
assert.equal(main.querySelectorAll("b").length, 0);
// Preset selection must retain archive layouts and operation/ACP arguments.
const archivePreset = (id, directory) => {
  const doc = JSON.parse(documentText);
  doc.identity.id = doc.identity.profile_id = id;
  doc.acp = { argv: ["acp"], placeholders: [] };
  doc.operations.login.argv = ["-p", "/usage", "--output-format", "json"];
  doc.operations.status.argv = ["status", "--format", "json"];
  doc.platforms = { "linux-x64": structuredClone(Object.values(doc.platforms)[0]) };
  const pin = doc.platforms["linux-x64"].install;
  pin.format = "tar.gz";
  delete pin.member;
  if (directory) { pin.directory = "cursor"; pin.entrypoint = "bin/cursor"; }
  else pin.member = "agy";
  return { ...preset, id, display_name: id, document: JSON.stringify(doc) };
};
const agy = archivePreset("agy-cli", false);
const cursor = archivePreset("cursor-cli", true);
const multiple = { bundled: [preset, agy, cursor], declarations: [preset, cursor,
  { ...agy, id: "custom-cli", display_name: "Custom CLI" }, { id: "invalid", status: "invalid" }] };
main.replaceChildren(mountDeclarationCatalog(main, multiple));
assert.equal(byClass(main, "decl-item").length, 5);
assert.equal(main.querySelectorAll("button").filter((n) => n.textContent === "Bump version").length, 4);
for (const name of [preset.display_name, agy.display_name, cursor.display_name])
  assert.equal(byClass(main, "row-title").filter((n) => n.textContent === name).length, 1);
const selector = byClass(main, "decl-template-select")[0];
assert.deepEqual(selector.children.map((n) => n.value), ["grok-build", "agy-cli", "cursor-cli"]);
await button("+ Custom provider").fire("click");
for (const template of [cursor, agy]) {
  selector.value = template.id;
  await button("Load template").fire("click");
  const directory = template.id === "cursor-cli";
  assert.equal(input("archive_layout").value, directory ? "directory" : "member");
  assert.equal(input("directory").parentNode.hidden, !directory);
  assert.equal(input("member").parentNode.hidden, directory);
  await button("Preview JSON").fire("click");
  const result = JSON.parse(main.querySelectorAll("textarea").find((n) => n.getAttribute("aria-label") === "Declaration JSON").value);
  const original = JSON.parse(template.document);
  assert.deepEqual(result.platforms, original.platforms);
  assert.deepEqual(result.acp, original.acp);
  assert.deepEqual(result.operations.login.argv, original.operations.login.argv);
  assert.deepEqual(result.operations.status.argv, original.operations.status.argv);
}
await button("Form builder").fire("click");
input("archive_layout").value = "directory";
await input("archive_layout").fire("change");
assert.equal(input("member").parentNode.hidden, true);
input("format").value = "raw";
await input("format").fire("change");
assert.equal(input("directory").parentNode.hidden, true);
assert.equal(input("archive_layout").parentNode.hidden, true);
input("acp_argv").value = "acp --model {model}";
await button("Preview JSON").fire("click");
const currentEditor = () => main.querySelectorAll("textarea").find((n) => n.getAttribute("aria-label") === "Declaration JSON");
assert.deepEqual(JSON.parse(currentEditor().value).acp, { argv: ["acp", "--model", "{model}"], placeholders: ["model"] });
assert.equal("member" in JSON.parse(currentEditor().value).platforms["linux-x64"].install, false);

// ZIP directory bundles preserve entrypoint arguments and Windows approval argv.
selector.value = cursor.id;
await button("Load template").fire("click");
assert.deepEqual(input("format").children.map((n) => n.value), ["raw", "tar.gz", "zip"]);
input("platform").value = "windows-x64";
input("format").value = "zip";
await input("format").fire("change");
assert.equal(input("entrypoint_args").parentNode.hidden, false);
input("entrypoint").value = "node.exe";
input("entrypoint_args").value = "index.js, --flag value\nlast";
await button("Preview JSON").fire("click");
const zip = JSON.parse(currentEditor().value);
assert.deepEqual(zip.platforms["windows-x64"].install, {
  ...JSON.parse(cursor.document).platforms["linux-x64"].install,
  format: "zip", entrypoint: "node.exe", entrypoint_args: ["index.js", "--flag", "value", "last"],
});
assert.deepEqual(zip.acp.unrestricted_argv, ["acp"]);
await button("Form builder").fire("click");
assert.equal(input("entrypoint_args").value, "index.js, --flag, value, last");
await button("Preview JSON").fire("click");
assert.deepEqual(JSON.parse(currentEditor().value), zip);
zip.acp.unrestricted_argv = ["acp", "--custom"];
currentEditor().value = JSON.stringify(zip);
await button("Form builder").fire("click");
input("entrypoint_args").value = "  ";
input("acp_argv").value = "acp --model {model}";
await button("Preview JSON").fire("click");
assert.equal("entrypoint_args" in JSON.parse(currentEditor().value).platforms["windows-x64"].install, false);
assert.deepEqual(JSON.parse(currentEditor().value).acp.unrestricted_argv, ["acp", "--custom"]);
await button("Form builder").fire("click");
input("entrypoint_args").value = "index.js";
input("archive_layout").value = "member";
await input("archive_layout").fire("change");
assert.equal(input("entrypoint_args").parentNode.hidden, true);
await button("Preview JSON").fire("click");
assert.equal("entrypoint_args" in JSON.parse(currentEditor().value).platforms["windows-x64"].install, false);

// Bump preview/update, pending state, and recoverable inline failure.
const bumped = JSON.parse(cursor.document);
bumped.platforms["linux-x64"].install.version = "2.0.0";
const bumpedText = JSON.stringify(bumped);
const bumps = [];
let resolveBump;
api.bumpProviderDeclarationVersion = (id, payload) => {
  bumps.push({ id, payload });
  return new Promise((resolve) => { resolveBump = resolve; });
};
const cursorRow = byClass(main, "decl-item")[2];
await button("Bump version", cursorRow).fire("click");
const drawer = byClass(cursorRow, "decl-bump")[0];
assert.equal(drawer.hidden, false);
const versionInput = drawer.querySelectorAll("input")[0];
assert.equal(versionInput.value, cursor.version);
versionInput.value = "2.0.0";
const pending = button("Fetch & preview JSON", drawer).fire("click");
assert.equal(button("Fetch & update", drawer).disabled, true);
assert.ok(drawer.textContent.includes("Fetching artifacts & computing SHA-256…"));
resolveBump({ document: bumpedText });
await pending;
assert.deepEqual(bumps[0], { id: "cursor-cli", payload: { version: "2.0.0", apply: false } });
assert.equal(currentEditor().value, bumpedText);
assert.equal(button("JSON editor").getAttribute("aria-pressed"), "true");
assert.ok(main.textContent.includes("Version fetched."));
api.bumpProviderDeclarationVersion = async () => { throw new Error("artifact unavailable"); };
await button("Fetch & update", drawer).fire("click");
assert.equal(drawer.hidden, false);
assert.ok(byClass(drawer, "error")[0].textContent.includes("artifact unavailable"));
assert.equal(button("Fetch & update", drawer).disabled, false);
api.bumpProviderDeclarationVersion = async (id, payload) => {
  bumps.push({ id, payload });
  return { catalog: { bundled: [], declarations: [{ ...cursor, version: "2.0.0" }] } };
};
await button("Fetch & update", drawer).fire("click");
assert.deepEqual(bumps[1], { id: "cursor-cli", payload: { version: "2.0.0", apply: true } });
assert.equal(byClass(main, "decl-item").length, 1);
assert.ok(main.textContent.includes("v2.0.0"));
assert.ok(main.textContent.includes("Provider version updated and offered to children."));
assert.equal(byClass(main, "decl-template-select")[0].parentNode.hidden, true);
console.log("Console provider declarations tests passed");
