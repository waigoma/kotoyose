// Aliases: model presets that bind a route, agent placement, effort, and library attachments. Uses /admin/models endpoints.

import { providerOf, providerPanels } from "../provider-ui.js";
import { api } from "../api.js";
import { el, rowMeta, attachmentTags } from "../dom.js";
import { sanctionOf, trainingRiskLabel } from "../format.js";
import { createDirtyTracker, openPeek } from "../peek.js";

const NEW = "new";
const EFFORT_VALUES = ["low", "medium", "high", "xhigh", "max"];
let searchQuery = "";

export function aliasHash(name) {
  return `#/aliases/${encodeURIComponent(name)}`;
}

/** Build the ModelPreset payload from an existing base and the form's values. */
export function aliasBody(base, values) {
  const body = { ...base };
  if (values.route) {
    body.route = values.route;
    // The server rejects provider/model/endpoint/token fields whenever a route is set.
    delete body.provider;
    delete body.model;
    delete body.anthropic_base_url;
    delete body.anthropic_auth_token_env;
    delete body.anthropic_auth_token_secret;
    delete body.subscription_only;
  } else {
    delete body.route;
  }

  if (values.route && values.place && values.surface) {
    body.agent = { place: values.place, surface: values.surface };
  } else {
    delete body.agent;
  }

  body.effort = values.effort || null;
  if (body.cursor && body.cursor.model_params && "effort" in body.cursor.model_params) {
    const modelParams = { ...body.cursor.model_params };
    delete modelParams.effort;
    const cursor = { ...body.cursor };
    if (Object.keys(modelParams).length > 0) cursor.model_params = modelParams;
    else delete cursor.model_params;
    body.cursor = cursor;
  }

  body.prompt_file_refs = values.promptFileRefs;
  body.skill_refs = values.skillRefs;
  body.mcp_refs = values.mcpRefs;
  return body;
}

function combosReferencing(config, alias) {
  return Object.entries(config.pools || {})
    .filter(([, pool]) => (pool.candidates || []).includes(alias))
    .map(([name]) => name)
    .sort();
}

function aliasStatus(config, preset) {
  if (!preset || !preset.route) return { label: "Direct", tone: "" };
  const sanction = (config.route_sanctions || {})[preset.route];
  return sanctionOf(sanction && sanction.sanction);
}

function aliasRow(name, preset, config, selected) {
  const target = preset.route ? [preset.route] : [preset.provider || "direct", preset.model];
  const shape = preset.agent ? [preset.agent.place, preset.agent.surface] : ["chat only"];
  const status = aliasStatus(config, preset);
  const official = config.route_sanctions?.[preset.route]?.sanction === "official";
  const attachments = [preset.prompt_file_refs, preset.mcp_refs].flat().filter(Boolean);
  const policy = (config.alias_data_policies || {})[name];
  const risk = preset.route && trainingRiskLabel(policy && policy.data_policy);
  return el("a", {
    class: "row route-row",
    href: aliasHash(name),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title" }, name),
    rowMeta(target, shape),
    attachmentTags(attachments),
    risk ? el("span", { class: "place-tag" }, risk) : null),
  official ? null : el("span", { class: "state-chip is-warning" }, status.label));
}

function matchesSearch(name, preset, config, needle) {
  if (!needle) return true;
  const status = aliasStatus(config, preset);
  const haystack = [
    name,
    preset.route,
    preset.agent && preset.agent.place,
    preset.agent && preset.agent.surface,
    preset.provider,
    preset.model,
    status.label,
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

function listCard(config, selected) {
  const models = config.models || {};
  const names = Object.keys(models).sort();
  const countNode = el("span", { class: "mono small muted" }, "");
  const rowsWrap = el("div", { class: "families-list" });
  const emptyNode = el("p", { class: "card empty-line" }, "");
  const searchInput = el("input", {
    class: "input",
    type: "search",
    value: searchQuery,
    placeholder: "Search aliases by name, route, place, surface…",
    "aria-label": "Search aliases",
  });

  function renderFiltered() {
    const needle = searchQuery.trim().toLowerCase();
    const filtered = names.filter((name) => matchesSearch(name, models[name], config, needle));
    countNode.textContent = needle ? `${filtered.length} / ${names.length}` : String(names.length);
    if (names.length === 0) {
      emptyNode.textContent = "No aliases yet. Create one to bind a route and attachments.";
      rowsWrap.replaceChildren(emptyNode);
      return;
    }
    if (filtered.length === 0) {
      emptyNode.textContent = `No aliases match "${searchQuery.trim()}".`;
      rowsWrap.replaceChildren(emptyNode);
      return;
    }
    rowsWrap.replaceChildren(...providerPanels(filtered,
      (name) => providerOf(config.routes?.[models[name].route], config),
      config, ["alias", "aliases"], (name) => aliasRow(name, models[name], config, name === selected)));
  }

  searchInput.addEventListener("input", () => {
    searchQuery = searchInput.value;
    renderFiltered();
  });
  renderFiltered();

  return el("section", { class: "families-list", "aria-label": "Alias list" },
    names.length > 0 ? el("div", { class: "alias-filter" }, searchInput, countNode) : null,
    rowsWrap);
}

function field(label, control, { wide = false, hint = null } = {}) {
  return el("label", { class: `field${wide ? " wide" : ""}` },
    el("span", { class: "field-label" }, label),
    control,
    hint);
}

function attachmentPicker(label, collection, existingRefs) {
  const names = [...new Set([...Object.keys(collection || {}), ...(existingRefs || [])])].sort();
  const selected = new Set(existingRefs || []);
  const box = names.length === 0
    ? el("span", { class: "hint" }, "None defined yet.")
    : el("div", { class: "check-box" }, names.map((itemName) => {
        const input = el("input", { type: "checkbox", value: itemName });
        input.checked = selected.has(itemName);
        input.addEventListener("change", () => {
          if (input.checked) selected.add(itemName);
          else selected.delete(itemName);
        });
        const def = (collection || {})[itemName];
        const desc = def && def.description ? el("span", { class: "small muted" }, def.description) : null;
        return el("label", { class: "check-row" },
          input,
          el("span", { class: "item-name" }, itemName),
          desc);
      }));

  const value = () => {
    const kept = (existingRefs || []).filter((ref) => selected.has(ref));
    const added = names.filter((itemName) => selected.has(itemName) && !kept.includes(itemName));
    return [...kept, ...added];
  };

  return {
    node: el("div", { class: "field wide" },
      el("span", { class: "field-label" }, label),
      box),
    value,
  };
}

function deleteControl(name, setBusy, status, onDeleted) {
  let armed = false;
  const button = el("button", { class: "btn btn-quiet-danger", type: "button" }, "Delete alias");
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
      await api.deleteModel(name);
      if (typeof onDeleted === "function") onDeleted();
      location.hash = "#/aliases";
    } catch (failure) {
      status.textContent = failure.message;
      setBusy(false);
      armed = false;
      button.textContent = "Delete alias";
    }
  });
  return button;
}

function aliasForm(config, name, preset, refresh, tracker) {
  const isNew = name === null;
  const start = isNew ? {} : preset;
  const routes = config.routes || {};
  const routeNames = Object.keys(routes).sort();
  const driversCatalog = (config.catalog && config.catalog.drivers) || {};
  const status = el("p", { class: "error", role: "alert" });

  const nameInput = el("input", {
    class: "input",
    type: "text",
    value: isNew ? "" : name,
    placeholder: "alias-name",
    "aria-label": "Alias name",
  });

  const routeOptions = [];
  if (!isNew && !start.route) {
    routeOptions.push(el("option", { value: "" }, "No route (direct)"));
  }
  for (const routeName of routeNames) {
    routeOptions.push(el("option", { value: routeName }, routeName));
  }
  const routeSelect = el("select", { class: "input", "aria-label": "Route" }, routeOptions);
  routeSelect.value = isNew ? (routeNames[0] || "") : (start.route || "");
  if (isNew && routeNames.length === 0) {
    routeSelect.replaceChildren(el("option", { value: "" }, "No routes available"));
    routeSelect.disabled = true;
  }
  const routeHint = el("span", { class: "hint" });

  const placeSelect = el("select", { class: "input", "aria-label": "Agent place" });
  const surfaceSelect = el("select", { class: "input", "aria-label": "Agent surface" });

  let currentPlace = (start.agent && start.agent.place) || "";
  let currentSurface = (start.agent && start.agent.surface) || "";

  function syncRouteInfo() {
    const routeName = routeSelect.value;
    const route = routes[routeName];
    if (!route) {
      routeHint.textContent = isNew
        ? "Create a route first before creating an alias."
        : `Direct (${start.provider || "unknown"}${start.model ? ` · ${start.model}` : ""}). Assign a route to switch.`;
      return {};
    }
    const sanction = config.route_sanctions?.[routeName]?.sanction;
    routeHint.replaceChildren(rowMeta(route.driver, route.model,
      sanction === "official" ? null : sanctionOf(sanction).label));
    return (driversCatalog[route.driver] && driversCatalog[route.driver].places) || {};
  }

  function syncSurfaces(placesMap) {
    const place = placeSelect.value;
    if (!place || !placesMap[place]) {
      surfaceSelect.replaceChildren(el("option", { value: "" }, "—"));
      surfaceSelect.value = "";
      surfaceSelect.disabled = true;
      currentSurface = "";
      return;
    }
    const surfaces = placesMap[place];
    surfaceSelect.replaceChildren(...surfaces.map((surface) => el("option", { value: surface }, surface)));
    surfaceSelect.disabled = false;
    surfaceSelect.value = surfaces.includes(currentSurface) ? currentSurface : (surfaces[0] || "");
    currentSurface = surfaceSelect.value;
  }

  function syncAgentChoices() {
    const placesMap = syncRouteInfo();
    const places = Object.keys(placesMap);
    if (!routeSelect.value || places.length === 0) {
      placeSelect.replaceChildren(el("option", { value: "" }, "None (chat only)"));
      placeSelect.value = "";
      placeSelect.disabled = !routeSelect.value;
      currentPlace = "";
      syncSurfaces({});
      return;
    }
    placeSelect.disabled = false;
    placeSelect.replaceChildren(
      el("option", { value: "" }, "None (chat only)"),
      ...places.map((place) => el("option", { value: place }, place)),
    );
    placeSelect.value = places.includes(currentPlace) ? currentPlace : "";
    currentPlace = placeSelect.value;
    syncSurfaces(placesMap);
  }

  routeSelect.addEventListener("change", () => {
    syncAgentChoices();
  });
  placeSelect.addEventListener("change", () => {
    currentPlace = placeSelect.value;
    syncSurfaces(syncRouteInfo());
  });
  surfaceSelect.addEventListener("change", () => {
    currentSurface = surfaceSelect.value;
  });
  syncAgentChoices();

  const effortOptions = [
    el("option", { value: "" }, "(default)"),
    ...EFFORT_VALUES.map((level) => el("option", { value: level }, level)),
  ];
  const effortSelect = el("select", { class: "input", "aria-label": "Effort" }, effortOptions);
  const initialEffort = (((start.cursor || {}).model_params || {}).effort) || start.effort || "";
  if (initialEffort && !EFFORT_VALUES.includes(initialEffort)) {
    effortSelect.append(el("option", { value: initialEffort }, initialEffort));
  }
  effortSelect.value = initialEffort;

  const promptFiles = attachmentPicker("Prompt files", config.prompt_files, start.prompt_file_refs);
  const skills = attachmentPicker("Skills", config.skill_definitions, start.skill_refs);
  const mcps = attachmentPicker("MCPs", config.mcps, start.mcp_refs);

  const unlocks = {
    tools: "Tools",
    mcp: "MCP servers",
    skills: "Skills",
    workspace: "Workspace",
    attachments: "File and image attachments",
  };
  const riskText = el("span", { class: "hint" });
  const checks = Object.entries(unlocks).map(([value, label]) => {
    const input = el("input", { type: "checkbox", value });
    return { input, node: el("label", { class: "check-row" }, input, label) };
  });
  const note = el("input", { class: "input", placeholder: "Public repository only" });
  const acknowledgement = el("fieldset", { class: "field wide" },
    el("legend", {}, "Training risk acknowledgement"), riskText,
    ...checks.map(({ node }) => node), field("Required note when unlocking capabilities", note));
  function syncAcknowledgement() {
    const route = (config.route_sanctions || {})[routeSelect.value];
    const risk = trainingRiskLabel(route && route.data_policy);
    const current = (config.alias_data_policies || {})[name];
    const active = !isNew && routeSelect.value === start.route && current && current.training_ack;
    riskText.textContent = active ? `${risk} · Acknowledgement active` : risk;
    acknowledgement.hidden = !risk;
    acknowledgement.disabled = !risk;
    for (const { input } of checks) input.checked = false;
    note.value = "";
  }
  routeSelect.addEventListener("change", syncAcknowledgement);
  syncAcknowledgement();

  const save = el("button", {
    class: "btn btn-primary",
    type: "submit",
    disabled: isNew && routeNames.length === 0,
  }, isNew ? "Create alias" : "Save changes");
  const remove = isNew ? null : deleteControl(name, (busy) => setBusy(busy), status, () => tracker.markClean());
  const controls = [save, remove].filter(Boolean);
  const setBusy = (busy) => controls.forEach((control) => { control.disabled = busy; });

  const submit = async (event) => {
    event.preventDefault();
    status.textContent = "";
    const target = nameInput.value.trim();
    if (!target) {
      status.textContent = "Name is required.";
      return;
    }
    const route = routeSelect.value;
    if (isNew && !route) {
      status.textContent = "Route is required.";
      return;
    }
    const body = aliasBody(isNew ? {} : preset, {
      route,
      place: placeSelect.value,
      surface: surfaceSelect.value,
      effort: effortSelect.value,
      promptFileRefs: promptFiles.value(),
      skillRefs: skills.value(),
      mcpRefs: mcps.value(),
    });
    const capabilities = acknowledgement.disabled ? [] : checks
      .filter(({ input }) => input.checked)
      .map(({ input }) => input.value);
    if (capabilities.length) {
      if (!note.value.trim()) {
        status.textContent = "Enter a note to acknowledge training risk.";
        return;
      }
      body.training_acknowledgement = { capabilities, note: note.value.trim() };
    }
    setBusy(true);
    try {
      if (isNew) {
        if (target === NEW) throw new Error("\"new\" is reserved for this form. Choose another name.");
        await api.createModel(target, body);
        tracker.markClean();
        location.hash = aliasHash(target);
        return;
      }
      if (target !== name) {
        await api.renameModel(name, target, body);
        tracker.markClean();
        location.hash = aliasHash(target);
        return;
      }
      await api.updateModel(name, body);
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
      field("Route", routeSelect, { wide: true, hint: routeHint }),
      field("Agent place", placeSelect),
      field("Agent surface", surfaceSelect),
      field("Effort", effortSelect, { wide: true }),
      promptFiles.node,
      skills.node,
      mcps.node,
      acknowledgement),
    status,
    el("div", { class: "form-actions" },
      el("div", { class: "form-actions-end" }, remove, save)));
  tracker.attach(form);
  return form;
}

function usedBySection(config, name, nativeOptions) {
  const combos = combosReferencing(config, name);
  const clients = (nativeOptions && nativeOptions.enabled ? nativeOptions.clients || [] : [])
    .filter((client) => (client.allowed_agent_models || []).includes(name))
    .map((client) => client.name)
    .sort();

  const items = [];
  if (combos.length > 0) {
    items.push(el("div", { class: "chips" },
      combos.map((combo) => el("a", {
        class: "chip",
        href: `#/combos/${encodeURIComponent(combo)}`,
      }, `combo: ${combo}`))));
  }
  if (clients.length > 0) {
    items.push(el("div", { class: "chips" },
      clients.map((client) => el("span", { class: "chip" }, `client: ${client}`))));
  }
  if (items.length === 0) {
    items.push(el("span", { class: "hint" },
      nativeOptions && nativeOptions.enabled
        ? "Not referenced by any combo or client policy."
        : "Not referenced by any combo."));
  }
  return el("div", { class: "field-stack" },
    el("span", { class: "field-label" }, "Used by"),
    ...items);
}

function openAliasPeek(config, selected, nativeOptions, refresh) {
  if (selected === null) return;
  const onClose = () => { location.hash = "#/aliases"; };
  const isNew = selected === NEW;
  const preset = isNew ? null : (config.models || {})[selected];
  if (!isNew && !preset) {
    openPeek({
      title: selected,
      subtitle: "Not found",
      body: el("div", { class: "stack" },
        el("p", { class: "error", role: "alert" }, `Alias "${selected}" was not found.`),
        el("a", { href: "#/aliases" }, "Back to aliases")),
      onClose,
    });
    return;
  }
  const tracker = createDirtyTracker();
  const sections = [aliasForm(config, isNew ? null : selected, preset, refresh, tracker)];
  if (!isNew) {
    sections.push(usedBySection(config, selected, nativeOptions));
  }
  const sub = isNew
    ? "An alias binds a route, optional agent limits, effort, and attachments."
    : null;
  const badge = isNew || config.route_sanctions?.[preset.route]?.sanction === "official"
    ? null : { label: aliasStatus(config, preset).label, tone: "warning" };
  openPeek({
    title: isNew ? "New alias" : selected,
    subtitle: sub,
    badge,
    body: sections,
    onClose,
    isDirty: tracker.isDirty,
  });
}

export async function renderAliases(main, { config, rest, refresh }) {
  const selected = rest[0] || null;
  let nativeOptions = null;
  if (selected && selected !== NEW && (config.models || {})[selected]) {
    try {
      nativeOptions = await api.nativeOptions();
    } catch (error) {
      nativeOptions = null;
    }
    if (!main.isConnected) return;
  }

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Aliases"),
        el("p", { class: "page-sub" }, "Model names clients call. Each alias binds a route, agent placement, effort, and attachments.")),
      el("a", { class: "btn btn-primary", href: `#/aliases/${NEW}` }, "New alias")),
    listCard(config, selected),
  );
  openAliasPeek(config, selected, nativeOptions, refresh);
}
