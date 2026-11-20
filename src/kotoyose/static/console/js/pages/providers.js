// Providers: manage Connections (accounts & endpoints) and inspect installed machine drivers.

import { api } from "../api.js";
import { pathField } from "../path-picker.js";
import { dot, el, errorLine, svgEl } from "../dom.js";
import {
  authLabel,
  authTone,
  resetLabel,
  sanctionOf,
  trainingRiskLabel,
  tightestWindowIndex,
  timeLabel,
} from "../format.js";
import { createDirtyTracker, createSettingsDisclosure, openPeek } from "../peek.js";

import { KNOWN_PROVIDERS, orderedProviderIds, providerPanelHead, cliVersionLabel } from "../provider-ui.js";

const NEW_CONNECTION = "new";
const DRIVERS_OPEN_KEY = "kotoyose.console.providers.drivers-open";

function loadDriversOpen() {
  try {
    return new Set(JSON.parse(window.localStorage.getItem(DRIVERS_OPEN_KEY) || "[]"));
  } catch {
    return new Set();
  }
}

function saveDriversOpen(open) {
  try {
    window.localStorage.setItem(DRIVERS_OPEN_KEY, JSON.stringify([...open]));
  } catch {
    // Remembering is a convenience; the page works without storage.
  }
}
const CONNECTION_KIND_LABELS = { subscription: "subscription", api_key: "api key", endpoint: "endpoint" };
const CONNECTION_KINDS = ["subscription", "api_key", "endpoint"];
const CLAUDE_SUBSCRIPTION_PROVIDERS = new Set(["claude", "anthropic"]);
const PRINT_CLI_CONNECTIONS = new Map([
  ["antigravity", { profile: "agy", binary: "agy", loginHome: "~/.local/share/kotoyose/agy-home" }],
  ["grok", { profile: "grok", binary: "grok", loginHome: "~/.local/share/kotoyose/grok-home" }],
  ["cursor", { profile: "cursor", binary: "agent", loginHome: "~/.local/share/kotoyose/cursor-home" }],
]);
const COLLAPSED_STORAGE_KEY = "kotoyose.console.providers.collapsed";

export const PROVIDER_TO_FAMILY = {
  claude: "claude",
  anthropic: "claude",
  codex: "openai",
  openai: "openai",
  antigravity: "google",
  cursor: "cursor",
  grok: "grok",
};

export const PROVIDER_FAMILIES = [
  { key: "claude", label: "Claude", monogram: "C", defaultProvider: "claude" },
  { key: "openai", label: "OpenAI", monogram: "O", defaultProvider: "codex" },
  { key: "google", label: "Google", monogram: "G", defaultProvider: "antigravity" },
  { key: "cursor", label: "Cursor", monogram: "Cu", defaultProvider: "cursor" },
  { key: "grok", label: "Grok Build", monogram: "Gk", defaultProvider: "grok" },
  { key: "other", label: "Other", monogram: "+", defaultProvider: "", optional: true },
];

const VALID_FAMILY_KEYS = new Set(PROVIDER_FAMILIES.map((f) => f.key));

export function familyForProvider(provider) {
  const key = String(provider || "").trim().toLowerCase();
  return Object.hasOwn(PROVIDER_TO_FAMILY, key) ? PROVIDER_TO_FAMILY[key] : "other";
}

function isClaudeSubscription(provider, kind) {
  return kind === "subscription" && CLAUDE_SUBSCRIPTION_PROVIDERS.has(String(provider || "").trim().toLowerCase());
}

function isPrintCliLogin(provider, kind) {
  return kind === "subscription" && PRINT_CLI_CONNECTIONS.has(String(provider || "").trim().toLowerCase());
}

function filterQueryString(filter = {}) {
  const params = new URLSearchParams();
  if (filter.family && filter.family !== "all" && VALID_FAMILY_KEYS.has(filter.family)) {
    params.set("family", filter.family);
  }
  if (filter.attention) {
    params.set("attention", "1");
  }
  if (filter.provider) {
    params.set("provider", filter.provider);
  }
  return params.toString();
}

export function providersHash(filter = {}) {
  const qs = filterQueryString({ family: filter.family, attention: filter.attention });
  return qs ? `#/providers?${qs}` : "#/providers";
}

export function providerHash(driver, filter = {}) {
  const qs = filterQueryString({ family: filter.family, attention: filter.attention });
  const base = `#/providers/${encodeURIComponent(driver)}`;
  return qs ? `${base}?${qs}` : base;
}

export function connectionHash(id, filter = {}) {
  const qs = filterQueryString(filter);
  const base = `#/providers/connection/${encodeURIComponent(id)}`;
  return qs ? `${base}?${qs}` : base;
}

function parseFilterFromQuery(query) {
  const params = query instanceof URLSearchParams
    ? query
    : (() => {
        const raw = typeof location !== "undefined" ? String(location.hash || "") : "";
        const idx = raw.indexOf("?");
        return new URLSearchParams(idx >= 0 ? raw.slice(idx + 1) : "");
      })();
  const rawFamily = String(params.get("family") || "").trim().toLowerCase();
  const family = VALID_FAMILY_KEYS.has(rawFamily) ? rawFamily : "all";
  const attentionVal = params.get("attention");
  const attention = attentionVal === "1" || attentionVal === "true";
  const defaultProvider = (params.get("provider") || "").trim();
  return { family, attention, defaultProvider };
}

function loadCollapsedProviders() {
  try {
    const saved = JSON.parse(window.localStorage.getItem(COLLAPSED_STORAGE_KEY));
    if (Array.isArray(saved)) return new Map(saved.map((id) => [String(id), true]));
    return new Map(Object.entries(saved || {}).filter(([, collapsed]) => typeof collapsed === "boolean"));
  } catch {
    return new Map();
  }
}

function saveCollapsedProviders(collapsedProviders) {
  try {
    window.localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(Object.fromEntries(collapsedProviders)));
  } catch {
    // Ignore storage failures (e.g. private browsing).
  }
}

function chevronIcon() {
  const props = {
    width: "16",
    height: "16",
    viewBox: "0 0 16 16",
    fill: "none",
    stroke: "currentColor",
    "stroke-width": "1.5",
    "stroke-linecap": "round",
    "stroke-linejoin": "round",
    "aria-hidden": "true",
    focusable: "false",
  };
  return svgEl("svg", props,
    svgEl("path", { d: "M5 6.5L8 9.5l3-3" }));
}

function routesUsingConnection(config, connId) {
  return Object.entries(config.routes || {})
    .filter(([, route]) => route && route.connection === connId)
    .map(([name]) => name)
    .sort();
}

export function normalizeConnections(config, connResponse) {
  if (connResponse && Array.isArray(connResponse.connections)) {
    return connResponse.connections;
  }
  return Object.entries(config.connections || {})
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([id, conn]) => ({
      id,
      ...conn,
      routes: routesUsingConnection(config, id),
    }));
}

export function connectionBody(values) {
  const body = {
    provider: values.provider.trim(),
    kind: values.kind,
  };
  if (values.kind === "subscription") {
    if (isClaudeSubscription(body.provider, values.kind)) {
      const mode = values.claudeAuthMode;
      const configDir = (values.configDir || "").trim();
      const secret = (values.secret || "").trim();
      if (mode === "config_dir") {
        if (configDir) body.config_dir = configDir;
      } else if (mode === "secret") {
        if (secret) body.secret = secret;
      } else {
        if (configDir) body.config_dir = configDir;
        if (secret) body.secret = secret;
      }
    } else if (isPrintCliLogin(body.provider, values.kind)) {
      const configDir = (values.configDir || "").trim();
      if (configDir) body.config_dir = configDir;
      body.profile = values.profile || PRINT_CLI_CONNECTIONS.get(body.provider.toLowerCase()).profile;
      const home = (values.home || "").trim();
      if (home) body.home = home;
    } else {
      const home = (values.home || "").trim();
      if (home) body.home = home;
      if (body.provider.toLowerCase() === "codex") {
        const authFile = (values.authFile || "").trim();
        if (authFile) body.auth_file = authFile;
      }
    }
  } else if (values.kind === "api_key") {
    body.secret = (values.secret || "").trim();
  } else if (values.kind === "endpoint") {
    body.endpoint = (values.endpoint || "").trim();
    body.secret = (values.secret || "").trim();
  }
  body.data_policy = values.dataPolicy;
  const quotaFrom = (values.quotaFrom || "").trim();
  if (quotaFrom) body.quota_from = quotaFrom;
  if (values.showQuota !== undefined) {
    body.show_quota = Boolean(values.showQuota);
  }
  return body;
}

function field(label, control, { wide = false, hint = null } = {}) {
  return el("label", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label),
    control,
    hint);
}

function quotaResetInfo(win) {
  if (win && win.resets_at) {
    const targetMs = new Date(win.resets_at).getTime();
    const abs = Number.isFinite(targetMs)
      ? new Date(targetMs).toLocaleString([], {
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
          hour: "2-digit",
          minute: "2-digit",
          hour12: false,
        })
      : String(win.resets_at);
    const rel = resetLabel(win.resets_at);
    const text = /^\d+[dhm]/.test(rel) ? `in ${rel}` : rel;
    return { text, title: abs };
  }
  if (win && win.resets_text) {
    return { text: win.resets_text, title: win.resets_text };
  }
  return { text: "", title: null };
}

const AGY_BUCKET_LABELS = {
  "gemini-weekly": "Gemini · Weekly",
  "gemini-5h": "Gemini · Session (5h)",
  "3p-weekly": "Claude & GPT · Weekly",
  "3p-5h": "Claude & GPT · Session (5h)",
  "gemini-argon-wtu-weekly": "Argon · Weekly",
  "gemini-argon-wtu-5h": "Argon · Session (5h)",
};
const AGY_GROUP_ALIASES = {
  "gemini models": "Gemini",
  "claude and gpt models": "Claude & GPT",
  "gemini 4 argon": "Argon",
};

/**
 * Provider ごとに違う窓の名前を同じ言い方にそろえる (表示だけ。title には元の名前を残す)。
 * Claude CLI: "Current session" / "Current week (all models)" / "Current week (Fable)"
 * Codex:      "5h" / "7d"
 * Antigravity: "Gemini · Weekly" / "Gemini · Session (5h)" / "Claude & GPT · Weekly" / "Argon · Weekly"
 */
export function quotaDisplayLabel(raw) {
  const label = String(raw || "").trim();
  const lower = label.toLowerCase();
  if (lower === "current session" || lower === "5h" || lower === "primary") return "Session (5h)";
  if (lower === "7d" || lower === "secondary" || lower === "current week (all models)") return "Weekly (7d)";
  const week = /^current week \((.+)\)$/i.exec(label);
  if (week) return `Weekly · ${week[1]}`;
  if (AGY_BUCKET_LABELS[lower]) return AGY_BUCKET_LABELS[lower];
  const groupWin = /^(.+?)\s*[·•]\s*(.+)$/.exec(label);
  if (groupWin) {
    const rawGroup = groupWin[1].trim();
    const group = AGY_GROUP_ALIASES[rawGroup.toLowerCase()] || rawGroup;
    const rawWin = groupWin[2].trim();
    const winLower = rawWin.toLowerCase();
    const win = (winLower === "weekly" || winLower === "7d" || winLower === "seven_day")
      ? "Weekly"
      : (winLower === "5h" || winLower === "five_hour" || winLower === "session" || winLower === "session (5h)")
        ? "Session (5h)"
        : rawWin;
    return `${group} · ${win}`;
  }
  return label || "Quota";
}

export function renderQuotaRows(quota, { via = null } = {}) {
  const list = Array.isArray(quota) ? quota : [];
  if (list.length === 0) return null;
  return el("div", { class: "quota-rows" },
    via ? el("div", { class: "quota-via hint mono" }, `via ${via}`) : null,
    list.map((win) => {
      const pct = Math.max(0, Math.min(100, Math.round(Number(win.used_percent) || 0)));
      const dotTone = pct >= 90 ? "negative" : pct >= 70 ? "warning" : "positive";
      const fillTone = pct >= 90 ? "danger" : pct >= 70 ? "warning" : "ok";
      const raw = win.label || win.window || "quota";
      const label = quotaDisplayLabel(raw);
      const reset = quotaResetInfo(win);
      return el("div", { class: "quota-win" },
        dot(dotTone),
        el("span", { class: "quota-label", title: raw }, label),
        el("div", { class: "quota-track" },
          el("div", { class: `quota-fill ${fillTone}`, style: `width: ${pct}%` })),
        el("span", { class: "quota-pct mono" }, `${pct}% used`),
        el("span", { class: "quota-reset mono", title: reset.title }, reset.text));
    }));
}

export function quotaSignature(quota) {
  const list = Array.isArray(quota) ? quota : [];
  if (list.length === 0) return "";
  return JSON.stringify(list.map((win) => ({
    label: String((win && (win.label || win.window)) || ""),
    used_percent: (win && win.used_percent !== undefined) ? win.used_percent : null,
    resets_at: (win && win.resets_at !== undefined) ? win.resets_at : null,
    resets_text: (win && win.resets_text !== undefined) ? win.resets_text : null,
  })));
}

export function renderCompactQuota(quota, { kind = "via", sourceId, via = null } = {}) {
  const list = Array.isArray(quota) ? quota : [];
  if (list.length === 0 || !sourceId) return null;
  const idx = tightestWindowIndex(list);
  const tightest = idx >= 0 ? list[idx] : list[0];
  const pct = Math.max(0, Math.min(100, Math.round(Number(tightest && tightest.used_percent) || 0)));
  const tone = pct >= 90 ? "negative" : pct >= 70 ? "warning" : "positive";
  const reset = quotaResetInfo(tightest);
  const prefix = kind === "same" ? `Same quota as ${sourceId}` : `Quota via ${sourceId}`;
  const resetPart = reset.text ? ` · resets ${reset.text}` : "";
  const text = `${prefix} · tightest ${pct}%${resetPart}`;

  let expanded = false;
  let rowsNode = null;
  const toggleBtn = el("button", {
    type: "button",
    class: `quota-compact is-${tone}`,
    "aria-expanded": "false",
    title: reset.title ? `${text} (${reset.title})` : text,
  },
  dot(tone),
  el("span", { class: "quota-compact-text" }, text));

  const wrap = el("div", { class: "quota-compact-wrap" }, toggleBtn);

  toggleBtn.addEventListener("click", (event) => {
    event.preventDefault();
    event.stopPropagation();
    expanded = !expanded;
    toggleBtn.setAttribute("aria-expanded", expanded ? "true" : "false");
    wrap.classList.toggle("is-expanded", expanded);
    if (expanded) {
      if (!rowsNode) {
        rowsNode = renderQuotaRows(list, { via });
      }
      if (rowsNode) wrap.append(rowsNode);
    } else if (rowsNode && rowsNode.parentNode) {
      rowsNode.parentNode.removeChild(rowsNode);
    }
  });

  return wrap;
}

function isKeyOrEndpointConnection(conn) {
  return Boolean(conn && (conn.kind === "api_key" || conn.kind === "endpoint"));
}

export function connectionAuthBadge(conn, setSecretNames) {
  const state = (conn && conn.state) || {};
  const auth = state.auth || "unknown";
  if (isKeyOrEndpointConnection(conn)) {
    if (auth === "ok") {
      return { tone: "positive", label: "key ok" };
    }
    if (auth === "logged_out" || auth === "expired" || auth === "login_required") {
      return { tone: "negative", label: "key rejected" };
    }
    if (auth === "unknown") {
      if (state.checked_at || state.last_error) {
        return { tone: "warning", label: "unreachable" };
      }
      return { tone: "", label: "not checked" };
    }
    return { tone: authTone(auth), label: authLabel(state) };
  }
  const isClaudeSecret = isClaudeSubscription(conn.provider, conn.kind) && !conn.config_dir;
  if (isClaudeSecret && auth === "unknown") {
    const isEnv = Boolean(conn.secret && conn.secret.startsWith("env:"));
    const configured = isEnv || Boolean(conn.secret && setSecretNames && setSecretNames.has(conn.secret));
    return configured
      ? { tone: "", label: "token set" }
      : { tone: "warning", label: "token missing" };
  }
  return { tone: authTone(auth), label: authLabel(state) };
}

function isExpiredByTimestamp(state) {
  if (!state || !state.expires_at) return false;
  const diffMs = new Date(state.expires_at).getTime() - Date.now();
  return Number.isFinite(diffMs) && diffMs <= 0;
}

export function connectionAttention(conn, setSecretNames = new Set()) {
  const state = (conn && conn.state) || {};
  const auth = state.auth || "unknown";
  const badge = connectionAuthBadge(conn, setSecretNames);
  const reasons = [];
  let severity = null; // "negative" | "warning" | null

  const flag = (level, reason) => {
    reasons.push(reason);
    if (level === "negative" || severity === null) {
      severity = level;
    }
  };

  if (
    auth === "expired"
    || auth === "logged_out"
    || auth === "login_required"
    || isExpiredByTimestamp(state)
    || badge.label === "expired"
  ) {
    flag("negative", badge.label === "ok" ? "expired" : badge.label);
  } else if (badge.label === "token missing") {
    flag("warning", "token missing");
  } else if (badge.label === "unreachable") {
    flag("warning", "unreachable");
  } else if (auth === "expiring") {
    flag("warning", "expiring");
  }

  if (state.last_error) {
    flag("negative", "error");
  }

  if (conn && conn.show_quota !== false) {
    const quota = Array.isArray(state.quota) ? state.quota : [];
    const hasHotQuota = quota.some((win) => (
      win
      && win.used_percent !== null
      && win.used_percent !== undefined
      && Number.isFinite(Number(win.used_percent))
      && Number(win.used_percent) >= 90
    ));
    if (hasHotQuota) {
      flag("negative", "quota >= 90%");
    }
  }

  return {
    needsAttention: reasons.length > 0,
    severity,
    reasons,
  };
}

function probeConnectionControl(id, refresh, statusEl) {
  const button = el("button", { class: "btn btn-sm", type: "button" }, "Check now");
  button.addEventListener("click", async () => {
    button.disabled = true;
    button.textContent = "Checking…";
    statusEl.textContent = "";
    try {
      await api.probeConnection(id);
      await refresh();
    } catch (failure) {
      button.disabled = false;
      button.textContent = "Check now";
      statusEl.textContent = failure.message;
    }
  });
  return button;
}

function connectionRow(conn, selected, filterState) {
  const { tone, label } = conn.badge;
  return el("a", {
    class: "connection-row",
    href: connectionHash(conn.id, filterState),
    "aria-current": selected ? "true" : null,
  },
  el("span", { class: "connection-kind small muted" }, CONNECTION_KIND_LABELS[conn.kind] || conn.kind),
  el("span", { class: "connection-name" }, conn.id),
  el("span", { class: `state-chip mono small${tone ? ` is-${tone}` : ""}` }, dot(tone), label),
  el("span", { class: "connection-chevron", "aria-hidden": "true" }, "›"));
}

function driverChip(driver, catalogEntry, install, routes, machineOk, selected, filterState) {
  // Level-2 list item: status, driver id, the places it can run, and how many routes use it.
  const places = catalogEntry?.places ? Object.keys(catalogEntry.places).sort() : [];
  const routeLabel = `${routes.length} ${routes.length === 1 ? "route" : "routes"}`;
  return el("li", {},
    el("a", {
      class: "driver-chip driver-item",
      href: providerHash(driver, filterState),
      "aria-current": selected ? "true" : null,
      "aria-label": [driver, places.join(", "), routeLabel].filter(Boolean).join(", "),
    },
    driverStatusDot(install, routes, machineOk),
    el("span", { class: "driver-item-name mono" }, driver),
    el("span", { class: "driver-item-places" },
      places.map((place) => el("span", { class: "place-tag" }, place))),
    el("span", { class: "driver-item-routes mono" }, routeLabel)));
}


function managedToolRow(tool, cliVersion) {
  const label = (value) => cliVersionLabel(value.binary_version)
    || cliVersionLabel(cliVersion)
    || (value.installed ? cliVersionLabel(value.version) : null)
    || `${value.profile} ${value.version}`;
  const status = el("span", { class: "small mono muted", role: "status" }, label(tool));
  const error = el("span", { class: "error small", role: "alert" });
  const button = el("button", {
    class: "btn btn-sm", type: "button", hidden: tool.installed,
  }, "Install");
  button.addEventListener("click", async () => {
    button.disabled = true;
    error.textContent = "";
    status.textContent = `Installing v${tool.version}…`;
    try {
      const result = { ...tool, ...await api.installTool(tool.profile) };
      status.replaceChildren(label(result));
      button.hidden = result.installed;
    } catch (failure) {
      status.replaceChildren(label(tool));
      error.textContent = failure.message;
    } finally {
      button.disabled = false;
    }
  });
  return el("div", { class: "chips" }, status, button, error);
}

function providerSection(entry, config, machine, machineOk, {
  selectedConnId,
  selectedDriver,
  filterState,
  collapsedProviders,
  reorder,
}) {
  const { id, connections, drivers, tools } = entry;
  const bodyId = `family-body-${id}`;
  const isCollapsed = collapsedProviders.get(id) ?? (connections.length === 0);
  const connLabel = `${connections.length} ${connections.length === 1 ? "connection" : "connections"}`;
  const visibleConnections = filterState.attention
    ? connections.filter((conn) => conn.attention.needsAttention)
    : connections;
  const status = ["negative", "warning", "", "positive"].find((tone) =>
    connections.some((conn) => (conn.attention.severity || conn.badge.tone) === tone));
  const signedOut = connections.some((conn) => ["logged_out", "login_required", "expired"].includes(conn.state?.auth));
  const statusLabel = signedOut ? "Logged out"
    : { negative: "Needs attention", warning: "Needs attention", positive: "OK" }[status] || "Unknown";
  const cliVersion = connections.find((conn) => conn.state?.cli_version)?.state.cli_version
    || [...drivers.keys()].filter((driver) => driver !== "cli.print")
      .map((driver) => machine?.drivers?.[driver]?.cli_version).find(Boolean);
  const addHref = connectionHash(NEW_CONNECTION, { ...filterState, provider: id });

  // Level 1: connections, then the add row as the last connection-shaped row.
  const connectionList = el("div", { class: "family-connections" },
    visibleConnections.map((conn) => connectionRow(conn, conn.id === selectedConnId, filterState)),
    el("a", { class: "provider-add", href: addHref },
      el("span", { class: "provider-add-mark", "aria-hidden": "true" }, "+"),
      el("span", { class: "provider-add-label" }, "New connection")));
  const body = el("div", { class: "family-body", id: bodyId, hidden: isCollapsed }, connectionList);
  if (drivers.size) {
    // Level 1 disclosure whose content is a level-2 list.
    const driversOpen = loadDriversOpen();
    const details = el("details", { class: "family-drivers", open: driversOpen.has(id) },
      el("summary", { class: "family-drivers-summary" },
        el("span", { class: "family-drivers-chevron", "aria-hidden": "true" }, chevronIcon()),
        "Drivers",
        el("span", { class: "family-drivers-count mono" }, String(drivers.size))),
      el("ul", { class: "driver-list family-drivers-chips" },
        [...drivers].map(([driver, routes]) => driverChip(
          driver,
          config.catalog?.drivers?.[driver],
          machine?.drivers?.[driver],
          routes,
          machineOk,
          driver === selectedDriver,
          filterState,
        ))));
    details.addEventListener("toggle", () => {
      const current = loadDriversOpen();
      if (details.open) current.add(id); else current.delete(id);
      saveDriversOpen(current);
    });
    body.append(details);
  }

  const handle = el("button", {
    class: "provider-handle", type: "button",
    "aria-keyshortcuts": "ArrowUp ArrowDown",
    title: "Drag or use ↑ and ↓ to reorder",
  }, "⠿");
  const { head, label, toggle } = providerPanelHead(id, connLabel, {
    toggleProps: { "aria-expanded": String(!isCollapsed), "aria-controls": bodyId },
    chevron: el("span", { class: "family-chevron", "aria-hidden": "true" }, chevronIcon()),
    statusTone: status,
    statusLabel,
    before: handle,
    after: tools.length ? tools.map((tool) => managedToolRow(tool, cliVersion)) : cliVersionLabel(cliVersion),
  });
  handle.setAttribute("aria-label", `Reorder ${label}`);
  const section = el("section", {
    class: `family-section${isCollapsed ? " is-collapsed" : ""}`,
    "aria-label": label,
    "data-provider": id,
  }, head, body);

  toggle.addEventListener("click", () => {
    body.hidden = !body.hidden;
    collapsedProviders.set(id, body.hidden);
    saveCollapsedProviders(collapsedProviders);
    toggle.setAttribute("aria-expanded", String(!body.hidden));
    section.classList.toggle("is-collapsed", body.hidden);
  });
  reorder(id, section, handle);
  return section;
}

function providerReorder(list, status, orderedIds, visibleIds) {
  const sections = new Map();
  let saving = false;
  let drag = null;

  function dropTarget(event) {
    const target = document.elementFromPoint(event.clientX, event.clientY)?.closest("[data-provider]");
    return target?.parentNode === list ? target : null;
  }

  async function move(id, index, handle) {
    const from = visibleIds.indexOf(id);
    if (saving || index < 0 || index >= visibleIds.length || from === index) return;
    const nextVisible = visibleIds.slice();
    nextVisible.splice(from, 1);
    nextVisible.splice(index, 0, id);
    const visible = new Set(visibleIds);
    let cursor = 0;
    const nextOrder = orderedIds.map((key) => visible.has(key) ? nextVisible[cursor++] : key);
    saving = true;
    status.textContent = "";
    try {
      await api.saveProviderOrder(nextOrder);
      orderedIds = nextOrder;
      visibleIds = nextVisible;
      if (!list.isConnected) return;
      const restoreFocus = document.activeElement === handle;
      list.replaceChildren(...visibleIds.map((key) => sections.get(key)));
      if (restoreFocus) handle.focus({ preventScroll: true });
    } catch (failure) {
      status.textContent = `Could not save provider order: ${failure.message}`;
    } finally {
      saving = false;
    }
  }

  return (id, section, handle) => {
    sections.set(id, section);
    handle.addEventListener("keydown", (event) => {
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      event.preventDefault();
      const direction = event.key === "ArrowUp" ? -1 : 1;
      return move(id, visibleIds.indexOf(id) + direction, handle);
    });
    handle.addEventListener("pointerdown", (event) => {
      if (saving || drag || event.button !== 0 || visibleIds.length < 2) return;
      handle.setPointerCapture(event.pointerId);
      drag = { id, pointerId: event.pointerId, target: null };
      handle.classList.add("is-dragging");
    });
    const ownsPointer = (event) => drag?.id === id && drag.pointerId === event.pointerId;
    handle.addEventListener("pointermove", (event) => {
      if (!ownsPointer(event)) return;
      const target = dropTarget(event);
      const next = target === sections.get(id) ? null : target;
      if (next === drag.target) return;
      drag.target?.classList.remove("is-drop-target");
      next?.classList.add("is-drop-target");
      drag.target = next;
    });
    const finishDrag = (event) => {
      if (!ownsPointer(event)) return;
      drag.target?.classList.remove("is-drop-target");
      drag = null;
      handle.classList.remove("is-dragging");
      if (event.type !== "pointerup") return;
      const targetId = dropTarget(event)?.getAttribute("data-provider");
      return move(id, visibleIds.indexOf(targetId), handle);
    };
    handle.addEventListener("pointerup", finishDrag);
    handle.addEventListener("pointercancel", finishDrag);
    handle.addEventListener("lostpointercapture", finishDrag);
  };
}

function providersToolbar(totalAttention, filterState) {
  const attentionBtn = el("button", {
    type: "button",
    class: `btn btn-sm providers-attention-toggle${filterState.attention ? " is-active" : ""}`,
    "aria-pressed": filterState.attention ? "true" : "false",
  },
  totalAttention > 0 ? dot("warning") : null,
  "Needs attention",
  totalAttention > 0 ? el("span", { class: "seg-count mono" }, String(totalAttention)) : null);

  attentionBtn.addEventListener("click", () => {
    location.hash = providersHash({ family: filterState.family, attention: !filterState.attention });
  });

  return el("div", { class: "providers-toolbar" }, attentionBtn);
}

function deleteConnectionControl(id, setBusy, status, onDeleted, afterDeleteHash = "#/providers") {
  let armed = false;
  const button = el("button", { class: "btn btn-danger", type: "button" }, "Delete connection");
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
      await api.deleteConnection(id);
      if (typeof onDeleted === "function") onDeleted();
      location.hash = afterDeleteHash;
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
      armed = false;
      button.textContent = "Delete connection";
    }
  });
  return button;
}

/**
 * Default connection id: "<provider>-<method>" where method is how it authenticates —
 * cli (official CLI login), token (official long-lived token), apikey or endpoint.
 * A numeric suffix avoids clashing with an existing connection.
 */
export function suggestConnectionId(provider, kind, claudeAuthMode, connections) {
  const base = (provider || "connection").trim().toLowerCase().replace(/[^a-z0-9-]+/g, "-") || "connection";
  const method = kind === "api_key" ? "apikey"
    : kind === "endpoint" ? "endpoint"
      : base === "claude" && claudeAuthMode === "secret" ? "token" : "cli";
  const taken = new Set((connections || []).map((item) => (typeof item === "string" ? item : item?.id)));
  const id = `${base}-${method}`;
  if (!taken.has(id)) return id;
  let n = 2;
  while (taken.has(`${id}-${n}`)) n += 1;
  return `${id}-${n}`;
}

function connectionForm(conn, isNew, secretNames, connections, refresh, tracker, {
  onError = null,
  defaultProvider = "",
  filterState = {},
} = {}) {
  const start = isNew ? { provider: defaultProvider || "claude", kind: "subscription", home: "default" } : conn;
  const status = el("p", { class: "error", role: "alert" });
  let claudeAuthMode = (!isNew && start.secret && !start.config_dir) ? "secret" : "config_dir";

  const idInput = el("input", {
    class: "input input-mono",
    type: "text",
    value: isNew ? "" : start.id,
    placeholder: "claude-cli",
    required: true,
    disabled: !isNew,
    "aria-label": "Connection ID",
  });
  // New connections get a "<provider>-<method>" id until the owner types their own.
  let idUserEdited = false;
  let autoId = "";
  idInput.addEventListener("input", () => {
    idUserEdited = true;
  });

  const providerOptions = [...new Set([...KNOWN_PROVIDERS, ...(start.provider ? [start.provider] : [])])];
  const providerSelect = el("select", { class: "input", "aria-label": "Provider" },
    providerOptions.map((p) => el("option", { value: p }, p)));
  providerSelect.value = start.provider || "claude";

  const kindSelect = el("select", { class: "input", "aria-label": "Kind" },
    CONNECTION_KINDS.map((k) => el("option", { value: k }, k)));
  kindSelect.value = start.kind || "subscription";

  const configDirModeBtn = el("button", {
    type: "button",
    class: "seg-btn",
    "aria-pressed": claudeAuthMode === "config_dir" ? "true" : "false",
  }, "Login on this machine (config dir)");
  const secretModeBtn = el("button", {
    type: "button",
    class: "seg-btn",
    "aria-pressed": claudeAuthMode === "secret" ? "true" : "false",
  }, "Long-lived token (secret)");
  const authModeField = el("div", { class: "field wide" },
    el("span", { class: "field-label" }, "Auth method"),
    el("div", { class: "seg-group seg-group--wrap", role: "group", "aria-label": "Auth method" },
      configDirModeBtn,
      secretModeBtn));

  const homeInput = el("input", {
    class: "input input-mono",
    type: "text",
    value: start.home || "default",
    placeholder: "default",
    "aria-label": "Home directory",
  });
  const authFileInput = el("input", {
    class: "input input-mono",
    type: "text",
    value: start.auth_file || "",
    placeholder: "/path/to/.codex/auth.json (optional)",
    "aria-label": "Auth file",
  });
  let configDirUserEdited = false;
  let autoConfigDir = "";
  const configDirInput = el("input", {
    class: "input input-mono",
    type: "text",
    value: start.config_dir || "",
    placeholder: "/srv/operator/.local/share/kotoyose/claude-host",
    "aria-label": "Config dir",
  });
  configDirInput.addEventListener("input", () => {
    configDirUserEdited = true;
  });
  const endpointInput = el("input", {
    class: "input input-mono",
    type: "text",
    value: start.endpoint || "",
    placeholder: "https://example.invalid/v1",
    "aria-label": "Endpoint URL",
  });
  const secretList = el("datalist", { id: "connection-secret-options" },
    secretNames.map((name) => el("option", { value: name })));
  const secretInput = el("input", {
    class: "input input-mono",
    type: "text",
    list: "connection-secret-options",
    value: start.secret || "",
    placeholder: "secret-name or env:VAR_NAME",
    "aria-label": "Secret",
  });

  const quotaChoices = (connections || [])
    .filter((c) => c.id !== start.id && !c.quota_from)
    .map((c) => c.id);
  if (start.quota_from && !quotaChoices.includes(start.quota_from)) {
    quotaChoices.push(start.quota_from);
  }
  const quotaFromSelect = el("select", { class: "input input-mono", "aria-label": "Quota from" },
    el("option", { value: "" }, "(none)"),
    quotaChoices.map((id) => el("option", { value: id }, id)));
  quotaFromSelect.value = start.quota_from || "";

  const homeHint = el("span", { class: "hint" });
  const homeField = pathField("Home", homeInput, { directories: true, hint: homeHint });
  const authFileField = pathField("Auth file", authFileInput, {
    wide: true,
    hint: el("span", { class: "hint" }, "Absolute path for Codex subscription auth.json"),
  });
  const configDirField = pathField("Config dir", configDirInput, {
    wide: true,
    directories: true,
    hint: el("span", { class: "hint" }, "Dedicated login home directory for official CLI login + /usage (host placement only; ~ is expanded on the server)"),
  });
  const endpointField = field("Endpoint", endpointInput, { wide: true });
  const secretField = field("Secret name or env:VAR", el("div", { class: "field-stack" }, secretInput, secretList), { wide: true });
  const quotaFromField = field("Quota from", quotaFromSelect, {
    wide: true,
    hint: el("span", { class: "hint" }, "Optional: show quota windows from another connection on the same account"),
  });
  const showQuotaInput = el("input", {
    type: "checkbox",
    checked: start.show_quota !== false,
    "aria-label": "Show quota on Dashboard",
  });
  showQuotaInput.checked = start.show_quota !== false;
  const showQuotaField = el("div", { class: "field wide" },
    el("label", { class: "check" },
      showQuotaInput,
      el("span", {}, "Show quota on Dashboard")),
    el("span", { class: "hint" }, "Also controls quota alerts. The panel always shows quota."));

  const dataPolicySelect = el("select", { class: "input", name: "data_policy", required: true },
    el("option", { value: "", disabled: true }, "Choose…"),
    el("option", { value: "no_training" }, "Not used for training"),
    el("option", { value: "trains" }, "Used for training"),
    el("option", { value: "unknown" }, "Unknown — treated as training"));
  dataPolicySelect.value = start.data_policy || "";
  const telemetryHint = el("span", { class: "hint" },
    "Antigravity also has its own telemetry setting (enable telemetry) in agy; check it matches this choice.");

  const syncVisibility = () => {
    const p = providerSelect.value.trim().toLowerCase();
    const k = kindSelect.value;
    if (isNew) {
      const suggested = suggestConnectionId(p, k, claudeAuthMode, connections);
      idInput.placeholder = suggested;
      if (!idUserEdited && idInput.value === autoId) {
        autoId = suggested;
        idInput.value = autoId;
      }
    }
    telemetryHint.hidden = p !== "antigravity";
    const claudeSub = isClaudeSubscription(p, k);
    const codexSub = k === "subscription" && p === "codex";
    const printCliSub = isPrintCliLogin(p, k);
    const otherSub = k === "subscription" && !claudeSub && !codexSub && !printCliSub;

    authModeField.hidden = !claudeSub;
    configDirField.hidden = !((claudeSub && claudeAuthMode === "config_dir") || printCliSub);
    homeField.hidden = !(codexSub || otherSub);
    homeHint.textContent = codexSub ? "Codex home directory (default: default)" : "Account profile label (default: default)";
    authFileField.hidden = !codexSub;
    endpointField.hidden = k !== "endpoint";
    secretField.hidden = !(k === "api_key" || k === "endpoint" || (claudeSub && claudeAuthMode === "secret"));

    const loginHome = PRINT_CLI_CONNECTIONS.get(p)?.loginHome;
    if (isNew && !configDirUserEdited && configDirInput.value === autoConfigDir) {
      autoConfigDir = printCliSub ? loginHome : "";
      configDirInput.value = autoConfigDir;
    }
    configDirInput.placeholder = printCliSub
      ? loginHome
      : "/srv/operator/.local/share/kotoyose/claude-host";

    configDirInput.required = Boolean(printCliSub || (claudeSub && claudeAuthMode === "config_dir"));
    endpointInput.required = !endpointField.hidden;
    secretInput.required = !secretField.hidden;

    configDirModeBtn.setAttribute("aria-pressed", claudeAuthMode === "config_dir" ? "true" : "false");
    secretModeBtn.setAttribute("aria-pressed", claudeAuthMode === "secret" ? "true" : "false");
  };
  syncVisibility();
  kindSelect.addEventListener("change", syncVisibility);
  providerSelect.addEventListener("change", syncVisibility);
  configDirModeBtn.addEventListener("click", () => {
    if (claudeAuthMode === "config_dir") return;
    claudeAuthMode = "config_dir";
    syncVisibility();
    tracker.markDirty();
  });
  secretModeBtn.addEventListener("click", () => {
    if (claudeAuthMode === "secret") return;
    claudeAuthMode = "secret";
    syncVisibility();
    tracker.markDirty();
  });

  const save = el("button", { class: "btn btn-primary", type: "submit" }, isNew ? "Create connection" : "Save changes");
  const remove = isNew ? null : deleteConnectionControl(
    start.id,
    (busy) => setBusy(busy),
    status,
    () => tracker.markClean(),
    providersHash(filterState),
  );
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((c) => { c.disabled = busy; });

  const failForm = (message) => {
    status.textContent = message;
    if (typeof onError === "function") onError();
  };

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const targetId = idInput.value.trim();
    if (!targetId) {
      failForm("Connection ID is required.");
      return;
    }
    if (!configDirField.hidden && configDirInput.required && !configDirInput.value.trim()) {
      failForm("Config dir is required.");
      return;
    }
    if (!endpointField.hidden && !endpointInput.value.trim()) {
      failForm("Endpoint is required.");
      return;
    }
    if (!secretField.hidden && !secretInput.value.trim()) {
      failForm("Secret is required.");
      return;
    }
    if (!dataPolicySelect.value) {
      failForm("Choose a data policy.");
      return;
    }
    const claudeSub = isClaudeSubscription(providerSelect.value, kindSelect.value);
    const body = connectionBody({
      provider: providerSelect.value,
      kind: kindSelect.value,
      profile: providerSelect.value === start.provider && kindSelect.value === start.kind ? start.profile : null,
      claudeAuthMode: claudeSub ? claudeAuthMode : null,
      home: homeField.hidden ? "" : homeInput.value,
      authFile: authFileField.hidden ? "" : authFileInput.value,
      configDir: configDirField.hidden ? "" : configDirInput.value,
      endpoint: endpointField.hidden ? "" : endpointInput.value,
      secret: secretField.hidden ? "" : secretInput.value,
      quotaFrom: quotaFromSelect.value,
      dataPolicy: dataPolicySelect.value,
      showQuota: showQuotaInput.checked,
    });
    setBusy(true);
    try {
      if (isNew) {
        if (targetId === NEW_CONNECTION) throw new Error('"new" is reserved.');
        await api.createConnection(targetId, body);
        tracker.markClean();
        location.hash = connectionHash(targetId, filterState);
        return;
      }
      await api.updateConnection(start.id, body);
      tracker.markClean();
      await refresh();
    } catch (failure) {
      failForm(failure.message);
      setBusy(false);
    }
  };

  const form = el("form", { class: "form", onsubmit: submit },
    el("div", { class: "form-grid" },
      field("Connection ID", idInput, { wide: true }),
      field("Provider", providerSelect),
      field("Kind", kindSelect),
      field("Data policy", dataPolicySelect, { hint: telemetryHint }),
      authModeField,
      homeField,
      authFileField,
      configDirField,
      endpointField,
      secretField,
      quotaFromField,
      showQuotaField),
    status,
    el("div", { class: "form-actions" },
      el("span", { class: "hint" }, "Never paste raw tokens here; reference a stored secret name or env:NAME."),
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function refreshConnectionControl(id, refresh, statusEl) {
  const button = el("button", { class: "btn btn-sm", type: "button" }, "Refresh now");
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    button.disabled = true;
    button.textContent = "Refreshing…";
    if (statusEl) statusEl.textContent = "";
    try {
      await api.refreshConnection(id);
      await refresh();
    } catch (failure) {
      button.disabled = false;
      button.textContent = "Refresh now";
      if (statusEl) statusEl.textContent = failure.message;
    }
  });
  return button;
}

function codexLoginSection(conn, refresh, statusEl) {
  const host = el("div", { class: "field-stack" });
  const loginButton = el("button", { class: "btn btn-sm btn-primary", type: "button" }, "Log in");
  let pollTimer = null;
  let activeSessionId = null;

  const stopPolling = () => {
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  const renderPanel = (session) => {
    host.textContent = "";
    if (!session) return;
    const statusText = el("span", { class: "mono small" }, `Status: ${session.status}`);
    const copyButton = el("button", { class: "btn btn-sm", type: "button" }, "Copy code");
    copyButton.addEventListener("click", async () => {
      try {
        await navigator.clipboard.writeText(session.user_code || "");
        copyButton.textContent = "Copied";
      } catch {
        copyButton.textContent = "Copy failed";
      }
    });
    const cancelButton = el("button", { class: "btn btn-sm btn-danger", type: "button" }, "Cancel");
    cancelButton.addEventListener("click", async () => {
      cancelButton.disabled = true;
      stopPolling();
      try {
        if (activeSessionId) {
          await api.cancelConnectionLogin(conn.id, activeSessionId);
        }
      } catch (failure) {
        if (statusEl) statusEl.textContent = failure.message;
      } finally {
        activeSessionId = null;
        loginButton.disabled = false;
        host.textContent = "";
      }
    });
    const codeRow = el("div", { class: "chips" },
      el("code", { class: "mono" }, session.user_code || ""),
      copyButton,
      session.status === "waiting" ? cancelButton : null);
    host.append(
      el("div", { class: "sub-section" },
        el("div", { class: "sub-head" },
          el("h4", { class: "sub-title" }, "Codex device-code login"),
          statusText),
        el("p", { class: "small" },
          "Open ",
          el("a", {
            href: session.verification_url,
            target: "_blank",
            rel: "noopener noreferrer",
            class: "mono",
          }, session.verification_url),
          " and enter code:"),
        codeRow,
        session.expires_at ? el("p", { class: "hint" }, `Expires: ${timeLabel(session.expires_at)}`) : null,
        session.error ? el("p", { class: "error small" }, session.error) : null),
    );
  };

  const startLogin = async () => {
    loginButton.disabled = true;
    if (statusEl) statusEl.textContent = "";
    stopPolling();
    try {
      const started = await api.startConnectionLogin(conn.id);
      activeSessionId = started.session_id;
      renderPanel({ ...started, status: "waiting", error: null });
      pollTimer = setInterval(async () => {
        if (!host.isConnected || !activeSessionId) {
          stopPolling();
          return;
        }
        try {
          const state = await api.pollConnectionLogin(conn.id, activeSessionId);
          renderPanel(state);
          if (state.status !== "waiting") {
            stopPolling();
            activeSessionId = null;
            loginButton.disabled = false;
            if (state.status === "completed") {
              await refresh();
            }
          }
        } catch (failure) {
          stopPolling();
          activeSessionId = null;
          loginButton.disabled = false;
          if (statusEl) statusEl.textContent = failure.message;
        }
      }, 2000);
    } catch (failure) {
      loginButton.disabled = false;
      if (statusEl) statusEl.textContent = failure.message;
    }
  };

  loginButton.addEventListener("click", startLogin);

  return { loginButton, panelHost: host, start: startLogin };
}

function claudeAuthFlow(conn, mode, refresh, statusEl) {
  const isCliLogin = mode === "cli_login";
  const isLogin = mode === "login";
  const isSetupToken = mode === "setup_token";
  const title = isCliLogin
    ? "CLI login"
    : (isLogin ? "Claude CLI login" : "Issue Claude setup-token");
  const loadingMessage = isCliLogin ? "Starting CLI…" : "Starting Claude CLI…";
  const submittingMessage = isCliLogin ? "Submitting code to CLI…" : "Submitting code to Claude CLI…";
  const pollIntervalMs = isCliLogin ? 1500 : 3000;

  const host = el("div", { class: "field-stack" });
  const startButton = el("button", {
    class: "btn btn-sm btn-primary",
    type: "button",
  }, isSetupToken ? "Issue token" : "Log in");
  let pollTimer = null;
  let countdownTimer = null;
  let activeSessionId = null;
  let submitting = false;

  const stopCountdown = () => {
    if (countdownTimer !== null) {
      clearInterval(countdownTimer);
      countdownTimer = null;
    }
  };

  const stopPolling = () => {
    stopCountdown();
    if (pollTimer !== null) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
  };

  const startApi = () => (
    isCliLogin
      ? api.startCliLogin(conn.id)
      : (isLogin ? api.startClaudeLogin(conn.id) : api.startClaudeSetupToken(conn.id))
  );
  const pollApi = (sid) => (
    isCliLogin
      ? api.pollCliLogin(conn.id, sid)
      : (isLogin ? api.pollClaudeLogin(conn.id, sid) : api.pollClaudeSetupToken(conn.id, sid))
  );
  const submitApi = (sid, code) => (
    isCliLogin
      ? api.submitCliLoginCode(conn.id, sid, code)
      : (isLogin ? api.submitClaudeLoginCode(conn.id, sid, code) : api.submitClaudeSetupTokenCode(conn.id, sid, code))
  );
  const cancelApi = (sid) => (
    isCliLogin
      ? api.cancelCliLogin(conn.id, sid)
      : (isLogin ? api.cancelClaudeLogin(conn.id, sid) : api.cancelClaudeSetupToken(conn.id, sid))
  );

  const renderLoading = (message) => {
    host.textContent = "";
    host.append(el("div", { class: "sub-section" },
      el("div", { class: "sub-head" },
        el("h4", { class: "sub-title" }, title),
        el("span", { class: "mono small" }, "Starting")),
      el("p", { class: "small muted", role: "status" }, message)));
  };

  const pollOnce = async () => {
    if (!host.isConnected || !activeSessionId || submitting) return;
    try {
      const st = await pollApi(activeSessionId);
      if (st.state !== "waiting_for_code" && st.state !== "waiting_for_browser") {
        stopPolling();
        activeSessionId = null;
        startButton.disabled = false;
        renderPanel(st);
      }
    } catch (_err) {
      stopPolling();
      activeSessionId = null;
      startButton.disabled = false;
    }
  };

  const renderPanel = (session) => {
    stopCountdown();
    host.textContent = "";
    if (!session) return;
    const stateLabel = session.state === "waiting_for_code"
      ? "Waiting for code"
      : session.state === "waiting_for_browser"
        ? "Waiting for browser sign-in"
        : session.state === "submitting"
        ? "Submitting"
        : session.state === "completed"
          ? "Completed"
          : session.state === "expired"
            ? "Expired"
            : session.state === "cancelled"
              ? "Cancelled"
              : "Failed";
    const statusText = el("span", { class: "mono small" }, `Status: ${stateLabel}`);
    const progressEl = el("p", { class: "small muted", role: "status" });
    const errorEl = el("p", { class: "error small", role: "alert" }, session.error || "");

    const bodyNodes = [];
    const cancelBtn = el("button", { class: "btn btn-sm btn-danger", type: "button" }, "Cancel");
    cancelBtn.addEventListener("click", async () => {
      cancelBtn.disabled = true;
      stopPolling();
      const sid = activeSessionId;
      activeSessionId = null;
      try {
        if (sid) await cancelApi(sid);
      } catch (failure) {
        if (statusEl) statusEl.textContent = failure.message;
      } finally {
        startButton.disabled = false;
        host.textContent = "";
      }
    });

    if (session.state === "waiting_for_browser" && session.url) {
      if (session.flow === "device_code" && session.user_code) {
        const copyBtn = el("button", { class: "btn btn-sm", type: "button" }, "Copy code");
        copyBtn.addEventListener("click", async () => {
          try {
            await navigator.clipboard.writeText(session.user_code);
            progressEl.textContent = "Code copied.";
          } catch (_err) {
            errorEl.textContent = "Could not copy code. Select the code to copy it manually.";
          }
        });
        bodyNodes.push(el("div", { class: "chips" },
          el("strong", { class: "mono" }, session.user_code), copyBtn));
      }
      bodyNodes.push(
        el("div", { class: "chips" },
          el("a", {
            class: "btn btn-sm btn-primary",
            href: session.url,
            target: "_blank",
            rel: "noopener noreferrer",
          }, "Open sign-in page"),
          cancelBtn),
        el("p", { class: "small muted", role: "status" }, "Waiting for the CLI to confirm sign-in…"),
        progressEl,
      );
    } else if (session.state === "waiting_for_code" && session.url && (!isCliLogin || (session.flow ?? "paste_code") === "paste_code")) {
      const codeInput = el("input", {
        class: "input input-mono",
        type: "text",
        autocomplete: "off",
        spellcheck: "false",
        value: "",
        placeholder: "Paste authorization code",
        "aria-label": "Authorization code",
      });
      const submitBtn = el("button", { class: "btn btn-sm btn-primary", type: "submit" }, "Submit");
      const codeForm = el("form", {
        class: "form",
        onsubmit: async (event) => {
          event.preventDefault();
          errorEl.textContent = "";
          const code = codeInput.value.trim();
          if (!code) {
            errorEl.textContent = "Paste the code shown on the sign-in page first.";
            return;
          }
          submitting = true;
          stopPolling();
          codeInput.disabled = true;
          submitBtn.disabled = true;
          statusText.textContent = "Status: Submitting";
          progressEl.textContent = submittingMessage;
          const sid = activeSessionId;
          try {
            const res = await submitApi(sid, code);
            codeInput.value = "";
            submitting = false;
            activeSessionId = null;
            startButton.disabled = false;
            renderPanel({ ...session, ...res });
          } catch (failure) {
            codeInput.value = "";
            submitting = false;
            if (failure.status === 422) {
              codeInput.disabled = false;
              submitBtn.disabled = false;
              statusText.textContent = "Status: Waiting for code";
              progressEl.textContent = "";
              errorEl.textContent = failure.message;
            } else {
              activeSessionId = null;
              startButton.disabled = false;
              renderPanel({ ...session, state: "failed", error: failure.message });
            }
          }
        },
      },
      field("Step 2: Paste code", codeInput, { wide: true }),
      el("div", { class: "chips" }, submitBtn, cancelBtn));

      let expiryHint = null;
      const expiresMs = session.expires_at ? Date.parse(session.expires_at) : NaN;
      if (isCliLogin && Number.isFinite(expiresMs)) {
        const formatCountdown = () => {
          const sec = Math.max(0, Math.ceil((expiresMs - Date.now()) / 1000));
          return `Expires in ${sec}s`;
        };
        const countdownEl = el("p", { class: "hint mono", role: "timer" }, formatCountdown());
        expiryHint = countdownEl;
        countdownTimer = setInterval(() => {
          if (!countdownEl.isConnected || !host.isConnected || !activeSessionId || submitting) {
            stopCountdown();
            return;
          }
          const sec = Math.max(0, Math.ceil((expiresMs - Date.now()) / 1000));
          countdownEl.textContent = `Expires in ${sec}s`;
          if (sec <= 0) {
            stopCountdown();
            void pollOnce();
          }
        }, 1000);
      } else if (session.expires_at) {
        expiryHint = el("p", { class: "hint" }, `Expires: ${timeLabel(session.expires_at)}`);
      }

      bodyNodes.push(
        el("p", { class: "small" },
          "Step 1: ",
          el("a", {
            class: "btn btn-sm btn-primary",
            href: session.url,
            target: "_blank",
            rel: "noopener noreferrer",
          }, "Open sign-in page")),
        expiryHint,
        codeForm,
        progressEl,
      );
    } else if (session.state === "completed") {
      const doneBtn = el("button", {
        class: "btn btn-sm btn-primary",
        type: "button",
        onclick: async () => {
          doneBtn.disabled = true;
          await refresh();
        },
      }, "Done");
      const summaryParts = [];
      if (isCliLogin) {
        summaryParts.push("Signed in.");
      } else if (isLogin) {
        summaryParts.push(session.email ? `Signed in as ${session.email}.` : "Signed in.");
        if (session.plan) summaryParts.push(`Plan: ${session.plan}.`);
        if (session.login_expires_at) summaryParts.push(`Expires: ${timeLabel(session.login_expires_at)}.`);
      } else {
        summaryParts.push(`Saved setup-token to secret '${session.secret}'.`);
        if (session.updated_at) summaryParts.push(`Updated: ${timeLabel(session.updated_at)}.`);
      }
      bodyNodes.push(
        el("p", { class: "small", role: "status" }, summaryParts.join(" ")),
        el("div", { class: "chips" }, doneBtn),
      );
    } else {
      const startAgainBtn = session.state === "expired"
        ? el("button", {
            class: "btn btn-sm btn-primary",
            type: "button",
            onclick: () => {
              void startFlow();
            },
          }, "Start again")
        : null;
      const closeBtn = el("button", {
        class: "btn btn-sm",
        type: "button",
        onclick: () => {
          host.textContent = "";
        },
      }, "Close");
      bodyNodes.push(el("div", { class: "chips" }, startAgainBtn, closeBtn));
    }

    host.append(
      el("div", { class: "sub-section" },
        el("div", { class: "sub-head" },
          el("h4", { class: "sub-title" }, title),
          statusText),
        ...bodyNodes,
        errorEl),
    );
  };

  const startFlow = async () => {
    startButton.disabled = true;
    if (statusEl) statusEl.textContent = "";
    stopPolling();
    renderLoading(loadingMessage);
    try {
      const started = await startApi();
      activeSessionId = started.session_id;
      renderPanel({ state: "waiting_for_code", ...started, error: null });
      pollTimer = setInterval(() => {
        if (!host.isConnected || !activeSessionId) {
          stopPolling();
          return;
        }
        void pollOnce();
      }, pollIntervalMs);
    } catch (failure) {
      startButton.disabled = false;
      host.textContent = "";
      if (statusEl) statusEl.textContent = failure.message;
    }
  };

  startButton.addEventListener("click", startFlow);

  return { button: startButton, panelHost: host, start: startFlow };
}

function claudeTokenSection(conn, setSecretNames, refresh, tracker) {
  const isEnv = Boolean(conn.secret && conn.secret.startsWith("env:"));
  const configured = Boolean(conn.secret && !isEnv && setSecretNames.has(conn.secret));
  const section = el("div", { class: "sub-section" },
    el("div", { class: "sub-head" },
      el("h3", { class: "sub-title" }, "Claude subscription token"),
      el("span", { class: "row-status mono small" },
        dot(isEnv ? "positive" : configured ? "positive" : "warning"),
        isEnv
          ? `Env (${conn.secret})`
          : configured
            ? `Secret configured (${conn.secret})`
            : `Missing (${conn.secret || "none"})`)),
    el("p", { class: "small" },
      isEnv
        ? "Reads token from server environment."
        : "Use Issue token above to generate and store a token via the official Claude Code CLI, or paste one manually below."));

  if (isEnv) {
    section.append(el("p", { class: "hint" },
      `This connection reads ${conn.secret.slice(4)} from the server environment. Switch Secret to a store name above to manage it here.`));
    return section;
  }
  if (!conn.secret) {
    section.append(el("p", { class: "error small" }, "Configure a secret name above before saving a token."));
    return section;
  }

  const tokenInput = el("input", {
    class: "input input-mono",
    type: "password",
    autocomplete: "off",
    value: "",
    placeholder: `Paste claude setup-token value for ${conn.secret}`,
    "aria-label": "Claude setup-token value",
  });
  const feedback = el("p", { class: "small", role: "status" });
  const saveButton = el("button", { class: "btn btn-primary btn-sm", type: "submit" }, "Save token");
  const form = el("form", {
    class: "form",
    onsubmit: async (event) => {
      event.preventDefault();
      feedback.className = "small";
      feedback.textContent = "";
      const tokenValue = tokenInput.value.trim();
      if (!tokenValue) {
        feedback.className = "error small";
        feedback.textContent = "Paste a token from `claude setup-token` first.";
        return;
      }
      saveButton.disabled = true;
      tokenInput.disabled = true;
      try {
        await api.putSecret(conn.secret, tokenValue, "Claude subscription OAuth token");
        tokenInput.value = "";
        tracker.markClean();
        feedback.textContent = `Saved to secret '${conn.secret}'.`;
        await refresh();
      } catch (failure) {
        feedback.className = "error small";
        feedback.textContent = failure.message;
      } finally {
        tokenInput.value = "";
        saveButton.disabled = false;
        tokenInput.disabled = false;
      }
    },
  },
  field(`Write-only token -> secret '${conn.secret}'`, tokenInput, { wide: true }),
  feedback,
  el("div", { class: "form-actions" },
    el("span", { class: "hint" }, "Fallback (terminal): claude setup-token. Saved values are never displayed back."),
    saveButton));
  tracker.attach(form);
  section.append(form);
  return section;
}

function cliConfigDirSection(conn, state, { isPrintCli = false } = {}) {
  const printCli = PRINT_CLI_CONNECTIONS.get(conn.provider.toLowerCase());
  const cursor = isPrintCli && printCli?.profile === "cursor";
  const grok = isPrintCli && printCli?.profile === "grok";
  const quote = (value) => "'" + value.replaceAll("'", "'\"'\"'") + "'";
  const cmd = cursor
    ? `HOME=${quote(conn.config_dir)} CURSOR_CONFIG_DIR=${quote(conn.config_dir + "/.cursor")} NO_OPEN_BROWSER=1 agent login`
    : isPrintCli
    ? `HOME=${conn.config_dir} ${printCli.binary}`
    : `CLAUDE_CONFIG_DIR=${conn.config_dir} claude`;
  const heading = isPrintCli ? "CLI login" : "Claude CLI login";
  const intro = isPrintCli
    ? "Click Log in above to sign in through the official CLI."
    : "Click Log in above to sign in through the official Claude Code CLI.";
  const slashHint = isPrintCli ? "/usage" : "/login";
  const footer = isPrintCli
    ? "The official CLI stores credentials in its dedicated login home."
    : "The official Claude Code CLI manages credentials inside its own config directory; Kotoyose only acts as a terminal for login and runs /usage.";
  const copyFeedback = el("span", { class: "hint mono", role: "status" });
  const copyBtn = el("button", {
    class: "btn btn-sm",
    type: "button",
    onclick: async () => {
      try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(cmd);
          copyFeedback.textContent = "Copied";
        } else {
          copyFeedback.textContent = "Clipboard unavailable";
        }
      } catch (_err) {
        copyFeedback.textContent = "Copy failed";
      }
    },
  }, "Copy");
  return el("div", { class: "sub-section" },
    el("div", { class: "sub-head" },
      el("h3", { class: "sub-title" }, heading),
      el("span", { class: "row-status mono small" },
        dot(authTone(state.auth)),
        authLabel(state))),
    el("p", { class: "small" }, intro),
    grok ? null : el("p", { class: "hint" },
      "Fallback (terminal): ",
      el("code", { class: "mono" }, cmd),
      cursor ? null : " then ",
      cursor ? null : el("code", { class: "mono" }, slashHint),
      " ",
      copyBtn,
      " ",
      copyFeedback),
    el("p", { class: "hint" }, footer));
}

function openConnectionPeek(connections, selectedConnId, secretNames, setSecretNames, refresh, filterState = {}) {
  if (selectedConnId === null) return;
  const backHash = providersHash(filterState);
  const onClose = () => { location.hash = backHash; };
  const isNew = selectedConnId === NEW_CONNECTION;
  const conn = isNew ? null : connections.find((c) => c.id === selectedConnId);
  if (!isNew && !conn) {
    openPeek({
      titleMono: true,
      title: selectedConnId,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Connection "${selectedConnId}" was not found.`),
        el("a", { href: backHash }, "Back to providers")),
      onClose,
    });
    return;
  }
  const routes = isNew ? [] : (conn.routes || []);
  const formTracker = createDirtyTracker();
  const tokenTracker = createDirtyTracker();
  let settingsDisclosure = null;
  const form = connectionForm(conn, isNew, secretNames, connections, refresh, formTracker, {
    defaultProvider: filterState.defaultProvider || "",
    filterState,
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
  let headerBadge = null;
  if (!isNew) {
    const state = conn.state || {};
    const badge = connectionAuthBadge(conn, setSecretNames);
    headerBadge = badge;
    const checkStatus = el("p", { class: "error small", role: "alert" });
    const isCodexSub = conn.provider === "codex" && conn.kind === "subscription";
    const isClaudeSub = isClaudeSubscription(conn.provider, conn.kind);
    const usesConfigDir = Boolean(isClaudeSub && conn.config_dir);
    const usesPrintCliLogin = Boolean(isPrintCliLogin(conn.provider, conn.kind) && conn.config_dir);
    const usesSecretStore = Boolean(isClaudeSub && !usesConfigDir && conn.secret && !conn.secret.startsWith("env:"));
    const codexLogin = isCodexSub ? codexLoginSection(conn, refresh, checkStatus) : null;
    const claudeFlow = usesConfigDir
      ? claudeAuthFlow(conn, "login", refresh, checkStatus)
      : (usesSecretStore
          ? claudeAuthFlow(conn, "setup_token", refresh, checkStatus)
          : (usesPrintCliLogin ? claudeAuthFlow(conn, "cli_login", refresh, checkStatus) : null));

    const headControls = el("div", { class: "chips" },
      probeConnectionControl(conn.id, refresh, checkStatus),
      isCodexSub ? refreshConnectionControl(conn.id, refresh, checkStatus) : null,
      codexLogin ? codexLogin.loginButton : null,
      claudeFlow ? claudeFlow.button : null);
    const claudeSecretStatus = isClaudeSub && !usesConfigDir
      ? [
        el("dt", {}, "Secret configured"),
        el("dd", { class: "mono" },
          conn.secret && conn.secret.startsWith("env:")
            ? `Yes (${conn.secret})`
            : conn.secret && setSecretNames.has(conn.secret)
              ? `Yes (${conn.secret})`
              : `No (${conn.secret || "unconfigured"})`),
      ]
      : null;
    const claudeDirStatus = (usesConfigDir || usesPrintCliLogin)
      ? [el("dt", {}, "Config dir"), el("dd", { class: "mono" }, conn.config_dir)]
      : null;
    const quotaVia = conn.quota_from || state.quota_via || null;
    const effectivePolicy = conn.effective_data_policy || conn.data_policy || "unknown";
    const dataPolicy = trainingRiskLabel(effectivePolicy) || "Not used for training";
    sections.push(el("div", { class: "sub-section" },
      el("div", { class: "sub-head" },
        el("h3", { class: "sub-title" }, "Status"),
        headControls),
      checkStatus,
      codexLogin ? codexLogin.panelHost : null,
      claudeFlow ? claudeFlow.panelHost : null,
      el("dl", { class: "kv" },
        el("dt", {}, "Auth"),
        el("dd", { class: "mono" },
          el("span", { class: "row-status" }, dot(badge.tone), badge.label)),
        claudeSecretStatus,
        claudeDirStatus,
        el("dt", {}, "Data policy"),
        el("dd", {}, dataPolicy),
        el("dt", {}, "Expires at"),
        el("dd", { class: "mono" }, state.expires_at ? timeLabel(state.expires_at) : "—"),
        el("dt", {}, "Plan"),
        el("dd", { class: "mono" }, state.plan || "—"),
        el("dt", {}, "CLI version"),
        el("dd", {}, cliVersionLabel(state.cli_version) || "—"),
        el("dt", {}, "Quota source"),
        el("dd", { class: "mono" }, quotaVia ? `${state.quota_source || "—"} (via ${quotaVia})` : (state.quota_source || "—")),
        el("dt", {}, "Last checked"),
        el("dd", { class: "mono" }, state.checked_at ? timeLabel(state.checked_at) : "Never"),
        state.last_error
          ? [el("dt", {}, "Last error"), el("dd", { class: "error small" }, state.last_error)]
          : null),
      renderQuotaRows(state.quota, { via: quotaVia })));
    if (usesConfigDir) {
      sections.push(cliConfigDirSection(conn, state, { isPrintCli: false }));
    } else if (usesPrintCliLogin) {
      sections.push(cliConfigDirSection(conn, state, { isPrintCli: true }));
    } else if (isClaudeSub) {
      sections.push(claudeTokenSection(conn, setSecretNames, refresh, tokenTracker));
    }
    sections.push(el("div", { class: "sub-section" },
      el("div", { class: "sub-head" },
        el("h3", { class: "sub-title" }, "Routes using this connection"),
        el("span", { class: "mono small muted" }, String(routes.length))),
      routes.length
        ? el("div", { class: "chips" }, routes.map((r) => el("a", { class: "chip", href: `#/routes/${encodeURIComponent(r)}` }, r)))
        : el("span", { class: "hint" }, "No route references this connection.")));
  }
  sections.push(settingsDisclosure.details);
  openPeek({
    titleMono: true,
    title: isNew ? "New connection" : conn.id,
    subtitle: isNew ? "Account login, API key secret, or custom endpoint" : `${conn.provider} · ${conn.kind}`,
    badge: headerBadge,
    body: sections,
    onClose,
    isDirty: () => formTracker.isDirty() || tokenTracker.isDirty(),
  });
}

function collectDrivers(config, machine) {
  const names = new Set([
    ...Object.keys((config.catalog && config.catalog.drivers) || {}),
    ...Object.keys((machine && machine.drivers) || {}),
  ]);
  for (const route of Object.values(config.routes || {})) {
    if (route && route.driver) names.add(route.driver);
  }
  return [...names].sort();
}

function routesForDriver(config, driver) {
  const sanctions = config.route_sanctions || {};
  return Object.entries(config.routes || {})
    .filter(([, route]) => route && route.driver === driver)
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([name, route]) => ({
      name,
      route,
      sanction: sanctions[name] || null,
    }));
}

function driverPlacesLabel(catalogEntry) {
  if (!catalogEntry || !catalogEntry.places) return "—";
  const places = Object.keys(catalogEntry.places).sort();
  return places.length ? places.join(", ") : "—";
}

function driverStatusDot(install, routes, machineOk) {
  if (routes.some((r) => r.sanction && r.sanction.sanction === "needs_ack")) {
    return dot("ring-warning");
  }
  if (install && install.cli_binary) {
    return dot("positive");
  }
  if (machineOk && routes.length > 0) {
    return dot("positive");
  }
  return dot("");
}

function renderRoutesSection(routes, filterState = {}) {
  return el("div", { class: "sub-section" },
    el("div", { class: "sub-head" },
      el("h3", { class: "sub-title" }, "Routes using this driver"),
      el("span", { class: "mono small muted" }, String(routes.length))),
    routes.length === 0
      ? el("p", { class: "hint" }, "No routes reference this driver yet.")
      : el("div", { class: "driver-list" }, routes.map(({ name, route, sanction }) => {
        const status = sanctionOf(sanction && sanction.sanction);
        return el("div", { class: "sub-card" },
          el("div", { class: "sub-card-head" },
            el("a", { class: "mono", href: `#/routes/${encodeURIComponent(name)}` }, name),
            el("span", { class: "row-status small" }, dot(status.tone), status.label)),
          el("dl", { class: "kv" },
            el("dt", {}, "Model"),
            el("dd", { class: "mono" }, route.model || "—"),
            el("dt", {}, "Connection"),
            el("dd", { class: "mono" }, route.connection
              ? el("a", { href: connectionHash(route.connection, filterState) }, route.connection)
              : "—")),
          sanction && sanction.ack_stale
            ? el("p", { class: "error" }, "Acknowledgement is out of date after route or connection edits.")
            : null);
      })));
}

function openDriverPeek(drivers, selected, config, machine, machineError, filterState = {}) {
  if (selected === null) return;
  const backHash = providersHash(filterState);
  const onClose = () => { location.hash = backHash; };
  if (!drivers.includes(selected)) {
    openPeek({
      titleMono: true,
      title: selected,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Driver "${selected}" was not found.`),
        el("a", { href: backHash }, "Back to providers")),
      onClose,
    });
    return;
  }
  const catalogEntry = ((config.catalog && config.catalog.drivers) || {})[selected];
  const install = ((machine && machine.drivers) || {})[selected] || null;
  const routes = routesForDriver(config, selected);
  const subtitleParts = [];
  if (catalogEntry && catalogEntry.provider) subtitleParts.push(`provider: ${catalogEntry.provider}`);
  const places = driverPlacesLabel(catalogEntry);
  if (places !== "—") subtitleParts.push(`places: ${places}`);

  openPeek({
    titleMono: true,
    title: selected,
    subtitle: subtitleParts.join(" · ") || "Driver",
    body: [
      renderInstallSection(install, machine, machineError),
      renderRoutesSection(routes, filterState),
    ],
    onClose,
  });
}

function groupByProvider(connections, drivers, tools, config, setSecretNames) {
  const entries = new Map();
  function entryFor(id) {
    if (!entries.has(id)) entries.set(id, { id, connections: [], drivers: new Map(), tools: [] });
    return entries.get(id);
  }
  for (const id of KNOWN_PROVIDERS) entryFor(id);
  for (const conn of connections) {
    const badge = connectionAuthBadge(conn, setSecretNames);
    if (isExpiredByTimestamp(conn.state)) {
      badge.tone = "negative";
      badge.label = "expired";
    }
    entryFor(conn.provider).connections.push({
      ...conn, badge, attention: connectionAttention(conn, setSecretNames),
    });
  }
  for (const tool of tools) entryFor(tool.provider).tools.push(tool);
  for (const driver of drivers) {
    const routes = routesForDriver(config, driver);
    if (driver === "cli.print") {
      const providersByConnection = new Map(connections.map((conn) => [conn.id, conn.provider]));
      for (const route of routes) {
        const entry = entries.get(providersByConnection.get(route.route.connection));
        if (!entry) continue;
        if (!entry.drivers.has(driver)) entry.drivers.set(driver, []);
        entry.drivers.get(driver).push(route);
      }
      continue;
    }
    const provider = config.catalog?.drivers?.[driver]?.provider;
    const entry = provider ? entryFor(provider) : entries.get(driver.split(".")[0]);
    entry?.drivers.set(driver, routes);
  }
  for (const entry of entries.values()) {
    entry.connections.sort((a, b) =>
      Number(b.attention.needsAttention) - Number(a.attention.needsAttention)
      || String(a.id).localeCompare(String(b.id)));
  }
  return entries;
}

export async function renderProviders(main, { config, rest, query, refresh }) {
  const selectedConnId = rest[0] === "connection" ? (rest[1] || null) : null;
  const selectedDriver = rest[0] && rest[0] !== "connection" ? rest[0] : null;
  const filterState = parseFilterFromQuery(query);

  const [machineResult, connResult, secretResult, toolsResult] = await Promise.allSettled([
    api.machine(),
    api.connections(),
    api.secrets(),
    api.tools(),
  ]);
  if (!main.isConnected) return;

  const machine = machineResult.status === "fulfilled" ? machineResult.value : null;
  const machineError = machineResult.status === "rejected" ? machineResult.reason : null;
  const connResponse = connResult.status === "fulfilled" ? connResult.value : null;
  const secretsList = secretResult.status === "fulfilled"
    ? ((secretResult.value && secretResult.value.secrets) || [])
    : [];
  const tools = toolsResult.status === "fulfilled" ? (toolsResult.value?.tools || []) : [];
  const secretNames = secretsList.map((s) => s.name);
  const setSecretNames = new Set(secretsList.filter((s) => s.set !== false).map((s) => s.name));

  const connections = normalizeConnections(config, connResponse);
  const drivers = collectDrivers(config, machine);
  const entries = groupByProvider(connections, drivers, tools, config, setSecretNames);
  const orderedIds = orderedProviderIds(entries.keys(), config)
    .filter((id) => id !== "print_cli" && !drivers.includes(id));
  const totalAttention = [...entries.values()].reduce((total, entry) =>
    total + entry.connections.filter((conn) => conn.attention.needsAttention).length, 0);
  const collapsedProviders = loadCollapsedProviders();
  const visibleIds = orderedIds.filter((id) => {
    const entry = entries.get(id);
    return entry
      && (filterState.family === "all" || familyForProvider(id) === filterState.family)
      && (!filterState.attention || entry.connections.some((conn) => conn.attention.needsAttention));
  });
  const list = el("div", { class: "families-list" });
  const orderStatus = el("p", { class: "error provider-order-error", role: "alert" });
  const reorder = providerReorder(list, orderStatus, orderedIds, visibleIds);
  const renderedSections = visibleIds.map((id) =>
    providerSection(entries.get(id), config, machine, !machineError, {
      selectedConnId, selectedDriver, filterState, collapsedProviders, reorder,
    }));

  const newConnFilter = { family: filterState.family, attention: filterState.attention };
  if (filterState.family !== "all") {
    const selectedFam = PROVIDER_FAMILIES.find((f) => f.key === filterState.family);
    if (selectedFam && selectedFam.defaultProvider) {
      newConnFilter.provider = selectedFam.defaultProvider;
    }
  }

  const bodyNodes = renderedSections.length > 0
    ? renderedSections
    : [
      el("div", { class: "card providers-empty-filter" },
        el("div", { class: "card-body providers-empty-filter-body" },
          el("span", { class: "muted" }, filterState.attention
            ? "No connections need attention."
            : "No providers match this filter."),
          el("a", { class: "btn btn-sm", href: providersHash({ family: filterState.family, attention: false }) }, "Show all"))),
    ];

  list.append(...bodyNodes);
  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Providers"),
        el("p", { class: "page-sub" },
          "Manage account logins, API keys, and endpoints as Connections, and inspect installed machine drivers.")),
      el("a", { class: "btn btn-primary", href: connectionHash(NEW_CONNECTION, newConnFilter) }, "New connection")),
    providersToolbar(totalAttention, filterState),
    orderStatus,
    list,
  );

  if (selectedConnId !== null) {
    openConnectionPeek(connections, selectedConnId, secretNames, setSecretNames, refresh, filterState);
  } else if (selectedDriver !== null) {
    openDriverPeek(drivers, selectedDriver, config, machine, machineError, filterState);
  }
}
