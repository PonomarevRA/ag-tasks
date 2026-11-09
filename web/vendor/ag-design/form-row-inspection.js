/**
 * Inspect declared form control rectangles without reading labels, values or screenshots.
 * @param {object} facts CSS-pixel container, one-to-32 unique controls, layout row/stack,
 * direction ltr/rtl and tolerance (0–4 CSS px, default 1). Missing rectangles stay unknown.
 * @returns {object} Numeric alignment/height/order/containment checks and an explicit geometry gate.
 * Set tracks:true to inspect above-control labels and feedback rectangles as well.
 * comparableHeight:false excludes a multiline control from the height family only.
 * This does not infer semantic groups, ink baselines or accessibility conformance.
 */
export function inspectFormRow({
  container,
  controls,
  layout = "row",
  direction = "ltr",
  tolerance = 1,
  tracks = false,
} = {}) {
  if (
    typeof tracks !== "boolean" ||
    !Array.isArray(controls) ||
    !controls.length ||
    controls.length > 32 ||
    !["row", "stack"].includes(layout) ||
    !["ltr", "rtl"].includes(direction) ||
    !Number.isFinite(tolerance) ||
    tolerance < 0 ||
    tolerance > 4
  )
    throw new TypeError(
      "Use a declared row/stack, 1–32 controls and a 0–4 CSS px tolerance",
    );
  const ids = new Set();
  for (const item of controls) {
    if (!item || !/^[a-z][a-z0-9_-]{0,80}$/i.test(item.id) || ids.has(item.id))
      throw new TypeError("Control IDs must be unique bounded identifiers");
    ids.add(item.id);
    validateRect(item.rect);
    if (
      item.comparableHeight !== undefined &&
      typeof item.comparableHeight !== "boolean"
    )
      throw new TypeError("comparableHeight must be boolean");
    if (tracks) {
      validateRect(item.label);
      if (
        item.feedback != null &&
        (!Array.isArray(item.feedback) || item.feedback.length > 8)
      )
        throw new TypeError(
          "Use up to eight feedback rectangles, or null for missing evidence",
        );
      for (const rect of item.feedback ?? []) validateRect(rect);
    }
  }
  validateRect(container);
  const measured = controls.filter((item) => item.rect != null);
  const checks = [];
  const complete = measured.length === controls.length;
  const comparable = complete && measured.length > 1;
  if (layout === "row") {
    checks.push(
      check(
        "form-row-control-alignment",
        comparable ? spread(measured, "y") : null,
        tolerance,
      ),
    );
    const family = measured.filter((item) => item.comparableHeight !== false);
    checks.push(
      check(
        "form-row-common-density",
        complete && family.length > 1 ? spread(family, "height") : null,
        tolerance,
      ),
    );
  }
  let orderResidual = 0;
  for (let index = 1; index < measured.length; index++) {
    const previous = measured[index - 1].rect,
      next = measured[index].rect;
    const residual =
      layout === "stack"
        ? previous.y + previous.height - next.y
        : direction === "rtl"
          ? next.x + next.width - previous.x
          : previous.x + previous.width - next.x;
    orderResidual = Math.max(orderResidual, residual);
  }
  checks.push(
    check(
      "form-row-reading-order",
      comparable ? orderResidual : null,
      tolerance,
    ),
  );
  if (tracks)
    checks.push(...inspectTracks(controls, { layout, direction, tolerance }));
  let overflow = 0;
  if (container != null) {
    for (const item of measured) {
      const rectangles = [item.rect];
      if (tracks) rectangles.push(item.label, ...(item.feedback ?? []));
      for (const rect of rectangles) {
        if (!rect) continue;
        overflow = Math.max(
          overflow,
          container.x - rect.x,
          container.y - rect.y,
          rect.x + rect.width - container.x - container.width,
          rect.y + rect.height - container.y - container.height,
        );
      }
    }
  }
  checks.push(
    check(
      "form-row-containment",
      complete && container != null ? overflow : null,
      tolerance,
    ),
  );
  const failed = checks.some((item) => item.status === "fail");
  const unknown = checks.some((item) => item.status === "unknown");
  return {
    schemaVersion: "ag-design/form-row-inspection-v1",
    layout,
    direction,
    tracks,
    tolerance,
    measuredControls: measured.length,
    declaredControls: controls.length,
    checks,
    geometryGate: failed ? "fail" : unknown ? "unknown" : "pass",
    manualRequired: [
      "semantic grouping",
      "persistent labels and descriptions",
      "feedback growth",
      "keyboard and assistive technology",
      "narrow and enlarged text scenarios",
    ],
    trust:
      "caller-measured CSS rectangles; no execution attestation or accessibility certification",
  };
}

/** Compare declared above-control tracks. Different hint lengths are intentionally allowed. */
function inspectTracks(controls, { layout, direction, tolerance }) {
  const complete = controls.every(
    (item) =>
      item.rect &&
      item.label &&
      Array.isArray(item.feedback) &&
      item.feedback.every(Boolean),
  );
  let edge = 0,
    overlap = 0;
  if (complete) {
    let previousBottom = null;
    for (const item of controls) {
      const start = (rect) =>
        direction === "rtl" ? rect.x + rect.width : rect.x;
      edge = Math.max(edge, Math.abs(start(item.label) - start(item.rect)));
      overlap = Math.max(
        overlap,
        item.label.y + item.label.height - item.rect.y,
      );
      let bottom = item.rect.y + item.rect.height;
      for (const rect of item.feedback) {
        edge = Math.max(edge, Math.abs(start(rect) - start(item.rect)));
        overlap = Math.max(overlap, bottom - rect.y);
        bottom = Math.max(bottom, rect.y + rect.height);
      }
      if (layout === "stack" && previousBottom !== null)
        overlap = Math.max(overlap, previousBottom - item.label.y);
      previousBottom = bottom;
    }
  }
  const labels = controls.map((item) => ({ rect: item.label }));
  const checks = [
    check("form-field-inline-alignment", complete ? edge : null, tolerance),
    check("form-field-track-order", complete ? overlap : null, tolerance),
  ];
  if (layout === "row")
    checks.push(
      check(
        "form-row-label-alignment",
        complete && controls.length > 1 ? spread(labels, "y") : null,
        tolerance,
      ),
    );
  return checks;
}

/** Reject malformed/non-finite geometry; null explicitly denotes missing evidence. */
function validateRect(rect) {
  if (rect == null) return;
  if (typeof rect !== "object" || Array.isArray(rect))
    throw new TypeError("Rectangle required");
  for (const key of ["x", "y", "width", "height"])
    if (!Number.isFinite(rect[key]) || Math.abs(rect[key]) > 1e7)
      throw new TypeError("Use bounded finite CSS pixel geometry");
  if (rect.width <= 0 || rect.height <= 0)
    throw new TypeError("Use positive visible control bounds");
}

/** Measure the widest deviation in one declared comparable dimension. */
function spread(items, key) {
  let minimum = Infinity,
    maximum = -Infinity;
  for (const { rect } of items) {
    minimum = Math.min(minimum, rect[key]);
    maximum = Math.max(maximum, rect[key]);
  }
  return maximum - minimum;
}

/** Preserve an absent measurement as unknown instead of silently passing a gate. */
function check(id, residualPx, tolerance) {
  return {
    id,
    residualPx,
    status:
      residualPx === null
        ? "unknown"
        : residualPx <= tolerance
          ? "pass"
          : "fail",
  };
}
