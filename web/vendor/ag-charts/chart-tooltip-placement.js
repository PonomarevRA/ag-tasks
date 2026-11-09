/** Measure the selected category in scene pixels; protect whole bars/disks/sectors and each selected line/radar marker. */
export function chartAnchor(scene, index, pointer) {
  const boxes = [];
  const point = (x, y, radius = 6) =>
    boxes.push({
      left: x - radius,
      right: x + radius,
      top: y - radius,
      bottom: y + radius,
    });
  for (const region of scene.regions) {
    if (region.index === index && region.kind === "rect")
      boxes.push({
        left: region.x - 3,
        top: region.y - 3,
        right: region.x + region.width + 3,
        bottom: region.y + region.height + 3,
      });
    if (region.index === index && region.kind === "disk")
      point(region.x, region.y, region.radius + 4);
    if (region.index === index && region.kind === "sector") {
      const angles = [region.start, region.end];
      for (
        let angle = Math.ceil(region.start / (Math.PI / 2)) * (Math.PI / 2);
        angle <= region.end;
        angle += Math.PI / 2
      )
        angles.push(angle);
      for (const angle of angles)
        for (const radius of [region.inner, region.radius])
          point(
            region.x + Math.cos(angle) * radius,
            region.y + Math.sin(angle) * radius,
            3,
          );
    }
    for (const value of region.points || [])
      if (value.index === index) point(value.x, value.y);
  }
  // A single isolated line observation may have no connecting region.
  for (const mark of scene.marks) {
    const attrs = mark.attrs;
    if (Number(attrs["data-point"]) === index && mark.tag === "circle")
      point(attrs.cx, attrs.cy, Number(attrs.r || 4) + 3);
  }
  if (pointer) point(pointer.x, pointer.y, 8);
  if (!boxes.length) point(320, 142);
  const bounds = {
    left: Infinity,
    top: Infinity,
    right: -Infinity,
    bottom: -Infinity,
  };
  for (const box of boxes) {
    bounds.left = Math.min(bounds.left, box.left);
    bounds.right = Math.max(bounds.right, box.right);
    bounds.top = Math.min(bounds.top, box.top);
    bounds.bottom = Math.max(bounds.bottom, box.bottom);
  }
  return bounds;
}

/** Choose a fully visible tooltip side outside the protected CSS-pixel rectangle; return null to dock instead of covering data. */
export function chartTooltipPlacement(anchor, size, bounds) {
  const gap = 10,
    { width, height } = size;
  if (
    !(width > 0 && height > 0) ||
    width > bounds.right - bounds.left ||
    height > bounds.bottom - bounds.top
  )
    return null;
  const clamp = (value, min, max) => Math.max(min, Math.min(max, value));
  const x = clamp(
    (anchor.left + anchor.right - width) / 2,
    bounds.left,
    bounds.right - width,
  );
  const y = clamp(
    (anchor.top + anchor.bottom - height) / 2,
    bounds.top,
    bounds.bottom - height,
  );
  const candidates = [
    { side: "top", x, y: anchor.top - gap - height },
    { side: "right", x: anchor.right + gap, y },
    { side: "bottom", x, y: anchor.bottom + gap },
    { side: "left", x: anchor.left - gap - width, y },
  ];
  for (const candidate of candidates) {
    if (
      candidate.x < bounds.left ||
      candidate.y < bounds.top ||
      candidate.x + width > bounds.right ||
      candidate.y + height > bounds.bottom
    )
      continue;
    return candidate;
  }
  return null;
}

/** Convert a viewport-pixel corner to local CSS pixels after positive axis-aligned scaling; unsupported transforms dock safely. */
export function chartLocalPlacement(plot, rect, origin, placement) {
  if (!placement) return null;
  const view = plot.ownerDocument.defaultView;
  for (let element = plot; element; element = element.parentElement) {
    const style = view?.getComputedStyle(element);
    if (!style) continue;
    if (
      (style.rotate && style.rotate !== "none") ||
      (style.perspective && style.perspective !== "none")
    )
      return null;
    if (
      style.scale &&
      style.scale !== "none" &&
      style.scale.split(/\s+/).some((value) => !(parseFloat(value) > 0))
    )
      return null;
    const transform = style.transform;
    if (!transform || transform === "none") continue;
    const match = /^matrix\(([^)]+)\)$/.exec(transform);
    if (!match) return null;
    const [a, b, c, d] = match[1].split(",").map(Number);
    if (!(a > 0 && d > 0) || b !== 0 || c !== 0) return null;
  }
  // offset dimensions exclude ancestor transforms; the measured zero-position tooltip includes border origins.
  const scaleX = rect.width / (plot.offsetWidth || rect.width),
    scaleY = rect.height / (plot.offsetHeight || rect.height);
  if (!(scaleX > 0 && scaleY > 0)) return null;
  return {
    x: (placement.x - origin.left) / scaleX,
    y: (placement.y - origin.top) / scaleY,
    side: placement.side,
  };
}
