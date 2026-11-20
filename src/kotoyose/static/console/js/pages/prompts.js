// Prompts: prompt file definitions and managed content preview/edit. Uses /admin/prompt-files endpoints.

import { api } from "../api.js";
import { pathField } from "../path-picker.js";
import { el, rowMeta, pathText } from "../dom.js";
import {
  NEW,
  deleteControl,
  field,
  itemHash,
  libraryListCard,
  notFoundBody,
  renderLibraryPage,
  textInput,
  usageByRef,
  usageSummaryTags,
  usedBySection,
  validateName,
} from "../library.js";
import { createDirtyTracker } from "../peek.js";

export function promptHash(name) {
  return itemHash("prompts", name);
}

function byteStats(text) {
  const value = String(text || "");
  const bytes = new TextEncoder().encode(value).length;
  const lines = value === "" ? 0 : value.split(/\r?\n/).length;
  return [`${bytes.toLocaleString("en-US")} bytes`, `${lines} ${lines === 1 ? "line" : "lines"}`];
}

function promptRow(name, prompt, usage, selected) {
  const aliasCount = usage?.models.length ?? 0;
  return el("a", {
    class: "row route-row",
    href: promptHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(prompt.description),
    pathText(prompt.path),
    usageSummaryTags(usage)),
  el("span", { class: "row-sub" },
    `${aliasCount} ${aliasCount === 1 ? "alias" : "aliases"}`));
}

function contentSection(name, main, contentTracker) {
  const meta = el("div", { class: "small muted" }, "Loading…");
  const pathHint = el("span", { class: "file-path mono" });
  const status = el("p", { class: "error", role: "alert" });
  const savedNote = el("p", { class: "success-note", role: "status" });
  const textarea = el("textarea", {
    class: "input input-mono textarea textarea-tall",
    placeholder: "Prompt content",
    "aria-label": "Prompt content",
    disabled: true,
  });
  const saveButton = el("button", {
    class: "btn btn-primary",
    type: "button",
    disabled: true,
  }, "Save content");
  const modeHint = el("span", { class: "hint" });

  let loadedWritable = false;

  const updateMeta = () => {
    const mode = loadedWritable ? "Writable" : "Read-only";
    meta.replaceChildren(rowMeta(byteStats(textarea.value), mode));
  };

  textarea.addEventListener("input", () => {
    savedNote.textContent = "";
    status.textContent = "";
    contentTracker.markDirty();
    updateMeta();
  });

  saveButton.addEventListener("click", async () => {
    if (!loadedWritable) return;
    status.textContent = "";
    savedNote.textContent = "";
    saveButton.disabled = true;
    try {
      const result = await api.updatePromptFileContent(name, textarea.value);
      if (!main.isConnected) return;
      if (result?.path) {
        pathHint.textContent = result.path;
        pathHint.setAttribute("title", result.path);
      }
      contentTracker.markClean();
      updateMeta();
      savedNote.textContent = "Content saved.";
    } catch (failure) {
      if (!main.isConnected) return;
      status.textContent = failure.message;
    } finally {
      if (main.isConnected) saveButton.disabled = !loadedWritable;
    }
  });

  (async () => {
    try {
      const data = await api.promptFileContent(name);
      if (!main.isConnected) return;
      loadedWritable = Boolean(data && data.writable);
      textarea.value = (data && data.content) || "";
      textarea.disabled = false;
      textarea.readOnly = !loadedWritable;
      saveButton.disabled = !loadedWritable;
      pathHint.textContent = data?.path || "";
      pathHint.setAttribute("title", pathHint.textContent);
      modeHint.textContent = loadedWritable
        ? "Stored under the managed prompt root."
        : "External path outside the managed prompt root: preview is read-only.";
      updateMeta();
    } catch (failure) {
      if (!main.isConnected) return;
      meta.textContent = "Unavailable";
      textarea.disabled = true;
      saveButton.disabled = true;
      status.textContent = failure.message;
    }
  })();

  return el("div", { class: "sub-section" },
    el("div", { class: "sub-head" },
      el("h3", { class: "sub-title" }, "Content"),
      meta),
    pathHint,
    textarea,
    modeHint,
    status,
    savedNote,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, saveButton)));
}

function promptForm(config, name, prompt, refresh, defTracker, contentTracker) {
  const isNew = name === null;
  const start = isNew ? { description: "", path: "" } : prompt;
  const status = el("p", { class: "error", role: "alert" });
  const createdLinkWrap = el("div", { class: "stack" });

  const nameInput = textInput(isNew ? "" : name, {
    placeholder: "prompt-name",
    "aria-label": "Prompt name",
  }, { mono: false });
  const descInput = textInput(start.description || "", {
    placeholder: "Short description (optional)",
    "aria-label": "Description",
  }, { mono: false });
  const defaultPlaceholder = config.prompt_root
    ? `${config.prompt_root}/<name>.md`
    : "prompts/<name>.md";
  const pathInput = textInput(start.path || "", {
    placeholder: isNew ? `${defaultPlaceholder} (optional)` : defaultPlaceholder,
    required: !isNew,
    "aria-label": "Path",
  });
  const initialContentInput = isNew
    ? el("textarea", {
        class: "input input-mono textarea",
        placeholder: "Optional initial prompt content (for managed paths)",
        "aria-label": "Initial content",
      })
    : null;

  const save = el("button", { class: "btn btn-primary", type: "submit" },
    isNew ? "Create prompt" : "Save definition");
  const remove = isNew ? null : deleteControl({
    label: "Delete prompt",
    onDelete: () => api.deletePromptFile(name),
    setBusy: (busy) => setBusy(busy),
    status,
    backHash: "#/prompts",
    onDeleted: () => {
      defTracker.markClean();
      contentTracker.markClean();
    },
  });
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    createdLinkWrap.replaceChildren();
    const { name: target, error } = validateName(nameInput.value, isNew);
    if (error) {
      status.textContent = error;
      return;
    }
    const description = descInput.value.trim();
    const path = pathInput.value.trim();
    if (!isNew && !path) {
      status.textContent = "Path is required.";
      return;
    }

    setBusy(true);
    try {
      if (isNew) {
        await api.createPromptFile(target, { description, path });
        const initialContent = initialContentInput ? initialContentInput.value : "";
        if (initialContent) {
          try {
            await api.updatePromptFileContent(target, initialContent);
          } catch (contentErr) {
            defTracker.markClean();
            status.textContent = `Prompt created, but saving initial content failed: ${contentErr.message}`;
            createdLinkWrap.replaceChildren(
              el("a", { href: promptHash(target) }, `Open ${target}`),
            );
            setBusy(false);
            return;
          }
        }
        defTracker.markClean();
        location.hash = promptHash(target);
        return;
      }
      if (target !== name) {
        await api.renamePromptFile(name, target, { description, path });
        defTracker.markClean();
        location.hash = promptHash(target);
        return;
      }
      await api.updatePromptFile(name, { description, path });
      defTracker.markClean();
      await refresh();
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
    }
  };

  const fields = [
    field("Name", nameInput, { wide: true }),
    field("Description", descInput, { wide: true }, ),
    pathField("Path", pathInput, {
      wide: true,
      start: config.prompt_root,
      hint: el("span", { class: "hint" }, isNew
        ? "Leave blank to create at the managed prompt root."
        : "Managed root paths allow editing content; external paths are read-only preview."),
    }),
  ];
  if (initialContentInput) {
    fields.push(field("Initial content", initialContentInput, { wide: true }));
  }

  const form = el("form", { class: "form", onsubmit: submit },
    el("div", { class: "form-grid" }, fields),
    status,
    createdLinkWrap,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  defTracker.attach(form);
  return form;
}

function buildPeek(config, selected, usageMap, refresh, main) {
  if (selected === null) return null;
  const onClose = () => { location.hash = "#/prompts"; };
  const isNew = selected === NEW;
  const prompt = isNew ? null : (config.prompt_files || {})[selected];
  if (!isNew && !prompt) {
    return {
      title: selected,
      subtitle: "Not found",
      body: notFoundBody(`Prompt "${selected}" was not found.`, "#/prompts", "Back to prompts"),
      onClose,
    };
  }
  const defTracker = createDirtyTracker();
  const contentTracker = createDirtyTracker();
  const sections = [promptForm(config, isNew ? null : selected, prompt, refresh, defTracker, contentTracker)];
  if (!isNew) {
    sections.push(contentSection(selected, main, contentTracker));
    sections.push(usedBySection(usageMap.get(selected), "No alias attaches this prompt file."));
  }
  return {
    title: isNew ? "New prompt" : selected,
    subtitle: isNew
      ? "A named system prompt file referenced by aliases."
      : (prompt.description || "Prompt file"),
    body: sections,
    onClose,
    isDirty: () => defTracker.isDirty() || contentTracker.isDirty(),
  };
}

export async function renderPrompts(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  const prompts = config.prompt_files || {};
  const names = Object.keys(prompts).sort();
  const usageMap = usageByRef(config, "prompt_file_refs");

  const listCard = libraryListCard({
    ariaLabel: "Prompt list",
    title: "All prompts",
    names,
    emptyText: "No prompt files yet. Create one to attach reusable instructions to aliases.",
    renderRow: (name) => promptRow(name, prompts[name], usageMap.get(name), name === selected),
  });

  renderLibraryPage(main, {
    title: "Prompts",
    subtitle: "Reusable system prompt files. Attach them to aliases in Build.",
    newHash: `#/prompts/${NEW}`,
    newLabel: "New prompt",
    listCard,
    peek: buildPeek(config, selected, usageMap, refresh, main),
  });
}
