// Host catalog of data declarations. Names are text nodes, never HTML.

import { api } from "./api.js";
import { el } from "./dom.js";
import { declarationSummary } from "./provider-declaration-view.js";

export { declarationSummary };

export function renderDeclarationCatalog(catalog, { onRemove, onEdit, onOffer, onBump } = {}) {
  const rows = catalog?.declarations || [];
  const presets = catalog?.bundled || [];
  const list = el("div", { class: "candidate-list" });
  if (catalog?.error) list.append(el("p", { class: "error", role: "alert" }, catalog.error));
  if (catalog?.startup_warnings?.length) list.append(el("div", { class: "error", role: "status" },
    el("h3", {}, "Configuration entries skipped at startup"),
    el("ul", {}, catalog.startup_warnings.map((warning) => el("li", {}, warning))),
    el("p", {}, "Fix the declarations, then use ", el("a", { href: "#/machine" }, "Reload config.yml on the Machine page"), ".")));
  const action = (label, callback, extra = "") => el("button", {
    type: "button", class: `btn btn-sm ${extra}`.trim(), onclick: async (event) => {
      const target = event?.currentTarget;
      if (target) target.disabled = true;
      try { await callback(); }
      catch (error) { list.append(el("p", { class: "error", role: "alert" }, error.message || String(error))); }
      finally { if (target) target.disabled = false; }
    },
  }, label);
  const appendRow = (item, preset = null) => {
    const active = preset ? rows.find((row) => row.id === preset.id) : item;
    const installed = Boolean(active || preset?.installed);
    const invalid = (active || item).status === "invalid";
    const summary = declarationSummary(active || item);
    const outdated = preset && !preset.up_to_date;
    const state = invalid ? "Invalid" : installed ? (outdated ? "Update available" : "Offered") : "Preset";
    const tone = invalid ? "negative" : installed ? (outdated ? "warning" : "positive") : "";
    const publisher = item.publisher || summary.publisher;
    const version = active?.version || item.version || item.platforms?.[0]?.version || active?.platforms?.[0]?.version || "";
    const meta = [publisher, version ? `v${version}` : ""].filter(Boolean).join(" · ");
    const main = el("div", { class: "decl-main candidate-info" },
      el("div", { class: "candidate-title" }, el("span", { class: "row-title" }, item.display_name || summary.title),
        el("span", { class: "chip chip-state" }, el("span", { class: `dot ${tone}`.trim(), "aria-hidden": "true" }), state),
        meta ? el("span", { class: "small muted" }, meta) : null),
      el("div", { class: "chips" }, (item.platforms || []).map((p) => el("span", { class: "chip" }, `${p.platform} · ${p.max_capability}`))));
    if (invalid) main.append(el("p", { class: "error" }, `Invalid: ${active.reason}`));
    else if (active) main.append(el("details", { class: "decl-details" }, el("summary", {}, "Technical details"),
      el("p", { class: "mono small" }, summary.sha256), el("h4", {}, "Commands"),
      el("ul", {}, summary.commands.map((command) => el("li", {}, command))), el("h4", {}, "Artifacts"),
      el("ul", {}, summary.artifacts.map((artifact) => el("li", {}, artifact)))));
    const actions = el("div", { class: "candidate-actions" });
    if (preset && (!installed || outdated) && onOffer) actions.append(action(installed ? "Update preset" : "Offer to children", () => onOffer(item.id), "btn-primary"));
    if (active && !invalid && onEdit) actions.append(action("Edit JSON", () => onEdit(active)));
    if (!invalid && onBump) {
      const input = el("input", { name: "bump_version", class: "input input-mono", placeholder: "e.g. 1.0.95" });
      input.value = version;
      const status = el("p", { role: "status" });
      const submit = (label, apply) => el("button", {
        type: "button", class: `btn btn-sm${apply ? " btn-primary" : ""}`,
        onclick: async () => {
          preview.disabled = update.disabled = true;
          status.className = "";
          status.textContent = "Fetching artifacts & computing SHA-256…";
          try {
            await onBump(item.id, input.value.trim(), apply);
            status.textContent = apply ? "Provider version updated." : "Version fetched. Preview JSON in the editor.";
          } catch (error) {
            status.className = "error";
            status.textContent = error.message || String(error);
          } finally { preview.disabled = update.disabled = false; }
        },
      }, label);
      const preview = submit("Fetch & preview JSON", false);
      const update = submit("Fetch & update", true);
      const drawer = el("div", { class: "probe-box decl-bump", hidden: true },
        el("label", { class: "field" }, el("span", { class: "field-label" }, "New version"), input),
        el("p", { class: "hint" }, "Fetches each platform artifact from declared download hosts, computes SHA-256 automatically, and validates."),
        status, el("div", { class: "form-actions" }, preview, update));
      main.append(drawer);
      actions.append(action("Bump version", () => { drawer.hidden = !drawer.hidden; }));
    }
    if (installed && onRemove) actions.append(action("Remove", () => onRemove(active?.filename || item.id), "btn-quiet-danger"));
    list.append(el("article", { class: "decl-item candidate-item" }, main, actions));
  };
  for (const preset of presets) appendRow(preset, preset);
  for (const row of rows) if (!presets.some((preset) => preset.id === row.id)) appendRow(row);
  if (!presets.length && !rows.length) list.append(el("p", { class: "hint" }, "No data declarations are loaded."));
  return list;
}

export function mountDeclarationCatalog(parent, catalog) {
  const message = el("p", { role: "status", "aria-live": "polite" });
  const offered = el("div");
  const editor = el("textarea", { class: "input input-mono textarea textarea-tall", rows: 24, "aria-label": "Declaration JSON" });
  const form = el("div", { class: "form" });
  const json = el("div", { class: "form" });
  const builder = el("div", { class: "decl-builder sub-section form", id: "declaration-builder", hidden: true });
  const openBuilder = (open) => { builder.hidden = !open; custom.setAttribute("aria-expanded", String(open)); };
  let base = {};
  let fields = {};
  let platforms = [];
  const split = (value) => value.split(/[,\n]+/).map((v) => v.trim()).filter(Boolean);
  const button = (label, action, primary = false) => el("button", {
    type: "button", class: primary ? "btn btn-primary" : "btn btn-sm",
    onclick: async (event) => {
      const target = event?.currentTarget;
      if (target) target.disabled = true;
      try { await action(); } catch (error) { message.textContent = error.message || String(error); message.className = "error"; }
      finally { if (target) target.disabled = false; }
    },
  }, label);
  const field = (container, key, label, value = "", options = null, multiline = false) => {
    const input = options ? el("select", { name: key, class: "input" }, options.map((v) => el("option", { value: v }, v)))
      : el(multiline ? "textarea" : "input", { name: key, class: multiline ? "input input-mono textarea" : /^(id|version|binary|sha256|.*_hosts|signed_out_pattern|member)$/.test(key) ? "input input-mono" : "input", rows: multiline ? 3 : undefined });
    input.value = value;
    container.append(el("label", { class: multiline ? "field wide" : "field" }, el("span", { class: "field-label" }, label), input));
    return input;
  };
  const show = (tab) => {
    form.hidden = tab !== "form";
    json.hidden = tab !== "json";
    formTab.setAttribute("aria-pressed", String(tab === "form"));
    jsonTab.setAttribute("aria-pressed", String(tab === "json"));
  };
  const load = (document) => {
    const value = JSON.parse(document);
    if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error("Declaration must be a JSON object.");
    base = value;
    editor.value = document;
    populate();
  };
  const platformList = el("div", { class: "form" });
  const addPlatform = (key = "linux-x64", data = {}) => {
    const row = el("div", { class: "probe-box form" });
    const grid = el("div", { class: "form-grid" });
    const pin = data.install || {};
    const controls = {};
    controls.platform = field(grid, "platform", "Platform", key,
      ["linux-x64", "linux-arm64", "windows-x64", "windows-arm64", "darwin-x64", "darwin-arm64"]);
    controls.capability = field(grid, "max_capability", "Max capability", data.max_capability || "read",
      ["read", "write", "mcp", "unrestricted"]);
    for (const [name, label] of [["version", "Version"], ["binary", "Binary name"], ["sha256", "SHA-256"]])
      controls[name] = field(grid, name, label, pin[name] || "");
    controls.format = field(grid, "format", "Format", pin.format || "raw", ["raw", "tar.gz", "zip"]);
    controls.urls = field(grid, "urls", "Download URLs (one per line)", (pin.urls || []).join("\n"), null, true);
    controls.member = field(grid, "member", "Archive member (tar.gz / zip)", pin.member || "");
    controls.original = data;
    controls.layout = field(grid, "archive_layout", "Archive layout", pin.directory ? "directory" : "member", ["member", "directory"]);
    controls.layout.children[0].textContent = "Single binary (member)";
    controls.layout.children[1].textContent = "Directory bundle (directory + entrypoint)";
    controls.directory = field(grid, "directory", "Archive directory", pin.directory || "");
    controls.entrypoint = field(grid, "entrypoint", "Archive entrypoint", pin.entrypoint || "");
    controls.entrypoint_args = field(grid, "entrypoint_args", "Entrypoint args (optional)", (pin.entrypoint_args || []).join(", "), null, true);
    const updateMember = () => {
      const archive = ["tar.gz", "zip"].includes(controls.format.value);
      controls.layout.parentNode.hidden = !archive;
      controls.member.parentNode.hidden = !archive || controls.layout.value !== "member";
      controls.entrypoint_args.parentNode.hidden = controls.directory.parentNode.hidden = controls.entrypoint.parentNode.hidden = !archive || controls.layout.value !== "directory";
    };
    controls.layout.addEventListener("change", updateMember);
    controls.format.addEventListener("change", updateMember);
    updateMember();
    controls.heading = el("h4", { class: "sub-title" }, `Platform ${platforms.length + 1}`);
    const removePlatform = button("Remove", () => {
      platforms = platforms.filter((p) => p !== controls); row.remove();
      platforms.forEach((p, index) => { p.heading.textContent = `Platform ${index + 1}`; });
    });
    removePlatform.className = "btn btn-sm btn-quiet-danger";
    row.append(el("div", { class: "sub-head" }, controls.heading, removePlatform), grid);
    platforms.push(controls);
    platformList.append(row);
  };
  const populate = () => {
    form.replaceChildren();
    fields = {};
    platforms = [];
    platformList.replaceChildren();
    const section = (title) => {
      const grid = el("div", { class: "form-grid" });
      form.append(el("div", { class: "sub-section" }, el("h4", { class: "sub-title" }, title), grid));
      return grid;
    };
    const identityGrid = section("Identity");
    const identity = base.identity || {};
    for (const [key, label] of [["id", "ID"], ["display_name", "Display name"], ["publisher", "Publisher"]])
      fields[key] = field(identityGrid, key, label, identity[key] || "");
    fields.driver = field(identityGrid, "driver", "Driver", base.driver?.id || "cli.acp", ["cli.acp", "cli.print"]);
    form.append(el("h4", { class: "sub-title" }, "Platforms"), platformList, el("div", { class: "form-actions" }, button("Add platform", () => addPlatform())));
    const entries = Object.entries(base.platforms || {});
    if (entries.length) entries.forEach(([key, data]) => addPlatform(key, data));
    else addPlatform();
    const networkGrid = section("Network");
    for (const [key, label] of [["download_hosts", "Download hosts"], ["login_hosts", "Login hosts"], ["runtime_hosts", "Runtime hosts"]])
      fields[key] = field(networkGrid, key, label, (base.network?.[key] || []).join(", "));
    const operationsGrid = section("Operations");
    fields.login = field(operationsGrid, "login", "Login command preset", base.operations?.login?.argv?.join(" ") || "login",
      ["login", "login --device-auth", "-p /usage --output-format json"]);
    fields.flow = field(operationsGrid, "flow", "Login flow", base.operations?.login?.flow || "browser_wait",
      ["device_code", "browser_wait", "paste_code"]);
    fields.status = field(operationsGrid, "status", "Status command preset", base.operations?.status?.argv?.join(" ") || "status",
      ["status", "login status", "models", "status --format json", "-p /usage --output-format json"]);
    fields.pattern = field(operationsGrid, "signed_out_pattern", "Signed-out pattern (optional)", base.operations?.status?.signed_out_pattern || "");
    fields.argv = field(operationsGrid, "print_argv", "Print driver argv", base.print?.argv?.join(" ") || "-- {prompt}",
      ["-- {prompt}", "--prompt={prompt}"]);
    const acpArgv = base.acp?.argv?.join(" ") || "acp --model {model}";
    fields.acp = field(operationsGrid, "acp_argv", "ACP driver argv", acpArgv,
      [...new Set(["acp --model {model}", "acp", acpArgv])]);
    const acpHint = el("p", { class: "hint" }, "ACP driver argv: acp --model {model}. Template-specific ACP arguments are preserved. The server validates supported capability and command combinations.");
    const updateDriver = () => {
      fields.argv.parentNode.hidden = fields.driver.value !== "cli.print";
      fields.acp.parentNode.hidden = fields.driver.value !== "cli.acp";
      acpHint.hidden = fields.driver.value !== "cli.acp";
    };
    fields.driver.addEventListener("change", updateDriver);
    updateDriver();
    form.append(acpHint, el("div", { class: "form-actions" },
      button("Preview JSON", () => { build(); show("json"); }),
      el("div", { class: "form-actions-end" }, button("Save & offer to children", async () => { build(); await save(); }, true))));
  };
  const build = () => {
    const id = fields.id.value.trim();
    if (!/^[a-z][a-z0-9-]*$/.test(id)) throw new Error("ID must be a lowercase slug.");
    if (!platforms.length) throw new Error("Add at least one platform.");
    const doc = structuredClone(base);
    doc.schema = "kotoyose.provider-data/1";
    const previousId = base.identity?.id;
    const profileId = !doc.identity?.profile_id || doc.identity.profile_id === previousId ? id : doc.identity.profile_id;
    doc.identity = { ...doc.identity, id, profile_id: profileId,
      display_name: fields.display_name.value.trim(), publisher: fields.publisher.value.trim() };
    doc.driver = { id: fields.driver.value, contract_version: 1 };
    doc.platforms = {};
    for (const p of platforms) {
      const key = p.platform.value;
      if (doc.platforms[key]) throw new Error("Each platform may appear only once.");
      const install = { version: p.version.value.trim(), binary: p.binary.value.trim(),
        sha256: p.sha256.value.trim(), format: p.format.value, urls: split(p.urls.value) };
      if (["tar.gz", "zip"].includes(install.format)) {
        if (p.layout.value === "directory") {
          install.directory = p.directory.value.trim();
          install.entrypoint = p.entrypoint.value.trim();
          const args = p.entrypoint_args.value.split(/[,\s]+/).filter(Boolean);
          if (args.length) install.entrypoint_args = args;
        } else install.member = p.member.value.trim();
      }
      doc.platforms[key] = { ...p.original, max_capability: p.capability.value, install };
    }
    const hosts = [...new Set(Object.values(doc.platforms).flatMap((p) => p.install.urls.map((url) => new URL(url).hostname)))];
    doc.network = { download_hosts: split(fields.download_hosts.value), login_hosts: split(fields.login_hosts.value),
      runtime_hosts: split(fields.runtime_hosts.value) };
    if (!doc.network.download_hosts.length) doc.network.download_hosts = hosts;
    doc.connection_kinds = ["subscription"];
    doc.operations = {
      login: { ...doc.operations?.login, argv: fields.login.value.split(" "), flow: fields.flow.value,
        timeout_seconds: fields.flow.value === "device_code" ? 300 : 120 },
      status: { ...doc.operations?.status, argv: fields.status.value.split(" "), timeout_seconds: 8 },
    };
    delete doc.operations.status.signed_out_pattern;
    if (fields.pattern.value.trim()) doc.operations.status.signed_out_pattern = fields.pattern.value.trim();
    if (doc.driver.id === "cli.acp") {
      delete doc.print;
      if (!doc.acp || fields.acp.value !== doc.acp.argv?.join(" ")) {
        doc.acp = { ...doc.acp, argv: fields.acp.value.split(" "), placeholders: fields.acp.value === "acp" ? [] : ["model"] };
      }
      if (doc.platforms["windows-x64"] && doc.acp.argv.length === 1 && doc.acp.argv[0] === "acp") {
        doc.acp.unrestricted_argv ??= ["acp"];
      }
    } else {
      delete doc.acp;
      doc.print = { ...doc.print, prompt: "argv", argv: fields.argv.value.split(" "), placeholders: ["prompt"] };
    }
    editor.value = JSON.stringify(doc, null, 2);
    base = doc;
    return doc;
  };
  const refresh = (next) => {
    catalog = next;
    offered.replaceChildren(renderDeclarationCatalog(catalog, { onRemove: remove, onEdit: (row) => {
      load(row.document); openBuilder(true); show("json");
    }, onOffer: async (id) => {
      refresh(await api.installBundledProviderDeclaration(id)); success("Provider offered to children.");
    }, onBump: async (id, version, apply) => {
      const result = await api.bumpProviderDeclarationVersion(id, { version, apply });
      if (apply) { refresh(result.catalog); success("Provider version updated and offered to children."); }
      else { load(result.document); openBuilder(true); show("json"); success("Version fetched. Preview JSON before saving."); }
    } }));
    const selected = templateSelect.value;
    templateSelect.replaceChildren(...(catalog.bundled || []).map((item) =>
      el("option", { value: item.id }, `${item.display_name} (v${item.version})`)));
    templateSelect.value = catalog.bundled?.some((item) => item.id === selected) ? selected : catalog.bundled?.[0]?.id || "";
    templateControls.hidden = !catalog.bundled?.length;
  };
  const success = (text) => { message.className = "success-note"; message.textContent = text; };
  const remove = async (id) => { refresh(await api.deleteProviderDeclaration(id)); success("Declaration removed."); };
  const save = async () => {
    const doc = JSON.parse(editor.value);
    if (doc?.schema !== "kotoyose.provider-data/1") throw new Error("Expected kotoyose.provider-data/1 schema.");
    const next = await api.uploadProviderDeclaration(new TextEncoder().encode(editor.value));
    refresh(next); success("Declaration saved and offered to children.");
  };
  const formTab = button("Form builder", () => { load(editor.value || "{}"); show("form"); });
  const jsonTab = button("JSON editor", () => { if (!form.hidden && fields.id?.value.trim()) build(); show("json"); });
  formTab.className = "seg-btn";
  jsonTab.className = "seg-btn";
  const file = el("input", { type: "file", hidden: true, accept: "application/json,.json", "aria-label": "Load declaration file" });
  file.addEventListener("change", async () => {
    try { if (file.files?.[0]) { load(await file.files[0].text()); show("json"); } }
    catch (error) { message.className = "error"; message.textContent = error.message; }
  });
  json.append(editor, file, el("div", { class: "form-actions" },
    el("div", { class: "probe-actions" }, button("Import .json file", () => file.click()),
      button("Format JSON", () => { editor.value = JSON.stringify(JSON.parse(editor.value), null, 2); })),
    el("div", { class: "form-actions-end" }, button("Save declaration", save, true))));
  const templateSelect = el("select", { class: "input decl-template-select", "aria-label": "Template preset" });
  const templateButton = button("Load template", () => {
    const template = catalog.bundled?.find((item) => item.id === templateSelect.value);
    if (!template) throw new Error("Template is unavailable.");
    load(template.document); show("form");
  });
  const templateControls = el("div", { class: "probe-actions" }, templateSelect, templateButton);
  const custom = button("+ Custom provider", () => openBuilder(builder.hidden));
  custom.setAttribute("aria-controls", "declaration-builder");
  custom.setAttribute("aria-expanded", "false");
  builder.append(el("div", { class: "sub-head" },
    el("div", { class: "seg-group", "aria-label": "Declaration editor mode" }, formTab, jsonTab),
    el("div", { class: "probe-actions" }, templateControls, button("Close", () => openBuilder(false)))), form, json);
  populate();
  show("form");
  refresh(catalog);
  return el("section", { class: "card", id: "data-declarations" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Optional CLI providers for children"), custom),
    el("div", { class: "card-body form" },
      el("p", { class: "hint" }, "Host can offer extra CLI providers like Grok Build to connected children. Each child owner approves and installs them locally."),
      offered, builder, message));
}
