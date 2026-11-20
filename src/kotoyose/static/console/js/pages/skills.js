// Skills: skill definitions referencing a plugin and skill name. Uses /admin/skills endpoints.

import { api } from "../api.js";
import { pathField } from "../path-picker.js";
import { el, rowMeta } from "../dom.js";
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

export function skillHash(name) {
  return itemHash("skills", name);
}

function skillRow(name, skill, usage, selected) {
  const parts = [skill.description, skill.skill, skill.plugin];
  const aliasCount = usage?.models.length ?? 0;
  return el("a", {
    class: "row route-row",
    href: skillHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(parts),
    usageSummaryTags(usage)),
  el("span", { class: "row-sub" },
    `${aliasCount} ${aliasCount === 1 ? "alias" : "aliases"}`));
}

function skillForm(name, skill, refresh, tracker) {
  const isNew = name === null;
  const start = isNew ? { description: "", plugin: "", skill: "", fallback_prompt: null } : skill;
  const status = el("p", { class: "error", role: "alert" });

  const nameInput = textInput(isNew ? "" : name, {
    placeholder: "skill-name",
    "aria-label": "Skill definition name",
    disabled: !isNew,
  });
  const descInput = textInput(start.description || "", {
    placeholder: "Short description (optional)",
    "aria-label": "Description",
  }, { mono: false });
  const pluginInput = textInput(start.plugin || "", {
    placeholder: "/path/to/plugin",
    required: true,
    "aria-label": "Plugin path",
  });
  const skillNameInput = textInput(start.skill || "", {
    placeholder: "skill-folder-name",
    required: true,
    "aria-label": "Skill name",
  }, { mono: false });
  const fallbackInput = textInput(start.fallback_prompt || "", {
    placeholder: "{plugin}/skills/{skill}/SKILL.md (optional)",
    "aria-label": "Fallback prompt path",
  });

  const save = el("button", { class: "btn btn-primary", type: "submit" },
    isNew ? "Create skill" : "Save changes");
  const remove = isNew ? null : deleteControl({
    label: "Delete skill",
    onDelete: () => api.deleteSkill(name),
    setBusy: (busy) => setBusy(busy),
    status,
    backHash: "#/skills",
    onDeleted: () => tracker.markClean(),
  });
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const target = isNew ? validateName(nameInput.value, true) : { name };
    if (target.error) {
      status.textContent = target.error;
      return;
    }
    const plugin = pluginInput.value.trim();
    const skillName = skillNameInput.value.trim();
    if (!plugin) {
      status.textContent = "Plugin path is required.";
      return;
    }
    if (!skillName) {
      status.textContent = "Skill name is required.";
      return;
    }
    const body = {
      description: descInput.value.trim(),
      plugin,
      skill: skillName,
      fallback_prompt: fallbackInput.value.trim() || null,
    };

    setBusy(true);
    try {
      if (isNew) {
        await api.createSkill(target.name, body);
        tracker.markClean();
        location.hash = skillHash(target.name);
        return;
      }
      await api.updateSkill(name, body);
      tracker.markClean();
      await refresh();
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
    }
  };

  const form = el("form", { class: "form", onsubmit: submit },
    el("div", { class: "form-grid" },
      field("Name", nameInput, {
        wide: true,
        hint: isNew ? null : el("span", { class: "hint" }, "Skill names are fixed once created."),
      }),
      field("Description", descInput, { wide: true }),
      pathField("Plugin path", pluginInput, { directories: true, wide: true }),
      field("Skill", skillNameInput, { wide: true }),
      pathField("Fallback prompt", fallbackInput, {
        wide: true,
        hint: el("span", { class: "hint" },
          "Optional prompt file prepended for providers that do not support native skill injection."),
      })),
    status,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function buildPeek(config, selected, usageMap, refresh) {
  if (selected === null) return null;
  const onClose = () => { location.hash = "#/skills"; };
  const isNew = selected === NEW;
  const skill = isNew ? null : (config.skill_definitions || {})[selected];
  if (!isNew && !skill) {
    return {
      title: selected,
      subtitle: "Not found",
      body: notFoundBody(`Skill "${selected}" was not found.`, "#/skills", "Back to skills"),
      onClose,
    };
  }
  const tracker = createDirtyTracker();
  const sections = [skillForm(isNew ? null : selected, skill, refresh, tracker)];
  if (!isNew) {
    sections.push(usedBySection(usageMap.get(selected), "No alias attaches this skill."));
  }
  return {
    title: isNew ? "New skill" : selected,
    subtitle: isNew
      ? "A named skill pointing at a plugin directory."
      : (skill.description || skill.skill),
    body: sections,
    onClose,
    isDirty: tracker.isDirty,
  };
}

export async function renderSkills(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  const skills = config.skill_definitions || {};
  const names = Object.keys(skills).sort();
  const usageMap = usageByRef(config, "skill_refs");

  const listCard = libraryListCard({
    ariaLabel: "Skill list",
    title: "All skills",
    names,
    emptyText: "No skills yet. Create one to attach a plugin skill to aliases.",
    renderRow: (name) => skillRow(name, skills[name], usageMap.get(name), name === selected),
  });

  renderLibraryPage(main, {
    title: "Skills",
    subtitle: "Plugin skills available to aliases. Each entry points to a plugin directory and skill name.",
    newHash: `#/skills/${NEW}`,
    newLabel: "New skill",
    listCard,
    peek: buildPeek(config, selected, usageMap, refresh),
  });
}
