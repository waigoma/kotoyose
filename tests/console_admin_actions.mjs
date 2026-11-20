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
  }

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
  createTextNode: (text) => Object.assign(new Element("#text"), { text }),
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
