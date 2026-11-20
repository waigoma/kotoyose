import { api } from "./api.js";
import { el } from "./dom.js";

export function pathPicker(input, { directories = false, start = "" } = {}) {
  const label = input.getAttribute("aria-label");
  const browse = el("button", {
    class: "btn btn-sm", type: "button", "aria-expanded": "false",
    "aria-label": label ? `Browse server paths for ${label}` : null,
  }, "Browse…");
  const location = el("p", { class: "file-path mono" });
  const status = el("p", { class: "hint", role: "status" });
  const entries = el("div", { class: "path-picker-entries" });
  const panel = el("section", {
    class: "card card-body stack path-picker-panel", hidden: true,
    "aria-label": "Server file browser", tabindex: "-1",
  }, location, status, entries);

  let generation = 0;
  function close() {
    generation++;
    panel.hidden = true;
    browse.setAttribute("aria-expanded", "false");
  }

  function choose(path) {
    input.value = path;
    input.dispatchEvent(new Event("input", { bubbles: true }));
    close();
    input.focus();
  }

  function button(label, action, kind) {
    return el("button", { class: "btn btn-sm path-entry", type: "button", onclick: action },
      label, kind ? el("span", { class: "place-tag" }, kind) : null);
  }

  async function load(path, fallback) {
    const run = ++generation;
    location.textContent = "";
    status.textContent = "Loading…";
    entries.replaceChildren();
    panel.focus();
    try {
      const data = await api.browse(path);
      if (run !== generation) return;
      location.textContent = data.current;
      status.textContent = data.entries.length === 0 ? "This directory is empty." : "";
      const controls = [
        data.parent ? button("Parent directory", () => load(data.parent)) : null,
        directories ? button("Choose this directory", () => choose(data.current)) : null,
        ...data.entries.filter((entry) => entry.is_dir || !directories).map((entry) => entry.is_dir
          ? button(entry.name, () => load(entry.path), "Directory")
          : button(entry.name, () => choose(entry.path), "File")),
      ].filter(Boolean);
      entries.replaceChildren(...controls);
      if (document.activeElement === panel) controls[0]?.focus();
    } catch (error) {
      if (run !== generation) return;
      if (fallback !== undefined && (error.status === 400 || error.status === 403)) {
        await load(fallback, fallback ? "" : undefined);
        return;
      }
      status.textContent = `Could not browse: ${error.message}`;
      const home = button("Go to home directory", () => load(""));
      entries.replaceChildren(home);
      if (document.activeElement === panel) home.focus();
    }
  }

  browse.addEventListener("click", () => {
    if (!panel.hidden) {
      close();
      return;
    }
    panel.hidden = false;
    browse.setAttribute("aria-expanded", "true");
    const typed = input.value.trim();
    const slash = typed.lastIndexOf("/");
    const parent = slash >= 0 ? typed.slice(0, slash) || "/" : "";
    const initial = directories || typed.endsWith("/") ? typed : parent;
    load(initial || start, initial && initial !== parent ? parent : "");
  });
  panel.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      close();
      browse.focus();
    }
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = [...entries.querySelectorAll("button")];
    const index = buttons.indexOf(document.activeElement);
    event.preventDefault();
    const next = event.key === "ArrowDown" ? index + 1 : (index < 0 ? buttons.length - 1 : index - 1);
    buttons[Math.max(0, Math.min(next, buttons.length - 1))]?.focus();
  });
  return el("div", { class: "path-picker" },
    el("div", { class: "inline-form" }, input, browse), panel);
}

export function pathField(label, input, { wide = false, hint, ...pickerOptions } = {}) {
  if (!input.getAttribute("aria-label")) input.setAttribute("aria-label", label);
  return el("div", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label), pathPicker(input, pickerOptions), hint);
}
