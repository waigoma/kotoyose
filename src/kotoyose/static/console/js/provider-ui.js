import { dot, el } from "./dom.js";
import { api } from "./api.js";
import { authLabel, authTone } from "./format.js";

const CHILD_PRESENCE = {
  waiting: { tone: "", label: "Waiting for child" },
  disconnected: { tone: "warning", label: "Disconnected" },
  revoked: { tone: "negative", label: "Revoked" },
  not_enabled: { tone: "warning", label: "Not enabled on child" },
  not_enrolled: { tone: "warning", label: "Child not enrolled" },
  online: { tone: "positive", label: "Online" },
};

function lastSeen(heartbeat) {
  const seconds = Math.floor((Date.now() - Date.parse(heartbeat)) / 1000);
  if (!Number.isFinite(seconds)) return null;
  if (seconds < 60) return "last seen just now";
  if (seconds < 3600) return `last seen ${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `last seen ${Math.floor(seconds / 3600)}h ago`;
  return `last seen ${Math.floor(seconds / 86400)}d ago`;
}

export function childPresenceBadge(state, heartbeat) {
  const badge = CHILD_PRESENCE[state];
  if (!badge) return null;
  return { ...badge, detail: state === "disconnected" ? lastSeen(heartbeat) : null };
}

export function childConnectionBadge(state) {
  if (state?.child_state !== "online") {
    return childPresenceBadge(state?.child_state, state?.last_heartbeat);
  }
  if (state.auth === "unknown") {
    return state.last_error
      ? { tone: "warning", label: "unknown", detail: state.last_error }
      : { tone: "", label: "Login not checked" };
  }
  return {
    tone: authTone(state.auth),
    label: state.auth === "logged_out" ? "Logged out" : authLabel(state),
  };
}

export function statusChip(badge) {
  if (!badge) return null;
  const tone = badge.tone ? ` is-${badge.tone}` : "";
  return el("span", { class: `state-chip mono small${tone}${badge.detail ? " has-detail" : ""}`, title: badge.detail },
    dot(badge.tone), badge.label,
    badge.detail ? el("span", { class: "muted" }, `· ${badge.detail}`) : null);
}

export async function loadChildConnectionBadges(config) {
  if (!Object.values(config.connections || {}).some((conn) => conn.on?.startsWith("child:"))) {
    return new Map();
  }
  try {
    const { connections } = await api.connections();
    return new Map(connections.map((conn) => [conn.id, childConnectionBadge(conn.state)]));
  } catch (_error) {
    return new Map();
  }
}

export function aliasChildBadge(config, preset, childBadges) {
  return childBadges.get(config.routes?.[preset?.route]?.connection);
}

export const PROVIDER_NAMES = new Map([
  ["claude", ["Anthropic", "Claude"]],
  ["codex", ["OpenAI", "Codex"]],
  ["openai_compat", ["OpenAI-compatible", ""]],
  ["antigravity", ["Google", "Antigravity"]],
  ["cursor", ["Cursor", "Cursor"]],
  ["grok", ["xAI", "Grok Build"]],
]);
export const KNOWN_PROVIDERS = [...PROVIDER_NAMES.keys()];
// One colour per company: the monogram tile is the only colour on a provider panel.
export const PROVIDER_TONES = new Map([
  ["claude", "claude"], ["codex", "openai"], ["antigravity", "google"],
  ["cursor", "cursor"], ["grok", "xai"],
]);

const API_PROVIDERS = new Map([
  ["anthropic", ["Anthropic", "claude"]],
  ["openai", ["OpenAI", "openai"]],
]);

export function orderedProviderIds(ids, config) {
  return [...new Set([...(config.settings?.provider_order || []), ...ids])];
}

export function providerOf(connectionOrRoute, config) {
  const item = connectionOrRoute || {};
  const provider = config.connections?.[item.connection]?.provider
    || (item.driver ? config.catalog?.drivers?.[item.driver]?.provider || item.driver.split(".")[0] : item.provider);
  return PROVIDER_NAMES.has(provider) || API_PROVIDERS.has(provider) ? provider : "other";
}

function providerIdentity(id, preserveCustomLabel) {
  const [apiCompany, apiTone] = !preserveCustomLabel && API_PROVIDERS.get(id) || [];
  const [company, product] = PROVIDER_NAMES.get(id)
    || [apiCompany || (!preserveCustomLabel && id === "other" ? "Other" : id), ""];
  return { company, product, tone: PROVIDER_TONES.get(id) || apiTone || "other" };
}

export function providerMark(id, statusTone, statusLabel, { preserveCustomLabel = false } = {}) {
  const { company, tone } = providerIdentity(id, preserveCustomLabel);
  return el("span", { class: `provider-mark is-${tone}` },
    el("span", { "aria-hidden": "true" }, (company || id).slice(0, 1).toUpperCase()),
    statusLabel ? el("span", {
      class: "provider-mark-status", role: "img", "aria-label": statusLabel, title: statusLabel,
    }, dot(statusTone)) : null);
}

export function providerPanelHead(id, countLabel, {
  toggleProps = null, chevron = null, statusTone, statusLabel, before, after,
} = {}) {
  const preserveCustomLabel = Boolean(toggleProps);
  const { company, product } = providerIdentity(id, preserveCustomLabel);
  const content = [
    chevron,
    providerMark(id, statusTone, statusLabel, { preserveCustomLabel }),
    el("span", { class: "provider-title" },
      el("span", { class: "family-name" }, company),
      product && product !== company ? el("span", { class: "provider-product" }, product) : null),
    el("span", { class: "family-counts" }, countLabel),
  ];
  const toggle = toggleProps ? el("button", { type: "button", class: "family-toggle", ...toggleProps }, content) : null;
  const label = product && product !== company ? `${company} ${product}` : company;
  return { head: el("div", { class: "family-head" }, before, toggle || content, after), label, toggle };
}

export function groupProviderItems(items, resolveProvider, config) {
  const groups = new Map();
  for (const item of items) {
    const id = resolveProvider(item);
    if (!groups.has(id)) groups.set(id, []);
    groups.get(id).push(item);
  }
  const order = orderedProviderIds([...KNOWN_PROVIDERS, ...groups.keys()], config);
  return [...order.filter((id) => id !== "other"), "other"]
    .filter((id) => groups.has(id))
    .map((id) => [id, groups.get(id)]);
}

export function providerPanels(items, resolveProvider, config, [singular, plural], renderRow) {
  return groupProviderItems(items, resolveProvider, config).map(([id, group]) => {
    const { head, label } = providerPanelHead(id, `${group.length} ${group.length === 1 ? singular : plural}`);
    return el("section", {
      class: "family-section grouped-provider", "data-provider": id, "aria-label": label,
    }, head, el("div", { class: "family-body rows" }, group.map(renderRow)));
  });
}

/** `CLI <version>` from any raw version string; the raw text stays in the title. */
export function cliVersionLabel(raw) {
  const version = raw?.match(/\d+(?:\.\d+)+(?:-[a-z0-9]+(?:[.-][a-z0-9]+)*)?/i)?.[0];
  return version ? el("span", { class: "small mono muted", title: raw }, `CLI ${version}`) : null;
}
