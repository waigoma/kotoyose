import { api } from "../api.js";
import { dot, el, errorLine, responsiveTable } from "../dom.js";
import { count, timeLabel } from "../format.js";

function table(headings, rows) {
  return responsiveTable(el("table", {},
    el("thead", {}, el("tr", {}, headings.map((heading) => el("th", {}, heading)))),
    el("tbody", {}, rows.map((cells) => el("tr", {}, cells.map((cell) => el("td", {}, cell)))))));
}

function status(label, tone) {
  return el("span", { class: "row-status" }, dot(tone), label);
}

function summary(run) {
  const metrics = [
    ["Sessions detected", run.sessions_detected],
    ["Turns extracted", run.turns_extracted],
    [run.dry_run ? "Would import" : "Imported", run.turns_imported],
    ["Duplicates skipped", run.turns_skipped_duplicate],
    ["Redactions", run.redactions_count],
    ["Truncations", run.truncations_count],
  ];
  return el("section", { class: "card", "aria-label": "Import result" },
    el("div", { class: "card-head" }, el("h3", { class: "card-title" }, run.dry_run ? "Dry-run result" : "Import result")),
    el("div", { class: "card-body stack" },
      el("dl", { class: "kv" }, metrics.map(([label, value]) => [el("dt", {}, label), el("dd", { class: "mono" }, count(value))])),
      (run.errors || []).map((error) => errorLine(typeof error === "string" ? error : JSON.stringify(error)))));
}

export async function renderImports(main, context) {
  const source = el("select", { class: "input", name: "source_kind", "aria-label": "Source" });
  const kinds = new Set(["all", "claude", "codex", "antigravity", "cursor", "grok"]);
  for (const kind of kinds) source.append(el("option", { value: kind }, kind));
  source.value = "all";
  const since = el("input", { class: "input input-mono", type: "date", name: "since", placeholder: "YYYY-MM-DD", "aria-label": "Since" });
  const origin = el("select", { class: "input", name: "origin_hint", "aria-label": "Origin hint" },
    [["", "Unspecified"], ["personal", "Personal"], ["work", "Work"]].map(([value, label]) => el("option", { value }, label)));
  const dryRun = el("input", { type: "checkbox", name: "dry_run", checked: true });
  const submit = el("button", { class: "btn btn-primary", type: "submit" }, "Dry Run");
  const result = el("div", { "aria-live": "polite" });
  const sourceBody = el("div", {}, el("p", { class: "card-body muted" }, "Loading sources…"));
  const runsBody = el("div", {}, el("p", { class: "card-body muted" }, "Loading runs…"));
  const childrenBody = el("div");
  const childResults = new Map();
  const refresh = el("button", { class: "btn btn-sm", type: "button" }, "Refresh");

  async function loadSources() {
    refresh.disabled = true;
    try {
      const data = await api.importSources();
      for (const item of data.sources || []) {
        if (!kinds.has(item.source_kind)) {
          kinds.add(item.source_kind);
          source.append(el("option", { value: item.source_kind }, item.source_kind));
        }
      }
      sourceBody.replaceChildren(data.sources?.length ? table(
        ["Source", "Default Path", "Status", "Files", "Imported Turns", "Last Imported"],
        data.sources.map((item) => [item.source_kind,
          el("span", { class: "mono file-path" }, item.default_path),
          status(item.exists ? "available" : "not found", item.exists ? "positive" : "warning"),
          count(item.file_count), count(item.imported_turns), timeLabel(item.last_imported_at)]))
        : el("p", { class: "card-body muted" }, "No local sources found."));
    } catch (error) {
      sourceBody.replaceChildren(el("div", { class: "card-body" }, errorLine(`Could not load sources: ${error.message}`)));
    } finally {
      refresh.disabled = false;
    }
  }

  async function loadRuns() {
    try {
      const data = await api.importRuns(20);
      runsBody.replaceChildren(data.runs?.length ? table(
        ["Started", "Source", "Mode", "Files", "Sessions", "Imported / Duplicates", "Redacted", "Status"],
        data.runs.map((run) => [timeLabel(run.started_at), run.source_kind,
          el("span", { class: "chip" }, run.dry_run ? "dry-run" : "import"),
          count(run.files_scanned), count(run.sessions_detected),
          `${count(run.turns_imported)} / ${count(run.turns_skipped_duplicate)}`,
          count(run.redactions_count), run.errors?.length
            ? status(`Errors (${run.errors.length})`, "negative")
            : status(run.completed_at ? "Completed" : "Running", run.completed_at ? "positive" : "ring-positive")]))
        : el("p", { class: "card-body muted" }, "No import runs yet."));
    } catch (error) {
      runsBody.replaceChildren(el("div", { class: "card-body" }, errorLine(`Could not load recent runs: ${error.message}`)));
    }
  }

  function childRow(child) {
    const hint = el("select", { class: "input", "aria-label": `${child.name} host origin hint` },
      [["", "Unspecified"], ["personal", "personal"], ["work", "work"]].map(([value, label]) => el("option", { value }, label)));
    hint.value = child.host_origin_hint || "";
    const feedback = el("div", { "aria-live": "polite" }, childResults.get(child.name));
    const save = el("button", { class: "btn btn-sm", type: "button", onclick: async () => {
      if (save.disabled) return;
      save.disabled = true;
      try {
        await api.setChildOriginHint(child.name, hint.value || null);
        await loadChildren();
      } catch (error) {
        feedback.replaceChildren(errorLine(`Could not save origin hint: ${error.message}`));
      } finally { save.disabled = false; }
    } }, "Save");
    const disabled = !child.connected || !child.supports_session_collect || !child.session_import_sources?.length;
    const actions = [true, false].map((dryRun) => el("button", {
      class: "btn btn-sm", type: "button", disabled, onclick: async () => {
        if (actions.some((button) => button.disabled)) return;
        actions.forEach((button) => { button.disabled = true; });
        try {
          const run = await api.collectChildSessions(child.name, { source_kind: "all", dry_run: dryRun });
          childResults.set(child.name, summary(run));
          feedback.replaceChildren(childResults.get(child.name));
          await Promise.all([loadChildren(), loadRuns()]);
        } catch (error) {
          feedback.replaceChildren(errorLine(`Could not collect sessions: ${error.message}`));
        } finally { actions.forEach((button) => { button.disabled = disabled; }); }
      },
    }, dryRun ? "Dry Run" : "Collect"));
    return [el("div", {}, el("span", { class: "mono" }, child.name), el("span", { class: "muted" }, child.platform || "")),
      status(child.connected ? "connected" : "offline", child.connected ? "positive" : "warning"),
      status(child.supports_session_collect ? "session-collect" : "disabled", child.supports_session_collect ? "positive" : "warning"),
      (child.session_import_sources || []).join(", ") || "none",
      el("div", { class: "stack" }, hint, save, el("span", { class: "chip" }, child.effective_origin_hint || "Unspecified")),
      count(child.imported_turns), timeLabel(child.last_imported_at), el("div", { class: "stack" }, actions, feedback)];
  }

  async function loadChildren() {
    try {
      const data = api.importChildren ? await api.importChildren() : { children: [] };
      childrenBody.replaceChildren(data.children?.length
        ? table(["Machine", "Connection", "Capability", "Allowed Sources", "Origin hint", "Imported Turns", "Last Imported", "Actions"], data.children.map(childRow))
        : el("p", { class: "card-body muted" }, "No enrolled child machines."));
    } catch (error) {
      childrenBody.replaceChildren(el("div", { class: "card-body" }, errorLine(`Could not load children: ${error.message}`)));
    }
  }

  const buttonLabel = () => { submit.textContent = dryRun.checked ? "Dry Run" : "Run Import"; };
  dryRun.addEventListener("change", buttonLabel);
  refresh.addEventListener("click", () => refresh.disabled ? undefined : Promise.all([loadSources(), loadRuns(), loadChildren()]));
  const field = (label, input) => el("label", { class: "field" }, el("span", { class: "field-label" }, label), input);
  const form = el("form", { class: "card-body form", onsubmit: async (event) => {
    event.preventDefault();
    if (submit.disabled) return;
    const body = { source_kind: source.value, dry_run: dryRun.checked };
    if (since.value.trim()) body.since = since.value.trim();
    if (origin.value) body.origin_hint = origin.value;
    submit.disabled = true;
    submit.textContent = "Running…";
    result.replaceChildren(el("p", { class: "muted", role: "status" }, "Running import…"));
    try {
      const run = await api.runImport(body);
      result.replaceChildren(summary(run));
      await Promise.all([loadSources(), loadRuns(), loadChildren()]);
    } catch (error) {
      result.replaceChildren(errorLine(`Could not run import: ${error.message}`));
    } finally {
      submit.disabled = false;
      buttonLabel();
    }
  } },
  el("div", { class: "form-grid" }, field("Source", source), field("Since (optional)", since), field("Origin hint", origin)),
  el("label", {}, dryRun, " Dry-run (preview without importing)"),
  el("div", { class: "form-actions" }, submit), result);
  main.append(
    el("header", { class: "page-head" }, el("div", {},
      el("h1", { class: "page-title" }, "Imports"),
      el("p", { class: "page-sub" }, "Import local session history into Kotoyose."))),
    el("section", { class: "card", "aria-label": "Local Sources" },
      el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Local Sources"), refresh), sourceBody),
    el("section", { class: "card", "aria-label": "Run Import" },
      el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Run Import")), form),
    el("section", { class: "card", "aria-label": "Remote Machines (Children)" },
      el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Remote Machines (Children)")), childrenBody),
    el("section", { class: "card", "aria-label": "Recent Runs" },
      el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Recent Runs")), runsBody));
  await Promise.all([loadSources(), loadRuns(), loadChildren()]);
}
