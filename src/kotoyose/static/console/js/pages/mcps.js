// MCPs: MCP server definitions, masked secret display, and inline connection probes. Uses /admin/mcps and /admin/mcp-probe.

import { api } from "../api.js";
import { pathField } from "../path-picker.js";
import { dot, el, rowMeta } from "../dom.js";
import {
  NEW,
  deleteControl,
  field,
  isSecretKey,
  itemHash,
  libraryListCard,
  maskSecret,
  notFoundBody,
  renderLibraryPage,
  textInput,
  usageByRef,
  usageSummaryTags,
  usedBySection,
  validateName,
} from "../library.js";
import { createDirtyTracker, createSettingsDisclosure } from "../peek.js";

const TRANSPORTS = ["stdio", "http", "sse"];
const STANDARD_STDIO_KEYS = new Set(["type", "command", "args", "env"]);
const STANDARD_HTTP_KEYS = new Set(["type", "url", "headers"]);
const PROBE_TIMEOUT_MS = 20000;

export function mcpHash(name) {
  return itemHash("mcps", name);
}

export function mcpTransport(server) {
  if (!server || typeof server !== "object") return "stdio";
  if (server.type) return server.type;
  return server.url ? "http" : "stdio";
}

function mcpRow(name, mcp, usage, selected) {
  const transport = mcpTransport(mcp && mcp.server);
  const aliasCount = usage?.models.length ?? 0;
  const aliasLabel = aliasCount > 0 ? `${aliasCount} ${aliasCount === 1 ? "alias" : "aliases"}` : null;
  return el("a", {
    class: "row route-row",
    href: mcpHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(mcp?.description),
    rowMeta(transport, aliasLabel),
    usageSummaryTags(usage)));
}

function sanitizeUrlForReadOnly(rawUrl) {
  if (!rawUrl) return "—";
  try {
    const parsed = new URL(rawUrl);
    if (parsed.username || parsed.password) {
      parsed.username = parsed.username ? "••••" : "";
      parsed.password = parsed.password ? "••••" : "";
    }
    if (parsed.search) {
      for (const [key, val] of [...parsed.searchParams.entries()]) {
        if (isSecretKey(key)) parsed.searchParams.set(key, maskSecret(val));
      }
    }
    return parsed.toString();
  } catch (cause) {
    return String(rawUrl);
  }
}

function formatSecretOrMasked(value) {
  if (value && typeof value === "object" && typeof value.secret === "string") {
    return `secret:${value.secret}`;
  }
  return maskSecret(value);
}

function readOnlySummary(server) {
  const transport = mcpTransport(server);
  const rows = [
    el("dt", {}, "Transport"),
    el("dd", {}, transport),
  ];

  if (transport === "stdio") {
    rows.push(el("dt", {}, "Command"), el("dd", { class: "mono" }, server.command || "—"));
    const args = Array.isArray(server.args) ? server.args : [];
    if (args.length > 0) {
      rows.push(el("dt", {}, "Args"), el("dd", { class: "mono" }, args.join(" ")));
    }
    const envEntries = Object.entries(server.env || {});
    rows.push(
      el("dt", {}, "Env"),
      el("dd", {}, envEntries.length === 0
        ? el("span", { class: "muted" }, "None")
        : el("div", { class: "chips" }, envEntries.map(([k, v]) => {
            return el("span", { class: "chip" }, `${k}=${formatSecretOrMasked(v)}`);
          }))),
    );
  } else {
    rows.push(el("dt", {}, "URL"), el("dd", { class: "mono" }, sanitizeUrlForReadOnly(server.url)));
    const headerEntries = Object.entries(server.headers || {});
    rows.push(
      el("dt", {}, "Headers"),
      el("dd", {}, headerEntries.length === 0
        ? el("span", { class: "muted" }, "None")
        : el("div", { class: "chips" }, headerEntries.map(([k, v]) => {
            return el("span", { class: "chip" }, `${k}: ${formatSecretOrMasked(v)}`);
          }))),
    );
  }

  for (const [key, val] of Object.entries(server || {})) {
    if (STANDARD_STDIO_KEYS.has(key) || STANDARD_HTTP_KEYS.has(key)) continue;
    if (isSecretKey(key)) {
      const display = typeof val === "object" && val !== null
        ? Object.entries(val).map(([k, v]) => `${k}: ${maskSecret(v)}`).join(", ")
        : maskSecret(val);
      rows.push(el("dt", {}, key), el("dd", { class: "mono" }, display || "—"));
    }
  }

  return el("div", { class: "ack-box" },
    el("span", { class: "field-label" }, "Saved configuration (secrets masked)"),
    el("dl", { class: "kv" }, rows));
}

function argsEditor(initialArgs, onChange) {
  const items = [...(Array.isArray(initialArgs) ? initialArgs : [])];
  const rowsEl = el("div", { class: "kv-editor" });

  const render = () => {
    if (items.length === 0) {
      rowsEl.replaceChildren(el("span", { class: "hint" }, "No arguments."));
      return;
    }
    rowsEl.replaceChildren(...items.map((arg, idx) => {
      const input = textInput(arg, {
        placeholder: "--flag or value",
        "aria-label": `Argument ${idx + 1}`,
      });
      input.addEventListener("input", () => {
        items[idx] = input.value;
        onChange();
      });
      const remove = el("button", {
        class: "btn btn-sm btn-quiet-danger",
        type: "button",
        "aria-label": `Remove argument ${idx + 1}`,
      }, "Remove");
      remove.addEventListener("click", () => {
        items.splice(idx, 1);
        render();
        onChange();
      });
      return el("div", { class: "kv-arg-row" }, input, remove);
    }));
  };

  const addBtn = el("button", { class: "btn btn-sm", type: "button" }, "Add argument");
  addBtn.addEventListener("click", () => {
    items.push("");
    render();
    onChange();
  });

  render();
  return {
    node: el("div", { class: "field wide" },
      el("span", { class: "field-label" }, "Arguments"),
      rowsEl,
      el("div", {}, addBtn)),
    values: () => items.filter((arg) => arg !== ""),
  };
}

function secretKvEditor(label, keyPlaceholder, valuePlaceholder, initialObj, emptyText, addLabel, onChange, secretNames = []) {
  const entries = Object.entries(initialObj || {}).map(([k, v]) => {
    if (v && typeof v === "object" && typeof v.secret === "string") {
      return { key: k, mode: "secret", value: "", secretName: v.secret };
    }
    return { key: k, mode: "literal", value: String(v ?? ""), secretName: secretNames[0] || "" };
  });
  const rowsEl = el("div", { class: "kv-editor" });
  const secretListId = `mcp-secret-options-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  const secretList = el("datalist", { id: secretListId },
    secretNames.map((secretName) => el("option", { value: secretName })));

  const render = () => {
    if (entries.length === 0) {
      rowsEl.replaceChildren(el("span", { class: "hint" }, emptyText));
      return;
    }
    rowsEl.replaceChildren(...entries.map((entry, idx) => {
      const keyInput = textInput(entry.key, {
        placeholder: keyPlaceholder,
        "aria-label": `${label} key ${idx + 1}`,
      });
      const modeSelect = el("select", {
        class: "input",
        "aria-label": `${label} source ${idx + 1}`,
      }, [
        el("option", { value: "literal" }, "Literal"),
        el("option", { value: "secret" }, "Secret"),
      ]);
      modeSelect.value = entry.mode;
      const valInput = entry.mode === "secret"
        ? textInput(entry.secretName, {
            list: secretListId,
            placeholder: "secret-name",
            "aria-label": `${label} value ${idx + 1}`,
          })
        : el("input", {
            class: "input input-mono",
            type: "password",
            autocomplete: "off",
            value: entry.value,
            placeholder: valuePlaceholder,
            "aria-label": `${label} value ${idx + 1}`,
          });
      keyInput.addEventListener("input", () => {
        entries[idx].key = keyInput.value;
        onChange();
      });
      modeSelect.addEventListener("change", () => {
        entries[idx].mode = modeSelect.value;
        render();
        onChange();
      });
      valInput.addEventListener("input", () => {
        if (entries[idx].mode === "secret") entries[idx].secretName = valInput.value;
        else entries[idx].value = valInput.value;
        onChange();
      });
      const remove = el("button", {
        class: "btn btn-sm btn-quiet-danger",
        type: "button",
        "aria-label": `Remove ${label} entry ${idx + 1}`,
      }, "Remove");
      remove.addEventListener("click", () => {
        entries.splice(idx, 1);
        render();
        onChange();
      });
      return el("div", { class: "kv-row" }, keyInput, modeSelect, valInput, remove);
    }));
  };

  const addBtn = el("button", { class: "btn btn-sm", type: "button" }, addLabel);
  addBtn.addEventListener("click", () => {
    entries.push({ key: "", mode: "literal", value: "", secretName: secretNames[0] || "" });
    render();
    onChange();
  });

  render();
  return {
    node: el("div", { class: "field wide" },
      el("span", { class: "field-label" }, label),
      secretList,
      rowsEl,
      el("div", {}, addBtn)),
    collect: () => {
      const out = {};
      for (const entry of entries) {
        const key = entry.key.trim();
        if (entry.mode === "secret") {
          const secretName = entry.secretName.trim();
          if (!key && !secretName) continue;
          if (!key) return { error: `${label}: key name is required when a secret is selected.` };
          if (!secretName) return { error: `${label}: secret name is required for '${key}'.` };
          out[key] = { secret: secretName };
        } else {
          if (!key && !entry.value) continue;
          if (!key) return { error: `${label}: key name is required when a value is set.` };
          out[key] = entry.value;
        }
      }
      return { map: out };
    },
  };
}

const PROBE_STATUS_VIEW = {
  ok: { tone: "positive", label: "Connection succeeded" },
  no_tools: { tone: "ring-positive", label: "Connected (0 tools)" },
  tools_unsupported: { tone: "warning", label: "Connected (tools capability unsupported)" },
  error: { tone: "negative", label: "Connection failed" },
};

const PROBE_STAGE_LABELS = {
  validation: "validation",
  connect: "connect",
  initialize: "initialize",
  tools_list: "tools/list",
  cleanup: "cleanup",
};

function renderProbeResult(container, result, sourceLabel) {
  if (!result || !Object.prototype.hasOwnProperty.call(PROBE_STATUS_VIEW, result.status)) {
    container.replaceChildren(
      el("div", { class: "probe-head" }, dot("negative"), "Unexpected probe response"),
    );
    return;
  }
  const view = PROBE_STATUS_VIEW[result.status];
  const metaParts = [sourceLabel];
  if (PROBE_STAGE_LABELS[result.stage]) metaParts.push(`stage: ${PROBE_STAGE_LABELS[result.stage]}`);
  if (Number.isFinite(result.elapsed_ms) && result.elapsed_ms >= 0) {
    metaParts.push(`${(result.elapsed_ms / 1000).toFixed(2)}s`);
  }
  if (result.status === "tools_unsupported") metaParts.push("tools: unsupported");
  else if (Number.isInteger(result.tool_count) && result.tool_count >= 0) {
    metaParts.push(`${result.tool_count} ${result.tool_count === 1 ? "tool" : "tools"}`);
  }

  const children = [
    el("div", { class: "probe-head" }, dot(view.tone), view.label),
    rowMeta(metaParts),
  ];
  if (typeof result.message === "string" && result.message) {
    children.push(el("p", { class: "small secondary" }, result.message));
  }
  if (Array.isArray(result.tools) && result.tools.length > 0) {
    children.push(el("div", { class: "chips" },
      result.tools
        .filter((tool) => tool && typeof tool.name === "string")
        .map((tool) => el("span", { class: "chip" }, tool.name))));
  }
  container.replaceChildren(...children);
}

function mcpForm(name, mcp, refresh, main, secretNames = [], tracker, { onError = null } = {}) {
  const isNew = name === null;
  const start = isNew ? { description: "", server: { type: "stdio", command: "", args: [], env: {} } } : mcp;
  const startServer = start.server || {};
  const startTransport = mcpTransport(startServer);

  const status = el("p", { class: "error", role: "alert" });
  const nameInput = textInput(isNew ? "" : name, {
    placeholder: "mcp-name",
    "aria-label": "MCP name",
  }, { mono: false });
  const descInput = textInput(start.description || "", {
    placeholder: "Short description (optional)",
    "aria-label": "Description",
  }, { mono: false });

  const transportSelect = el("select", { class: "input", "aria-label": "Transport" },
    TRANSPORTS.map((t) => el("option", { value: t }, t)));
  transportSelect.value = TRANSPORTS.includes(startTransport) ? startTransport : "stdio";

  let probeController = null;
  let probeTimer = null;
  const probeButton = el("button", { class: "btn", type: "button" }, "Test connection");
  const cancelProbeButton = el("button", { class: "btn btn-sm", type: "button", hidden: true }, "Cancel");
  const probeResult = el("div", { class: "probe-box", role: "status", "aria-live": "polite", hidden: true });

  const clearProbe = () => {
    if (probeTimer) {
      clearTimeout(probeTimer);
      probeTimer = null;
    }
    if (probeController) {
      probeController.abort();
      probeController = null;
    }
    probeButton.disabled = false;
    probeButton.textContent = "Test connection";
    cancelProbeButton.hidden = true;
    probeResult.hidden = true;
    probeResult.replaceChildren();
  };

  const handleFieldChange = () => {
    tracker.markDirty();
    clearProbe();
  };

  const commandInput = textInput(startServer.command || "", {
    placeholder: "npx or /path/to/server",
    "aria-label": "Command",
  });
  commandInput.addEventListener("input", clearProbe);

  const urlInput = textInput(startServer.url || "", {
    placeholder: "https://example.com/mcp",
    "aria-label": "URL",
  });
  urlInput.addEventListener("input", clearProbe);

  const args = argsEditor(startServer.args, handleFieldChange);
  const envEditor = secretKvEditor(
    "Environment variables",
    "VAR_NAME",
    "secret value",
    startServer.env,
    "No environment variables.",
    "Add variable",
    handleFieldChange,
    secretNames,
  );
  const headersEditor = secretKvEditor(
    "Headers",
    "Header-Name",
    "secret header value",
    startServer.headers,
    "No headers.",
    "Add header",
    handleFieldChange,
    secretNames,
  );

  // Preserve extra secret-looking scalar fields from config as password inputs.
  const extraSecretInputs = [];
  for (const [key, val] of Object.entries(startServer)) {
    if (STANDARD_STDIO_KEYS.has(key) || STANDARD_HTTP_KEYS.has(key)) continue;
    if (isSecretKey(key) && typeof val === "string") {
      const input = el("input", {
        class: "input input-mono",
        type: "password",
        autocomplete: "off",
        value: val,
        "aria-label": key,
      });
      input.addEventListener("input", clearProbe);
      extraSecretInputs.push({ key, input, node: field(key, input, { wide: true }) });
    }
  }

  const stdioFields = el("div", { class: "form-grid wide" },
    pathField("Command", commandInput, { wide: true }),
    args.node,
    envEditor.node);
  const httpFields = el("div", { class: "form-grid wide" },
    field("URL", urlInput, { wide: true }),
    headersEditor.node);

  const syncTransport = () => {
    const isStdio = transportSelect.value === "stdio";
    stdioFields.hidden = !isStdio;
    httpFields.hidden = isStdio;
  };
  transportSelect.addEventListener("change", () => {
    syncTransport();
    clearProbe();
  });
  syncTransport();

  const buildServer = () => {
    const transport = transportSelect.value;
    const server = {};
    for (const [key, val] of Object.entries(startServer)) {
      if (STANDARD_STDIO_KEYS.has(key) || STANDARD_HTTP_KEYS.has(key)) continue;
      server[key] = val;
    }
    for (const extra of extraSecretInputs) {
      if (extra.input.value) server[extra.key] = extra.input.value;
      else delete server[extra.key];
    }
    server.type = transport;
    if (transport === "stdio") {
      const command = commandInput.value.trim();
      if (!command) return { error: "Command is required for stdio MCPs." };
      const envRes = envEditor.collect();
      if (envRes.error) return { error: envRes.error };
      server.command = command;
      const collectedArgs = args.values();
      if (collectedArgs.length > 0) server.args = collectedArgs;
      if (Object.keys(envRes.map).length > 0) server.env = envRes.map;
    } else {
      const url = urlInput.value.trim();
      if (!url) return { error: "URL is required for http/sse MCPs." };
      const headersRes = headersEditor.collect();
      if (headersRes.error) return { error: headersRes.error };
      server.url = url;
      if (Object.keys(headersRes.map).length > 0) server.headers = headersRes.map;
    }
    return { server };
  };

  const initialBuilt = isNew ? null : buildServer();
  const initialSnapshot = !isNew && initialBuilt && !initialBuilt.error
    ? JSON.stringify(initialBuilt.server)
    : null;

  cancelProbeButton.addEventListener("click", () => {
    clearProbe();
    probeResult.hidden = false;
    probeResult.replaceChildren(
      el("div", { class: "probe-head" }, dot("warning"), "Connection test cancelled"),
    );
  });

  probeButton.addEventListener("click", async () => {
    status.textContent = "";
    const built = buildServer();
    if (built.error) {
      clearProbe();
      probeResult.hidden = false;
      probeResult.replaceChildren(
        el("div", { class: "probe-head" }, dot("negative"), built.error),
      );
      return;
    }

    clearProbe();
    const isModified = isNew || JSON.stringify(built.server) !== initialSnapshot;
    const sourceLabel = !isNew && !isModified ? "Saved MCP" : "Unsaved form";
    const controller = new AbortController();
    probeController = controller;
    probeButton.disabled = true;
    probeButton.textContent = "Testing…";
    cancelProbeButton.hidden = false;
    probeResult.hidden = false;
    probeResult.replaceChildren(
      el("span", { class: "small muted" }, `Running ${sourceLabel.toLowerCase()} connection test…`),
    );

    probeTimer = setTimeout(() => {
      if (probeController !== controller) return;
      controller.abort();
      probeController = null;
      probeTimer = null;
      probeButton.disabled = false;
      probeButton.textContent = "Test connection";
      cancelProbeButton.hidden = true;
      probeResult.replaceChildren(
        el("div", { class: "probe-head" }, dot("negative"), "Connection test timed out after 20s"),
      );
    }, PROBE_TIMEOUT_MS);

    try {
      const result = !isNew && !isModified
        ? await api.probeMcp(name, { signal: controller.signal })
        : await api.probeMcpDraft(built.server, { signal: controller.signal });
      if (!main.isConnected || probeController !== controller) return;
      renderProbeResult(probeResult, result, sourceLabel);
    } catch (failure) {
      if (failure && failure.name === "AbortError") return;
      if (!main.isConnected || probeController !== controller) return;
      probeResult.replaceChildren(
        el("div", { class: "probe-head" }, dot("negative"), "Connection test failed"),
        el("p", { class: "small error" }, failure.message || "Request failed"),
      );
    } finally {
      if (probeController === controller) {
        clearTimeout(probeTimer);
        probeTimer = null;
        probeController = null;
        probeButton.disabled = false;
        probeButton.textContent = "Test connection";
        cancelProbeButton.hidden = true;
      }
    }
  });

  const save = el("button", { class: "btn btn-primary", type: "submit" },
    isNew ? "Create MCP" : "Save changes");
  const remove = isNew ? null : deleteControl({
    label: "Delete MCP",
    onDelete: () => api.deleteMcp(name),
    setBusy: (busy) => setBusy(busy),
    status,
    backHash: "#/mcps",
    onDeleted: () => {
      clearProbe();
      tracker.markClean();
    },
  });
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const failForm = (message) => {
    status.textContent = message;
    if (typeof onError === "function") onError();
  };

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const { name: target, error } = validateName(nameInput.value, isNew);
    if (error) {
      failForm(error);
      return;
    }
    const built = buildServer();
    if (built.error) {
      failForm(built.error);
      return;
    }
    const body = {
      description: descInput.value.trim(),
      server: built.server,
    };

    setBusy(true);
    try {
      if (isNew) {
        await api.createMcp(target, body);
        tracker.markClean();
        location.hash = mcpHash(target);
        return;
      }
      if (target !== name) {
        await api.renameMcp(name, target, body);
        tracker.markClean();
        location.hash = mcpHash(target);
        return;
      }
      await api.updateMcp(name, body);
      tracker.markClean();
      await refresh();
    } catch (failure) {
      failForm(failure.message);
      setBusy(false);
    }
  };

  const form = el("form", { class: "form", onsubmit: submit },
    el("div", { class: "form-grid" },
      field("Name", nameInput, { wide: true }),
      field("Description", descInput, { wide: true }),
      field("Transport", transportSelect, { wide: true }),
      stdioFields,
      httpFields,
      ...extraSecretInputs.map((extra) => extra.node)),
    el("div", { class: "field-stack" },
      el("div", { class: "probe-actions" },
        probeButton,
        cancelProbeButton,
        el("span", { class: "hint" }, "Runs initialize and tools/list only (uses saved MCP when unchanged, or the current form when edited).")),
      probeResult),
    status,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return { form, clearProbe };
}

function buildPeek(config, selected, usageMap, refresh, main, secretNames = []) {
  if (selected === null) return null;
  const isNew = selected === NEW;
  const mcp = isNew ? null : (config.mcps || {})[selected];
  if (!isNew && !mcp) {
    return {
      title: selected,
      subtitle: "Not found",
      body: notFoundBody(`MCP "${selected}" was not found.`, "#/mcps", "Back to MCPs"),
      onClose: () => { location.hash = "#/mcps"; },
    };
  }
  const tracker = createDirtyTracker();
  let settingsDisclosure = null;
  const { form, clearProbe } = mcpForm(isNew ? null : selected, mcp, refresh, main, secretNames, tracker, {
    onError: () => {
      if (settingsDisclosure) settingsDisclosure.open();
    },
  });
  settingsDisclosure = createSettingsDisclosure({
    title: "Settings",
    open: true,
    content: form,
    form,
  });
  const sections = [];
  if (!isNew) {
    sections.push(readOnlySummary(mcp.server || {}));
    sections.push(usedBySection(usageMap.get(selected), "No alias attaches this MCP."));
  }
  sections.push(settingsDisclosure.details);
  const transport = isNew ? null : mcpTransport(mcp.server);
  return {
    title: isNew ? "New MCP" : selected,
    subtitle: isNew
      ? "A named MCP server definition (stdio, http, or sse)."
      : rowMeta(transport, mcp.description),
    body: sections,
    onClose: () => {
      clearProbe();
      location.hash = "#/mcps";
    },
    isDirty: tracker.isDirty,
  };
}

export async function renderMcps(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  const mcps = config.mcps || {};
  const names = Object.keys(mcps).sort();
  const usageMap = usageByRef(config, "mcp_refs");
  const secretData = await api.secrets().catch(() => null);
  if (!main.isConnected) return;
  const secretNames = (secretData?.secrets || []).map((s) => s.name);

  const listCard = libraryListCard({
    ariaLabel: "MCP list",
    title: "All MCPs",
    names,
    emptyText: "No MCP servers yet. Create one to expose external tools to aliases.",
    renderRow: (name) => mcpRow(name, mcps[name], usageMap.get(name), name === selected),
  });

  renderLibraryPage(main, {
    title: "MCPs",
    subtitle: "Model Context Protocol server definitions. Secret values are masked in read-only views.",
    newHash: `#/mcps/${NEW}`,
    newLabel: "New MCP",
    listCard,
    peek: buildPeek(config, selected, usageMap, refresh, main, secretNames),
  });
}
