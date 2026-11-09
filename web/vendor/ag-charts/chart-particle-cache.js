export const PARTICLE_CYCLE_MS = 8000;
/** Own responsive periodic rasters with a strict raw RGBA budget; axes, tooltip and host DOM are never cached. */
export function particleFrameCache(
  document,
  requestedCount = 6,
  { budget = 1048576, width = 320 } = {},
) {
  const count = Number.isFinite(requestedCount)
    ? Math.max(4, Math.min(20, Math.round(requestedCount)))
    : 6;
  const maxBytes = Number.isFinite(budget)
    ? Math.max(262144, Math.min(4194304, Math.floor(budget)))
    : 1048576;
  const frames = [];
  let available = true,
    rasterWidth = 1,
    rasterHeight = 1;
  /** Zero backing stores immediately instead of waiting for native/GPU garbage collection. */
  function clear() {
    for (const frame of frames) frame.width = frame.height = 0;
    frames.length = 0;
  }
  /** Fit CSS width and phase count inside the byte ceiling; repeated same-size observations do not invalidate frames. */
  function resize(cssWidth) {
    const measured = Number.isFinite(cssWidth) && cssWidth > 0 ? cssWidth : 320;
    const maximum = Math.floor(Math.sqrt(maxBytes / (count * 4 * (300 / 640))));
    const nextWidth = Math.max(16, Math.min(640, Math.ceil(measured), maximum));
    const nextHeight = Math.max(1, Math.floor((nextWidth * 300) / 640));
    if (nextWidth === rasterWidth && nextHeight === rasterHeight) return false;
    clear();
    rasterWidth = nextWidth;
    rasterHeight = nextHeight;
    return true;
  }
  /** Bake one phase per visible paint to avoid a synchronous warm-up burst. */
  function capture(source) {
    if (frames.length >= count || !available) return;
    const frame = document.createElement("canvas");
    frame.width = rasterWidth;
    frame.height = rasterHeight;
    const context = frame.getContext("2d");
    if (!context?.drawImage) {
      frame.width = frame.height = 0;
      available = false;
      return;
    }
    context.drawImage(source, 0, 0, rasterWidth, rasterHeight);
    frames.push(frame);
  }
  /** Crossfade adjacent periodic phases; the stable SVG/data layer keeps its full resolution. */
  function composite(context, time) {
    if (frames.length !== count || !context.drawImage) return false;
    const phase = ((time % PARTICLE_CYCLE_MS) / PARTICLE_CYCLE_MS) * count;
    const index = Math.floor(phase),
      fraction = phase - index;
    context.setTransform(2, 0, 0, 2, 0, 0);
    context.clearRect(0, 0, 640, 300);
    context.globalAlpha = 1 - fraction;
    context.globalCompositeOperation = "source-over";
    context.drawImage(frames[index], 0, 0, 640, 300);
    context.globalAlpha = fraction;
    context.globalCompositeOperation = "lighter";
    context.drawImage(frames[(index + 1) % count], 0, 0, 640, 300);
    context.globalAlpha = 1;
    context.globalCompositeOperation = "source-over";
    return true;
  }
  resize(width);
  return {
    capture,
    composite,
    clear,
    resize,
    get available() {
      return available;
    },
    get size() {
      return frames.length;
    },
    get ready() {
      return frames.length === count;
    },
    get nextTime() {
      return (frames.length / count) * PARTICLE_CYCLE_MS;
    },
    get rasterBytes() {
      return frames.length * rasterWidth * rasterHeight * 4;
    },
    get width() {
      return rasterWidth;
    },
    get height() {
      return rasterHeight;
    },
  };
}
