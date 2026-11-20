// Combos: pool aliases that group agent aliases under a selection policy. Uses /admin/pools endpoints and /admin/pools/state.

import { api } from "../api.js";
import { dot, el, errorLine, rowMeta } from "../dom.js";
import { timeLabel } from "../format.js";
import { createDirtyTracker, openPeek } from "../peek.js";

const NEW = "new";

export function comboHash(name) {
  return `#/combos/${encodeURIComponent(name)}`;
}

/** Shape ({ place, surface }) of an alias if it declares an agent, or null. */
export function aliasShape(config, alias) {
  const preset = (config.models || {})[alias];
  if (!preset || !preset.agent) return null;
  return { place: preset.agent.place, surface: preset.agent.surface };
}

/** Describe a candidate's runtime pool state for dot + label rendering. */
export function describeCandidateState(entry) {
  if (!entry || entry.state === "unknown") return { tone: "", label: "unknown" };
  if (entry.state === "available") return { tone: "positive", label: "available" };
  if (entry.state === "limited") {
    return {
      tone: "warning",
      label: entry.until ? `limited until ${timeLabel(entry.until)}` : "limited",
    };
  }
  if (entry.state === "auth_expired") return { tone: "negative", label: "auth expired" };
  return { tone: "", label: String(entry.state) };
}

function candidateStateMap(poolState, poolName) {
  const map = new Map();
  const entry = ((poolState && poolState.pools) || {})[poolName];
  for (const candidate of (entry && entry.candidates) || []) {
    map.set(candidate.alias, candidate);
  }
  return map;
}

function comboRow(name, pool, stateMap, selected) {
  const candidates = pool.candidates || [];
  const requires = pool.requires || [];
  return el("a", {
    class: "row route-row",
    href: comboHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(pool.policy, requires.length ? `requires: ${requires.join(", ")}` : null),
    candidates.length
      ? el("div", { class: "chips chips-top" }, candidates.map((alias) => {
          const state = describeCandidateState(stateMap.get(alias));
          return el("span", { class: "chip chip-state", title: state.label },
            dot(state.tone),
            alias,
            state.label !== "unknown" ? el("span", { class: "small muted" }, state.label) : null);
        }))
      : null),
  el("span", { class: "row-sub" },
    `${candidates.length} ${candidates.length === 1 ? "candidate" : "candidates"}`));
}

function listCard(config, poolState, poolStateError, selected) {
  const pools = config.pools || {};
  const names = Object.keys(pools).sort();
  return el("section", { class: "card", "aria-label": "Combo list" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "All combos"),
      el("span", { class: "mono small muted" }, String(names.length))),
    poolStateError
      ? el("div", { class: "list-filter" },
          errorLine(`Pool state is unavailable: ${poolStateError.message}`))
      : null,
    names.length === 0
      ? el("p", { class: "card-body muted" }, "No combos yet. Create one to group aliases under a policy.")
      : el("div", { class: "rows" }, names.map((name) => comboRow(
        name, pools[name], candidateStateMap(poolState, name), name === selected))));
}

function field(label, control, { wide = false, hint = null } = {}) {
  return el("label", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label),
    control,
    hint);
}

function deleteControl(name, setBusy, status, onDeleted) {
  let armed = false;
  const button = el("button", { class: "btn btn-quiet-danger", type: "button" }, "Delete combo");
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
      await api.deletePool(name);
      if (typeof onDeleted === "function") onDeleted();
      location.hash = "#/combos";
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
      armed = false;
      button.textContent = "Delete combo";
    }
  });
  return button;
}

function comboForm(config, poolState, name, pool, refresh, tracker) {
  const isNew = name === null;
  const start = isNew ? { candidates: [], policy: "", requires: [] } : pool;
  const catalogPool = (config.catalog && config.catalog.pool) || {};
  const strategies = [...(catalogPool.strategies || [])];
  if (start.policy && !strategies.includes(start.policy)) strategies.push(start.policy);
  const allRequires = [...new Set([...(catalogPool.requires || []), ...(start.requires || [])])].sort();

  const status = el("p", { class: "error", role: "alert" });
  const nameInput = el("input", {
    class: "input",
    type: "text",
    value: isNew ? "" : name,
    placeholder: "combo-name",
    "aria-label": "Combo name",
    disabled: !isNew,
  });

  const policySelect = el("select", { class: "input", "aria-label": "Policy" },
    strategies.map((strategy) => el("option", { value: strategy }, strategy)));
  policySelect.value = start.policy || strategies[0] || "";

  const selectedRequires = new Set(start.requires || []);
  const requiresBox = allRequires.length === 0
    ? el("span", { class: "hint" }, "No capability requirements available.")
    : el("div", { class: "check-box" }, allRequires.map((req) => {
        const input = el("input", { type: "checkbox", value: req });
        input.checked = selectedRequires.has(req);
        input.addEventListener("change", () => {
          if (input.checked) selectedRequires.add(req);
          else selectedRequires.delete(req);
        });
        return el("label", { class: "check-row" }, input, el("span", { class: "small" }, req));
      }));

  const candidates = [...(start.candidates || [])];
  const stateMap = isNew ? new Map() : candidateStateMap(poolState, name);
  const candidateList = el("div", { class: "candidate-list" });
  const addSelect = el("select", { class: "input", "aria-label": "Add candidate" });
  const addButton = el("button", { class: "btn", type: "button" }, "Add");
  const candidateHint = el("span", { class: "hint" });

  function currentShape() {
    for (const alias of candidates) {
      const shape = aliasShape(config, alias);
      if (shape) return shape;
    }
    return null;
  }

  function renderCandidates() {
    const active = currentShape();
    if (candidates.length === 0) {
      candidateList.replaceChildren(el("span", { class: "hint" }, "No candidates added yet."));
    } else {
      candidateList.replaceChildren(...candidates.map((alias, index) => {
        const shape = aliasShape(config, alias);
        const state = describeCandidateState(stateMap.get(alias));
        const up = el("button", {
          class: "btn btn-sm",
          type: "button",
          "aria-label": `Move ${alias} up`,
          disabled: index === 0,
        }, "↑");
        const down = el("button", {
          class: "btn btn-sm",
          type: "button",
          "aria-label": `Move ${alias} down`,
          disabled: index === candidates.length - 1,
        }, "↓");
        const remove = el("button", {
          class: "btn btn-sm btn-quiet-danger",
          type: "button",
          "aria-label": `Remove ${alias}`,
        }, "Remove");

        up.addEventListener("click", () => {
          [candidates[index - 1], candidates[index]] = [candidates[index], candidates[index - 1]];
          tracker.markDirty();
          renderCandidates();
        });
        down.addEventListener("click", () => {
          [candidates[index + 1], candidates[index]] = [candidates[index], candidates[index + 1]];
          tracker.markDirty();
          renderCandidates();
        });
        remove.addEventListener("click", () => {
          candidates.splice(index, 1);
          tracker.markDirty();
          renderCandidates();
        });

        const subParts = shape ? [shape.place, shape.surface] : ["missing agent"];
        if (state.label !== "unknown") subParts.push(state.label);
        return el("div", { class: "candidate-item" },
          el("div", { class: "candidate-info" },
            el("div", { class: "candidate-title" },
              el("span", { class: "mono small muted" }, `${index + 1}.`),
              dot(state.tone),
              el("span", { class: "item-name" }, alias)),
            rowMeta(subParts)),
          el("div", { class: "candidate-actions" }, up, down, remove));
      }));
    }

    const eligible = Object.keys(config.models || {}).sort().filter((alias) => {
      if (candidates.includes(alias)) return false;
      const shape = aliasShape(config, alias);
      if (!shape) return false;
      if (!active) return true;
      return shape.place === active.place && shape.surface === active.surface;
    });

    if (eligible.length === 0) {
      addSelect.replaceChildren(el("option", { value: "" }, "No matching aliases available"));
      addSelect.disabled = true;
      addButton.disabled = true;
    } else {
      addSelect.replaceChildren(...eligible.map((alias) => {
        const shape = aliasShape(config, alias);
        return el("option", { value: alias }, `${alias} (${shape.place} · ${shape.surface})`);
      }));
      addSelect.disabled = false;
      addButton.disabled = false;
    }

    candidateHint.textContent = active
      ? `Adding from aliases matching ${active.place} · ${active.surface}.`
      : "Only aliases with an agent place and surface can be added.";
  }

  addButton.addEventListener("click", () => {
    if (!addSelect.value) return;
    candidates.push(addSelect.value);
    tracker.markDirty();
    renderCandidates();
  });
  renderCandidates();

  const save = el("button", { class: "btn btn-primary", type: "submit" }, isNew ? "Create combo" : "Save changes");
  const remove = isNew ? null : deleteControl(name, (busy) => setBusy(busy), status, () => tracker.markClean());
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const target = isNew ? nameInput.value.trim() : name;
    if (!target) {
      status.textContent = "Name is required.";
      return;
    }
    const body = {
      candidates: [...candidates],
      policy: policySelect.value,
      requires: allRequires.filter((req) => selectedRequires.has(req)),
    };
    setBusy(true);
    try {
      if (isNew) {
        if (target === NEW) throw new Error("\"new\" is reserved for this form. Choose another name.");
        await api.createPool(target, body);
        tracker.markClean();
        location.hash = comboHash(target);
        return;
      }
      await api.updatePool(name, body);
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
        hint: isNew ? null : el("span", { class: "hint" }, "Combo names are fixed once created."),
      }),
      field("Policy", policySelect, { wide: true }),
      el("div", { class: "field wide" },
        el("span", { class: "field-label" }, "Requires"),
        requiresBox),
      el("div", { class: "field wide" },
        el("span", { class: "field-label" }, "Candidates (in priority order)"),
        candidateList,
        el("div", { class: "inline-add" }, addSelect, addButton),
        candidateHint)),
    status,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function openComboPeek(config, poolState, selected, refresh) {
  if (selected === null) return;
  const onClose = () => { location.hash = "#/combos"; };
  const isNew = selected === NEW;
  const pool = isNew ? null : (config.pools || {})[selected];
  if (!isNew && !pool) {
    openPeek({
      title: selected,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Combo "${selected}" was not found.`),
        el("a", { href: "#/combos" }, "Back to combos")),
      onClose,
    });
    return;
  }
  const sub = isNew
    ? "A combo groups aliases behind a selection policy."
    : rowMeta(`${(pool.candidates || []).length} ${(pool.candidates || []).length === 1 ? "candidate" : "candidates"}`, pool.policy);
  const tracker = createDirtyTracker();
  openPeek({
    title: isNew ? "New combo" : selected,
    subtitle: sub,
    body: comboForm(config, poolState, isNew ? null : selected, pool, refresh, tracker),
    onClose,
    isDirty: tracker.isDirty,
  });
}

export async function renderCombos(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  let poolState = { pools: {} };
  let poolStateError = null;
  try {
    poolState = await api.poolState();
  } catch (error) {
    poolStateError = error;
  }
  if (!main.isConnected) return;

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Combos"),
        el("p", { class: "page-sub" }, "Ordered groups of agent aliases with a selection policy and fallback.")),
      el("a", { class: "btn btn-primary", href: `#/combos/${NEW}` }, "New combo")),
    listCard(config, poolState, poolStateError, selected),
  );
  openComboPeek(config, poolState, selected, refresh);
}
