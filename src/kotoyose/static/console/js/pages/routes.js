// Routes: the list, and a detail/edit panel for the selected route. Uses the existing /admin/routes endpoints.

import { providerOf, providerPanels } from "../provider-ui.js";
import { api } from "../api.js";
import { el, rowMeta, attachmentTags } from "../dom.js";
import { sanctionOf, trainingRiskLabel } from "../format.js";
import { createDirtyTracker, createSettingsDisclosure, openPeek } from "../peek.js";

// `new` is the create form's address, so no route can be opened under that name.
const NEW = "new";
const modelCache = new Map();

export function routeHash(name) {
  return `#/routes/${encodeURIComponent(name)}`;
}

/** route name → aliases that point at it. */
export function aliasesByRoute(config) {
  const map = new Map();
  for (const [alias, preset] of Object.entries(config.models || {})) {
    if (!preset || !preset.route) continue;
    if (!map.has(preset.route)) map.set(preset.route, []);
    map.get(preset.route).push(alias);
  }
  return map;
}

/** Copy the route as stored, then overwrite the fields this form edits. Unknown fields (model_provider) survive. */
export function routeBody(base, values) {
  const body = { ...base };
  body.driver = values.driver;
  body.model = values.model.trim();
  body.connection = values.connection;
  delete body.credential;
  delete body.endpoint;
  delete body.pool;
  if (values.driver !== "cli.print") delete body.profile;
  else if (values.profile) body.profile = values.profile;
  if (values.subscriptionOnly) body.subscription_only = true;
  else delete body.subscription_only;
  return body;
}

function sourceLabel(source, connection) {
  if (!source) return "";
  const label = source === "claude_cli_initialize"
    ? "Claude CLI"
    : source === "cursor_cli_models" ? "Cursor CLI" : source === "agy_cli_models"
      ? "Antigravity CLI"
      : source;
  return connection ? `from ${label} (${connection})` : `from ${label}`;
}

/** Model suggestions for a provider/connection. Cached per request shape; a failed lookup is not cached. */
export function suggestionsFor(provider, connection) {
  const params = connection ? { connection } : {};
  const key = JSON.stringify([provider, params]);
  if (!modelCache.has(key)) {
    const pending = api.providerModels(provider, params).then((data) => {
      if (!data || data.error_code || data.is_fallback || data.source === "bundled_catalog") {
        modelCache.delete(key);
      }
      return data;
    });
    modelCache.set(key, pending);
    pending.catch(() => modelCache.delete(key));
  }
  return modelCache.get(key);
}

function routeRow(name, route, sanction, aliases, selected) {
  const status = sanctionOf(sanction && sanction.sanction);
  const risk = trainingRiskLabel(sanction && sanction.data_policy);
  return el("a", {
    class: "row route-row",
    href: routeHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(route.driver, route.model),
    risk ? el("span", { class: "place-tag" }, risk) : null,
    attachmentTags(aliases)),
  sanction?.sanction === "official" ? null
    : el("span", { class: "state-chip is-warning" }, status.label));
}

function listCard(config, selected, aliases) {
  const names = Object.keys(config.routes || {}).sort();
  const sanctions = config.route_sanctions || {};
  return el("section", { class: "families-list", "aria-label": "Route list" },
    names.length === 0
      ? el("p", { class: "card empty-line" }, "No routes yet. Create one to connect an alias to a model.")
      : providerPanels(names, (name) => providerOf(config.routes[name], config), config, ["route", "routes"],
        (name) => routeRow(name, config.routes[name], sanctions[name], aliases.get(name) || [], name === selected)));
}

function field(label, control, { wide = false, hint = null } = {}) {
  return el("label", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label),
    control,
    hint);
}

function textInput(value, props = {}) {
  return el("input", { class: "input", type: "text", value: value || "", ...props });
}

function ackBox(name, sanction, refresh) {
  if (!sanction || sanction.sanction === "official") {
    return el("div", { class: "ack-box" },
      el("span", { class: "secondary" }, "Official route. No acknowledgement is needed."));
  }
  const acknowledged = sanction.sanction === "acknowledged";
  const reasons = acknowledged ? (sanction.ack && sanction.ack.reasons) || [] : sanction.sanction_reasons || [];
  const error = el("p", { class: "error", role: "alert" });
  const toggle = el("button", {
    type: "button",
    class: "switch",
    role: "switch",
    "aria-checked": acknowledged ? "true" : "false",
    "aria-label": "Acknowledged risk",
  });
  toggle.addEventListener("click", async () => {
    toggle.disabled = true;
    error.textContent = "";
    try {
      if (acknowledged) await api.revokeRouteAck(name);
      else await api.ackRoute(name);
      await refresh();
    } catch (failure) {
      error.textContent = failure.message;
      toggle.disabled = false;
    }
  });
  return el("div", { class: "ack-box" },
    el("div", { class: "ack-row" },
      el("div", { class: "ack-text" },
        el("span", {}, "Acknowledged risk"),
        el("span", { class: "small muted" }, acknowledged
          ? "Calls through this route are allowed for this version."
          : "Calls through this route are blocked until you acknowledge the risk.")),
      toggle),
    reasons.length ? el("ul", { class: "reasons" }, reasons.map((reason) => el("li", {}, reason))) : null,
    sanction.ack_stale
      ? el("p", { class: "error" }, "The route changed after it was acknowledged, so that acknowledgement no longer applies.")
      : null,
    sanction.ack
      ? el("span", { class: "small muted" },
        `Acknowledged by ${sanction.ack.acknowledged_by} · ${sanction.ack.at}${sanction.ack.note ? ` · ${sanction.ack.note}` : ""}`)
      : null,
    error);
}

function deleteControl(name, setBusy, status, onDeleted) {
  let armed = false;
  const button = el("button", { class: "btn btn-quiet-danger", type: "button" }, "Delete route");
  button.addEventListener("click", async () => {
    if (!armed) {
      armed = true;
      button.textContent = "Confirm delete";
      status.textContent = "";
      return;
    }
    status.textContent = "";
    setBusy(true);
    try {
      await api.deleteRoute(name);
      if (typeof onDeleted === "function") onDeleted();
      location.hash = "#/routes";
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
      armed = false;
      button.textContent = "Delete route";
    }
  });
  return button;
}

function routeForm(config, name, route, refresh, tracker, { onError = null } = {}) {
  const isNew = name === null;
  const drivers = Object.keys((config.catalog && config.catalog.drivers) || {}).sort();
  const connections = config.connections || {};
  const connectionIds = Object.keys(connections).sort();
  const start = isNew ? {} : route;
  const status = el("p", { class: "error", role: "alert" });

  const nameInput = textInput(isNew ? "" : name, { placeholder: "route-name", "aria-label": "Route name" });
  const driverSelect = el("select", { class: "input", "aria-label": "Driver" },
    drivers.map((driver) => el("option", { value: driver }, driver)));
  driverSelect.value = start.driver || drivers[0] || "";
  const providerHint = el("span", { class: "hint" });
  const modelList = el("datalist", { id: "route-model-options" });
  const modelHint = el("span", { class: "hint" });
  const modelInput = textInput(start.model, {
    list: "route-model-options", autocomplete: "off", required: true, "aria-label": "Model",
  });
  const connectionOptionLabel = (connId) => {
    const conn = connections[connId];
    if (!conn) return connId;
    const parts = [conn.provider, conn.kind];
    if (conn.endpoint) parts.push(conn.endpoint);
    return `${connId} (${parts.join(" · ")})`;
  };
  const connectionSelect = el("select", { class: "input", required: true, "aria-label": "Connection" },
    connectionIds.length === 0
      ? [el("option", { value: "" }, "No connections configured")]
      : connectionIds.map((connId) => el("option", { value: connId }, connectionOptionLabel(connId))));
  if (start.connection && connectionIds.includes(start.connection)) {
    connectionSelect.value = start.connection;
  } else {
    const initialProvider = ((config.catalog && config.catalog.drivers) || {})[driverSelect.value]?.provider;
    const match = connectionIds.find((connId) => connections[connId]?.provider === initialProvider);
    if (match) connectionSelect.value = match;
  }
  const connectionHint = el("span", { class: "hint" },
    "Connections define accounts and endpoints. ",
    el("a", { href: "#/providers" }, "Manage connections"));
  const subscriptionInput = el("input", { type: "checkbox", "aria-label": "Subscription only" });
  subscriptionInput.checked = Boolean(start.subscription_only);

  const printCliProfile = (conn) => conn?.profile;
  const values = () => ({
    driver: driverSelect.value,
    model: modelInput.value,
    connection: connectionSelect.value,
    profile: printCliProfile(connections[connectionSelect.value]),
    subscriptionOnly: subscriptionInput.checked,
  });

  const showProvider = () => {
    const conn = connections[connectionSelect.value];
    const profile = driverSelect.value === "cli.print" ? printCliProfile(conn) : null;
    const provider = driverSelect.value === "cli.print"
      ? (profile ? conn.provider : null)
      : ((config.catalog && config.catalog.drivers) || {})[driverSelect.value]?.provider;
    providerHint.textContent = profile ? `profile: ${profile}` : provider ? `provider: ${provider}` : "";
    return provider;
  };
  showProvider();
  let suggestionSeq = 0;
  const loadModelSuggestions = async () => {
    const provider = showProvider();
    if (!provider) return;
    const current = values();
    const selectedConn = connections[current.connection];
    const connection = selectedConn && selectedConn.provider === provider ? current.connection : "";
    const seq = ++suggestionSeq;
    try {
      const data = await suggestionsFor(provider, connection);
      if (seq !== suggestionSeq) return;
      modelList.replaceChildren(...(data.models || []).map((model) => {
        return el("option", { value: typeof model === "string" ? model : model.id });
      }));
      const src = sourceLabel(data.source, connection);
      modelHint.textContent = [src, data.warning].filter(Boolean).join(" · ");
    } catch (failure) {
      if (seq !== suggestionSeq) return;
      modelHint.textContent = `Suggestions unavailable: ${failure.message}`;
    }
  };

  driverSelect.addEventListener("change", () => {
    const provider = showProvider();
    const selectedConn = connections[connectionSelect.value];
    if (driverSelect.value === "cli.print" && !printCliProfile(selectedConn)) {
      const match = connectionIds.find((connId) => printCliProfile(connections[connId]));
      if (match) connectionSelect.value = match;
    } else if (provider && (!selectedConn || selectedConn.provider !== provider)) {
      const match = connectionIds.find((connId) => connections[connId]?.provider === provider);
      if (match) connectionSelect.value = match;
    }
    loadModelSuggestions();
  });

  connectionSelect.addEventListener("change", () => {
    loadModelSuggestions();
  });

  modelInput.addEventListener("focus", () => {
    loadModelSuggestions();
  });

  const save = el("button", { class: "btn btn-primary", type: "submit" }, isNew ? "Create route" : "Save changes");
  const remove = isNew ? null : deleteControl(name, (busy) => setBusy(busy), status, () => tracker.markClean());
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const failForm = (message) => {
    status.textContent = message;
    if (typeof onError === "function") onError();
  };

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const target = nameInput.value.trim();
    if (!target) {
      failForm("Name is required.");
      return;
    }
    const body = routeBody(isNew ? {} : route, values());
    setBusy(true);
    try {
      if (isNew) {
        if (target === NEW) throw new Error("\"new\" is reserved for this form. Choose another name.");
        await api.createRoute(target, body);
        tracker.markClean();
        location.hash = routeHash(target);
        return;
      }
      await api.updateRoute(name, body);
      if (target !== name) {
        await api.renameRoute(name, target);
        tracker.markClean();
        location.hash = routeHash(target);
        return;
      }
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
      field("Driver", driverSelect, { hint: providerHint }),
      field("Model", el("div", { class: "field-stack" }, modelInput, modelList, modelHint)),
      field("Connection", connectionSelect, { wide: true, hint: connectionHint }),
      el("label", { class: "check wide" }, subscriptionInput,
        "Subscription only (this connection is a subscription)")),
    status,
    el("div", { class: "form-actions" },
      el("span", { class: "hint" }, isNew ? null : "Changing the route makes an existing acknowledgement stale."),
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function routeSummarySection(selected, route, sanction, refresh) {
  return el("div", { class: "sub-section" },
    el("h3", { class: "sub-title" }, "Summary"),
    el("dl", { class: "kv" },
      el("dt", {}, "Driver"),
      el("dd", {}, route.driver || ""),
      el("dt", {}, "Model"),
      el("dd", {}, route.model || ""),
      route.connection ? [
        el("dt", {}, "Connection"),
        el("dd", {}, el("a", { href: `#/providers/connection/${encodeURIComponent(route.connection)}` }, route.connection)),
      ] : null,
      route.subscription_only ? [el("dt", {}, "Subscription only"), el("dd", {}, "Yes")] : null),
    ackBox(selected, sanction, refresh));
}

function openRoutePeek(config, selected, aliases, refresh) {
  if (selected === null) return;
  const onClose = () => { location.hash = "#/routes"; };
  const isNew = selected === NEW;
  if (!isNew && !config.routes[selected]) {
    openPeek({
      title: selected,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Route "${selected}" was not found.`),
        el("a", { href: "#/routes" }, "Back to routes")),
      onClose,
    });
    return;
  }
  const route = isNew ? null : config.routes[selected];
  const sanction = isNew ? null : (config.route_sanctions || {})[selected];
  const usedBy = isNew ? [] : aliases.get(selected) || [];
  const tracker = createDirtyTracker();
  let settingsDisclosure = null;
  const form = routeForm(config, isNew ? null : selected, route, refresh, tracker, {
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
    sections.push(routeSummarySection(selected, route, sanction, refresh));
    sections.push(el("div", { class: "sub-section" },
      el("div", { class: "sub-head" },
        el("h3", { class: "sub-title" }, "Used by"),
        el("span", { class: "mono small muted" }, String(usedBy.length))),
      attachmentTags(usedBy, (alias) => `#/aliases/${encodeURIComponent(alias)}`)
        || el("span", { class: "hint" }, "No alias points at this route.")));
  }
  sections.push(settingsDisclosure.details);

  const statusBadge = isNew || sanction?.sanction === "official" ? null
    : { label: sanctionOf(sanction?.sanction).label, tone: "warning" };
  openPeek({
    title: isNew ? "New route" : selected,
    subtitle: isNew
      ? "A route names a driver, a model, and a connection."
      : rowMeta(route.driver, route.model),
    badge: statusBadge,
    body: sections,
    onClose,
    isDirty: tracker.isDirty,
  });
}

export async function renderRoutes(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  const aliases = aliasesByRoute(config);
  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Routes"),
        el("p", { class: "page-sub" }, "Where a model call goes: driver, model and connection. Aliases point at a route.")),
      el("a", { class: "btn btn-primary", href: `#/routes/${NEW}` }, "New route")),
    listCard(config, selected, aliases),
  );
  openRoutePeek(config, selected, aliases, refresh);
}
