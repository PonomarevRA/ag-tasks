/** Resolve AG web corner geometry in CSS px; concentric mode covers uniform insets and circular arcs only. */
export function resolveCornerRadius({
  shape = "fixed",
  radius = 7,
  parentRadius,
  inset = 0,
  fallbackRadius = 7,
  width,
  height,
} = {}) {
  if (!["fixed", "capsule", "concentric"].includes(shape))
    throw new TypeError("Unknown shape family");
  for (const value of [
    radius,
    parentRadius,
    inset,
    fallbackRadius,
    width,
    height,
  ]) {
    if (value !== undefined && (!Number.isFinite(value) || value < 0))
      throw new TypeError("Corner geometry must use finite nonnegative CSS px");
  }
  if (shape === "capsule" && (width === undefined || height === undefined))
    throw new TypeError("Capsule geometry needs width and height");
  let resolved = radius;
  if (shape === "capsule") resolved = Math.min(width, height) / 2;
  if (shape === "concentric") {
    resolved =
      parentRadius === undefined
        ? fallbackRadius
        : Math.max(0, parentRadius - inset);
  }
  // CSS also normalizes oversized radii. Return the effective radius when dimensions are known.
  if (width !== undefined) resolved = Math.min(resolved, width / 2);
  if (height !== undefined) resolved = Math.min(resolved, height / 2);
  return resolved;
}

/** Apply size/shape without shrinking text or changing focus semantics; return an exact inline-style restoration. */
export function applyControlGeometry(
  element,
  { density = "compact", shape, ...geometry } = {},
) {
  const heights = { compact: 32, comfortable: 40, touch: 44 };
  if (!Object.hasOwn(heights, density))
    throw new TypeError("Unknown control density");
  const resolved = resolveCornerRadius({
    shape: shape ?? "fixed",
    ...(shape === "capsule" ? { width: 1, height: 1 } : {}),
    ...geometry,
  });
  // An omitted shape preserves consumer CSS, including circular chat launchers and host radius tokens.
  const radius = shape === "capsule" ? "9999px" : `${resolved}px`;
  const previous = {
    height: element.style.minHeight,
    radius: element.style.borderRadius,
    density: element.getAttribute("data-ag-density"),
    shape: element.getAttribute("data-ag-shape"),
  };
  element.style.minHeight = `max(var(--ag-control-height, 32px), ${heights[density]}px)`;
  if (shape !== undefined) element.style.borderRadius = radius;
  element.setAttribute("data-ag-density", density);
  element.setAttribute("data-ag-shape", shape ?? "inherited");
  return () => {
    element.style.minHeight = previous.height;
    element.style.borderRadius = previous.radius;
    for (const key of ["density", "shape"]) {
      if (previous[key] === null) element.removeAttribute(`data-ag-${key}`);
      else element.setAttribute(`data-ag-${key}`, previous[key]);
    }
  };
}
