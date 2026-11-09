const DECLARED = "[data-ag-width]";
const CONTENT_TAGS = new Set(["DIV", "SECTION", "ARTICLE", "MAIN", "ASIDE"]);
const SCHEMA = "ag-design/width-audit-v1";

/**
 * Read root/inner-column widths in one bounded local DOM scope, without text, IDs or field values.
 * Declare data-ag-width="fill|content"; content defaults to12em (AG diagnostic policy, not WCAG).
 * An optional data-ag-width-min-em supplies a purpose-specific0.5..100em minimum.
 * Undeclared contained roots and suspect text columns remain unknown, never an inferred pass.
 * Rerun after fonts/layout settle; this captures one environment, not scenario completeness or AT.
 */
export function auditLayoutWidths(root, {maxNodes = 2000, maxTargets = 64} = {}) {
  if (!root?.querySelectorAll || !Number.isInteger(maxNodes) || maxNodes < 1 || maxNodes > 5000 ||
      !Number.isInteger(maxTargets) || maxTargets < 1 || maxTargets > 64)
    throw new TypeError("Use a local DOM scope,1..5000nodes and1..64targets");
  const document = root.ownerDocument ?? root;
  const view = document.defaultView;
  const walker = document.createTreeWalker(root, 1);
  const targets = [];
  let node = root.nodeType === 1 ? root : walker.nextNode();
  let inspectedNodes = 0, truncated = false;
  while (node) {
    if (inspectedNodes >= maxNodes) {truncated = true; break;}
    inspectedNodes++;
    const style = view.getComputedStyle(node);
    const mode = node.getAttribute("data-ag-width");
    const declared = node.matches(DECLARED);
    const contained = style.containerType?.includes("inline-size");
    const suspect = !declared && !contained && suspectColumn(node, style, view);
    if (declared || contained || suspect || node === root) {
      if (targets.length === maxTargets) {truncated = true; break;}
      targets.push(captureWidth(node, style, view, address(node, root, inspectedNodes), declared, mode));
    }
    node = walker.nextNode();
  }
  const coverage = {declared: 0, inferred: 0, measured: 0, unknown: 0, excluded: 0};
  for (const target of targets) {
    coverage[target.source]++;
    if (target.status === "unknown") coverage.unknown++;
    else if (target.status === "not-applicable") coverage.excluded++;
    else coverage.measured++;
  }
  let geometryGate = targets.some(target => target.status === "fail") ? "fail" : "pass";
  if (geometryGate !== "fail" && (truncated || coverage.unknown || !coverage.measured)) geometryGate = "unknown";
  return {schemaVersion: SCHEMA, targets, coverage, geometryGate, inspectedNodes, truncated,
    environment: {viewportWidth: view.innerWidth, scopeWidth: root.nodeType === 1 ? root.getBoundingClientRect().width : null},
    manualRequired: ["purpose-specific minimums", "undeclared candidates", "normal/narrow/text200/narrow-text200", "keyboard/AT/optics"],
    trust: "redacted local DOM geometry; caller-reported evidence, not qualification"};
}

/** Compute available content width only for an unambiguous allocated wrapper; complex tracks stay unknown. */
function availableWidth(element, view, mode) {
  const parent = element.parentElement;
  if (!parent) return null;
  const style = view.getComputedStyle(parent);
  if (["contents", "none"].includes(style.display)) return null;
  if (mode === "fill" && style.display.includes("grid") && style.gridTemplateColumns.trim().split(/\s+/).length !== 1) return null;
  if (mode === "fill" && style.display.includes("flex") && !style.flexDirection.startsWith("column")) {
    let siblings = 0;
    let visits = 0;
    for (const child of parent.children) {
      if (++visits > 64) return null;
      if (view.getComputedStyle(child).display !== "none" && ++siblings > 1) return null;
    }
    if (siblings !== 1) return null;
  }
  const width = parent.clientWidth - number(style.paddingLeft) - number(style.paddingRight);
  return Number.isFinite(width) && width > 0 ? width : null;
}

/** Infer only a suspect content column, not controls, decorative rails or every valid grid child. */
function suspectColumn(element, style, view) {
  if (!CONTENT_TAGS.has(element.tagName) || excludedAncestor(element) || element.getAttribute("aria-hidden") === "true" ||
      !element.parentElement) return false;
  // A visible textbox is a control surface, not an inferred prose column.
  // Keep extremely collapsed controls (<2em) as candidates; explicit width contracts always take precedence.
  const control = element.getAttribute("role") === "textbox" || element.getAttribute("contenteditable") === "true";
  const font = Number.parseFloat(style.fontSize);
  if (control && element.getBoundingClientRect().width >= 2 * font) return false;
  const parent = view.getComputedStyle(element.parentElement);
  if (!parent.display.includes("grid") && !parent.display.includes("flex")) return false;
  const available = element.parentElement.clientWidth - number(parent.paddingLeft) - number(parent.paddingRight);
  return available > 0 && font > 0 && element.getBoundingClientRect().width + 1 < Math.min(12 * font, available) && readableStructure(element);
}

/** Keep hidden, detached, transformed and unsupported slots distinct from a measurable zero-width collapse. */
function captureWidth(element, style, view, id, declared, mode) {
  const fontSize = Number.parseFloat(style.fontSize);
  const minEm = element.hasAttribute("data-ag-width-min-em") ? Number(element.getAttribute("data-ag-width-min-em")) : 12;
  const target = {id, source: declared ? "declared" : "inferred", mode: declared ? mode : null,
    width: null, availableWidth: null, fontSize: Number.isFinite(fontSize) ? fontSize : null,
    minEm: Number.isFinite(minEm) ? minEm : null, hidden: false, status: "unknown", deficitPx: null, reason: "unmeasurable"};
  if (id.startsWith("unknown-")) {target.reason = "identity-limit"; return target;}
  if (!element.isConnected) {target.reason = "detached"; return target;}
  let ancestor = element, ancestors = 0;
  while (ancestor) {
    if (++ancestors > 64) {target.reason = "ancestor-limit"; return target;}
    const current = view.getComputedStyle(ancestor);
    const inlineZoom = ancestor.style?.zoom;
    if (ancestor.hidden || current.display === "none" || current.visibility === "hidden") {
      target.hidden = true; target.status = "not-applicable"; target.reason = "hidden"; return target;
    }
    if ((current.transform && current.transform !== "none") ||
        [current.scale, current.rotate, current.translate].some(value => value && value !== "none") ||
        (current.zoom && !["1", "normal"].includes(current.zoom)) ||
        (inlineZoom && !["1", "normal"].includes(String(inlineZoom)))) {target.reason = "transformed"; return target;}
    ancestor = ancestor.parentElement;
  }
  if (declared && (!["fill", "content"].includes(mode) || !Number.isFinite(minEm) || minEm < .5 || minEm > 100)) {
    target.reason = "invalid-declaration"; return target;
  }
  if (!element.getClientRects().length || (style.writingMode && style.writingMode !== "horizontal-tb") || ["absolute", "fixed"].includes(style.position)) return target;
  const available = availableWidth(element, view, mode ?? "content");
  if (available === null) {target.reason = "unsupported-slot"; return target;}
  const width = element.getBoundingClientRect().width;
  if (!Number.isFinite(width) || width < 0 || !Number.isFinite(fontSize) || fontSize <= 0) return target;
  target.width = width; target.availableWidth = available;
  if (!declared) {target.reason = "undeclared-purpose"; return target;}
  const minimum = mode === "fill" ? available : Math.min(minEm * fontSize, available);
  target.deficitPx = Math.max(0, minimum - width);
  target.status = target.deficitPx > 1 ? "fail" : "pass";
  target.reason = target.status === "fail" ? "width-deficit" : "measured";
  return target;
}

/** CSS computed zero/auto values do not contribute to padding. */
function number(value) {return Number.parseFloat(value) || 0;}

/** Probe at most64descendants; exhausted content classification stays an inferred unknown. */
function readableStructure(element) {
  if (hasDirectText(element)) return true;
  const pending = [];
  for (const child of element.children) {
    if (pending.length >= 65) return true;
    pending.push(child);
  }
  let visited = 0;
  while (pending.length && visited < 64) {
    const node = pending.pop();
    visited++;
    if (["P", "H1", "H2", "H3", "H4", "ARTICLE"].includes(node.tagName)) return true;
    if (["BUTTON", "SELECT", "TEXTAREA", "INPUT", "SVG", "LABEL"].includes(node.tagName)) continue;
    if (hasDirectText(node)) return true;
    for (const child of node.children) {
      if (pending.length >= 65) return true;
      pending.push(child);
    }
  }
  return pending.length > 0;
}

/** Read only a bounded text-presence bit, never retain or return literal document/user text. */
function hasDirectText(element) {
  let visited = 0;
  for (const node of element.childNodes) {
    if (++visited > 64) return true;
    if (node.nodeType === 3 && node.nodeValue.trim()) return true;
  }
  return false;
}

/** Anonymous element paths stay stable when an earlier editor subtree virtualizes lines. */
function address(element, root, fallback) {
  if (root.nodeType === 9) root = root.documentElement;
  const parts = [];
  let node = element;
  while (node !== root) {
    if (!node?.parentElement || parts.length === 16) return `unknown-${fallback}`;
    let index = 1, sibling = node.previousElementSibling;
    while (sibling) {
      if (++index > 128) return `unknown-${fallback}`;
      sibling = sibling.previousElementSibling;
    }
    parts.unshift(index);
    node = node.parentElement;
  }
  return "width-root" + (parts.length ? "-" + parts.join("-") : "");
}

/** Decorative inherited aria-hidden rails and real toolbars are not inferred prose columns. */
function excludedAncestor(element) {
  let node = element, count = 0;
  while (node && count++ < 64) {
    if (node.getAttribute("aria-hidden") === "true" || node.getAttribute("role") === "toolbar") return true;
    node = node.parentElement;
  }
  return false;
}
