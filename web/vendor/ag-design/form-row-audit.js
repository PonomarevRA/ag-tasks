import { inspectFormRow } from "./form-row-inspection.js";

const ROW = "[data-ag-form-row]";

/**
 * Measure explicitly declared above-control field groups inside one local root.
 * Reads rectangles and structural associations only; never returns text, IDs or values.
 * Host declares direct children with data-ag-form-field and row/stack/auto layout.
 * Auto supports one CSS grid band or a nowrap flex row/column. Unsupported cases stay unknown.
 * No listeners, DOM writes, screenshots, scripts or network; rerun after each settled state.
 */
export function auditFormRows(root, { maxRows = 32, maxNodes = 2000 } = {}) {
  if (
    !root?.querySelectorAll ||
    !Number.isInteger(maxRows) ||
    maxRows < 1 ||
    maxRows > 32 ||
    !Number.isInteger(maxNodes) ||
    maxNodes < 1 ||
    maxNodes > 5000
  )
    throw new TypeError("Use a local DOM root, 1–32 rows and 1–5000 nodes");
  const document = root.ownerDocument ?? root;
  const walker = document.createTreeWalker(root, 1);
  const rows = [];
  const capturedRows = new Set();
  const visibleControls = [];
  let node = root.nodeType === 1 ? root : walker.nextNode();
  let inspectedNodes = 0,
    truncated = false;
  while (node) {
    if (inspectedNodes >= maxNodes) {
      truncated = true;
      break;
    }
    inspectedNodes++;
    if (isTextControl(node) && visible(node)) visibleControls.push(node);
    if (node.matches(ROW) && visible(node)) {
      if (rows.length >= maxRows) {
        truncated = true;
        break;
      }
      rows.push(captureRow(node, `row-${rows.length + 1}`));
      capturedRows.add(node);
    }
    node = walker.nextNode();
  }
  let declaredControls = 0;
  for (const control of visibleControls) {
    const field = control.closest("[data-ag-form-field]");
    if (field && capturedRows.has(field.parentElement)) declaredControls++;
  }
  let measuredControls = 0;
  for (const row of rows) {
    for (const field of row.facts?.controls ?? []) {
      if (field.rect !== null) measuredControls++;
    }
  }
  const coverage = {
    visibleControls: visibleControls.length,
    declaredControls,
    undeclaredControls: visibleControls.length - declaredControls,
    measuredControls,
  };
  let geometryGate = "pass";
  if (
    coverage.undeclaredControls > 0 ||
    truncated ||
    rows.length === 0 ||
    rows.some((row) => row.geometryGate === "unknown")
  )
    geometryGate = "unknown";
  if (rows.some((row) => row.geometryGate === "fail")) geometryGate = "fail";
  return {
    schemaVersion: "ag-design/form-row-audit-v1",
    rows,
    coverage,
    geometryGate,
    truncated,
    inspectedNodes,
    scope: "declared above-control field groups only",
    manualRequired: [
      "undeclared groups and side labels",
      "label meaning and typography",
      "keyboard and AT",
      "narrow and enlarged text scenarios",
      "dynamic feedback growth",
    ],
    trust: "local DOM rectangles; partial evidence, not qualification",
  };
}

/**
 * Recognize the same native text-control family supported by row capture.
 * @param {Element} element A node in the caller's bounded local root.
 * @returns {boolean} Whether this visible candidate requires declared form-row coverage.
 */
function isTextControl(element) {
  if (element.tagName === "SELECT" || element.tagName === "TEXTAREA")
    return true;
  return (
    element.tagName === "INPUT" &&
    [
      "text",
      "email",
      "search",
      "url",
      "tel",
      "password",
      "number",
      "date",
      "time",
      "datetime-local",
      "month",
      "week",
    ].includes(element.type)
  );
}

/** Collect a bounded group and retain absent/unsupported facts as unknown. */
function captureRow(row, id) {
  const fields = [];
  if (row.children.length > 64)
    return unknownRow(id, "too-many-direct-children");
  for (const child of row.children) {
    if (fields.length >= 32) return unknownRow(id, "too-many-fields");
    if (child.matches("[data-ag-form-field]") && visible(child))
      fields.push(child);
  }
  const view = row.ownerDocument.defaultView;
  const style = view.getComputedStyle(row);
  const layout = resolveLayout(row, fields, style);
  if (!layout || fields.length === 0 || unsupportedGeometry(row))
    return unknownRow(id, "missing-fields-or-unsupported-layout");
  const controls = fields.map((field, index) =>
    captureField(field, `field-${index + 1}`),
  );
  const facts = {
    container: bounds(row),
    controls,
    layout,
    direction: style.direction || "ltr",
    tracks: true,
  };
  try {
    return { id, facts, ...inspectFormRow(facts) };
  } catch (error) {
    if (error instanceof TypeError)
      return unknownRow(id, "invalid-or-unbounded-geometry");
    throw error;
  }
}

/** Resolve layout from host declarations and wrapper bands, never from control tops. */
function resolveLayout(row, fields, style) {
  const declared = row.getAttribute("data-ag-form-row");
  if (["row", "stack"].includes(declared)) return declared;
  if (declared !== "auto") return null;
  if (style.display === "flex" && style.flexWrap === "nowrap") {
    if (style.flexDirection === "row") return "row";
    if (style.flexDirection === "column") return "stack";
  }
  if (style.display !== "grid") return null;
  const tracks = style.gridTemplateColumns.trim().split(/\s+/);
  if (!tracks.every((track) => /^\d+(?:\.\d+)?px$/.test(track))) return null;
  if (tracks.length === 1) return "stack";
  if (tracks.length !== fields.length) return null;
  const tops = fields.map((field) => field.getBoundingClientRect().top);
  if (Math.max(...tops) - Math.min(...tops) > 1) return null;
  return "row";
}

/** Capture one labelled text field. Missing, external or ambiguous associations are not a pass. */
function captureField(field, id) {
  const walker = field.ownerDocument.createTreeWalker(field, 1);
  const candidates = [];
  let node = walker.nextNode(),
    count = 0;
  while (node && count < 128) {
    count++;
    if (node.matches("input:not([type=hidden]),select,textarea"))
      candidates.push(node);
    node = walker.nextNode();
  }
  const control = !node && candidates.length === 1 ? candidates[0] : null;
  const result = { id, rect: null, label: null, feedback: null };
  if (!control || !visible(control) || unsupportedGeometry(control))
    return result;
  if (
    control.tagName === "INPUT" &&
    ![
      "text",
      "email",
      "search",
      "url",
      "tel",
      "password",
      "number",
      "date",
      "time",
      "datetime-local",
      "month",
      "week",
    ].includes(control.type)
  )
    return result;
  result.rect = bounds(control);
  result.comparableHeight = control.tagName !== "TEXTAREA" && !control.multiple;
  const labelReferences = control.getAttribute("aria-labelledby") ?? "";
  const descriptionReferences = control.getAttribute("aria-describedby") ?? "";
  if (labelReferences.length > 1024 || descriptionReferences.length > 1024)
    return result;
  const named = labelReferences.trim().split(/\s+/).filter(Boolean);
  let label =
    named.length === 1 ? field.ownerDocument.getElementById(named[0]) : null;
  if (named.length === 0 && control.labels?.length === 1)
    label = control.labels[0];
  if (
    label &&
    !label.contains(control) &&
    field.contains(label) &&
    visible(label) &&
    !unsupportedGeometry(label)
  )
    result.label = bounds(label);
  const ids = descriptionReferences.trim().split(/\s+/).filter(Boolean);
  if (ids.length > 8) return result;
  const feedback = [];
  for (const reference of ids) {
    const element = field.ownerDocument.getElementById(reference);
    if (!element || !field.contains(element)) return result;
    if (unsupportedGeometry(element)) return result;
    if (visible(element)) feedback.push(element);
  }
  feedback.sort((a, b) =>
    a === b ? 0 : a.compareDocumentPosition(b) & 4 ? -1 : 1,
  );
  result.feedback = feedback.map(bounds);
  return result;
}

/** Client rectangles do not reliably describe transformed or vertical-writing layout tracks. */
function unsupportedGeometry(element) {
  const view = element.ownerDocument.defaultView;
  let depth = 0;
  for (let ancestor = element; ancestor; ancestor = ancestor.parentElement) {
    if (++depth > 64) return true;
    const style = view.getComputedStyle(ancestor);
    // Individual transforms are independent of computed `transform` in modern CSS.
    for (const property of ["transform", "scale", "rotate", "translate"]) {
      const value = style[property];
      if (value && value !== "none") return true;
    }
    if (style.writingMode && style.writingMode !== "horizontal-tb") return true;
  }
  return false;
}

/** Skip hidden, inert and non-rendered content; a disabled visible field still occupies layout. */
function visible(element) {
  if (
    element.closest("[hidden],[inert]") ||
    element.getClientRects().length === 0
  )
    return false;
  const visibility =
    element.ownerDocument.defaultView.getComputedStyle(element).visibility;
  const rect = element.getBoundingClientRect();
  return (
    visibility !== "hidden" &&
    visibility !== "collapse" &&
    rect.width > 0 &&
    rect.height > 0
  );
}

/** Emit only CSS pixel geometry, never user content or original document identifiers. */
function bounds(element) {
  const { x, y, width, height } = element.getBoundingClientRect();
  return { x, y, width, height };
}

/** An unsupported declaration remains visible to agents as a coverage gap. */
function unknownRow(id, reason) {
  return { id, reason, geometryGate: "unknown", checks: [] };
}
