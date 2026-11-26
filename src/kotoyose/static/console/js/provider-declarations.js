// Host catalog of data declarations. Names are text nodes, never HTML.

import { api } from "./api.js";
import { el } from "./dom.js";
import { declarationSummary } from "./provider-declaration-view.js";

export { declarationSummary };

export function renderDeclarationCatalog(catalog, { onRemove } = {}) {
  const rows = (catalog && catalog.declarations) || [];
  const list = el("div", { class: "form" });
  if (catalog && catalog.error) {
    list.append(el("p", { class: "error", role: "alert" }, catalog.error));
  }
  if (catalog?.startup_warnings?.length) {
    list.append(el("div", { class: "error", role: "status" },
      el("h3", {}, "Configuration entries skipped at startup"),
      el("ul", {}, catalog.startup_warnings.map((warning) => el("li", {}, warning))),
      el("p", {}, "Fix the declarations, then use ",
        el("a", { href: "#/machine" }, "Reload config.yml on the Machine page"), ".")));
  }
  if (!rows.length) {
    list.append(el("p", { class: "hint" }, "No data declarations are loaded."));
  }
  for (const row of rows) {
    const summary = declarationSummary(row);
    const remove = onRemove ? el("button", {
      class: "btn btn-sm", type: "button",
      onclick: () => onRemove(row.filename || row.id),
    }, "Remove") : null;
    if (row.status === "invalid") {
      list.append(el("article", { class: "card" },
        el("div", { class: "card-body form" },
          el("h3", {}, summary.title),
          el("p", { class: "error" }, `Invalid: ${row.reason}`),
          remove)));
      continue;
    }
    const commands = el("ul");
    for (const command of summary.commands) commands.append(el("li", {}, command));
    const artifacts = el("ul");
    for (const artifact of summary.artifacts) artifacts.append(el("li", {}, artifact));
    list.append(el("article", { class: "card" },
      el("div", { class: "card-body form" },
        el("h3", {}, summary.title),
        el("p", {}, summary.publisher),
        el("p", { class: "hint" }, summary.sha256),
        el("h4", {}, "Commands"),
        commands,
        el("h4", {}, "Artifacts"),
        artifacts,
        remove)));
  }
  return list;
}

export function mountDeclarationCatalog(parent, catalog) {
  const file = el("input", { type: "file", accept: "application/json,.json" });
  const message = el("p", { role: "status" });
  const upload = el("button", { class: "btn btn-sm", type: "button" }, "Upload declaration");
  upload.addEventListener("click", async () => {
    const selected = file.files && file.files[0];
    if (!selected) {
      message.textContent = "Choose a JSON declaration first.";
      return;
    }
    try {
      const next = await api.uploadProviderDeclaration(new Uint8Array(await selected.arrayBuffer()));
      message.textContent = "Declaration uploaded.";
      parent.replaceChildren(mountDeclarationCatalog(parent, next));
    } catch (error) {
      message.textContent = error.message || "Upload failed.";
    }
  });
  const catalogView = renderDeclarationCatalog(catalog, {
    onRemove: async (id) => {
      try {
        const next = await api.deleteProviderDeclaration(id);
        message.textContent = "Declaration removed.";
        parent.replaceChildren(mountDeclarationCatalog(parent, next));
      } catch (error) {
        message.textContent = error.message || "Remove failed.";
      }
    },
  });
  const card = el("section", { class: "card", id: "data-declarations" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Provider declarations for children")),
    el("div", { class: "card-body form" },
      el("p", { class: "hint" },
        "Providers this host offers to its children (for example Grok Build). Each child owner approves, installs and logs in on the child. "
        + "Upload a kotoyose.provider-data/1 JSON file; built-in providers are never replaced."),
      file, upload, message, catalogView));
  return card;
}
