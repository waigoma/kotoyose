// Usage: overview stat cards, 24h hourly SVG chart, and auto-refreshing recent requests feed.

import { api } from "../api.js";
import { dot, el, errorLine, svgEl, responsiveTable } from "../dom.js";
import {
  clockLabel,
  count,
  durationLabel,
  hourLabel,
  statusTone,
  throughputLabel,
  timeLabel,
  tokenPair,
} from "../format.js";

const REFRESH_MS = 10_000;
const RECENT_LIMIT = 25;
let selectedMetric = "requests";

function executionHref(id) {
  return `#/executions/${encodeURIComponent(id)}`;
}

function statCard(label, value, secondary) {
  return el("section", { class: "card stat-card" },
    el("span", { class: "stat-label" }, label),
    el("span", { class: "stat-value" }, value),
    secondary ? el("span", { class: "stat-sub" }, secondary) : null);
}

function fmtAvgDuration(bucket) {
  if (!bucket || !bucket.total_requests) return "—";
  return durationLabel(bucket.avg_duration_ms);
}

function renderStatsRow(stats) {
  const p24 = (stats && stats.past_24h) || {};
  const p7 = (stats && stats.past_7d) || {};
  return [
    statCard(
      "Requests (24h)",
      count(p24.total_requests ?? 0),
      `7d: ${count(p7.total_requests ?? 0)}`,
    ),
    statCard(
      "Success / failure (24h)",
      `${count(p24.successful ?? 0)} / ${count(p24.failed ?? 0)}`,
      `7d: ${count(p7.successful ?? 0)} / ${count(p7.failed ?? 0)}`,
    ),
    statCard(
      "Tokens in → out (24h)",
      `${count(p24.total_tokens_in ?? 0)} → ${count(p24.total_tokens_out ?? 0)}`,
      `7d: ${count(p7.total_tokens_in ?? 0)} → ${count(p7.total_tokens_out ?? 0)}`,
    ),
    statCard(
      "Average duration (24h)",
      fmtAvgDuration(p24),
      p7.total_requests ? `7d: ${fmtAvgDuration(p7)}` : null,
    ),
  ];
}

function niceAxisMax(rawMax) {
  if (rawMax <= 0) return 4;
  if (rawMax <= 4) return 4;
  const magnitude = 10 ** Math.floor(Math.log10(rawMax));
  const scaled = rawMax / magnitude;
  if (scaled <= 1) return magnitude;
  if (scaled <= 2) return 2 * magnitude;
  if (scaled <= 5) return 5 * magnitude;
  return 10 * magnitude;
}

function compactAxisValue(value) {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(value % 1_000_000 === 0 ? 0 : 1)}M`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(value % 1_000 === 0 ? 0 : 1)}k`;
  return String(value);
}

function buildHourlySvg(items, metric) {
  const width = 840;
  const height = 220;
  const padLeft = 52;
  const padRight = 16;
  const padTop = 16;
  const padBottom = 28;
  const plotW = width - padLeft - padRight;
  const plotH = height - padTop - padBottom;
  const field = metric === "tokens_out" ? "tokens_out" : "requests";
  const seriesLabel = metric === "tokens_out" ? "Output tokens per hour" : "Requests per hour";

  const values = items.map((item) => Number(item[field] || 0));
  const rawMax = Math.max(0, ...values);
  const yMax = niceAxisMax(rawMax);
  const gridSteps = 4;

  const gridNodes = [];
  for (let step = 0; step <= gridSteps; step += 1) {
    const ratio = step / gridSteps;
    const y = padTop + plotH - ratio * plotH;
    const tickVal = Math.round(ratio * yMax);
    gridNodes.push(
      svgEl("line", {
        x1: padLeft,
        y1: y,
        x2: width - padRight,
        y2: y,
        stroke: "var(--hairline)",
        "stroke-width": "1",
      }),
      svgEl("text", {
        x: padLeft - 8,
        y: y + 4,
        "text-anchor": "end",
        fill: "var(--text-3)",
        "font-family": "var(--font-mono)",
        "font-size": "11",
      }, compactAxisValue(tickVal)),
    );
  }

  const slotCount = Math.max(1, items.length);
  const slotW = plotW / slotCount;
  const barW = Math.max(6, slotW - 8);
  const barNodes = [];
  const xLabelNodes = [];

  items.forEach((item, index) => {
    const value = Number(item[field] || 0);
    const x = padLeft + index * slotW + (slotW - barW) / 2;
    const centerX = padLeft + index * slotW + slotW / 2;
    const tooltip = `${hourLabel(item.hour)} · ${count(item.requests)} req · ${count(item.tokens_out)} out tok`;

    if (value > 0) {
      const barH = Math.max(2, (value / yMax) * plotH);
      const y = padTop + plotH - barH;
      barNodes.push(
        svgEl("rect", {
          x: x.toFixed(1),
          y: y.toFixed(1),
          width: barW.toFixed(1),
          height: barH.toFixed(1),
          rx: "2",
          fill: "var(--accent)",
        }, svgEl("title", {}, tooltip)),
      );
    } else {
      barNodes.push(
        svgEl("rect", {
          x: x.toFixed(1),
          y: padTop,
          width: barW.toFixed(1),
          height: plotH,
          fill: "transparent",
        }, svgEl("title", {}, tooltip)),
      );
    }

    if (index % 4 === 0 || index === items.length - 1) {
      xLabelNodes.push(
        svgEl("text", {
          x: centerX.toFixed(1),
          y: height - 8,
          "text-anchor": "middle",
          fill: "var(--text-3)",
          "font-family": "var(--font-mono)",
          "font-size": "11",
        }, hourLabel(item.hour)),
      );
    }
  });

  return svgEl(
    "svg",
    {
      class: "chart-svg",
      viewBox: `0 0 ${width} ${height}`,
      role: "img",
      "aria-label": `${seriesLabel} for the last 24 hours`,
    },
    gridNodes,
    barNodes,
    xLabelNodes,
  );
}

function rowDuration(row) {
  if (row.status === "in_progress" && row.received_at) {
    const started = Date.parse(row.received_at);
    if (Number.isFinite(started)) return durationLabel(Date.now() - started);
  }
  return durationLabel(row.duration_ms);
}

function renderRecentTable(items) {
  if (!items || items.length === 0) {
    return el("p", { class: "card-body muted" }, "No requests yet.");
  }
  return responsiveTable(el("table", {},
      el("thead", {},
        el("tr", {},
          el("th", {}, "Time"),
          el("th", {}, "Source"),
          el("th", {}, "Alias"),
          el("th", {}, "Status"),
          el("th", { class: "num" }, "Tokens"),
          el("th", { class: "num" }, "Duration"),
          el("th", { class: "num" }, "tok/s"),
        )),
      el("tbody", {}, items.map((row) => {
        const href = executionHref(row.id);
        const tr = el("tr", { class: "clickable-row" },
          el("td", { class: "mono small", title: row.received_at || "" },
            el("a", { class: "table-link", href }, timeLabel(row.received_at))),
          el("td", { class: "small" },
            row.source ? el("span", { class: "chip" }, row.source) : el("span", { class: "muted" }, "—")),
          el("td", { class: "item-name" }, row.model || ""),
          el("td", { class: "small" },
            el("span", { class: "row-status" }, dot(statusTone(row.status)), row.status || "—")),
          el("td", { class: "mono small num" }, tokenPair(row.tokens_in, row.tokens_out, row.tokens_source)),
          el("td", { class: "mono small num" }, rowDuration(row)),
          el("td", { class: "mono small num" }, throughputLabel(row)),
        );
        tr.addEventListener("click", (event) => {
          if (event.target instanceof Element && event.target.closest("a")) return;
          const selection = window.getSelection && window.getSelection();
          if (selection && String(selection)) return;
          location.hash = href;
        });
        return tr;
      })),
    ),
  );
}

export async function renderUsage(main) {
  main.classList.add("main--wide");
  const statusStamp = el("span", { class: "mono small muted" }, "Loading…");
  const statsGrid = el("div", { class: "stat-grid" },
    renderStatsRow({}));

  const chartTotal = el("span", { class: "mono small muted" }, "");
  const chartBody = el("div", { class: "chart-wrap" },
    el("p", { class: "muted" }, "Loading…"));

  let latestHourly = [];
  const reqBtn = el("button", {
    type: "button",
    class: "seg-btn",
    "aria-pressed": selectedMetric === "requests" ? "true" : "false",
  }, "Requests");
  const tokBtn = el("button", {
    type: "button",
    class: "seg-btn",
    "aria-pressed": selectedMetric === "tokens_out" ? "true" : "false",
  }, "Output tokens");

  function drawChart() {
    reqBtn.setAttribute("aria-pressed", selectedMetric === "requests" ? "true" : "false");
    tokBtn.setAttribute("aria-pressed", selectedMetric === "tokens_out" ? "true" : "false");
    const totalVal = latestHourly.reduce(
      (sum, item) => sum + Number((selectedMetric === "tokens_out" ? item.tokens_out : item.requests) || 0),
      0,
    );
    chartTotal.textContent = selectedMetric === "tokens_out"
      ? `${count(totalVal)} output tokens`
      : `${count(totalVal)} requests`;
    chartBody.replaceChildren(buildHourlySvg(latestHourly, selectedMetric));
  }

  reqBtn.addEventListener("click", () => {
    selectedMetric = "requests";
    drawChart();
  });
  tokBtn.addEventListener("click", () => {
    selectedMetric = "tokens_out";
    drawChart();
  });

  const chartCard = el("section", { class: "card", "aria-label": "Hourly activity" },
    el("div", { class: "card-head" },
      el("div", { class: "title-stack" },
        el("h2", { class: "card-title" }, "Hourly activity (24h)"),
        chartTotal),
      el("div", { class: "seg-group", role: "group", "aria-label": "Chart series" }, reqBtn, tokBtn)),
    chartBody);

  const recentBody = el("div", {}, el("p", { class: "card-body muted" }, "Loading…"));
  const recentCard = el("section", { class: "card", "aria-label": "Recent requests" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Recent requests"),
      el("span", { class: "small muted" }, `Latest ${RECENT_LIMIT}`)),
    recentBody);

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Usage"),
        el("p", { class: "page-sub" }, "Request volume, token usage, and recent activity.")),
      statusStamp),
    statsGrid,
    chartCard,
    recentCard,
  );

  let disposed = false;
  let inFlight = false;
  let timer = null;

  function stopTimer() {
    if (timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  }

  function cleanup() {
    if (disposed) return;
    disposed = true;
    stopTimer();
    document.removeEventListener("visibilitychange", onVisibilityChange);
  }

  async function loadData() {
    if (disposed || !main.isConnected) {
      cleanup();
      return;
    }
    if (inFlight) return;
    inFlight = true;
    try {
      const [statsRes, hourlyRes, recentRes] = await Promise.allSettled([
        api.stats(),
        api.hourlyStats(24),
        api.interactions(RECENT_LIMIT),
      ]);
      if (disposed || !main.isConnected) {
        cleanup();
        return;
      }

      if (statsRes.status === "fulfilled") {
        statsGrid.replaceChildren(...renderStatsRow(statsRes.value));
      } else {
        statsGrid.replaceChildren(
          el("section", { class: "card card-body wide-stat" },
            errorLine(`Stats are unavailable: ${statsRes.reason.message}`)),
        );
      }

      if (hourlyRes.status === "fulfilled") {
        latestHourly = (hourlyRes.value && hourlyRes.value.items) || [];
        drawChart();
      } else {
        chartBody.replaceChildren(errorLine(`Hourly activity is unavailable: ${hourlyRes.reason.message}`));
      }

      if (recentRes.status === "fulfilled") {
        recentBody.replaceChildren(renderRecentTable((recentRes.value && recentRes.value.items) || []));
      } else {
        recentBody.replaceChildren(
          el("div", { class: "card-body" },
            errorLine(`Recent requests are unavailable: ${recentRes.reason.message}`)),
        );
      }

      statusStamp.textContent = document.hidden
        ? "Auto-refresh paused"
        : `Auto-refresh 10s · ${clockLabel(new Date().toISOString())}`;
    } finally {
      inFlight = false;
    }
  }

  function startTimer() {
    stopTimer();
    if (disposed || document.hidden) return;
    timer = setInterval(() => {
      if (disposed || !main.isConnected) {
        cleanup();
        return;
      }
      if (!document.hidden) loadData();
    }, REFRESH_MS);
  }

  function onVisibilityChange() {
    if (disposed || !main.isConnected) {
      cleanup();
      return;
    }
    if (document.hidden) {
      stopTimer();
      statusStamp.textContent = "Auto-refresh paused";
    } else {
      loadData();
      startTimer();
    }
  }

  document.addEventListener("visibilitychange", onVisibilityChange);
  await loadData();
  if (!disposed && main.isConnected && !document.hidden) {
    startTimer();
  }
  return cleanup;
}
