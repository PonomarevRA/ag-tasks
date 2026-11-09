import { insideAsciiSector } from "./chart-ascii-radial.js";
import { sampleParticleTargets } from "./chart-particle-targets.js";

export const asciiChartCharsets = Object.freeze({
  classic: ".:+*#@",
  binary: "01",
  braille: "⠂⠄⠆⠒⠶⡶⣶⣿",
});

/** Quantize real shape samples to a bounded character grid. Edge cells win over interior cells of the same series. */
export function asciiChartTargets(regions, options = {}) {
  const size = options.asciiFontSize ?? 10;
  const dx = size * 0.65,
    dy = size;
  const samples = sampleParticleTargets(regions, {
    particleDensity: 2,
    particleCount: 12000,
  });
  const sectors = new Map();
  for (const region of regions)
    if (region.kind === "sector")
      sectors.set(`${region.series}:${region.index}`, region);
  const cells = new Map();
  for (const p of samples) {
    const column = Math.round(p.x / dx),
      row = Math.round(p.y / dy);
    const sector = sectors.get(`${p.series}:${p.index}`);
    if (sector && !insideAsciiSector(sector, column * dx, row * dy, size))
      continue;
    const key = sector
      ? `${p.series}:${p.index}:${column}:${row}`
      : `${p.series}:${column}:${row}`;
    const previous = cells.get(key);
    if (previous && (!previous.interior || p.interior)) continue;
    const glyph = Math.abs(
      Math.imul(column + 17, 731) ^ Math.imul(row + 3, 971),
    );
    let alpha = p.interior ? 0.5 : 0.95;
    if (sector) alpha = 0.78;
    cells.set(key, {
      ...p,
      x: column * dx,
      y: row * dy,
      r: size / 2,
      glyph,
      alpha,
      ...(sector ? { sector } : {}),
    });
  }
  // A uniform stride preserves later series when a large scene exceeds the global character budget.
  const candidates = [...cells.values()],
    limit = options.asciiMaxCells ?? 2400;
  if (candidates.length <= limit) return candidates;
  const result = [];
  for (let i = 0; i < limit; i++)
    result.push(candidates[Math.floor((i * candidates.length) / limit)]);
  return result;
}
