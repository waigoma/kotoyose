// Notion-style right-anchored side peek dialog, focus restoration, and form dirty-state tracking.

import { dot, el } from "./dom.js";

const CLOSE_TRANSITION_MS = 220;
const WIDTH_STORAGE_KEY = "kotoyose.console.peekWidth";
const DEFAULT_WIDTH = 640;
const MIN_WIDTH = 420;
const MIN_LEFT_GAP = 120; // 一覧の端が少しは見えるように残す幅
const KEY_STEP = 32;

function viewportWidth() {
  const width = Number(typeof window !== "undefined" ? window.innerWidth : NaN);
  return Number.isFinite(width) && width > 0 ? width : DEFAULT_WIDTH + MIN_LEFT_GAP;
}

function clampWidth(width) {
  const max = Math.max(MIN_WIDTH, viewportWidth() - MIN_LEFT_GAP);
  return Math.round(Math.min(Math.max(width, MIN_WIDTH), max));
}

function loadWidth() {
  try {
    const value = Number(window.localStorage.getItem(WIDTH_STORAGE_KEY));
    return Number.isFinite(value) && value > 0 ? value : DEFAULT_WIDTH;
  } catch {
    return DEFAULT_WIDTH;
  }
}

function saveWidth(width) {
  try {
    window.localStorage.setItem(WIDTH_STORAGE_KEY, String(width));
  } catch {
    // 保存できない環境 (private window 等) でも幅の変更自体は効く。
  }
}

function applyWidth(panel, width) {
  const clamped = clampWidth(width);
  if (panel.style && typeof panel.style.setProperty === "function") {
    panel.style.setProperty("--peek-width", `${clamped}px`);
  }
  return clamped;
}

/** 背後のページをスクロールさせない。スクロールバーの幅だけ右に余白を足して横ずれを防ぐ。 */
function lockPageScroll() {
  const root = document.documentElement;
  if (!root || !root.classList || root.classList.contains("peek-scroll-lock")) return;
  const scrollbar = viewportWidth() - (Number(root.clientWidth) || viewportWidth());
  if (root.style && typeof root.style.setProperty === "function") {
    root.style.setProperty("--peek-scrollbar-gap", `${Math.max(scrollbar, 0)}px`);
  }
  root.classList.add("peek-scroll-lock");
}

function unlockPageScroll() {
  const root = document.documentElement;
  if (!root || !root.classList) return;
  root.classList.remove("peek-scroll-lock");
  if (root.style && typeof root.style.removeProperty === "function") {
    root.style.removeProperty("--peek-scrollbar-gap");
  }
}

/** 左端の把手。drag・矢印キーで幅を変え、double click で既定に戻す。 */
function createResizeHandle(panel) {
  const handle = el("div", {
    class: "peek-resize",
    role: "separator",
    "aria-orientation": "vertical",
    "aria-label": "Resize panel",
    title: "Drag to resize (double-click to reset)",
    tabindex: "0",
  });
  const sync = (width) => {
    handle.setAttribute("aria-valuenow", String(width));
    handle.setAttribute("aria-valuemin", String(MIN_WIDTH));
    handle.setAttribute("aria-valuemax", String(clampWidth(Number.MAX_SAFE_INTEGER)));
  };
  let current = applyWidth(panel, loadWidth());
  sync(current);

  handle.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    handle.setPointerCapture(event.pointerId);
    panel.classList.add("is-resizing");
    document.documentElement.classList.add("peek-resizing");
    const onMove = (moveEvent) => {
      current = applyWidth(panel, window.innerWidth - moveEvent.clientX);
      sync(current);
    };
    const onUp = () => {
      handle.removeEventListener("pointermove", onMove);
      handle.removeEventListener("pointerup", onUp);
      handle.removeEventListener("pointercancel", onUp);
      panel.classList.remove("is-resizing");
      document.documentElement.classList.remove("peek-resizing");
      saveWidth(current);
    };
    handle.addEventListener("pointermove", onMove);
    handle.addEventListener("pointerup", onUp);
    handle.addEventListener("pointercancel", onUp);
  });
  handle.addEventListener("keydown", (event) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    current = applyWidth(panel, current + (event.key === "ArrowLeft" ? KEY_STEP : -KEY_STEP));
    sync(current);
    saveWidth(current);
  });
  handle.addEventListener("dblclick", () => {
    current = applyWidth(panel, DEFAULT_WIDTH);
    sync(current);
    saveWidth(current);
  });
  const onWindowResize = () => {
    current = applyWidth(panel, current);
    sync(current);
  };
  const canListen = typeof window !== "undefined" && typeof window.addEventListener === "function";
  if (canListen) window.addEventListener("resize", onWindowResize);
  return {
    handle,
    dispose: () => { if (canListen) window.removeEventListener("resize", onWindowResize); },
  };
}

let active = null;
let capturedEl = null;
let capturedHref = null;
let pendingRestoreEl = null;
let pendingRestoreHref = null;
let peekReusedInRender = false;

function prefersReducedMotion() {
  return Boolean(
    typeof window !== "undefined"
    && window.matchMedia
    && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );
}

function appendBody(container, body) {
  if (body === undefined || body === null || body === false) return;
  if (Array.isArray(body)) {
    for (const item of body) appendBody(container, item);
    return;
  }
  container.append(body instanceof Node ? body : document.createTextNode(String(body)));
}

/** Capture the currently focused trigger element before a hash navigation replaces #main. */
export function captureTriggerFocus() {
  if (active && !active.closing) return;
  const current = document.activeElement;
  if (current && current !== document.body && current !== document.documentElement) {
    capturedEl = current;
    capturedHref = (typeof current.getAttribute === "function" && current.getAttribute("href")) || null;
  } else {
    capturedEl = null;
    capturedHref = null;
  }
}

/** Restore focus to the row/button that originally opened the peek, if it is in the DOM. */
export function restoreTriggerFocus(container = document.getElementById("main")) {
  if (active && !active.closing) return;
  const elTarget = pendingRestoreEl;
  const hrefTarget = pendingRestoreHref;
  pendingRestoreEl = null;
  pendingRestoreHref = null;

  if (elTarget && elTarget.isConnected && typeof elTarget.focus === "function") {
    elTarget.focus({ preventScroll: true });
    return;
  }
  if (hrefTarget && container && typeof container.querySelectorAll === "function") {
    for (const anchor of container.querySelectorAll("a[href]")) {
      if (anchor.getAttribute("href") === hrefTarget) {
        anchor.focus({ preventScroll: true });
        return;
      }
    }
  }
}

/** Track whether a form/panel inside the peek has unsaved edits. */
export function createDirtyTracker(root = null) {
  let dirty = false;
  const markDirty = () => { dirty = true; };
  const attach = (node) => {
    if (!node || typeof node.addEventListener !== "function") return;
    node.addEventListener("input", markDirty);
    node.addEventListener("change", markDirty);
  };
  if (root) attach(root);
  return {
    attach,
    isDirty: () => dirty,
    markDirty,
    markClean: () => { dirty = false; },
  };
}

function removeActiveNow() {
  if (!active) return;
  const state = active;
  active = null;
  if (state.closeTimer !== null) {
    clearTimeout(state.closeTimer);
    state.closeTimer = null;
  }
  document.removeEventListener("keydown", state.onKeyDown);
  if (state.disposeResize) state.disposeResize();
  if (state.root && state.root.parentNode) {
    state.root.remove();
  }
  unlockPageScroll();
}

/** Immediately remove any open peek without running onClose or dirty confirmation. */
export function destroyPeek() {
  removeActiveNow();
}

/** Reset the per-render reuse flag before a page renders. */
export function beginPeekRender() {
  peekReusedInRender = false;
}

/** Whether openPeek was called during the current render pass. */
export function wasPeekReused() {
  return peekReusedInRender;
}

/** Whether a non-closing peek is currently open. */
export function isPeekOpen() {
  return Boolean(active && !active.closing);
}

/** Close the active peek. Prompts for confirmation when isDirty() returns true unless force=true. */
export function closePeek({ force = false, skipOnClose = false } = {}) {
  if (!active || active.closing) return true;
  if (!force && typeof active.isDirty === "function" && active.isDirty()) {
    if (!window.confirm("Discard unsaved changes?")) {
      return false;
    }
  }

  const state = active;
  state.closing = true;
  document.removeEventListener("keydown", state.onKeyDown);

  pendingRestoreEl = state.triggerEl;
  pendingRestoreHref = state.triggerHref;

  const beforeHash = location.hash;
  if (!skipOnClose && typeof state.onClose === "function") {
    state.onClose();
  }
  const navigated = location.hash !== beforeHash;

  const finish = () => {
    if (active === state) {
      removeActiveNow();
    } else if (state.root && state.root.parentNode) {
      state.root.remove();
    }
    if (!navigated) {
      restoreTriggerFocus();
    }
  };

  state.root.classList.remove("is-open");
  if (prefersReducedMotion()) {
    finish();
  } else {
    state.panel.addEventListener("transitionend", (event) => {
      if (event.target === state.panel) finish();
    }, { once: true });
    state.closeTimer = setTimeout(finish, CLOSE_TRANSITION_MS);
  }
  return true;
}

function applyBadge(badgeEl, badge) {
  badgeEl.replaceChildren();
  if (!badge) {
    badgeEl.hidden = true;
    return;
  }
  if (badge instanceof Node) {
    badgeEl.append(badge);
  } else {
    if (badge.tone !== undefined) badgeEl.append(dot(badge.tone));
    if (badge.label) badgeEl.append(document.createTextNode(String(badge.label)));
  }
  badgeEl.hidden = false;
}

/** Collapsible <details> disclosure for edit forms inside a peek. Opens automatically on invalid input. */
export function createSettingsDisclosure({ title = "Settings", open = true, content, form = null } = {}) {
  const summary = el("summary", { class: "peek-details-summary" },
    el("span", { class: "sub-title" }, title));
  const details = el("details", { class: "sub-section peek-details", open: Boolean(open) }, summary);
  appendBody(details, content);
  const openSection = () => {
    details.open = true;
  };
  const targetForm = form || (content && content.tagName === "FORM" ? content : null);
  if (targetForm && typeof targetForm.addEventListener === "function") {
    targetForm.addEventListener("invalid", openSection, true);
  }
  return { details, open: openSection };
}

/**
 * Open (or update in place) the right-anchored side peek dialog.
 * @param {object} options
 * @param {string} options.title
 * @param {string|Node|null} [options.subtitle]
 * @param {boolean} [options.titleMono]
 * @param {{tone?: string, label?: string}|Node|null} [options.badge]
 * @param {Node|Node[]|string} options.body
 * @param {Function} [options.onClose]
 * @param {Function|null} [options.isDirty]
 */
export function openPeek({ title, subtitle = null, titleMono = false, badge = null, body, onClose = null, isDirty = null }) {
  peekReusedInRender = true;
  const subtitleContent = subtitle || "";
  const peekTitleClass = `card-title peek-title${titleMono ? " mono" : ""}`;

  if (active) {
    if (active.closeTimer !== null) {
      clearTimeout(active.closeTimer);
      active.closeTimer = null;
    }
    if (active.closing) {
      active.closing = false;
      document.addEventListener("keydown", active.onKeyDown);
      pendingRestoreEl = null;
      pendingRestoreHref = null;
    }
    active.titleEl.textContent = title || "";
    active.titleEl.className = peekTitleClass;
    active.subtitleEl.replaceChildren(subtitleContent);
    active.subtitleEl.hidden = !subtitleContent;
    applyBadge(active.badgeEl, badge);
    active.bodyEl.replaceChildren();
    appendBody(active.bodyEl, body);
    active.bodyEl.scrollTop = 0;
    active.onClose = onClose;
    active.isDirty = isDirty;
    active.root.classList.add("is-open");
    return active.bodyEl;
  }

  const triggerEl = capturedEl || (document.activeElement !== document.body ? document.activeElement : null);
  const triggerHref = capturedHref || location.hash || null;
  capturedEl = null;
  capturedHref = null;
  pendingRestoreEl = null;
  pendingRestoreHref = null;

  const titleId = "peek-dialog-title";
  const titleEl = el("h2", { class: peekTitleClass, id: titleId }, title || "");
  const badgeEl = el("span", { class: "row-status mono small peek-badge", hidden: true });
  applyBadge(badgeEl, badge);
  const subtitleEl = el("span", {
    class: "small muted peek-subtitle",
    hidden: !subtitleContent,
  }, subtitleContent);
  const closeBtn = el("button", {
    class: "btn btn-sm peek-close",
    type: "button",
    "aria-label": "Close panel",
    title: "Close (Esc)",
  }, "×");
  const bodyEl = el("div", { class: "peek-body detail-body" });
  appendBody(bodyEl, body);

  const panel = el("aside", {
    class: "peek-panel",
    role: "dialog",
    "aria-modal": "true",
    "aria-labelledby": titleId,
    tabindex: "-1",
  },
  el("header", { class: "peek-head" },
    el("div", { class: "title-stack peek-head-text" },
      el("div", { class: "peek-title-row" }, titleEl, badgeEl),
      subtitleEl),
    closeBtn),
  bodyEl);

  const resize = createResizeHandle(panel);
  panel.append(resize.handle); // 位置は CSS の absolute で左端に置く

  const backdrop = el("div", { class: "peek-backdrop", "aria-hidden": "true" });
  const root = el("div", { class: "peek-root" }, backdrop, panel);

  const onKeyDown = (event) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      closePeek();
    }
  };

  backdrop.addEventListener("click", () => closePeek());
  closeBtn.addEventListener("click", () => closePeek());
  document.addEventListener("keydown", onKeyDown);

  active = {
    root,
    backdrop,
    panel,
    titleEl,
    badgeEl,
    subtitleEl,
    bodyEl,
    closeBtn,
    onClose,
    isDirty,
    triggerEl,
    triggerHref,
    onKeyDown,
    closing: false,
    closeTimer: null,
    disposeResize: resize.dispose,
  };

  document.body.append(root);
  lockPageScroll();
  if (prefersReducedMotion()) {
    root.classList.add("is-open");
  } else {
    requestAnimationFrame(() => {
      if (active && active.root === root && !active.closing) {
        root.classList.add("is-open");
      }
    });
  }
  panel.focus({ preventScroll: true });
  return bodyEl;
}
