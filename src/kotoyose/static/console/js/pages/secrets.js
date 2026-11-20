// Secrets: encrypted secret management, usage tracking, and migration hints.

import { api } from "../api.js";
import { dot, el, errorLine, rowMeta, responsiveTable } from "../dom.js";

function suggestSecretName(raw) {
  return String(raw || "")
    .replace(/[^A-Za-z0-9_.-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .toLowerCase()
    .slice(0, 128) || "secret-key";
}

function collectFallbackUnmanaged(config) {
  const items = [];
  for (const [connName, conn] of Object.entries(config?.connections || {}).sort()) {
    if (conn?.secret && conn.secret.startsWith("env:")) {
      const envName = conn.secret.slice(4);
      items.push({
        kind: "connection",
        target: connName,
        label: `connection:${connName} (${conn.provider || ""})`,
        current: conn.secret,
        suggested_name: suggestSecretName(envName),
      });
    }
  }
  const scanMcp = (ownerLabel, mcpName, server) => {
    for (const section of ["env", "headers"]) {
      const mapping = server?.[section];
      if (!mapping || typeof mapping !== "object") continue;
      for (const [key, val] of Object.entries(mapping).sort()) {
        if (typeof val === "string" && val && !/\{\{header:[A-Za-z0-9-]+\}\}/.test(val)) {
          items.push({
            kind: "mcp",
            target: `${ownerLabel}.${section}.${key}`,
            label: `${ownerLabel} ${section}.${key}`,
            current: "literal",
            suggested_name: suggestSecretName(`${mcpName}-${key}`),
          });
        }
      }
    }
  };
  for (const [mcpName, def] of Object.entries(config?.mcps || {}).sort()) {
    scanMcp(`mcp:${mcpName}`, mcpName, def?.server);
  }
  for (const [aliasName, preset] of Object.entries(config?.models || {}).sort()) {
    for (const [srvName, srv] of Object.entries(preset?.mcp_servers || {}).sort()) {
      scanMcp(`alias:${aliasName}.mcp:${srvName}`, srvName, srv);
    }
  }
  return items;
}

function formatMasked(entry) {
  if (!entry.set) return "—";
  return entry.last4 ? `••••${entry.last4}` : "••••";
}

function updatedMeta(entry) {
  if (!entry.set) return null;
  const parts = [];
  if (entry.updated_at) parts.push(el("span", { class: "mono" }, String(entry.updated_at).replace("T", " ").slice(0, 19)));
  if (entry.updated_by) parts.push(`by ${entry.updated_by}`);
  if (entry.version) parts.push(el("span", { class: "mono" }, `v${entry.version}`));
  return rowMeta(parts);
}

function serverSettingsCard(settings, refresh) {
  const token = el("input", {
    class: "input input-mono", type: "password", autocomplete: "new-password",
    "aria-label": "Anthropic auth token",
  });
  const baseUrl = el("input", {
    class: "input input-mono", type: "text", value: settings.anthropic_base_url || "",
    "aria-label": "Anthropic base URL",
  });
  const tokenSet = Boolean(settings.anthropic_auth_token?.set);
  const clear = el("button", {
    class: "btn btn-sm btn-quiet-danger", type: "button", disabled: !tokenSet,
  }, "Clear token");
  const save = el("button", { class: "btn btn-sm btn-primary", type: "submit" }, "Save settings");
  const status = el("p", { class: "small", role: "status" });

  async function update(body) {
    if (save.disabled) return;
    save.disabled = clear.disabled = token.disabled = baseUrl.disabled = true;
    status.className = "small muted";
    status.textContent = "Saving…";
    try {
      await api.saveSettings(body);
      token.value = "";
      await refresh({ notice: "Server settings saved." });
    } catch {
      status.className = "small error";
      status.textContent = "Could not save server settings.";
    } finally {
      save.disabled = token.disabled = baseUrl.disabled = false;
      clear.disabled = !tokenSet;
    }
  }

  clear.addEventListener("click", () => update({ anthropic_auth_token: "" }));
  const submit = (event) => {
    event.preventDefault();
    const body = { anthropic_base_url: baseUrl.value.trim() };
    if (token.value) body.anthropic_auth_token = token.value;
    return update(body);
  };
  return el("form", { class: "card-body form", onsubmit: submit },
    el("div", { class: "form-grid" },
      el("label", { class: "field" },
        el("span", { class: "field-label" }, "Anthropic auth token"),
        token,
        el("span", { class: "hint" }, tokenSet ? "Set · Enter a replacement to change it." : "Not set · Write-only.")),
      el("label", { class: "field" },
        el("span", { class: "field-label" }, "Anthropic base URL"), baseUrl)),
    el("div", { class: "form-actions" },
      status, el("div", { class: "form-actions-end" }, clear, save)));
}

async function loadServerSettings(card, refresh) {
  try {
    const settings = await api.settings();
    card.append(serverSettingsCard(settings, refresh));
  } catch {
    card.append(el("div", { class: "card-body" }, errorLine("Could not load server settings.")));
  }
}

export async function renderSecrets(main, { config, refresh, notice }) {
  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Secrets"),
        el("p", { class: "page-sub" }, "Write-only encrypted credentials referenced by routes, MCP servers, and settings."))),
  );

  const settingsCard = el("section", { class: "card", "aria-label": "Server settings" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Server settings"),
      el("span", { class: "small muted", role: "status" }, notice)));
  main.append(settingsCard);
  const settingsLoad = loadServerSettings(settingsCard, refresh);

  let data = null;
  let storeError = null;
  try {
    data = await api.secrets();
  } catch (error) {
    storeError = error;
  }
  if (!main.isConnected) return;

  const isDisabled = storeError !== null;
  const secrets = data?.secrets || [];
  const unmanaged = data?.unmanaged || collectFallbackUnmanaged(config);

  const formCard = el("section", { class: "card", "aria-label": "Set or replace secret" });
  const nameInput = el("input", {
    class: "input",
    type: "text",
    placeholder: "provider-api-key",
    autocomplete: "off",
    required: true,
    "aria-label": "Secret name",
  });
  const purposeInput = el("input", {
    class: "input",
    type: "text",
    placeholder: "Optional note (e.g. OpenAI production route)",
    "aria-label": "Purpose",
  });
  const valueInput = el("input", {
    class: "input input-mono",
    type: "password",
    placeholder: "Paste secret value (never displayed again after save)",
    autocomplete: "new-password",
    required: true,
    "aria-label": "Secret value",
  });
  const formTitle = el("h2", { class: "card-title" }, "Add or replace secret");
  const formStatus = el("p", { class: "error", role: "alert" });
  const formHint = el("p", { class: "hint" }, "Values are AES-256-GCM encrypted before storage and cannot be read back.");
  const saveBtn = el("button", { class: "btn btn-primary", type: "submit" }, "Save secret");
  const clearBtn = el("button", { class: "btn btn-sm", type: "button" }, "Clear form");

  const openInForm = (name = "", purpose = "", title = "Add or replace secret") => {
    formTitle.textContent = title;
    nameInput.value = name;
    purposeInput.value = purpose;
    valueInput.value = "";
    formStatus.textContent = "";
    formCard.hidden = false;
    valueInput.focus();
  };

  clearBtn.addEventListener("click", () => {
    formTitle.textContent = "Add or replace secret";
    nameInput.value = "";
    purposeInput.value = "";
    valueInput.value = "";
    formStatus.textContent = "";
  });

  if (isDisabled) {
    saveBtn.disabled = true;
  }

  const submitForm = async (event) => {
    event.preventDefault();
    formStatus.textContent = "";
    const name = nameInput.value.trim();
    const value = valueInput.value;
    const purpose = purposeInput.value.trim();
    if (!name) {
      formStatus.textContent = "Secret name is required.";
      return;
    }
    if (!value) {
      formStatus.textContent = "Secret value is required.";
      return;
    }
    saveBtn.disabled = true;
    try {
      await api.putSecret(name, value, purpose);
      valueInput.value = "";
      await refresh();
    } catch (failure) {
      formStatus.textContent = failure.message;
      saveBtn.disabled = false;
    }
  };

  formCard.append(
    el("div", { class: "card-head" }, formTitle),
    el("form", { class: "card-body form", onsubmit: submitForm },
      el("div", { class: "form-grid" },
        el("label", { class: "field" }, el("span", { class: "field-label" }, "Name"), nameInput),
        el("label", { class: "field" }, el("span", { class: "field-label" }, "Purpose"), purposeInput),
        el("label", { class: "field wide" }, el("span", { class: "field-label" }, "Value (write-only)"), valueInput)),
      formHint,
      formStatus,
      el("div", { class: "form-actions" },
        el("span", { class: "hint" }, data?.key_id ? `Master key_id: ${data.key_id}` : null),
        el("div", { class: "form-actions-end" }, clearBtn, saveBtn))),
  );

  const listCard = el("section", { class: "card", "aria-label": "Managed secrets" });
  const listStatus = el("p", { class: "error", role: "alert" });
  const newBtn = el("button", { class: "btn btn-sm btn-primary", type: "button", disabled: isDisabled }, "Add secret");
  newBtn.addEventListener("click", () => openInForm("", "", "Add secret"));

  const listBody = [];
  if (isDisabled) {
    listBody.push(
      el("div", { class: "card-body stack" },
        errorLine(`Secret store disabled: ${storeError.message}`),
        el("p", { class: "hint" },
          "Configure $CREDENTIALS_DIRECTORY/kotoyose-master or KOTOYOSE_MASTER_KEY_FILE (mode 0600) to enable encrypted secrets.")),
    );
  } else if (secrets.length === 0) {
    listBody.push(el("p", { class: "card-body muted" }, "No secrets stored yet."));
  } else {
    const rows = secrets.map((entry) => {
      const usedBy = entry.used_by || [];
      const setBtn = el("button", { class: "btn btn-sm", type: "button" }, entry.set ? "Replace" : "Set");
      setBtn.addEventListener("click", () => {
        openInForm(entry.name, entry.purpose || "", entry.set ? `Replace secret: ${entry.name}` : `Set secret: ${entry.name}`);
      });

      let armed = false;
      const delBtn = el("button", {
        class: "btn btn-sm btn-quiet-danger",
        type: "button",
        disabled: usedBy.length > 0 || !entry.set,
        title: usedBy.length > 0 ? `In use by: ${usedBy.join(", ")}` : "Delete secret",
      }, "Delete");
      delBtn.addEventListener("click", async () => {
        if (!armed) {
          armed = true;
          delBtn.textContent = "Confirm delete";
          listStatus.textContent = "";
          return;
        }
        delBtn.disabled = true;
        listStatus.textContent = "";
        try {
          await api.deleteSecret(entry.name);
          await refresh();
        } catch (failure) {
          listStatus.textContent = failure.message;
          delBtn.disabled = false;
          armed = false;
          delBtn.textContent = "Delete";
        }
      });

      return el("tr", {},
        el("td", { class: "item-name" }, entry.name),
        el("td", { class: "small secondary" }, entry.purpose || ""),
        el("td", { class: "small" },
          el("span", { class: "row-status" },
            dot(entry.set ? "positive" : "warning"),
            entry.set ? "Set" : "Not set")),
        el("td", { class: "mono small" }, formatMasked(entry)),
        el("td", { class: "small muted" }, updatedMeta(entry)),
        el("td", {}, el("div", { class: "attachment-tags" },
          usedBy.map((u) => el("span", { class: "place-tag" }, u)))),
        el("td", {}, el("div", { class: "row-actions" }, setBtn, delBtn)));
    });
    listBody.push(
      listStatus,
      responsiveTable(el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Name"),
            el("th", {}, "Purpose"),
            el("th", {}, "Status"),
            el("th", {}, "Value"),
            el("th", {}, "Updated"),
            el("th", {}, "Used by"),
            el("th", {}, "Actions"))),
          el("tbody", {}, rows))),
    );
  }

  listCard.append(
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Managed secrets"),
      el("div", { class: "row-actions" },
        el("span", { class: "mono small muted" }, String(secrets.length)),
        newBtn)),
    ...listBody,
  );

  const unmanagedCard = el("section", { class: "card", "aria-label": "Not managed — move to Secrets" });
  const unmanagedBody = [];
  if (unmanaged.length === 0) {
    unmanagedBody.push(el("p", { class: "card-body muted" }, "No unmanaged env: or literal credentials detected."));
  } else {
    const uRows = unmanaged.map((item) => {
      const moveBtn = el("button", {
        class: "btn btn-sm",
        type: "button",
        disabled: isDisabled,
      }, "Move to Secrets");
      moveBtn.addEventListener("click", () => {
        openInForm(
          item.suggested_name,
          `Moved from ${item.label}`,
          `Move ${item.label} to Secrets`,
        );
      });
      return el("tr", {},
        el("td", { class: "item-name" }, item.label),
        el("td", { class: "mono small secondary" }, item.current),
        el("td", { class: "mono small muted" }, `secret:${item.suggested_name}`),
        el("td", {}, moveBtn));
    });
    unmanagedBody.push(
      el("div", { class: "card-body" },
        el("p", { class: "hint" },
          "These references still use env:NAME or literal values. Pre-fill a secret here, paste the value yourself, then point the route or MCP at the new secret reference.")),
      responsiveTable(el("table", {},
          el("thead", {}, el("tr", {},
            el("th", {}, "Reference"),
            el("th", {}, "Current source"),
            el("th", {}, "Suggested secret"),
            el("th", {}, "Action"))),
          el("tbody", {}, uRows))),
    );
  }
  unmanagedCard.append(
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Not managed — move to Secrets"),
      el("span", { class: "mono small muted" }, String(unmanaged.length))),
    ...unmanagedBody,
  );

  main.append(listCard, formCard, unmanagedCard);
  await settingsLoad;
}
