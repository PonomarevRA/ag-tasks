const STRIPS = 15,
  PERIOD = 500;
/** Keep two small strip atlases, refreshing one band per paint; shared timestamps preserve a coherent slow flow. */
export function particleStripCache(
  document,
  { budget = 1048576, width = 320 } = {},
) {
  let oldCanvas,
    newCanvas,
    oldContext,
    newContext,
    ready = false,
    available = true,
    index = 0,
    w = 1,
    h = 1;
  const times = new Float64Array(STRIPS);
  /** Release both atlases immediately; a new data/palette/viewport starts a fresh coherent strip sequence. */
  function clear() {
    for (const c of [oldCanvas, newCanvas]) if (c) c.width = c.height = 0;
    oldCanvas = newCanvas = oldContext = newContext = null;
    ready = false;
    index = 0;
  }
  /** Two atlases are bounded as a whole, independent of strip count; cap resolution to the displayed small background. */
  function resize(cssWidth) {
    const b = Number.isFinite(budget)
      ? Math.max(262144, Math.min(4194304, budget))
      : 1048576;
    const maximum = Math.floor(Math.sqrt(b / ((8 * 300) / 640)));
    const nextW = Math.max(
        32,
        Math.min(320, Math.ceil(cssWidth || width || 320), maximum),
      ),
      nextH = Math.max(15, Math.floor((nextW * 300) / 640));
    if (nextW === w && nextH === h) return false;
    clear();
    w = nextW;
    h = nextH;
    return true;
  }
  /** Allocate only when visible rendering first requests a band. */
  function initialize(source, time) {
    oldCanvas = document.createElement("canvas");
    newCanvas = document.createElement("canvas");
    for (const c of [oldCanvas, newCanvas]) {
      c.width = w;
      c.height = h;
    }
    oldContext = oldCanvas.getContext("2d");
    newContext = newCanvas.getContext("2d");
    if (!oldContext?.drawImage || !newContext?.drawImage) {
      available = false;
      clear();
      return false;
    }
    oldContext.drawImage(source, 0, 0, w, h);
    newContext.drawImage(source, 0, 0, w, h);
    times.fill(time - PERIOD);
    ready = true;
    return true;
  }
  /** Render at most1/15of particles to a future band, then blend each stored band toward the same continuous clock. */
  function paint(context, source, time, draw) {
    if (!ready) {
      draw(time);
      if (!initialize(source, time)) return;
    }
    const y = (index * 300) / STRIPS,
      bottom = ((index + 1) * 300) / STRIPS,
      row = Math.floor((index * h) / STRIPS),
      rowEnd = Math.floor(((index + 1) * h) / STRIPS);
    oldContext.clearRect(0, row, w, rowEnd - row);
    oldContext.drawImage(
      newCanvas,
      0,
      row,
      w,
      rowEnd - row,
      0,
      row,
      w,
      rowEnd - row,
    );
    draw(time + PERIOD, { top: y, bottom });
    newContext.clearRect(0, row, w, rowEnd - row);
    newContext.drawImage(
      source,
      0,
      y * 2,
      1280,
      (bottom - y) * 2,
      0,
      row,
      w,
      rowEnd - row,
    );
    times[index] = time;
    index = (index + 1) % STRIPS;
    context.setTransform(2, 0, 0, 2, 0, 0);
    context.clearRect(0, 0, 640, 300);
    for (let i = 0; i < STRIPS; i++) {
      const a = Math.floor((i * h) / STRIPS),
        b = Math.floor(((i + 1) * h) / STRIPS),
        top = (i * 300) / STRIPS,
        height = 300 / STRIPS,
        t = Math.min(1, Math.max(0, (time - times[i]) / PERIOD));
      context.globalAlpha = 1 - t;
      context.globalCompositeOperation = "source-over";
      context.drawImage(oldCanvas, 0, a, w, b - a, 0, top, 640, height);
      context.globalAlpha = t;
      context.globalCompositeOperation = "lighter";
      context.drawImage(newCanvas, 0, a, w, b - a, 0, top, 640, height);
    }
    context.globalAlpha = 1;
    context.globalCompositeOperation = "source-over";
  }
  resize(width);
  return {
    paint,
    clear,
    resize,
    get available() {
      return available;
    },
    get size() {
      return ready ? 2 : 0;
    },
    get rasterBytes() {
      return ready ? w * h * 8 : 0;
    },
  };
}
