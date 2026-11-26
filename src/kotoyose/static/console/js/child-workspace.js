import { el } from "./dom.js";

const NOT_REPORTED = "Not reported by this child version";

/** Render the workspace policy fields inside a definition list. */
export function workspacePolicyFields(report) {
  const roots = report?.workspace_roots;
  return [
    el("dt", {}, "Workspace folders"),
    el("dd", {}, roots?.length
      ? roots.map((root) => el("div", { class: "mono" }, root))
      : (roots ? "No workspace folders configured" : NOT_REPORTED)),
    el("dt", {}, "Codex sandbox limit"),
    el("dd", { class: report ? "mono" : null }, report?.codex_sandbox_max ?? NOT_REPORTED),
  ];
}

/** Render the host's advisory messages as plain text. */
export function workspaceWarnings(warnings) {
  if (!warnings?.length) return null;
  return el("ul", { class: "workspace-warnings small", "aria-label": "Child workspace warnings" },
    warnings.map(({ message }) => el("li", {}, message)));
}
