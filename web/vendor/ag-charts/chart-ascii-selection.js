import { asciiChartCharsets } from "./chart-ascii-targets.js";
import { selectedSectorPoint } from "./chart-ascii-radial.js";
const ns = "http://www.w3.org/2000/svg";

/** Sample the real selected perimeter: rectangles, point/bubble halos and complete pie/donut sectors. Null observations have no source mark. */
export function asciiSelectionPoints(shape, scene, size = 10) {
  const result = [],
    seen = new Set(),
    spacing = Math.max(4, size * 0.8);
  const value = (name) => Number(shape.getAttribute(name));
  /** Avoid duplicate corner glyphs; positions stay finite and serialized at subpixel precision. */
  function add(x, y) {
    const key = x.toFixed(2) + ":" + y.toFixed(2);
    if (!seen.has(key)) {
      seen.add(key);
      result.push({ x: Number(x.toFixed(2)), y: Number(y.toFixed(2)) });
    }
  }
  /** Use an even finite subdivision; endpoints remain on the supplied edge. */
  function line(ax, ay, bx, by) {
    const steps = Math.max(
      1,
      Math.ceil(Math.hypot(bx - ax, by - ay) / spacing),
    );
    for (let i = 0; i <= steps; i++)
      add(ax + ((bx - ax) * i) / steps, ay + ((by - ay) * i) / steps);
  }
  /** Sample each real arc by length, including its endpoints. */
  function arc(cx, cy, r, start, end) {
    const steps = Math.max(1, Math.ceil((Math.abs(end - start) * r) / spacing));
    for (let i = 0; i <= steps; i++) {
      const angle = start + ((end - start) * i) / steps;
      add(cx + Math.cos(angle) * r, cy + Math.sin(angle) * r);
    }
  }
  const region = scene.regions.find(
    (r) =>
      r.kind === "sector" &&
      r.index === value("data-point") &&
      r.series === value("data-series"),
  );
  if (region) {
    arc(region.x, region.y, region.radius, region.start, region.end);
    if (region.inner)
      arc(region.x, region.y, region.inner, region.end, region.start);
    if (region.end - region.start < Math.PI * 2 - 1e-8)
      for (const a of [region.start, region.end])
        line(
          region.x + Math.cos(a) * region.inner,
          region.y + Math.sin(a) * region.inner,
          region.x + Math.cos(a) * region.radius,
          region.y + Math.sin(a) * region.radius,
        );
    for (let i = 0; i < result.length; i++)
      result[i] = selectedSectorPoint(region, result[i].x, result[i].y);
  } else if (shape.tagName.toLowerCase() === "rect") {
    const x = value("x"),
      y = value("y"),
      right = x + value("width"),
      bottom = y + value("height");
    line(x, y, right, y);
    line(right, y, right, bottom);
    line(right, bottom, x, bottom);
    line(x, bottom, x, y);
  } else if (shape.tagName.toLowerCase() === "circle")
    arc(value("cx"), value("cy"), value("r") + size * 0.45, 0, Math.PI * 2);
  // At most 192 characters per real mark, one SVG text node rather than hundreds of glyph nodes.
  if (result.length <= 192) return result;
  const bounded = [];
  for (let i = 0; i < 192; i++)
    bounded.push(result[Math.floor((i * result.length) / 192)]);
  return bounded;
}

/** Render the selected contour with actual characters from the current charset. No stroked shape or animation loop is added. */
export function asciiSelectionShape(document, shape, { scene, options }) {
  const size = options.asciiFontSize ?? 10,
    points = asciiSelectionPoints(shape, scene, size);
  if (!points.length) return null;
  const chars = Array.from(
    asciiChartCharsets[options.asciiCharset ?? "classic"],
  );
  const text = document.createElementNS(ns, "text");
  const attrs = {
    class: "ag-vt-chart__selected ag-vt-chart__selected--ascii",
    "data-ascii-selection": "true",
    x: points.map((p) => p.x).join(" "),
    y: points.map((p) => p.y).join(" "),
    fill: "var(--ag-text)",
    stroke: "none",
    "font-family": "ui-monospace, SFMono-Regular, Consolas, monospace",
    "font-size": size * 0.9,
    "text-anchor": "middle",
    "dominant-baseline": "central",
  };
  for (const [key, value] of Object.entries(attrs))
    text.setAttribute(key, String(value));
  text.textContent = chars[Math.max(0, chars.length - 2)].repeat(points.length);
  return text;
}

/** Solid two-tone radial boundaries separate close colours before selection; the selected contour remains ASCII characters. */
export function asciiSectorBoundaries(document, scene, svg) {
  const sectors = scene.regions.filter((r) => r.kind === "sector");
  if (!sectors.length) return null;
  const group = document.createElementNS(ns, "g");
  group.setAttribute("class", "ag-vt-chart__sector-boundaries");
  const marks = new Map();
  for (const shape of svg.querySelectorAll("[data-point]"))
    marks.set(
      `${shape.getAttribute("data-series")}:${shape.getAttribute("data-point")}`,
      shape,
    );
  for (const region of sectors) {
    const shape = marks.get(`${region.series}:${region.index}`);
    if (!shape) continue;
    const copy = shape.cloneNode(false);
    for (const key of ["data-point", "data-series", "data-selected"])
      copy.removeAttribute(key);
    for (const [key, value] of Object.entries({
      fill: "none",
      stroke: "var(--ag-chart-radial-boundary, var(--ag-text))",
      "stroke-width": 0.9,
      "stroke-opacity": 1,
      "stroke-dasharray": "none",
      "stroke-linejoin": "round",
      "vector-effect": "non-scaling-stroke",
    }))
      copy.setAttribute(key, String(value));
    const halo = copy.cloneNode(false);
    halo.setAttribute(
      "stroke",
      "var(--ag-chart-radial-halo, var(--ag-surface))",
    );
    halo.setAttribute("stroke-width", "2.9");
    group.append(halo, copy);
    if (shape.tagName.toLowerCase() === "circle" && region.inner) {
      const inner = copy.cloneNode(false);
      inner.setAttribute("r", String(region.inner));
      const innerHalo = inner.cloneNode(false);
      innerHalo.setAttribute(
        "stroke",
        "var(--ag-chart-radial-halo, var(--ag-surface))",
      );
      innerHalo.setAttribute("stroke-width", "2.9");
      group.append(innerHalo, inner);
    }
  }
  return group;
}
