// Sidebar: the order and grouping are fixed by the owner's design. Counts come from /admin/config where data exists.

import { el } from "./dom.js";


// `count` names a key of the config payload whose length is shown; "attention" is set by the dashboard.
export const NAV_GROUPS = [
  { items: [{ key: "dashboard", label: "Dashboard", count: "attention" }] },
  { label: "Connect", items: [
    { key: "providers", label: "Providers" },
    { key: "endpoint", label: "Endpoint" },
    { key: "children", label: "Children" },
  ] },
  { label: "Build", items: [
    { key: "routes", label: "Routes", count: "routes" },
    { key: "combos", label: "Combos", count: "pools" },
    { key: "aliases", label: "Aliases", count: "models" },
  ] },
  { label: "Library", items: [
    { key: "prompts", label: "Prompts", count: "prompt_files" },
    { key: "skills", label: "Skills", count: "skill_definitions" },
    { key: "mcps", label: "MCPs", count: "mcps" },
  ] },
  { label: "Monitor", items: [
    { key: "usage", label: "Usage" },
    { key: "imports", label: "Imports" },
    { key: "executions", label: "Executions" },
  ] },
  { label: "Settings", items: [
    { key: "machine", label: "Machine" },
    { key: "secrets", label: "Secrets" },
  ] },
];

export const PAGE_KEYS = new Set(NAV_GROUPS.flatMap((group) => group.items.map((item) => item.key)));

function countOf(item, config, counts) {
  // Empty until the dashboard has counted; the span exists so setNavCount can fill it in.
  if (item.count === "attention") return counts.attention ?? "";
  if (!item.count || !config) return null;
  const source = config[item.count];
  return source ? Object.keys(source).length : null;
}

/** The sidebar as a node. `counts.attention` is filled in by the dashboard once it has loaded. */
export function renderSidebar({ current, config, counts, session = null, onSignOut = null }) {
  const groups = NAV_GROUPS.map((group) => el("div", { class: "nav-section" },
    group.label ? el("span", { class: "nav-section-label" }, group.label) : null,
    el("ul", { class: group.label ? "nav-list is-grouped" : "nav-list" }, group.items.map((item) => {
      const value = countOf(item, config, counts);
      return el("li", {},
        el("a", {
          class: "nav-link",
          href: `#/${item.key}`,
          "aria-current": item.key === current ? "page" : null,
          "data-nav": item.key,
        }, item.label, value === null || value === undefined
          ? null
          : el("span", { class: "nav-count", "data-count": item.key }, String(value))));
    })),
  ));
  const sessionBlock = session && session.user
    ? el("div", { class: "sidebar-session" },
        el("div", { class: "sidebar-user" },
          "Signed in as ",
          el("span", { class: "sidebar-user-name mono" }, session.user)),
        el("button", {
          type: "button",
          class: "btn btn-sm sidebar-signout",
          onclick: onSignOut,
        }, "Sign out"))
    : null;
  const drawerId = "console-nav-drawer";
  const toggleBtn = el("button", {
    type: "button",
    class: "btn btn-sm nav-toggle",
    "aria-expanded": "false",
    "aria-controls": drawerId,
  }, "Menu");

  const drawer = el("div", { class: "sidebar-drawer", id: drawerId },
    el("nav", { "aria-label": "Main" }, groups),
    el("div", { class: "sidebar-foot" },
      sessionBlock),
  );

  const sidebar = el("aside", { class: "sidebar", "aria-label": "Console" },
    el("div", { class: "sidebar-bar" },
      el("div", { class: "brand" }, el("span", { class: "brand-ring", "aria-hidden": "true" }), "Kotoyose"),
      toggleBtn),
    drawer,
  );

  const setMenuOpen = (open) => {
    sidebar.classList.toggle("is-menu-open", open);
    toggleBtn.setAttribute("aria-expanded", open ? "true" : "false");
  };

  toggleBtn.addEventListener("click", () => {
    setMenuOpen(!sidebar.classList.contains("is-menu-open"));
  });

  drawer.addEventListener("click", (event) => {
    const target = event.target;
    if (target && typeof target.closest === "function" && target.closest("a.nav-link")) {
      setMenuOpen(false);
    }
  });

  return sidebar;
}

/** Update a count shown in the sidebar, for counts that a page computes after loading. */
export function setNavCount(key, value) {
  const node = document.querySelector(`[data-count="${key}"]`);
  if (node) node.textContent = String(value);
}
