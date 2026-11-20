import { dot, el } from "./dom.js";

export const PROVIDER_NAMES = new Map([
  ["claude", ["Anthropic", "Claude"]],
  ["codex", ["OpenAI", "Codex"]],
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
