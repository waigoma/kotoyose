// Children: enrolled machines, a one-time code, and revoke. The policy file stays on the child.

import { api } from "../api.js";
import { workspacePolicyFields, workspaceWarnings } from "../child-workspace.js";
import { dot, el, errorLine, rowMeta } from "../dom.js";
import { childPresenceBadge } from "../provider-ui.js";

export async function renderChildren(main, { refresh }) {
  const status = el("p", { class: "small muted", role: "status" });
  const address = el("input", {
    class: "input input-mono",
    type: "text",
    placeholder: "127.0.0.1:9443",
    "aria-label": "Address children dial",
  });
  const codeLine = el("p", { class: "mono small" });
  const issue = el("button", { class: "btn btn-sm", type: "button" }, "Issue enrollment code");
  issue.addEventListener("click", async () => {
    issue.disabled = true;
    status.className = "small muted";
    status.textContent = "";
    codeLine.textContent = "";
    try {
      const issued = await api.issueChildCode(address.value.trim());
      codeLine.textContent = issued.code;
      status.textContent = issued.expires_at ? `Expires ${issued.expires_at}` : "Enrollment code issued.";
    } catch (error) {
      status.className = "small error";
      status.textContent = error.message;
    } finally {
      issue.disabled = false;
    }
  });

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Children"),
        el("p", { class: "page-sub" }, "Machines enrolled to this host. The policy file stays on the child."))),
    el("section", { class: "card" },
      el("div", { class: "card-body form" },
        el("label", { class: "field" },
          el("span", { class: "field-label" }, "Address in the enrollment code"),
          address),
        el("div", { class: "form-actions" }, issue, status),
        codeLine)),
  );

  let body;
  try {
    body = await api.children();
  } catch (error) {
    if (!main.isConnected) return;
    main.append(el("section", { class: "card card-body" }, errorLine(error.message)));
    return;
  }
  if (!main.isConnected) return;

  const children = body.children || [];
  if (!children.length) {
    main.append(el("section", { class: "card card-body" },
      el("p", { class: "muted" }, "No children enrolled.")));
    return;
  }

  const rows = children.map((child) => {
    const revoke = el("button", {
      class: "btn btn-sm btn-quiet-danger",
      type: "button",
      disabled: child.revoked ? true : null,
    }, child.revoked ? "Revoked" : "Revoke");
    revoke.addEventListener("click", async () => {
      if (revoke.disabled || !window.confirm(`Revoke ${child.name}?`)) return;
      revoke.disabled = true;
      try {
        await api.revokeChild(child.name);
        if (main.isConnected) await refresh();
      } catch (error) {
        status.className = "small error";
        status.textContent = error.message;
        revoke.disabled = false;
      }
    });
    const adapters = (child.adapters || []).join(", ");
    const mcp = (child.mcp || []).join(", ");
    const presence = childPresenceBadge(child.child_state, child.last_heartbeat);
    const localLlms = Object.entries(child.local_llm || {});
    return el("div", { class: "row" },
      el("div", { class: "row-main" },
        el("span", { class: "row-title" }, child.name),
        rowMeta([
          presence?.label,
          child.version || "no hello",
          child.build_id ? `Build ${child.build_id}` : null,
          child.update_offer ? `Update available: ${child.update_offer.version} (${child.update_offer.build_id})` : null,
          presence?.detail || child.last_heartbeat,
        ]),
        el("dl", { class: "kv" }, workspacePolicyFields(child.workspace_policy)),
        localLlms.length ? el("div", {},
          el("p", { class: "small muted" }, "Local LLMs"),
          el("ul", { class: "small" }, localLlms.map(([name, models]) =>
            el("li", {}, el("code", {}, name), " · ", models.join(", "))))) : null,
        workspaceWarnings(child.alias_warnings)),
      el("div", { class: "row-sub" },
        dot(presence?.tone),
        adapters ? `adapters ${adapters}` : "no adapters",
        mcp ? `mcp ${mcp}` : "no mcp"),
      revoke);
  });
  main.append(el("section", { class: "card" }, el("div", { class: "rows" }, rows)));
}
