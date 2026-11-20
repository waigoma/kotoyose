// Entry point: hash router and shell. Every navigation re-renders the shell and reloads /admin/config.

import { api } from "./api.js";
import { el } from "./dom.js";
import { PAGE_KEYS, renderSidebar } from "./nav.js";
import {
  beginPeekRender,
  captureTriggerFocus,
  closePeek,
  destroyPeek,
  isPeekOpen,
  restoreTriggerFocus,
  wasPeekReused,
} from "./peek.js";
import { renderAgents } from "./pages/agents.js";
import { renderAliases } from "./pages/aliases.js";
import { renderCombos } from "./pages/combos.js";
import { renderDashboard } from "./pages/dashboard.js";
import { renderEndpoint } from "./pages/endpoint.js";
import { renderExecutions } from "./pages/executions.js";
import { renderMachine } from "./pages/machine.js";
import { renderMcps } from "./pages/mcps.js";
import { renderPrompts } from "./pages/prompts.js";
import { renderProviders } from "./pages/providers.js";
import { renderRoutes } from "./pages/routes.js";
import { renderSecrets } from "./pages/secrets.js";
import { renderSkills } from "./pages/skills.js";
import { renderUsage } from "./pages/usage.js";

const PAGES = {
  dashboard: renderDashboard,
  providers: renderProviders,
  endpoint: renderEndpoint,
  routes: renderRoutes,
  combos: renderCombos,
  aliases: renderAliases,
  prompts: renderPrompts,
  skills: renderSkills,
  mcps: renderMcps,
  agents: renderAgents,
  usage: renderUsage,
  executions: renderExecutions,
  machine: renderMachine,
  secrets: renderSecrets,
};

/** `#/routes/<name>?k=v` → { page: "routes", rest: ["<name>"], query: URLSearchParams }. Unknown pages fall back to the dashboard. */
export function parseHash(hash) {
  const raw = String(hash || "").replace(/^#\/?/, "");
  const qIndex = raw.indexOf("?");
  const pathPart = qIndex >= 0 ? raw.slice(0, qIndex) : raw;
  const queryPart = qIndex >= 0 ? raw.slice(qIndex + 1) : "";
  const parts = pathPart.split("/").filter(Boolean);
  const decoded = parts.map((part) => {
    try {
      return decodeURIComponent(part);
    } catch (cause) {
      return part;
    }
  });
  const page = PAGE_KEYS.has(decoded[0]) ? decoded[0] : "dashboard";
  return { page, rest: decoded.slice(1), query: new URLSearchParams(queryPart) };
}

let generation = 0;
let cleanupPage = null;
let currentPage = null;

function runCleanup() {
  if (!cleanupPage) return;
  const fn = cleanupPage;
  cleanupPage = null;
  try {
    fn();
  } catch (error) {
    // Ignore teardown errors during navigation.
  }
}

async function handleSignOut() {
  try {
    await api.logout();
  } catch (error) {
    // Navigate to login even if the session already expired.
  }
  location.assign("/admin/login");
}

async function render({ notice } = {}) {
  const run = ++generation;
  runCleanup();
  const app = document.getElementById("app");
  const { page, rest, query } = parseHash(location.hash);
  captureTriggerFocus();
  if (currentPage && currentPage !== page) {
    destroyPeek();
  }
  currentPage = page;
  const [configResult, sessionResult] = await Promise.allSettled([
    api.config(),
    api.session(),
  ]);
  const config = configResult.status === "fulfilled" ? configResult.value : null;
  const configError = configResult.status === "rejected" ? configResult.reason : null;
  const session = sessionResult.status === "fulfilled" ? sessionResult.value : null;
  // A newer navigation started while this one was loading: let that one draw.
  if (run !== generation) return;

  const counts = { attention: undefined };
  const main = el("main", { class: "main", id: "main" });
  app.replaceChildren(
    renderSidebar({ current: page, config, counts, session, onSignOut: handleSignOut }),
    main,
  );

  if (configError) {
    destroyPeek();
    main.append(el("p", { class: "error", role: "alert" },
      `Could not load the configuration: ${configError.message}`));
    return;
  }
  beginPeekRender();
  const renderPage = PAGES[page];
  if (renderPage) {
    const teardown = await renderPage(main, { config, rest, query, counts, notice, refresh: render });
    if (run !== generation || !main.isConnected) {
      if (typeof teardown === "function") teardown();
      return;
    }
    if (typeof teardown === "function") {
      cleanupPage = teardown;
    }
    if (!wasPeekReused()) {
      if (isPeekOpen()) {
        closePeek({ force: true, skipOnClose: true });
      } else {
        restoreTriggerFocus(main);
      }
    }
  } else {
    destroyPeek();
  }
}

window.addEventListener("hashchange", () => { render(); });

if (!location.hash) {
  history.replaceState(null, "", "#/dashboard");
}
render();
