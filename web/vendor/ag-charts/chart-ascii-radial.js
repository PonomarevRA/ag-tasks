const TAU = Math.PI * 2;

/** Keep quantized glyph centres in their own sector. A small glyph-scaled clearance separates neighbouring cells without changing data angles. */
export function insideAsciiSector(region, x, y, size) {
  const radius = Math.hypot(x - region.x, y - region.y);
  if (radius < region.inner || radius > region.radius) return false;
  const span = region.end - region.start;
  if (span >= TAU - 1e-8) return true;
  const delta =
    (((Math.atan2(y - region.y, x - region.x) - region.start) % TAU) + TAU) %
    TAU;
  if (delta > span) return false;
  // Limit clearance for a narrow slice instead of treating it as a zero value.
  const clearance = Math.min(size * 0.2, radius * span * 0.15);
  const sideDistance =
    radius * Math.sin(Math.min(delta, span - delta, Math.PI / 2));
  return sideDistance >= clearance;
}

/** Compute a presentation transform once per sector; data angles and values remain unchanged. */
export function sectorSelectionTransform(region) {
  const whole = region.end - region.start >= TAU - 1e-8;
  const angle = (region.start + region.end) / 2;
  return {
    x: region.x,
    y: region.y,
    scale: 1.045,
    dx: whole ? 0 : Math.cos(angle) * 4,
    dy: whole ? 0 : Math.sin(angle) * 4,
  };
}

/** Match the selected glyph fill with its character outline; this transform never changes hit-test geometry. */
export function selectedSectorPoint(region, x, y) {
  const t = sectorSelectionTransform(region);
  return {
    x: t.x + (x - t.x) * t.scale + t.dx,
    y: t.y + (y - t.y) * t.scale + t.dy,
  };
}

/** Cache a unit direction and visual flow band per radial cell; inward bleed exceeds exterior bleed. */
export function prepareAsciiRadialFlow(target, size) {
  if (!target.sector) return null;
  const sector = target.sector,
    dx = target.x - sector.x,
    dy = target.y - sector.y;
  const radius = Math.hypot(dx, dy),
    angle = (sector.start + sector.end) / 2;
  return {
    radius,
    ux: radius ? dx / radius : Math.cos(angle),
    uy: radius ? dy / radius : Math.sin(angle),
    inner: Math.max(0, sector.inner - size * 0.8),
    outer: sector.radius + size * 0.35,
    fade: Math.max(1, size * 0.45),
  };
}
/** Move toward the centre and wrap at a transparent edge using the existing painter phase; no scheduler is created. */
export function asciiRadialFlowRadius(flow, phase) {
  const span = flow.outer - flow.inner;
  return flow.outer - ((flow.outer - flow.radius + phase * 2.8) % span);
}
/** Fade exit and re-entry at the flow band's ends so the wrap does not flash across the ring. */
export function asciiRadialFlowOpacity(flow, radius) {
  return Math.max(
    0,
    Math.min(
      1,
      (flow.outer - radius) / flow.fade,
      (radius - flow.inner) / flow.fade,
    ),
  );
}
