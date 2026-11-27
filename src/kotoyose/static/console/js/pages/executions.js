// Executions: conversation/session list on the left, ordered turns with resume lineage and live SSE events on the right.

import { api } from "../api.js";
import { dot, el, errorLine, rowMeta } from "../dom.js";
import { openPeek } from "../peek.js";
import {
  clockLabel,
  count,
  durationLabel,
  shortId,
  statusTone,
  throughputLabel,
  timeLabel,
  tokenPair,
} from "../format.js";

const SESSION_LIMIT = 100;
const COLLAPSE_CHAR_LIMIT = 420;
const COLLAPSE_LINE_LIMIT = 8;

let searchQuery = "";
let showSubagents = false;

function enc(value) {
  return encodeURIComponent(value);
}

function sessionHref(session) {
  const key = enc(session.conversation_key || "");
  if (!session.provider && !session.runtime_session_id) {
    return `#/executions/session/${key}`;
  }
  return `#/executions/session/${key}/${enc(session.provider || "~")}/${enc(session.runtime_session_id || "~")}`;
}

function parseRoute(rest) {
  if (!rest || rest.length === 0) return { mode: "empty" };
  if (rest[0] === "session" && rest[1]) {
    return {
      mode: "session",
      conversationKey: rest[1],
      provider: rest[2] && rest[2] !== "~" ? rest[2] : null,
      runtimeSessionId: rest[3] && rest[3] !== "~" ? rest[3] : null,
    };
  }
  if (rest.length === 1 && /^\d+$/.test(rest[0])) {
    return { mode: "interaction", interactionId: Number(rest[0]) };
  }
  if (rest.length === 1) {
    return { mode: "session", conversationKey: rest[0], provider: null, runtimeSessionId: null };
  }
  return { mode: "empty" };
}

function sessionMatchesSearch(session, needle) {
  if (!needle) return true;
  const haystack = [
    session.conversation_key,
    session.model,
    session.provider,
    session.runtime_session_id,
    session.claude_session_id,
    ...(session.sources || []),
  ]
    .filter(Boolean)
    .join(" ")
    .toLowerCase();
  return haystack.includes(needle);
}

function isSelectedSession(session, target) {
  if (!target || !target.conversationKey) return false;
  if (session.conversation_key !== target.conversationKey) return false;
  if (target.runtimeSessionId && session.runtime_session_id !== target.runtimeSessionId) return false;
  if (target.provider && session.provider !== target.provider) return false;
  return true;
}

function sessionRow(session, selected) {
  const metaParts = [
    session.model,
    session.last_seen_at ? el("span", { class: "mono" }, timeLabel(session.last_seen_at)) : null,
  ];
  if (session.provider) metaParts.push(session.provider);
  return el("a", {
    class: "row route-row",
    href: sessionHref(session),
    "aria-current": selected ? "true" : null,
  },
  el("div", { class: "row-main" },
    el("span", { class: "row-title mono" }, session.conversation_key || "—"),
    rowMeta(metaParts)),
  el("span", { class: "row-sub" },
    session.is_active ? dot("positive") : null,
    `${count(session.turn_count)} ${session.turn_count === 1 ? "turn" : "turns"}`));
}

function sessionsCard(sessions, sessionsError, target, isAlive) {
  const countNode = el("span", { class: "mono small muted" }, "");
  const rowsWrap = el("div", { class: "rows" });
  const emptyNode = el("p", { class: "card-body muted" }, "");
  const searchInput = el("input", {
    class: "input",
    type: "search",
    value: searchQuery,
    placeholder: "Search conversations by key, alias, session…",
    "aria-label": "Search conversations",
  });

  function renderFiltered() {
    const needle = searchQuery.trim().toLowerCase();
    const filtered = sessions.filter((s) => sessionMatchesSearch(s, needle));
    countNode.textContent = needle ? `${filtered.length} / ${sessions.length}` : String(sessions.length);
    if (sessionsError) {
      rowsWrap.replaceChildren(
        el("div", { class: "card-body" }, errorLine(`Conversations are unavailable: ${sessionsError.message}`)),
      );
      return;
    }
    if (sessions.length === 0) {
      emptyNode.textContent = "No conversations recorded yet.";
      rowsWrap.replaceChildren(emptyNode);
      return;
    }
    if (filtered.length === 0) {
      emptyNode.textContent = `No conversations match "${searchQuery.trim()}".`;
      rowsWrap.replaceChildren(emptyNode);
      return;
    }
    rowsWrap.replaceChildren(
      ...filtered.map((s) => sessionRow(s, isSelectedSession(s, target))),
    );
  }

  searchInput.addEventListener("input", () => {
    searchQuery = searchInput.value;
    renderFiltered();
  });
  const subagentToggle = el("input", { type: "checkbox", checked: showSubagents });
  subagentToggle.addEventListener("change", async () => {
    showSubagents = subagentToggle.checked;
    subagentToggle.disabled = true;
    try {
      const res = await api.sessions(SESSION_LIMIT, 0, { includeSubagents: showSubagents });
      sessions = (res && res.items) || [];
      sessionsError = null;
    } catch (err) {
      sessionsError = err;
    } finally {
      if (isAlive()) {
        subagentToggle.disabled = false;
        renderFiltered();
      }
    }
  });
  renderFiltered();

  return el("section", { class: "card", "aria-label": "Conversations" },
    el("div", { class: "card-head" },
      el("h2", { class: "card-title" }, "Conversations"),
      countNode),
    el("div", { class: "list-filter" },
      searchInput,
      el("label", { class: "small" }, subagentToggle, " Show subagent / delegated")),
    rowsWrap);
}

function previewText(text) {
  const lines = text.split("\n");
  if (lines.length > COLLAPSE_LINE_LIMIT) {
    const sliced = lines.slice(0, COLLAPSE_LINE_LIMIT).join("\n");
    return sliced.length > COLLAPSE_CHAR_LIMIT ? `${sliced.slice(0, COLLAPSE_CHAR_LIMIT)}…` : `${sliced}\n…`;
  }
  return `${text.slice(0, COLLAPSE_CHAR_LIMIT)}…`;
}

function collapsibleText(text, emptyFallback = "—") {
  if (!text) {
    return el("pre", { class: "turn-text muted" }, emptyFallback);
  }
  const str = String(text);
  const isLong = str.length > COLLAPSE_CHAR_LIMIT || str.split("\n").length > COLLAPSE_LINE_LIMIT;
  if (!isLong) {
    return el("pre", { class: "turn-text" }, str);
  }

  let expanded = false;
  const collapsedStr = previewText(str);
  const pre = el("pre", { class: "turn-text" }, collapsedStr);
  const toggle = el("button", {
    type: "button",
    class: "btn btn-sm",
    "aria-expanded": "false",
  }, "Show more");
  toggle.addEventListener("click", () => {
    expanded = !expanded;
    pre.textContent = expanded ? str : collapsedStr;
    toggle.textContent = expanded ? "Show less" : "Show more";
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
  });
  return el("div", { class: "turn-text-wrap" }, pre, el("div", {}, toggle));
}

function turnSessionId(turn) {
  return (turn && (turn.runtime_session_id || turn.claude_session_id)) || null;
}

function computeResumeNotes(turns) {
  const firstBySid = new Map();
  const prevBySid = new Map();
  return turns.map((turn, index) => {
    const sid = turnSessionId(turn);
    let note = null;
    if (sid && prevBySid.has(sid)) {
      const prev = prevBySid.get(sid);
      note = `resumed from turn ${prev.index} (${shortId(sid)})`;
    } else if (turn.session_event === "resumed" || turn.session_event === "cache_hit") {
      note = sid ? `resumed from ${shortId(sid)}` : `resumed (${turn.session_event})`;
    }
    if (sid) {
      if (!firstBySid.has(sid)) firstBySid.set(sid, { index: index + 1, id: turn.id });
      prevBySid.set(sid, { index: index + 1, id: turn.id });
    }
    return note;
  });
}

function eventRowNode(ev) {
  return el("div", { class: "event-row" },
    el("span", { class: "mono muted" }, ev.ts ? clockLabel(ev.ts) : "—"),
    el("span", { class: "event-kind" }, ev.tool_name || ev.kind || "event"),
    el("span", { class: "event-summary" }, ev.summary || ""));
}

function turnDurationText(turn) {
  if (turn.status === "in_progress" && turn.received_at) {
    const started = Date.parse(turn.received_at);
    if (Number.isFinite(started)) return durationLabel(Date.now() - started);
  }
  return durationLabel(turn.duration_ms);
}

function turnMeta(turn) {
  return rowMeta(
    `started ${timeLabel(turn.received_at)}`,
    turn.completed_at ? `completed ${timeLabel(turn.completed_at)}` : null,
    `duration ${turnDurationText(turn)}`,
    `tokens ${tokenPair(turn.tokens_in, turn.tokens_out, turn.tokens_source)}`,
    turn.output_tokens_per_second != null ? throughputLabel(turn) : null,
    turn.source ? `source ${turn.source}` : null,
  );
}

function renderTurnCard(turn, index, resumeNote, isTarget, registerStream, isAlive) {
  const statusNode = el("span", { class: "row-status small" },
    dot(statusTone(turn.status)),
    turn.status || "—");
  const metaNode = el("div", { class: "turn-meta" }, turnMeta(turn));
  const outputBody = el("div", { class: "turn-block" },
    el("span", { class: "field-label" }, turn.error_message && !turn.response ? "Error" : "Output"),
    turn.status === "in_progress" && !turn.response
      ? el("pre", { class: "turn-text muted" }, "Waiting for output…")
      : collapsibleText(turn.response, turn.error_message ? "" : "(no output)"),
    turn.error_message ? errorLine(turn.error_message) : null,
  );

  const card = el("article", {
    class: `turn-card${isTarget ? " is-target" : ""}`,
    "data-interaction-id": String(turn.id),
  },
  el("div", { class: "turn-head" },
    el("div", { class: "turn-head-left" },
      el("span", { class: "mono small" }, `Turn ${index + 1}`),
      el("span", { class: "mono small muted" }, `#${turn.id}`),
      statusNode,
      resumeNote ? el("span", { class: "resume-note mono" }, resumeNote) : null),
    el("span", { class: "item-name" }, turn.model || "")),
  metaNode,
  el("div", { class: "turn-block" },
    el("span", { class: "field-label" }, "Prompt"),
    collapsibleText(turn.prompt, "(empty prompt)")),
  outputBody);

  if (turn.status === "in_progress") {
    const liveStatus = el("span", { class: "row-status small muted" },
      dot("ring-positive"),
      "Streaming events…");
    const timeline = el("div", { class: "event-timeline" },
      el("span", { class: "small muted" }, "Connecting to event stream…"));
    const eventsBlock = el("div", { class: "turn-block" },
      el("div", { class: "sub-head" },
        el("span", { class: "field-label" }, "Live events"),
        liveStatus),
      timeline);
    card.append(eventsBlock);

    const seenSeq = new Set();
    let lastSeq = -1;
    let hasEvents = false;
    let finalized = false;

    const appendEvent = (ev) => {
      if (!ev || seenSeq.has(ev.seq)) return;
      seenSeq.add(ev.seq);
      lastSeq = Math.max(lastSeq, ev.seq);
      const nearEnd = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight <= 40;
      if (!hasEvents) {
        hasEvents = true;
        timeline.replaceChildren();
      }
      timeline.append(eventRowNode(ev));
      if (nearEnd) timeline.scrollTop = timeline.scrollHeight;
    };

    const connectStream = () => {
      if (finalized || !isAlive()) return;
      const source = new EventSource(
        `/admin/interactions/${enc(turn.id)}/events/stream?after_seq=${lastSeq}`,
      );
      registerStream(source);
      source.addEventListener("activity", (event) => {
        if (!isAlive()) {
          source.close();
          return;
        }
        try {
          appendEvent(JSON.parse(event.data));
        } catch (err) {
          // Ignore malformed event payloads.
        }
      });
      source.onerror = async () => {
        source.close();
        if (finalized || !isAlive()) return;
        try {
          const latest = await api.interaction(turn.id);
          if (!isAlive()) return;
          if (latest.status === "in_progress") {
            connectStream();
            return;
          }
          finalized = true;
          statusNode.replaceChildren(dot(statusTone(latest.status)), latest.status || "—");
          metaNode.textContent = buildTurnMetaText(latest);
          outputBody.replaceChildren(
            el("span", { class: "field-label" }, latest.error_message && !latest.response ? "Error" : "Output"),
            collapsibleText(latest.response, latest.error_message ? "" : "(no output)"),
            latest.error_message ? errorLine(latest.error_message) : null,
          );
          liveStatus.replaceChildren(
            dot(statusTone(latest.status)),
            hasEvents ? "Stream complete" : "Completed (no events)",
          );
          if (!hasEvents) {
            timeline.replaceChildren(el("span", { class: "small muted" }, "No runtime events recorded."));
          }
        } catch (err) {
          liveStatus.replaceChildren(dot("negative"), `Stream closed (${err.message})`);
        }
      };
    };

    api.interactionEvents(turn.id)
      .then((res) => {
        if (!isAlive()) return;
        const initial = (res && res.items) || [];
        if (initial.length > 0) {
          initial.forEach(appendEvent);
        } else if (!hasEvents) {
          timeline.replaceChildren(el("span", { class: "small muted" }, "Waiting for events…"));
        }
        connectStream();
      })
      .catch(() => {
        if (isAlive()) connectStream();
      });
  }

  return card;
}

function lineageNote(turns) {
  for (const turn of turns) {
    let metadata = turn.metadata;
    if (typeof metadata === "string") {
      try { metadata = JSON.parse(metadata); } catch { continue; }
    }
    const parent = metadata && (metadata.parent_conversation_key || metadata.forked_from);
    if (typeof parent === "string" && parent) {
      return el("span", { class: "small muted" }, "Forked from ",
        el("a", { class: "mono", href: `#/executions/session/${enc(parent)}` }, parent));
    }
  }
  return null;
}

function continuePanel(source, config, isAlive) {
  const models = [...new Set([
    ...Object.keys((config && config.models) || {}),
    ...Object.keys((config && config.pools) || {}),
  ])].sort();
  if (!models.length) return null;
  const model = el("select", { name: "model", class: "input" },
    models.map((name) => el("option", { value: name }, name)));
  model.value = models[0];
  const message = el("textarea", {
    name: "message", class: "input", required: true,
    placeholder: "Continue this conversation on the selected model…",
  });
  const status = el("div", { class: "small muted", "aria-live": "polite" });
  const result = el("div", { "aria-live": "polite" });
  const submit = el("button", { type: "submit", class: "btn" }, "Continue (Fork)");
  let running = false;
  let previewing = false;
  let blocked = false;
  let revision = 0;
  const sync = () => { submit.disabled = running || previewing || blocked; };
  async function preview() {
    const current = ++revision;
    blocked = false;
    status.replaceChildren();
    previewing = typeof api.handoffPreview === "function";
    sync();
    if (!previewing) return;
    status.textContent = "Checking handoff…";
    try {
      const data = await api.handoffPreview({ ...source, model: model.value });
      if (current !== revision || !isAlive()) return;
      blocked = data.policy_blocked_reason != null;
      status.replaceChildren(blocked ? errorLine(data.policy_blocked_reason)
        : el("span", {}, `${data.handoff_turns_included} / ${data.handoff_turns_total} turns · ${data.preview_bytes} bytes${data.handoff_truncated ? " · truncated" : ""}`));
    } catch {
      if (current === revision && isAlive()) status.replaceChildren();
    } finally {
      if (current === revision && isAlive()) { previewing = false; sync(); }
    }
  }
  model.addEventListener("change", preview);
  const form = el("form", { class: "stack" },
    el("label", {}, "Model", model), el("label", {}, "Message", message), status, submit, result);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (running || previewing || blocked || !message.value.trim() || !isAlive()) return;
    running = true;
    model.disabled = true;
    sync();
    result.replaceChildren();
    try {
      const data = await api.continueSession({ ...source, model: model.value, message: message.value });
      if (!isAlive()) return;
      result.replaceChildren(el("pre", { class: "turn-text" }, data.response),
        el("a", { href: `#/executions/session/${enc(data.conversation_key)}` },
          `Open forked session (${data.conversation_key})`));
    } catch (err) {
      if (isAlive()) result.replaceChildren(errorLine(err.message));
    } finally {
      running = false;
      if (isAlive()) { model.disabled = false; sync(); }
    }
  });
  void preview();
  return el("section", { class: "card", "aria-label": "Continue with another model" },
    el("div", { class: "card-head" }, el("h2", { class: "card-title" }, "Continue with another model")),
    el("div", { class: "card-body" }, form));
}

function openExecutionPeek({ title, subtitle, turns, targetId, errorMsg, registerStream, isAlive, source, config }) {
  const onClose = () => { location.hash = "#/executions"; };
  if (errorMsg) {
    return openPeek({
      title: "Execution unavailable",
      subtitle: null,
      body: el("div", { class: "stack" },
        errorLine(errorMsg),
        el("a", { href: "#/executions" }, "Back to executions")),
      onClose,
    });
  }
  if (!turns) return null;
  if (turns.length === 0) {
    return openPeek({
      title,
      titleMono: true,
      subtitle,
      body: el("p", { class: "muted" }, "No turns found for this conversation."),
      onClose,
    });
  }

  const resumeNotes = computeResumeNotes(turns);
  return openPeek({
    title,
    titleMono: true,
    subtitle: el("span", { class: "stack" }, subtitle, lineageNote(turns)),
    body: el("div", { class: "turns-list" },
      source ? continuePanel(source, config, isAlive) : null,
      turns.map((turn, idx) =>
        renderTurnCard(turn, idx, resumeNotes[idx], turn.id === targetId, registerStream, isAlive))),
    onClose,
  });
}

export async function renderExecutions(main, { rest, config }) {
  main.classList.add("main--wide");
  const route = parseRoute(rest);
  const activeStreams = new Set();
  let disposed = false;

  const isAlive = () => !disposed && main.isConnected;
  const registerStream = (source) => {
    activeStreams.add(source);
  };
  const cleanup = () => {
    disposed = true;
    for (const source of activeStreams) {
      try {
        source.close();
      } catch (err) {
        // Ignore close errors.
      }
    }
    activeStreams.clear();
  };

  let sessions = [];
  let sessionsError = null;
  let detailProps = { turns: null };
  let highlightTarget = null;

  const sessionsPromise = api.sessions(SESSION_LIMIT, 0, { includeSubagents: showSubagents })
    .then((res) => {
      sessions = (res && res.items) || [];
    })
    .catch((err) => {
      sessionsError = err;
    });

  let detailPromise = Promise.resolve();
  if (route.mode === "interaction") {
    detailPromise = api.interaction(route.interactionId)
      .then(async (interaction) => {
        const sid = turnSessionId(interaction);
        highlightTarget = {
          conversationKey: interaction.conversation_key || null,
          provider: interaction.provider || null,
          runtimeSessionId: sid,
        };
        if (interaction.conversation_key) {
          const query = sid
            ? { runtimeSessionId: sid, provider: interaction.provider || null }
            : {};
          const sessionRes = await api.sessionDetail(interaction.conversation_key, query);
          let turns = (sessionRes && sessionRes.turns) || [];
          if (!turns.some((t) => t.id === interaction.id)) {
            turns = turns.length === 0 ? [interaction] : [...turns, interaction];
          }
          detailProps = {
            title: interaction.conversation_key,
            subtitle: rowMeta(`${turns.length} ${turns.length === 1 ? "turn" : "turns"}`,
              sid ? `session ${shortId(sid)}` : null),
            turns,
            targetId: interaction.id,
          };
        } else {
          detailProps = {
            title: `Interaction #${interaction.id}`,
            subtitle: rowMeta(interaction.model, interaction.status),
            turns: [interaction],
            targetId: interaction.id,
          };
        }
      })
      .catch((err) => {
        detailProps = { errorMsg: `Could not load interaction #${route.interactionId}: ${err.message}` };
      });
  } else if (route.mode === "session") {
    highlightTarget = {
      conversationKey: route.conversationKey,
      provider: route.provider,
      runtimeSessionId: route.runtimeSessionId,
    };
    detailPromise = api.sessionDetail(route.conversationKey, {
      runtimeSessionId: route.runtimeSessionId,
      provider: route.provider,
    })
      .then((sessionRes) => {
        const turns = (sessionRes && sessionRes.turns) || [];
        const sid = route.runtimeSessionId || (turns.length > 0 ? turnSessionId(turns[turns.length - 1]) : null);
        const totalIn = turns.reduce((sum, t) => sum + (t.tokens_in || 0), 0);
        const totalOut = turns.reduce((sum, t) => sum + (t.tokens_out || 0), 0);
        detailProps = {
          title: route.conversationKey,
          subtitle: rowMeta(`${turns.length} ${turns.length === 1 ? "turn" : "turns"}`,
            sid ? `session ${shortId(sid)}` : null, `${count(totalIn)} → ${count(totalOut)} tokens`),
          turns,
          targetId: null,
        };
      })
      .catch((err) => {
        detailProps = { errorMsg: `Could not load conversation "${route.conversationKey}": ${err.message}` };
      });
  }

  await Promise.all([sessionsPromise, detailPromise]);
  if (!isAlive()) {
    cleanup();
    return cleanup;
  }

  main.append(
    el("header", { class: "page-head" },
      el("div", {},
        el("h1", { class: "page-title" }, "Executions"),
        el("p", { class: "page-sub" }, "Browse conversations, inspect turns, and follow live activity.")),
      el("a", { class: "btn", href: "#/usage" }, "View usage")),
    sessionsCard(sessions, sessionsError, highlightTarget, isAlive),
  );

  const peekBody = route.mode !== "empty"
    ? openExecutionPeek({ ...detailProps, registerStream, isAlive, config,
      source: route.mode === "session" ? {
        source_conversation_key: route.conversationKey,
        ...(route.provider ? { source_provider: route.provider } : {}),
        ...(route.runtimeSessionId ? { source_runtime_session_id: route.runtimeSessionId } : {}),
      } : null })
    : null;

  if (peekBody && detailProps.targetId) {
    const targetNode = peekBody.querySelector(`[data-interaction-id="${detailProps.targetId}"]`);
    if (targetNode && typeof targetNode.scrollIntoView === "function") {
      targetNode.scrollIntoView({ block: "nearest" });
    }
  }

  return cleanup;
}
