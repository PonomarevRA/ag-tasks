/** Parse opaque sRGB forms used by AG profiles; unsupported custom CSS uses explicit fallback tones. */
function rgb(value) {
  if (typeof value !== "string") return null;
  const hex = /^#([a-f0-9]{6})$/i.exec(value.trim());
  if (hex)
    return [0, 2, 4].map((offset) =>
      parseInt(hex[1].slice(offset, offset + 2), 16),
    );
  const triplet =
    /^rgb\(\s*(\d+(?:\.\d+)?)[, ]+(\d+(?:\.\d+)?)[, ]+(\d+(?:\.\d+)?)\s*\)$/i.exec(
      value.trim(),
    );
  if (triplet) {
    const result = triplet.slice(1).map(Number);
    if (result.every((channel) => channel >= 0 && channel <= 255))
      return result;
  }
  return null;
}
/** Return WCAG2 relative luminance from opaque0..255sRGB channels. */
function luminance(channels) {
  const linear = channels.map((value) => {
    const s = value / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  });
  return linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
}
/** Compare normalized opaque RGB triplets; APCA is reported separately, not used as a non-text compliance gate. */
function ratio(a, b) {
  const x = luminance(a),
    y = luminance(b);
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}
/** Serialize an opaque RGB triplet for the actual SVG stroke. */
function hex(channels) {
  return (
    "#" +
    channels
      .map((value) => Math.round(value).toString(16).padStart(2, "0"))
      .join("")
  );
}
/** Pick solid boundary tones at least9:1 apart: for every adjacent colour one tone then reaches3:1 (WCAG G209). */
export function radialBoundaryPalette(surface, text) {
  const base = rgb(surface),
    ink = rgb(text);
  if (base && ink) {
    // Prefer a profile-derived line, increasing strength only until the divider contract is met.
    for (let step = 0; step <= 14; step++) {
      const strength = 0.72 + step * 0.02;
      const candidate = base.map((value, i) =>
        Math.round(value + (ink[i] - value) * strength),
      );
      if (ratio(base, candidate) >= 9)
        return { halo: hex(base), stroke: hex(candidate), source: "profile" };
    }
  }
  // Low-contrast or unrecognized local overrides must not silently erase the divider.
  const dark = base ? luminance(base) < 0.3 : true;
  return {
    halo: dark ? "#000000" : "#ffffff",
    stroke: dark ? "#ffffff" : "#000000",
    source: "fallback",
  };
}
