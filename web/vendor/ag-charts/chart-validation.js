import { axisDateValues } from "./chart-axis.js";
export const chartTypes = Object.freeze([
  "composed",
  "line",
  "area",
  "bar",
  "pie",
  "donut",
  "scatter",
  "bubble",
  "radar",
]);

/** Validate one non-null value in its chart-specific shape; scalar and point contracts cannot be mixed. */
function validateValue(value, type) {
  if (value === null) return;
  if (type === "scatter" || type === "bubble") {
    if (!value || !Number.isFinite(value.x) || !Number.isFinite(value.y))
      throw new TypeError("Finite x/y and nonnegative bubble size required");
    if (type === "bubble" && (!Number.isFinite(value.size) || value.size < 0))
      throw new TypeError("Finite x/y and nonnegative bubble size required");
    return;
  }
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    Math.abs(value) > 1e12
  )
    throw new TypeError("Finite bounded values or null required");
  if (["pie", "donut", "radar"].includes(type) && value < 0)
    throw new RangeError("Radial charts require nonnegative values");
}

/** Validate one identity and exactly one supplied value per category before checking its individual values. */
function validateSeries(item, ids, labelCount, type) {
  if (!item || !item.id || ids.has(item.id))
    throw new TypeError("Unique series and one value per label are required");
  if (typeof item.label !== "string" || !item.label.trim())
    throw new TypeError("Unique series and one value per label are required");
  if (!Array.isArray(item.values) || item.values.length !== labelCount)
    throw new TypeError("Unique series and one value per label are required");
  if (type === "composed" && !["line", "area", "bar"].includes(item.type))
    throw new TypeError("Composed series require line, area or bar type");
  ids.add(item.id);
  for (const value of item.values) validateValue(value, type);
}

/** Explicit bounds must be finite and ordered. A radar maximum must stay positive to avoid an undefined radial scale. */
function validateDomain(state) {
  if (state.min !== undefined && !Number.isFinite(state.min))
    throw new RangeError("Invalid domain");
  if (state.max !== undefined && !Number.isFinite(state.max))
    throw new RangeError("Invalid domain");
  if (
    state.min !== undefined &&
    state.max !== undefined &&
    state.min >= state.max
  )
    throw new RangeError("Invalid domain");
  if (state.type === "radar" && state.max !== undefined && state.max <= 0)
    throw new RangeError("Radar maximum must be positive");
}

/** Reject ambiguous modes, excessive lists and invalid series before chart state or DOM changes. */
export function validateChart(state) {
  const {
    type,
    labels,
    series,
    renderer = "svg",
    arrangement = "grouped",
  } = state;
  if (
    !chartTypes.includes(type) ||
    !["svg", "particles", "ascii"].includes(renderer) ||
    !["grouped", "stacked", "horizontal"].includes(arrangement)
  )
    throw new TypeError("Unknown chart mode");
  if (typeof state.label !== "string" || !state.label.trim())
    throw new TypeError("A chart label is required");
  if (
    !Array.isArray(labels) ||
    labels.length > 120 ||
    labels.some((label) => typeof label !== "string")
  )
    throw new RangeError("Use at most 120 category labels");
  axisDateValues(labels, state.axisDates);
  if (!Array.isArray(series) || !series.length || series.length > 6)
    throw new RangeError("Use 1–6 series");
  const ids = new Set();
  for (const item of series) validateSeries(item, ids, labels.length, type);
  if (type === "radar" && labels.length < 3)
    throw new RangeError("Radar needs at least three categories");
  const hasBars =
    type === "bar" ||
    (type === "composed" && series.some((item) => item.type === "bar"));
  if (hasBars && (state.min > 0 || state.max < 0))
    throw new RangeError("Bar domain must include zero");
  if (["pie", "donut"].includes(type) && series.length !== 1)
    throw new TypeError("Pie/donut use one series");
  for (const [key, min, max] of [
    ["asciiFontSize", 6, 14],
    ["asciiMaxCells", 128, 4000],
    ["asciiSpeed", 0, 2],
    ["asciiGlow", 0, 1],
    ["particleDensity", 0.25, 3],
    ["particleCount", 100, 16000],
    ["particleFrames", 4, 20],
    ["particleCacheBudget", 262144, 4194304],
    ["particleSize", 0.3, 2],
    ["particleBloom", 0, 1],
    ["particleJitter", 0, 3],
  ]) {
    const value = state[key];
    if (
      value !== undefined &&
      (!Number.isFinite(value) ||
        value < min ||
        value > max ||
        ([
          "asciiMaxCells",
          "particleCount",
          "particleFrames",
          "particleCacheBudget",
        ].includes(key) &&
          !Number.isInteger(value)))
    )
      throw new RangeError("Invalid " + key);
  }
  for (const key of ["paused", "animated", "showTooltip", "particleCache"])
    if (state[key] !== undefined && typeof state[key] !== "boolean")
      throw new TypeError("Boolean " + key + " required");
  if (
    state.particleCacheMode !== undefined &&
    !["frames", "strips"].includes(state.particleCacheMode)
  )
    throw new TypeError("Unknown particle cache mode");
  if (
    state.asciiCharset !== undefined &&
    !["classic", "binary", "braille"].includes(state.asciiCharset)
  )
    throw new TypeError("Unknown ASCII charset");
  if (type === "composed" && arrangement !== "grouped")
    throw new TypeError("Composed charts use grouped shared scales");
  validateDomain(state);
}
