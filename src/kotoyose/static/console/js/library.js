// Shared helpers for the Library pages (Prompts, Skills, MCPs).

import { attachmentTags, el } from "./dom.js";
import { openPeek } from "./peek.js";

export const NEW = "new";

export function itemHash(page, name) {
  return `#/${page}/${encodeURIComponent(name)}`;
}

export function field(label, control, { wide = false, hint = null } = {}) {
  return el("label", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label),
    control,
    hint);
}

export function textInput(value, props = {}, { mono = true } = {}) {
  return el("input", {
    class: mono ? "input input-mono" : "input",
    type: "text",
    value: value ?? "",
    ...props,
  });
}

export function validateName(raw, isNew) {
  const name = String(raw || "").trim();
  if (!name) return { error: "Name is required." };
  if (name.includes("/")) return { error: "Name cannot contain \"/\"." };
  if (isNew && name === NEW) return { error: "\"new\" is reserved for this form. Choose another name." };
  return { name };
}

export function deleteControl({ label, onDelete, setBusy, status, backHash, onDeleted = null }) {
  let armed = false;
  const button = el("button", { class: "btn btn-quiet-danger", type: "button" }, label);
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
      await onDelete();
      if (typeof onDeleted === "function") onDeleted();
      location.hash = backHash;
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
      armed = false;
      button.textContent = label;
    }
  });
  return button;
}

/** Map resource names to the aliases that reference them. */
export function usageByRef(config, refsField) {
  const map = new Map();
  for (const [alias, preset] of Object.entries(config.models || {})) {
    const refs = (preset && preset[refsField]) || [];
    for (const ref of Array.isArray(refs) ? refs : [refs]) {
      if (!ref) continue;
      if (!map.has(ref)) map.set(ref, { models: [] });
      map.get(ref).models.push(alias);
    }
  }
  for (const entry of map.values()) entry.models.sort();
  return map;
}

export function usageSummaryTags(usage) {
  if (!usage) return null;
  return attachmentTags(usage.models);
}

export function usedBySection(usage, emptyText) {
  const names = usage?.models || [];
  return el("div", { class: "field-stack" },
    el("span", { class: "field-label" }, "Used by"),
    attachmentTags(names, (alias) => `#/aliases/${encodeURIComponent(alias)}`)
      || el("span", { class: "hint" }, emptyText));
}

export function maskSecret(value) {
  if (value === null || value === undefined || value === "") return "—";
  const text = String(value);
  if (text.length <= 4) return "••••";
  return `••••${text.slice(-4)}`;
}

export function isSecretKey(key) {
  return /token|secret|password|passwd|api_key|apikey|auth|credential|bearer|private_key/i.test(String(key || ""));
}

export function libraryListCard({ ariaLabel, title, names, emptyText, renderRow }) {
  return el("section", { class: "card", "aria-label": ariaLabel },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, title),
      el("span", { class: "mono small muted" }, String(names.length))),
    names.length === 0
      ? el("p", { class: "card-body muted" }, emptyText)
      : el("div", { class: "rows" }, names.map(renderRow)));
}

export function notFoundBody(message, backHash, backLabel) {
  return el("div", { class: "stack" },
    el("p", { class: "error", role: "alert" }, message),
    el("a", { href: backHash }, backLabel));
}

export function renderLibraryPage(main, { title, subtitle, newHash, newLabel, listCard, peek = null }) {
  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, title),
        el("p", { class: "page-sub" }, subtitle)),
      el("a", { class: "btn btn-primary", href: newHash }, newLabel)),
    listCard,
  );
  if (peek) openPeek(peek);
}
