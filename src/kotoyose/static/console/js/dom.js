// DOM helpers. Strings always enter as text nodes, so no innerHTML is used anywhere in the console.

/**
 * Create an element. `props` takes attributes, `class`, `on*` listeners and boolean flags.
 * `children` may be nodes, strings, numbers, arrays, or null/false (skipped).
 */
export function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") node.className = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (value === true) node.setAttribute(key, "");
    else node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

/** Append children (nodes, strings, numbers, nested arrays). null / false / undefined are skipped. */
export function append(node, children) {
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

/** A small colored dot. `tone` is one of positive, warning, negative, ring-positive, ring-warning. */
export function dot(tone) {
  return el("span", { class: `dot ${tone || ""}`, "aria-hidden": "true" });
}

/** An inline error line, or nothing. */
export function errorLine(message) {
  return el("p", { class: "error", role: "alert" }, message || null);
}

const SVG_NS = "http://www.w3.org/2000/svg";

/** Create an SVG element with attributes (no innerHTML). */
export function svgEl(tag, props = {}, ...children) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === undefined || value === null || value === false) continue;
    node.setAttribute(key, String(value));
  }
  append(node, children);
  return node;
}

export function rowMeta(...parts) {
  const values = parts.flat().filter((part) => part !== null && part !== undefined && part !== false && part !== "");
  return values.length ? el("span", { class: "row-sub" }, values.map((part) => el("span", {}, part))) : null;
}

export function pathText(path) {
  return path ? el("span", { class: "file-path mono", title: path }, path) : null;
}

export function attachmentTags(names, hrefFor = null) {
  if (!names.length) return null;
  const limit = 4;
  const tag = (name) => {
    const href = hrefFor?.(name);
    return el(href ? "a" : "span", { class: "place-tag", href }, name);
  };
  const remaining = names.slice(limit);
  const count = `+${remaining.length}`;
  const more = remaining.length === 0 ? null : hrefFor
    ? el("details", { class: "attachment-overflow" },
      el("summary", { class: "place-tag", "aria-label": `Show ${remaining.length} more attachments` }, count),
      el("div", { class: "attachment-tags" }, remaining.map(tag)))
    : el("span", { class: "place-tag", title: remaining.join(", ") }, count);
  return el("div", { class: "attachment-tags" }, names.slice(0, limit).map(tag), more);
}

export function responsiveTable(table) {
  table.setAttribute("class", "table responsive-table");
  table.setAttribute("role", "table");
  const [head, body] = table.children;
  head.setAttribute("role", "rowgroup");
  body.setAttribute("role", "rowgroup");
  const header = head.children[0];
  header.setAttribute("role", "row");
  const headings = Array.from(header.children, (cell) => {
    cell.setAttribute("role", "columnheader");
    cell.setAttribute("scope", "col");
    return cell.textContent;
  });
  for (const row of body.children) {
    row.setAttribute("role", "row");
    Array.from(row.children).forEach((cell, i) => {
      cell.setAttribute("role", "cell");
      const label = el("span", { class: "table-cell-label", "aria-hidden": "true" }, headings[i]);
      const content = el("div", { class: "table-cell-content" }, Array.from(cell.childNodes || cell.children));
      cell.replaceChildren(label, content);
    });
  }
  return el("div", { class: "table-scroll" }, table);
}
