import { particleStripCache } from "./chart-particle-strips.js";
import {
  particleFrameCache,
  PARTICLE_CYCLE_MS,
} from "./chart-particle-cache.js";
/** Cache resolved semantic palette groups outside the frame loop; profile mutations explicitly refresh the cache. */
export function particlePainter(
  document,
  element,
  canvas,
  context,
  targets,
  options,
) {
  const view = document.defaultView,
    glow = document.createElement("canvas");
  glow.width = 320;
  glow.height = 150;
  const glowContext = glow.getContext("2d"),
    groups = new Map();
  const wave = new Float32Array(1024);
  for (let i = 0; i < wave.length; i++)
    wave[i] = Math.sin((i / wave.length) * Math.PI * 2);
  const cache = particleFrameCache(document, options.particleFrames ?? 6, {
    budget: options.particleCacheBudget,
  });
  const strips =
    options.particleCacheMode !== "frames"
      ? particleStripCache(document, { budget: options.particleCacheBudget })
      : null;
  let dark = false;
  /** Raise grain cores toward the profile text on dark surfaces, preserving the semantic hue. */
  function luminous(color, text) {
    if (
      !dark ||
      !/^#[0-9a-f]{6}$/i.test(color) ||
      !/^#[0-9a-f]{6}$/i.test(text)
    )
      return color;
    const rgb = [];
    for (let i = 1; i < 7; i += 2)
      rgb.push(
        Math.round(
          parseInt(color.slice(i, i + 2), 16) * 0.72 +
            parseInt(text.slice(i, i + 2), 16) * 0.28,
        ),
      );
    return `rgb(${rgb.join(",")})`;
  }
  /** Resolve one group per semantic token; never ask computed style for each particle or frame. */
  function palette() {
    cache.clear();
    strips?.clear();
    groups.clear();
    const style = view.getComputedStyle(element);
    dark =
      element.closest('[data-theme="dark"]') !== null ||
      style.colorScheme === "dark";
    for (let i = 0; i < targets.length; i++) {
      const token = targets[i].color;
      if (!groups.has(token)) {
        const key = token.match(/^var\((--ag-[\w-]+)\)$/)?.[1];
        const color =
          (key ? style.getPropertyValue(key).trim() : token) || "#777777";
        groups.set(token, {
          color,
          core: luminous(color, style.getPropertyValue("--ag-text").trim()),
          indices: [],
        });
      }
      groups.get(token).indices.push(i);
    }
  }
  /** Paint bright fine grains and a low-resolution soft halo; idle wave lookup avoids per-dot trigonometry. */
  function paintRaw(field, time, moving, band) {
    context.setTransform(2, 0, 0, 2, 0, 0);
    if (band) {
      context.save();
      context.beginPath();
      context.rect(0, band.top, 640, band.bottom - band.top);
      context.clip();
    }
    context.clearRect(0, 0, 640, 300);
    context.globalCompositeOperation = dark ? "lighter" : "source-over";
    const jitter = moving ? (options.particleJitter ?? 0.75) : 0;
    const phase = (time / PARTICLE_CYCLE_MS) * 1024;
    const bandMargin = jitter * 1.8 + (options.particleSize ?? 0.7) * 1.3;
    for (const group of groups.values()) {
      context.fillStyle = group.color;
      let lastColor = group.color;
      for (const i of group.indices) {
        const p = targets[i],
          j = i * 2;
        if (
          band &&
          (p.y < band.top - bandMargin || p.y > band.bottom + bandMargin)
        )
          continue;
        const color = p.alpha > 0.5 ? group.core : group.color;
        if (color !== lastColor) {
          context.fillStyle = color;
          lastColor = color;
        }
        const spatial = p.x * 1.7 + p.y * 0.65;
        const a = (phase + p.phase * 163 + spatial) & 1023,
          b = (phase * 2 + p.phase * 277 - spatial) & 1023;
        const amplitude = jitter * (p.interior ? 1.3 : 0.22);
        const x =
            field.positions[j] +
            (wave[a] + wave[(b + 250) & 1023] * 0.35) * amplitude,
          y =
            field.positions[j + 1] +
            (wave[b] + wave[(a + 80) & 1023] * 0.35) * amplitude;
        const arrival = Math.max(
          0,
          Math.min(1, (field.elapsed - field.delay[i]) / 300),
        );
        const shimmer = moving ? (p.interior ? 0.42 : 0.08) : 0;
        const illumination =
          0.68 +
          wave[(phase + spatial + 180) & 1023] * 0.25 +
          wave[(a + 260) & 1023] * 0.07;
        context.globalAlpha =
          p.alpha * arrival * (1 - shimmer + illumination * shimmer);
        context.beginPath();
        context.arc(x, y, p.r, 0, Math.PI * 2);
        context.fill();
      }
    }
    context.globalAlpha = 1;
    context.globalCompositeOperation = "source-over";
    const bloom = options.particleBloom ?? 0.65;
    if (glowContext?.drawImage && context.drawImage && bloom > 0) {
      glowContext.clearRect(0, 0, 320, 150);
      glowContext.drawImage(canvas, 0, 0, 320, 150);
      context.globalCompositeOperation = dark ? "lighter" : "source-over";
      context.globalAlpha = bloom * (dark ? 0.65 : 0.3);
      context.filter = "blur(5px)";
      context.drawImage(glow, 0, 0, 640, 300);
      context.globalAlpha = bloom * (dark ? 0.25 : 0.12);
      context.filter = "blur(12px)";
      context.drawImage(glow, 0, 0, 640, 300);
      context.filter = "none";
      context.globalAlpha = 1;
      context.globalCompositeOperation = "source-over";
    }
    if (band) context.restore();
  }
  /** Keep entrance/morph live, then amortize dust/glow work across a bounded periodic phase cache. */
  function paint(field, time, moving) {
    const useCache =
      moving &&
      field.elapsed >= 2000 &&
      options.particleCache !== false &&
      cache.available &&
      (options.particleJitter ?? 0.75) > 0;
    if (!useCache) {
      paintRaw(field, time, moving);
      return;
    }
    if (strips?.available) {
      strips.paint(context, canvas, time, (at, band) =>
        paintRaw(field, at, true, band),
      );
      return;
    }
    if (cache.composite(context, time)) return;
    paintRaw(field, cache.nextTime, true);
    cache.capture(canvas);
    paintRaw(field, time, true);
  }
  palette();
  return {
    paint,
    palette,
    releaseFrames: () => {
      cache.clear();
      strips?.clear();
    },
    resize: () => {
      const width = canvas.getBoundingClientRect().width;
      return strips ? strips.resize(width) : cache.resize(width);
    },
    get cachedFrames() {
      return strips ? strips.size : cache.size;
    },
    get rasterBytes() {
      return strips ? strips.rasterBytes : cache.rasterBytes;
    },
    dispose() {
      cache.clear();
      strips?.clear();
      groups.clear();
      glow.width = glow.height = 0;
    },
  };
}
