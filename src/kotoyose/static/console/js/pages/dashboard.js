// Dashboard: what needs attention, and the latest requests. Uses only the existing admin reads.

import { childConnectionBadge, cliVersionLabel, providerOf, providerPanels } from "../provider-ui.js";
import { api } from "../api.js";
import { setNavCount } from "../nav.js";
import { dot, el, errorLine, rowMeta, responsiveTable } from "../dom.js";
import { BLOCKED_STATES, authLabel, count, timeLabel } from "../format.js";
import {
  connectionAuthBadge,
  connectionHash,
  quotaSignature,
  renderCompactQuota,
  renderQuotaRows,
} from "./providers.js";

const ACTIVITY_LIMIT = 10;

function routeHref(name) {
  return `#/routes/${encodeURIComponent(name)}`;
}

/** Items that need a person, built from the config, pool state, and connection health. */
export function attentionItems(config, pools, connections = []) {
  const items = [];
  for (const conn of connections || []) {
    const state = (conn && conn.state) || {};
    const auth = state.auth;
    if (auth === "expired" || auth === "logged_out" || auth === "expiring") {
      const statusText = childConnectionBadge(state)?.label || authLabel(state);
      const detailParts = [conn.provider, conn.kind];
      if (state.plan) detailParts.push(state.plan);
      if (state.last_error) detailParts.push(state.last_error);
      items.push({
        tone: auth === "expiring" ? "warning" : "negative",
        title: `Connection ${conn.id}: ${statusText}`,
        detail: detailParts,
        action: { label: "Open connection", href: connectionHash(conn.id) },
      });
    }
  }
  for (const [name, sanction] of Object.entries(config.route_sanctions || {})) {
    if (sanction.sanction === "needs_ack") {
      items.push({
        tone: "warning",
        ring: true,
        title: `Route ${name} needs acknowledgement`,
        detail: sanction.sanction_reasons?.length ? sanction.sanction_reasons : "Outside the official surface",
        action: { label: "Open route", href: routeHref(name) },
      });
    } else if (sanction.ack_stale) {
      items.push({
        tone: "warning",
        title: `The acknowledgement for ${name} is out of date`,
        detail: "The route changed after it was acknowledged. Acknowledge it again.",
        action: { label: "Open route", href: routeHref(name) },
      });
    }
  }
  for (const [poolName, pool] of Object.entries(pools.pools || {})) {
    const blocked = (pool.candidates || []).filter((candidate) => BLOCKED_STATES.has(candidate.state));
    if (blocked.length === 0) continue;
    const authExpired = blocked.some((candidate) => candidate.state === "auth_expired");
    items.push({
      tone: authExpired ? "negative" : "warning",
      title: `Pool ${poolName}: ${blocked.length} of ${pool.candidates.length} candidates blocked`,
      detail: blocked
        .map((candidate) => {
          const state = candidate.state === "auth_expired" ? "auth expired" : "limited";
          return candidate.until ? `${candidate.alias} ${state} until ${timeLabel(candidate.until)}` : `${candidate.alias} ${state}`;
        }),
    });
  }
  return items;
}

function attentionRow(item) {
  return el("div", { class: "attention-item" },
    item.ring ? dot("ring-warning") : dot(item.tone),
    el("div", { class: "attention-text" },
      el("span", { class: "item-name" }, item.title),
      rowMeta(item.detail)),
    item.action ? el("a", { href: item.action.href, class: "small" }, item.action.label) : null,
  );
}

function attentionCard() {
  const body = el("div", { class: "attention" }, el("p", { class: "card-body muted" }, "Loading…"));
  const tally = el("span", { class: "mono muted small" }, "");
  const card = el("section", { class: "card", "aria-label": "Needs attention" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Needs attention"), tally),
    body);
  return { card, body, tally };
}

function activityCard() {
  const body = el("div", {}, el("p", { class: "card-body muted" }, "Loading…"));
  const card = el("section", { class: "card", "aria-label": "Recent activity" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Recent activity"),
      el("span", { class: "small muted" }, "Latest requests")),
    body);
  return { card, body };
}

function quotaCard() {
  const body = el("div", {}, el("p", { class: "card-body muted" }, "Loading…"));
  const card = el("section", { class: "families-list", "aria-label": "Subscription quota" },
    el("h2", { class: "card-title" }, "Quota"),
    body);
  return { card, body };
}

/** Providers の「Show quota on Dashboard」を外した connection は Dashboard の残量にも出さない。 */
export function quotaConnections(connections) {
  const subs = (connections || []).filter((conn) => conn && conn.kind === "subscription");
  const shown = subs.filter((conn) => conn.show_quota !== false);
  return { shown, hidden: subs.length - shown.length };
}

function renderQuotaList(connections, config, setSecretNames) {
  const { shown: subs, hidden } = quotaConnections(connections);
  const hiddenNote = hidden > 0
    ? el("p", { class: "small muted quota-hidden-note" },
      `${hidden} hidden by “Show quota on Dashboard” in Providers.`)
    : null;
  if (subs.length === 0) {
    return el("div", { class: "card empty-line" },
      el("span", {}, hidden > 0
        ? "Quota is hidden for every subscription connection."
        : "No subscription connections configured."),
      hiddenNote);
  }
  // 参照 (quota_from) と完全一致の残量は 1 行にまとめる。
  const owners = new Map();
  const rows = new Map(subs.map((conn) => {
    const state = conn.state || {};
    const quota = Array.isArray(state.quota) ? state.quota : [];
    const quotaVia = conn.quota_from || state.quota_via || null;
    let duplicateOf = null;
    if (!quotaVia && quota.length > 0) {
      const sig = quotaSignature(quota);
      if (sig && owners.has(sig)) duplicateOf = owners.get(sig);
      else if (sig) owners.set(sig, conn.id);
    }
    const compactSource = quotaVia || duplicateOf;
    const bars = compactSource
      ? renderCompactQuota(quota, { kind: quotaVia ? "via" : "same", sourceId: compactSource, via: quotaVia })
      : renderQuotaRows(quota, { via: null });
    const badge = connectionAuthBadge(conn, setSecretNames);
    return [conn, el("div", { class: "quota-row" },
      el("div", { class: "quota-conn-meta" },
        el("div", { class: "conn-row-top" },
          el("a", { class: "row-title", href: connectionHash(conn.id) }, conn.id),
          el("span", { class: "row-status" }, dot(badge.tone), badge.label)),
        rowMeta(state.plan, cliVersionLabel(state.cli_version), badge.detail)),
      bars || (!state.child_state || state.child_state === "online" ? el("span", { class: "small muted" },
        state.child_quota_shared === false ? "Quota not shared" : "No quota telemetry yet") : null))];
  }));
  return el("div", { class: "families-list quota-list" }, hiddenNote,
    providerPanels(subs, (conn) => providerOf(conn, config), config, ["connection", "connections"], (conn) => rows.get(conn)));
}

function statusTone(status) {
  if (status === "succeeded" || status === "completed") return "positive";
  if (status === "failed" || status === "error") return "negative";
  return "";
}

function activityTable(items) {
  if (items.length === 0) {
    return el("p", { class: "card-body muted" }, "No requests yet.");
  }
  return responsiveTable(el("table", {},
      el("thead", {}, el("tr", {},
        el("th", {}, "Time"), el("th", {}, "Alias"), el("th", {}, "Status"),
        el("th", { class: "num" }, "Tokens in"), el("th", { class: "num" }, "Tokens out"))),
      el("tbody", {}, items.map((row) => el("tr", {},
        el("td", { class: "mono small" }, timeLabel(row.received_at)),
        el("td", { class: "item-name" }, row.model || ""),
        el("td", { class: "small" },
          el("span", { class: "row-status" }, dot(statusTone(row.status)), row.status || "—")),
        el("td", { class: "mono small num" }, count(row.tokens_in)),
        el("td", { class: "mono small num" }, count(row.tokens_out))))),
    ),
  );
}

export async function renderDashboard(main, { config, counts }) {
  const stamp = new Date().toLocaleString("en-US", { hour12: false });
  const attention = attentionCard();
  const quota = quotaCard();
  const activity = activityCard();
  main.append(
    el("header", { class: "page-head" },
      el("h1", { class: "page-title" }, "Dashboard"),
      el("span", { class: "mono small muted" }, stamp)),
    attention.card,
    quota.card,
    activity.card,
  );

  const [pools, interactions, connectionsRes, secretsRes] = await Promise.allSettled([
    api.poolState(),
    api.interactions(ACTIVITY_LIMIT),
    api.connections(),
    api.secrets(),
  ]);
  const secretsList = secretsRes.status === "fulfilled" ? ((secretsRes.value && secretsRes.value.secrets) || []) : [];
  const setSecretNames = new Set(secretsList.filter((s) => s.set !== false).map((s) => s.name));
  const connections = connectionsRes.status === "fulfilled" && Array.isArray(connectionsRes.value && connectionsRes.value.connections)
    ? connectionsRes.value.connections
    : [];

  if (pools.status === "fulfilled") {
    const items = attentionItems(config, pools.value, connections);
    counts.attention = items.length;
    setNavCount("dashboard", items.length);
    attention.tally.parentNode.hidden = items.length === 0;
    attention.tally.textContent = `${items.length} ${items.length === 1 ? "item" : "items"}`;
    attention.body.replaceChildren(...(items.length === 0
      ? [attentionRow({ tone: "positive", title: "Nothing needs attention" })]
      : items.map(attentionRow)));
  } else {
    const reason = pools.reason;
    attention.body.replaceChildren(el("div", { class: "card-body stack" },
      errorLine(`Attention data is unavailable: ${reason.message}`),
      el("p", { class: "hint" }, "Route acknowledgements below are still read from the configuration.")));
    // The route items come from the config alone, so they still show.
    const routeItems = attentionItems(config, { pools: {} }, connections);
    attention.body.append(...routeItems.map(attentionRow));
  }

  if (connectionsRes.status === "fulfilled") {
    quota.body.replaceChildren(renderQuotaList(connections, config, setSecretNames));
  } else {
    quota.body.replaceChildren(el("div", { class: "card-body" },
      errorLine(`Quota data is unavailable: ${connectionsRes.reason.message}`)));
  }

  if (interactions.status === "fulfilled") {
    activity.body.replaceChildren(activityTable(interactions.value.items || []));
  } else {
    activity.body.replaceChildren(el("div", { class: "card-body" },
      errorLine(`Recent activity is unavailable: ${interactions.reason.message}`)));
  }
}
