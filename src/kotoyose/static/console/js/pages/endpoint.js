// Endpoint: inbound OpenAI-compatible base URL, client API keys, and editable clients.

import { api } from "../api.js";
import { pathPicker } from "../path-picker.js";
import { dot, el, errorLine, attachmentTags, rowMeta, responsiveTable } from "../dom.js";
import { count, timeLabel } from "../format.js";
import { createDirtyTracker, openPeek } from "../peek.js";

export const NEW_CLIENT = "__new__";

const CLIENT_NAME_RE = /^[a-z0-9][a-z0-9-]{0,63}$/;
const PLACE_LABELS = [
  ["host", "Host"],
  ["isolated", "Jail"],
];
const SURFACE_OPTIONS = [
  ["", "Unrestricted (alias default)"],
  ["none", "None"],
  ["read", "Read"],
  ["exec", "Run commands"],
  ["write", "Write"],
];
const SURFACE_LABEL_MAP = Object.fromEntries(
  SURFACE_OPTIONS.filter(([value]) => value !== ""),
);

export function clientHash(name) {
  return `#/endpoint/clients/${encodeURIComponent(name)}`;
}

function formatPlace(place) {
  const found = PLACE_LABELS.find(([value]) => value === place);
  return found ? found[1] : place;
}

function showCreatedKeyDialog(result, onClose) {
  const secretInput = el("input", {
    class: "input input-mono",
    type: "text",
    readonly: true,
    value: result.key || "",
    "aria-label": "New API key",
  });
  const copyBtn = el("button", { class: "btn btn-primary", type: "button" }, "Copy");
  const closeBtn = el("button", { class: "btn", type: "button" }, "Close");

  const dialog = el("dialog", { class: "modal", "aria-labelledby": "new-key-title" },
    el("div", { class: "modal-body" },
      el("h2", { class: "card-title", id: "new-key-title" }, "API key created"),
      el("p", { class: "secondary" }, "Copy this key now. You will not see this key again."),
      el("dl", { class: "kv" },
        el("dt", {}, "Name"),
        el("dd", { class: "item-name" }, result.name || "—"),
        el("dt", {}, "Client"),
        el("dd", { class: "item-name" }, result.client || "—")),
      secretInput,
      el("div", { class: "modal-actions" }, copyBtn, closeBtn)));

  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(secretInput.value);
      copyBtn.textContent = "Copied";
    } catch (cause) {
      secretInput.select();
      copyBtn.textContent = "Select and copy";
    }
  });
  closeBtn.addEventListener("click", () => {
    dialog.close();
  });
  dialog.addEventListener("close", () => {
    secretInput.value = "";
    dialog.remove();
    onClose();
  });

  document.body.append(dialog);
  dialog.showModal();
  secretInput.focus();
  secretInput.select();
}

function baseUrlCard() {
  const baseUrl = `${location.origin}/v1`;
  const copyBtn = el("button", { class: "btn btn-sm", type: "button" }, "Copy URL");
  copyBtn.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(baseUrl);
      copyBtn.textContent = "Copied";
      setTimeout(() => {
        if (copyBtn.isConnected) copyBtn.textContent = "Copy URL";
      }, 1500);
    } catch (cause) {
      copyBtn.textContent = "Copy failed";
    }
  });

  return el("section", { class: "card", "aria-label": "Inbound base URL" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Inbound endpoint"),
      el("span", { class: "mono small muted" }, "/v1")),
    el("div", { class: "card-body stack" },
      el("div", { class: "copy-row" },
        el("span", { class: "copy-box mono" }, baseUrl),
        copyBtn),
      el("p", { class: "hint" },
        "Point OpenAI-compatible clients at this base URL with Authorization: Bearer <api-key>.")));
}

function keyActionButton(keyRow, statusError, reload) {
  const isRevoked = Boolean(keyRow.revoked_at);
  const idleLabel = isRevoked ? "Delete" : "Revoke";
  const armedLabel = isRevoked ? "Confirm delete" : "Confirm revoke";
  let armed = false;

  const button = el("button", {
    class: `btn btn-sm${isRevoked ? " btn-quiet-danger" : ""}`,
    type: "button",
  }, idleLabel);

  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = armedLabel;
      statusError.textContent = "";
      return;
    }
    button.disabled = true;
    statusError.textContent = "";
    try {
      if (isRevoked) {
        await api.deleteApiKey(keyRow.name);
      } else {
        await api.revokeApiKey(keyRow.name);
      }
      await reload();
    } catch (failure) {
      statusError.textContent = failure.message;
      button.disabled = false;
      armed = false;
      button.textContent = idleLabel;
    }
  });
  return button;
}

function keysCard(data, clientsList, reload) {
  const keys = data.keys || [];
  const clientNames = clientsList.length > 0
    ? clientsList.map((client) => client.name).sort()
    : (data.clients || []);
  const createError = errorLine("");
  const actionError = errorLine("");

  const nameInput = el("input", {
    class: "input",
    type: "text",
    required: true,
    pattern: "^[A-Za-z0-9_.-]+$",
    placeholder: "key-name",
    "aria-label": "Key name",
  });
  const clientSelect = el("select", { class: "input", required: true, "aria-label": "Client" },
    ...(clientNames.length === 0
      ? [el("option", { value: "" }, "No clients configured")]
      : clientNames.map((name) => el("option", { value: name }, name))));
  const createBtn = el("button", { class: "btn btn-primary", type: "submit", disabled: clientNames.length === 0 }, "Create key");

  const form = el("form", { class: "form" },
    el("div", { class: "inline-form" },
      el("label", { class: "field" },
        el("span", { class: "field-label" }, "Name"),
        nameInput),
      el("label", { class: "field" },
        el("span", { class: "field-label" }, "Client"),
        clientSelect),
      createBtn),
    createError);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    createError.textContent = "";
    const keyName = nameInput.value.trim();
    if (!keyName) {
      createError.textContent = "Key name is required.";
      return;
    }
    if (!clientSelect.value) {
      createError.textContent = "Client is required.";
      return;
    }
    createBtn.disabled = true;
    try {
      const result = await api.createApiKey(keyName, clientSelect.value);
      nameInput.value = "";
      showCreatedKeyDialog(result, reload);
    } catch (failure) {
      createError.textContent = failure.message;
    } finally {
      createBtn.disabled = false;
    }
  });

  const table = keys.length === 0
    ? el("p", { class: "muted" }, "No client API keys yet.")
    : responsiveTable(el("table", {},
        el("thead", {}, el("tr", {},
          el("th", {}, "Name"),
          el("th", {}, "Client"),
          el("th", {}, "Key"),
          el("th", {}, "Created"),
          el("th", {}, "Last used"),
          el("th", {}, "Status"),
          el("th", { class: "action-cell" }, "Action"))),
        el("tbody", {}, keys.map((row) => {
          const isRevoked = Boolean(row.revoked_at);
          return el("tr", {},
            el("td", { class: "item-name" }, row.name),
            el("td", { class: "item-name" }, row.client || "Unbound (re-issue)"),
            el("td", { class: "mono small" }, `${row.prefix}…${row.last4}`),
            el("td", { class: "mono small" }, timeLabel(row.created_at)),
            el("td", { class: "mono small" }, row.last_used_at ? timeLabel(row.last_used_at) : "Never"),
            el("td", { class: "small" },
              el("span", { class: "row-status" },
                dot(isRevoked ? "negative" : "positive"),
                isRevoked ? rowMeta("Revoked", el("span", { class: "mono" }, timeLabel(row.revoked_at))) : "Active")),
            el("td", { class: "action-cell" }, keyActionButton(row, actionError, reload)));
        }))));

  return el("section", { class: "card", "aria-label": "API keys" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "API keys"),
      el("span", { class: "mono small muted" }, String(keys.length))),
    el("div", { class: "card-body detail-body" },
      form,
      actionError,
      table));
}

function clientSummaryParts(client) {
  const models = (client.models || []).includes("*")
    ? "All models"
    : ((client.models || []).join(", ") || "no models");
  const places = (client.places || []).map(formatPlace).join(", ") || "none";
  const surface = client.max_surface ? (SURFACE_LABEL_MAP[client.max_surface] || client.max_surface) : "Unrestricted";
  const roots = client.workspace_roots || [];
  const wsText = roots.length === 0 ? "chat only" : `${count(roots.length)} workspace${roots.length === 1 ? "" : "s"}`;
  return [`Models: ${models}`, `Places: ${places}`, `Max permission: ${surface}`, wsText];
}

function clientsCard(clientsList, selectedName) {
  const newBtn = el("a", { class: "btn btn-sm btn-primary", href: clientHash(NEW_CLIENT) }, "New client");
  const body = clientsList.length === 0
    ? el("p", { class: "card-body muted" }, "No clients configured yet. Create one to scope API keys and workspace access.")
    : el("div", { class: "rows" }, clientsList.map((client) => {
      return el("a", {
        class: "row",
        href: clientHash(client.name),
        "aria-current": client.name === selectedName ? "true" : null,
      },
      el("div", { class: "row-main" },
        el("span", { class: "row-title" }, client.name),
        rowMeta(clientSummaryParts(client)),
        attachmentTags(client.active_keys || []),
        ),
      el("span", { class: "row-status small muted" }, "Edit"));
    }));

  return el("section", { class: "card", "aria-label": "Clients" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Clients"),
      el("div", { class: "inline-add" },
        el("span", { class: "mono small muted" }, String(clientsList.length)),
        newBtn)),
    body);
}

function workspaceDirsEditor(initialRoots, onChanged) {
  const rowsHost = el("div", { class: "stack" });
  const inputs = [];

  const addRow = (initialValue = "") => {
    const input = el("input", {
      class: "input input-mono",
      type: "text",
      placeholder: "/var/lib/client/workspaces",
      value: initialValue,
      "aria-label": "Workspace directory",
    });
    const removeBtn = el("button", { class: "btn btn-sm", type: "button" }, "Remove");
    const row = el("div", { class: "inline-form path-picker-row" }, pathPicker(input, { directories: true }), removeBtn);
    const entry = { input, row };
    inputs.push(entry);
    input.addEventListener("input", onChanged);
    removeBtn.addEventListener("click", () => {
      const idx = inputs.indexOf(entry);
      if (idx >= 0) inputs.splice(idx, 1);
      row.remove();
      onChanged();
    });
    rowsHost.append(row);
  };

  for (const root of initialRoots || []) addRow(root);

  const addBtn = el("button", { class: "btn btn-sm", type: "button" }, "Add directory");
  addBtn.addEventListener("click", () => {
    addRow("");
    const last = inputs[inputs.length - 1];
    if (last) last.input.focus();
    onChanged();
  });

  const node = el("div", { class: "field wide" },
    el("span", { class: "field-label" }, "Workspace directories"),
    rowsHost,
    el("div", { class: "row-actions" }, addBtn),
    el("span", { class: "hint" }, "Absolute directory paths (empty = chat only)."));

  return {
    node,
    values: () => inputs.map((entry) => entry.input.value.trim()).filter(Boolean),
  };
}

function boundKeysSection(client) {
  const keys = client.keys || [];
  return el("div", { class: "sub-section" },
    el("h3", { class: "sub-title" }, "Bound API keys"),
    keys.length === 0
      ? el("p", { class: "muted small" }, "No API keys belong to this client.")
      : responsiveTable(el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Name"),
            el("th", {}, "Key"),
            el("th", {}, "Status"))),
          el("tbody", {}, keys.map((key) => {
            const revoked = Boolean(key.revoked_at);
            return el("tr", {},
              el("td", { class: "item-name" }, key.name),
              el("td", { class: "mono small" }, `${key.prefix || "kys_"}…${key.last4 || ""}`),
              el("td", { class: "small" },
                el("span", { class: "row-status" },
                  dot(revoked ? "negative" : "positive"),
                  revoked ? rowMeta("Revoked", el("span", { class: "mono" }, timeLabel(key.revoked_at))) : "Active")));
          })))));
}

function clientForm(config, existing, refresh, tracker) {
  const isNew = existing === null;
  const start = existing || {
    name: "",
    models: [],
    places: ["isolated"],
    max_surface: null,
    workspace_roots: [],
    active_keys: [],
  };
  const status = errorLine("");

  const nameInput = el("input", {
    class: "input",
    type: "text",
    required: true,
    pattern: "^[a-z0-9][a-z0-9-]{0,63}$",
    placeholder: "my-client",
    value: start.name || "",
    disabled: !isNew,
    "aria-label": "Client name",
  });

  const configuredAliases = Object.keys((config && config.models) || {});
  const extraAliases = (start.models || []).filter((m) => m !== "*" && !configuredAliases.includes(m));
  const aliasChoices = Array.from(new Set([...configuredAliases, ...extraAliases])).sort();

  const allModelsBox = el("input", {
    type: "checkbox",
    value: "*",
    "aria-label": "All models",
  });
  allModelsBox.checked = (start.models || []).includes("*");

  const aliasBoxes = aliasChoices.map((alias) => {
    const box = el("input", {
      type: "checkbox",
      value: alias,
      "aria-label": `Model ${alias}`,
    });
    box.checked = (start.models || []).includes(alias);
    return { alias, box };
  });

  const syncModelsState = () => {
    const allowAll = allModelsBox.checked;
    for (const { box } of aliasBoxes) {
      box.disabled = allowAll;
    }
  };
  allModelsBox.addEventListener("change", () => {
    syncModelsState();
    tracker.markDirty();
  });
  syncModelsState();

  const modelsPicker = el("div", { class: "field wide", role: "group", "aria-label": "Models" },
    el("span", { class: "field-label" }, "Models"),
    el("label", { class: "check-row" },
      allModelsBox,
      el("span", {}, "All models"),
      el("span", { class: "muted small" }, "every configured alias, including ones added later")),
    aliasBoxes.length === 0
      ? el("span", { class: "hint" }, "No aliases configured yet.")
      : el("div", { class: "check-box" }, aliasBoxes.map(({ alias, box }) => el("label", { class: "check-row" },
        box,
        el("span", { class: "item-name" }, alias)))));

  const placeBoxes = PLACE_LABELS.map(([value, label]) => {
    const box = el("input", {
      type: "checkbox",
      value,
      "aria-label": label,
    });
    box.checked = (start.places || []).includes(value);
    return { value, box };
  });

  const placesField = el("div", { class: "field", role: "group", "aria-label": "Places" },
    el("span", { class: "field-label" }, "Places"),
    el("div", { class: "check-box" }, placeBoxes.map(({ box }, idx) => el("label", { class: "check-row" },
      box,
      el("span", {}, PLACE_LABELS[idx][1])))));

  const surfaceSelect = el("select", { class: "input", "aria-label": "Max permission" },
    SURFACE_OPTIONS.map(([value, label]) => el("option", { value }, label)));
  surfaceSelect.value = start.max_surface || "";

  const workspaces = workspaceDirsEditor(start.workspace_roots || [], () => tracker.markDirty());

  const form = el("form", { class: "form" });

  const saveBtn = el("button", { class: "btn btn-primary", type: "submit" },
    isNew ? "Create client" : "Save changes");

  const activeKeys = start.active_keys || [];
  let deleteControl = null;
  let deleteHint = null;
  if (!isNew) {
    if (activeKeys.length > 0) {
      deleteControl = el("button", {
        class: "btn btn-quiet-danger",
        type: "button",
        disabled: true,
        title: `Bound to active API keys: ${activeKeys.join(", ")}`,
      }, "Delete client");
      deleteHint = el("span", { class: "hint" },
        `Delete disabled: bound to active API key${activeKeys.length === 1 ? "" : "s"} (${activeKeys.join(", ")}).`);
    } else {
      let armed = false;
      const removeBtn = el("button", { class: "btn btn-quiet-danger", type: "button" }, "Delete client");
      removeBtn.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          removeBtn.textContent = "Confirm delete";
          status.textContent = "";
          return;
        }
        removeBtn.disabled = true;
        saveBtn.disabled = true;
        status.textContent = "";
        try {
          await api.deleteClient(start.name);
          tracker.markClean();
          location.hash = "#/endpoint";
        } catch (failure) {
          status.textContent = failure.message;
          removeBtn.disabled = false;
          saveBtn.disabled = false;
          armed = false;
          removeBtn.textContent = "Delete client";
        }
      });
      deleteControl = removeBtn;
    }
  }

  form.append(...[
    el("div", { class: "form-grid" },
      el("label", { class: "field wide" },
        el("span", { class: "field-label" }, "Name"),
        nameInput,
        el("span", { class: "hint" },
          isNew
            ? "Lowercase letters, digits, and hyphens (max 64 chars)."
            : "Client names cannot be changed after creation.")),
      modelsPicker,
      placesField,
      el("label", { class: "field" },
        el("span", { class: "field-label" }, "Max permission"),
        surfaceSelect),
      workspaces.node),
    status,
    deleteHint,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, deleteControl, saveBtn)),
  ].filter(Boolean));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    status.textContent = "";
    const clientName = isNew ? nameInput.value.trim() : start.name;
    if (!CLIENT_NAME_RE.test(clientName)) {
      status.textContent = "Client name must use lowercase letters, digits, and hyphens (start with letter/digit).";
      return;
    }
    const models = allModelsBox.checked
      ? ["*"]
      : aliasBoxes.filter(({ box }) => box.checked).map(({ alias }) => alias);
    const places = placeBoxes.filter(({ box }) => box.checked).map(({ value }) => value);
    const maxSurface = surfaceSelect.value || null;
    const workspaceRoots = workspaces.values();

    const payload = {
      models,
      places,
      max_surface: maxSurface,
      workspace_roots: workspaceRoots,
    };
    if (existing) {
      for (const fieldName of [
        "external_mcps",
        "allowed_env_vars",
        "allowed_provenance",
        "max_run_seconds",
        "max_memory_bytes",
        "max_processes",
        "max_cpus",
        "max_concurrent_runs",
      ]) {
        if (existing[fieldName] !== undefined) {
          payload[fieldName] = existing[fieldName];
        }
      }
    }

    saveBtn.disabled = true;
    try {
      await api.putClient(clientName, payload);
      tracker.markClean();
      const targetHash = clientHash(clientName);
      if (location.hash === targetHash) {
        await refresh();
      } else {
        location.hash = targetHash;
      }
    } catch (failure) {
      status.textContent = failure.message;
    } finally {
      saveBtn.disabled = false;
    }
  });

  tracker.attach(form);
  return form;
}

function openClientPeek(config, selectedName, clientsList, refresh) {
  if (!selectedName) return;
  const onClose = () => {
    location.hash = "#/endpoint";
  };
  const isNew = selectedName === NEW_CLIENT;
  const existing = isNew ? null : clientsList.find((client) => client.name === selectedName);
  if (!isNew && !existing) {
    openPeek({
      title: selectedName,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Client "${selectedName}" was not found.`),
        el("a", { href: "#/endpoint" }, "Back to Endpoint")),
      onClose,
    });
    return;
  }

  const tracker = createDirtyTracker();
  const sections = [clientForm(config, existing, refresh, tracker)];
  if (existing) {
    sections.push(boundKeysSection(existing));
  }
  openPeek({
    title: isNew ? "New client" : existing.name,
    subtitle: isNew
      ? "Define allowed models, places, permission ceiling, and workspace directories."
      : rowMeta(clientSummaryParts(existing)),
    body: sections,
    onClose,
    isDirty: tracker.isDirty,
  });
}

export async function renderEndpoint(main, { config = {}, rest = [], refresh }) {
  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Endpoint"),
        el("p", { class: "page-sub" },
          "Kotoyose inbound API base URL, client API keys, and client policies."))));

  let keysData;
  let clientsData;
  try {
    [keysData, clientsData] = await Promise.all([api.apiKeys(), api.clients()]);
  } catch (error) {
    if (!main.isConnected) return;
    main.append(el("section", { class: "card card-body" },
      errorLine(`Could not load API key data: ${error.message}`)));
    return;
  }
  if (!main.isConnected) return;

  const clientsList = (clientsData && clientsData.clients) || [];
  const selectedClient = rest[0] === "clients" ? (rest[1] || null) : null;

  main.append(
    baseUrlCard(),
    keysCard(keysData, clientsList, refresh),
    clientsCard(clientsList, selectedClient),
  );
  openClientPeek(config, selectedClient, clientsList, refresh);
}
