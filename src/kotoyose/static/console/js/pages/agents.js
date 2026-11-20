// Agents: subagent definitions (description, prompt, tools, model). Uses /admin/agents endpoints.

import { api } from "../api.js";
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

const AGENT_MODELS = ["inherit", "haiku", "sonnet", "opus"];

export function agentHash(name) {
  return itemHash("agents", name);
}

function agentRow(name, agent, usage, selected) {
  const modelLabel = (agent && agent.model) || "inherit";
  const toolsSummary = Array.isArray(agent && agent.tools)
    ? `${agent.tools.length} ${agent.tools.length === 1 ? "tool" : "tools"}`
    : "inherit tools";
  return el("a", {
    class: "row route-row",
    href: agentHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(agent?.description),
    rowMeta(modelLabel, toolsSummary),
    usageSummaryTags(usage)));
}

function parseToolsList(raw) {
  return String(raw || "")
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function agentForm(name, agent, refresh, tracker) {
  const isNew = name === null;
  const start = isNew
    ? { description: "", prompt: "", tools: null, model: "inherit" }
    : agent;
  const status = el("p", { class: "error", role: "alert" });

  const nameInput = textInput(isNew ? "" : name, {
    placeholder: "agent-name",
    "aria-label": "Agent name",
  }, { mono: false });
  const descInput = textInput(start.description || "", {
    placeholder: "When to delegate to this subagent",
    required: true,
    "aria-label": "Description",
  }, { mono: false });

  const modelOptions = [...AGENT_MODELS];
  if (start.model && !modelOptions.includes(start.model)) modelOptions.push(start.model);
  const modelSelect = el("select", { class: "input", "aria-label": "Model" },
    modelOptions.map((m) => el("option", { value: m }, m === "inherit" ? "inherit (same as parent)" : m)));
  modelSelect.value = start.model || "inherit";

  const inheritToolsCheck = el("input", { type: "checkbox", "aria-label": "Inherit parent tools" });
  inheritToolsCheck.checked = start.tools === null || start.tools === undefined;
  const toolsInput = textInput(Array.isArray(start.tools) ? start.tools.join(", ") : "", {
    placeholder: "Read, Grep, Glob",
    "aria-label": "Allowed tools",
    disabled: inheritToolsCheck.checked,
  });
  inheritToolsCheck.addEventListener("change", () => {
    toolsInput.disabled = inheritToolsCheck.checked;
  });

  const promptInput = el("textarea", {
    class: "input input-mono textarea textarea-tall",
    placeholder: "System prompt for this subagent",
    required: true,
    "aria-label": "Prompt",
  });
  promptInput.value = start.prompt || "";

  const save = el("button", { class: "btn btn-primary", type: "submit" },
    isNew ? "Create agent" : "Save changes");
  const remove = isNew ? null : deleteControl({
    label: "Delete agent",
    onDelete: () => api.deleteAgent(name),
    setBusy: (busy) => setBusy(busy),
    status,
    backHash: "#/agents",
    onDeleted: () => tracker.markClean(),
  });
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const { name: target, error } = validateName(nameInput.value, isNew);
    if (error) {
      status.textContent = error;
      return;
    }
    const description = descInput.value.trim();
    if (!description) {
      status.textContent = "Description is required.";
      return;
    }
    const prompt = promptInput.value;
    if (!prompt.trim()) {
      status.textContent = "Prompt is required.";
      return;
    }
    const tools = inheritToolsCheck.checked ? null : parseToolsList(toolsInput.value);
    const body = {
      description,
      prompt,
      tools,
      model: modelSelect.value,
    };

    setBusy(true);
    try {
      if (isNew) {
        await api.createAgent(target, body);
        tracker.markClean();
        location.hash = agentHash(target);
        return;
      }
      if (target !== name) {
        await api.renameAgent(name, target, body);
        tracker.markClean();
        location.hash = agentHash(target);
        return;
      }
      await api.updateAgent(name, body);
      tracker.markClean();
      await refresh();
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
    }
  };

  const form = el("form", { class: "form", onsubmit: submit },
    el("div", { class: "form-grid" },
      field("Name", nameInput, { wide: true }),
      field("Description", descInput, { wide: true }),
      field("Model", modelSelect, { wide: true }),
      el("div", { class: "field wide" },
        el("span", { class: "field-label" }, "Tools"),
        el("label", { class: "check" }, inheritToolsCheck, "Inherit parent tool configuration"),
        toolsInput,
        el("span", { class: "hint" }, "Comma- or space-separated tool names when inheritance is disabled.")),
      field("Prompt", promptInput, { wide: true })),
    status,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function buildPeek(config, selected, usageMap, refresh) {
  if (selected === null) return null;
  const onClose = () => { location.hash = "#/agents"; };
  const isNew = selected === NEW;
  const agent = isNew ? null : (config.agent_definitions || {})[selected];
  if (!isNew && !agent) {
    return {
      title: selected,
      subtitle: "Not found",
      body: notFoundBody(`Agent "${selected}" was not found.`, "#/agents", "Back to agents"),
      onClose,
    };
  }
  const tracker = createDirtyTracker();
  const sections = [agentForm(isNew ? null : selected, agent, refresh, tracker)];
  if (!isNew) {
    sections.push(usedBySection(usageMap.get(selected), "No alias attaches this agent."));
  }
  return {
    title: isNew ? "New agent" : selected,
    subtitle: isNew
      ? "A named subagent definition with prompt, tools, and model."
      : rowMeta(agent.model || "inherit", agent.description),
    body: sections,
    onClose,
    isDirty: tracker.isDirty,
  };
}

export async function renderAgents(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  const agents = config.agent_definitions || {};
  const names = Object.keys(agents).sort();
  const usageMap = usageByRef(config, "agent_refs");

  const listCard = libraryListCard({
    ariaLabel: "Agent list",
    title: "All agents",
    names,
    emptyText: "No agents yet. Create one to define a reusable subagent.",
    renderRow: (name) => agentRow(name, agents[name], usageMap.get(name), name === selected),
  });

  renderLibraryPage(main, {
    title: "Agents",
    subtitle: "Reusable subagent definitions with system prompt, tool restrictions, and model choice.",
    newHash: `#/agents/${NEW}`,
    newLabel: "New agent",
    listCard,
    peek: buildPeek(config, selected, usageMap, refresh),
  });
}
