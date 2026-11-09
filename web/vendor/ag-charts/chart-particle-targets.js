const TAU = Math.PI * 2;
/** Seeded noise keeps targets stable across replay and palette changes, without regular rows of dots. */
export function particleRandom(seed = 731) {
  return () => {
    seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0;
    return seed / 4294967296;
  };
}
/** Measure a region for a fair global allocation; short series cannot exhaust later series' budget. */
function regionWeight(region) {
  if (region.kind === "rect")
    return Math.sqrt(region.width * region.height) * 3;
  if (region.kind === "disk") return region.radius * 8;
  if (region.kind === "sector")
    return (
      ((region.end - region.start) * (region.radius ** 2 - region.inner ** 2)) /
      110
    );
  const points = region.points;
  let length = 0;
  for (let i = 1; i < points.length; i++)
    length += Math.hypot(
      points[i].x - points[i - 1].x,
      points[i].y - points[i - 1].y,
    );
  return Math.max(8, length) * (region.kind === "polygon" ? 2.5 : 1);
}
/** Test arbitrary simple polygons, including signed area runs; missing observations are separate regions. */
export function insidePolygon(x, y, points) {
  let inside = false;
  for (let i = 0, j = points.length - 1; i < points.length; j = i++) {
    const a = points[i],
      b = points[j];
    if (
      a.y > y !== b.y > y &&
      x < ((b.x - a.x) * (y - a.y)) / (b.y - a.y) + a.x
    )
      inside = !inside;
  }
  return inside;
}
/** Sample a polyline by arc length so the luminous edge remains equally dense on steep slopes. */
function linePoint(region, random) {
  const points = region.edge || region.points;
  const { lengths, total } = region.lineMeasure;
  let distance = random() * total;
  for (let i = 0; i < lengths.length; i++) {
    if (distance <= lengths[i] || i === lengths.length - 1) {
      const a = points[i],
        b = points[i + 1],
        t = lengths[i] ? distance / lengths[i] : 0;
      return {
        x: a.x + (b.x - a.x) * t,
        y: a.y + (b.y - a.y) * t,
        index: t < 0.5 ? a.index : b.index,
      };
    }
    distance -= lengths[i];
  }
  return points[0];
}
/** Precompute polygon bounds once, outside the particle loop. */
function polygonBounds(region) {
  let left = Infinity,
    right = -Infinity,
    top = Infinity,
    bottom = -Infinity;
  for (const p of region.points) {
    left = Math.min(left, p.x);
    right = Math.max(right, p.x);
    top = Math.min(top, p.y);
    bottom = Math.max(bottom, p.y);
  }
  return { left, right, top, bottom };
}
/** Sample the actual shape. Rejection has a finite bound; thin polygons fall back to their real edge. */
function regionPoint(region, random, bounds) {
  if (region.kind === "rect")
    return {
      x: region.x + random() * region.width,
      y: region.y + random() * region.height,
    };
  if (region.kind === "disk" || region.kind === "sector") {
    const inner = region.inner || 0;
    const radius = Math.sqrt(
      inner ** 2 + random() * (region.radius ** 2 - inner ** 2),
    );
    const gap =
      region.kind === "sector"
        ? Math.min(0.015, (region.end - region.start) / 8)
        : 0;
    const angle =
      region.kind === "sector"
        ? region.start + gap + random() * (region.end - region.start - gap * 2)
        : random() * TAU;
    return {
      x: region.x + Math.cos(angle) * radius,
      y: region.y + Math.sin(angle) * radius,
    };
  }
  if (region.kind === "line") return linePoint(region, random);
  for (let attempt = 0; attempt < 40; attempt++) {
    const x = bounds.left + random() * (bounds.right - bounds.left),
      y = bounds.top + random() * (bounds.bottom - bounds.top);
    if (insidePolygon(x, y, region.points)) return { x, y };
  }
  return linePoint(region, random);
}
/** Build fine dust with a strict 16k ceiling. Density changes appearance, never the data geometry or domains. */
export function sampleParticleTargets(regions, options = {}) {
  const random = particleRandom(),
    result = [];
  const weights = regions.map(regionWeight),
    total = weights.reduce((a, b) => a + b, 0);
  if (!total) return result;
  const budget = Math.min(
    16000,
    options.particleCount ?? 12000,
    Math.round(8000 * (options.particleDensity ?? 1)),
  );
  let remaining = budget,
    remainingWeight = total;
  for (let ri = 0; ri < regions.length; ri++) {
    const region = { ...regions[ri] },
      weight = weights[ri];
    let count = remainingWeight
      ? Math.round((remaining * weight) / remainingWeight)
      : 0;
    if (region.kind === "line" && region.points.length === 1)
      count = Math.min(count, 12);
    remaining -= count;
    remainingWeight -= weight;
    if (region.points) {
      const points = region.edge || region.points,
        lengths = [];
      let total = 0;
      for (let i = 1; i < points.length; i++) {
        const length = Math.hypot(
          points[i].x - points[i - 1].x,
          points[i].y - points[i - 1].y,
        );
        lengths.push(length);
        total += length;
      }
      region.lineMeasure = { lengths, total };
    }
    const bounds = region.kind === "polygon" ? polygonBounds(region) : null;
    for (let i = 0; i < count; i++) {
      const point = regionPoint(region, random, bounds);
      if (!point) continue;
      result.push({
        ...point,
        interior: region.kind !== "line",
        color: region.color,
        series: region.series,
        index: region.index ?? point.index,
        r: (options.particleSize ?? 0.7) * (0.65 + random() * 0.65),
        alpha: region.kind === "polygon" ? 0.42 : 0.86,
        phase: random() * TAU,
      });
    }
  }
  return result;
}
