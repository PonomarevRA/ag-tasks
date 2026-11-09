import { radialBoundaryPalette } from "./chart-radial-contrast.js";
import {
  sectorSelectionTransform,
  prepareAsciiRadialFlow,
  asciiRadialFlowRadius,
  asciiRadialFlowOpacity,
} from "./chart-ascii-radial.js";
import { asciiChartCharsets } from "./chart-ascii-targets.js";

/** Paint actual monospace characters from a small semantic-colour atlas; never cache full animation frames. */
export function asciiChartPainter(
  document,
  element,
  canvas,
  context,
  targets,
  options,
) {
  const atlas = document.createElement("canvas"),
    groups = new Map(),
    sectorTransforms = new Map();
  for (const target of targets)
    if (target.sector && !sectorTransforms.has(target.sector))
      sectorTransforms.set(
        target.sector,
        sectorSelectionTransform(target.sector),
      );
  const chars = Array.from(
    asciiChartCharsets[options.asciiCharset ?? "classic"],
  );
  const size = options.asciiFontSize ?? 10;
  const radialFlows = targets.map((target) =>
    prepareAsciiRadialFlow(target, size),
  );
  let atlasContext,
    available = true,
    selectedIndex = -1;
  /** Rebuild one glyph row per used semantic colour. Palette changes do not alter data or grid coordinates. */
  function palette() {
    groups.clear();
    const style = document.defaultView.getComputedStyle(element);
    if (sectorTransforms.size) {
      const boundary = radialBoundaryPalette(
        style.getPropertyValue("--ag-surface").trim(),
        style.getPropertyValue("--ag-text").trim(),
      );
      // Update only changed values: this scope is also observed for host theme changes.
      for (const [name, value] of [
        ["--ag-chart-radial-boundary", boundary.stroke],
        ["--ag-chart-radial-halo", boundary.halo],
      ])
        if (element.style.getPropertyValue(name) !== value)
          element.style.setProperty(name, value);
      element.dataset.radialBoundarySource = boundary.source;
    }
    for (const p of targets) {
      if (groups.has(p.color)) continue;
      const token = p.color.match(/^var\((--ag-[\w-]+)\)$/)?.[1];
      const color =
        (token ? style.getPropertyValue(token).trim() : p.color) || "#777777";
      groups.set(p.color, { color, row: groups.size });
    }
    atlas.width = 32 * chars.length;
    atlas.height = 32 * Math.max(1, groups.size);
    atlasContext = atlas.getContext("2d");
    available = Boolean(atlasContext?.fillText && context.drawImage);
    if (!available) return;
    atlasContext.font = `${size * 2}px ui-monospace, SFMono-Regular, Consolas, monospace`;
    atlasContext.textAlign = "center";
    atlasContext.textBaseline = "middle";
    for (const group of groups.values()) {
      atlasContext.fillStyle = group.color;
      atlasContext.shadowColor = group.color;
      atlasContext.shadowBlur = (options.asciiGlow ?? 0.25) * 3;
      for (let i = 0; i < chars.length; i++)
        atlasContext.fillText(chars[i], i * 32 + 16, group.row * 32 + 16);
    }
  }
  /** Draw a staggered assembly, then a slow opacity/character wave with subpixel sway; final values remain unchanged. */
  function paint(field, time, moving) {
    if (!atlas.width) palette();
    context.setTransform(2, 0, 0, 2, 0, 0);
    context.clearRect(0, 0, 640, 300);
    if (!available) return;
    const speed = options.asciiSpeed ?? 0.6,
      phase = moving ? (time * speed) / 1600 : 0;
    for (let i = 0; i < targets.length; i++) {
      const p = targets[i],
        group = groups.get(p.color);
      const wave = Math.sin(phase + p.x * 0.018 - p.y * 0.025);
      const arrival = Math.max(
        0,
        Math.min(1, (field.elapsed - field.delay[i]) / 300),
      );
      let shimmer = 1;
      if (moving) shimmer = p.sector ? 0.9 + 0.1 * wave : 0.72 + 0.28 * wave;
      const selected = p.sector && p.index === selectedIndex,
        flow = radialFlows[i],
        flowing = flow && moving,
        radius = flowing ? asciiRadialFlowRadius(flow, phase) : 0,
        flowAlpha = flowing ? asciiRadialFlowOpacity(flow, radius) : 1;
      context.globalAlpha = Math.min(
        1,
        p.alpha * arrival * shimmer * flowAlpha * (selected ? 1.25 : 1),
      );
      let glyph = p.glyph % chars.length;
      if (moving && p.interior)
        glyph = (glyph + Math.floor((wave + 1) * 1.4)) % chars.length;
      const sway = moving && !flow ? wave * (p.interior ? 0.65 : 0.15) : 0;
      let positionX = field.positions[i * 2],
        positionY = field.positions[i * 2 + 1];
      if (flowing) {
        const distance = radius - flow.radius;
        positionX += flow.ux * distance;
        positionY += flow.uy * distance;
      }
      if (selected) {
        const t = sectorTransforms.get(p.sector);
        positionX = t.x + (positionX - t.x) * t.scale + t.dx;
        positionY = t.y + (positionY - t.y) * t.scale + t.dy;
      }
      context.drawImage(
        atlas,
        glyph * 32,
        group.row * 32,
        32,
        32,
        positionX - 8,
        positionY - 8 + sway,
        16,
        16,
      );
    }
    context.globalAlpha = 1;
  }
  /** Offscreen disposal zeros the single atlas; visible painting rebuilds it lazily. */
  function releaseFrames() {
    atlas.width = atlas.height = 0;
  }
  palette();
  return {
    paint,
    /** Change only presentation state; the layer repaints once if motion is paused. */
    select(index) {
      if (selectedIndex === index) return false;
      selectedIndex = index;
      return true;
    },
    palette,
    releaseFrames,
    resize: () => false,
    get cachedFrames() {
      return atlas.width ? 1 : 0;
    },
    get rasterBytes() {
      return atlas.width * atlas.height * 4;
    },
    dispose() {
      releaseFrames();
      groups.clear();
      if (sectorTransforms.size) {
        element.style.removeProperty("--ag-chart-radial-boundary");
        element.style.removeProperty("--ag-chart-radial-halo");
        delete element.dataset.radialBoundarySource;
      }
      sectorTransforms.clear();
      radialFlows.length = 0;
    },
  };
}
