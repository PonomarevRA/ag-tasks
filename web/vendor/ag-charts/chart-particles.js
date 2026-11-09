import { node } from "./runtime.js";
import { createParticleField } from "./chart-particle-field.js";
import { asciiChartPainter } from "./chart-ascii-paint.js";
import { particlePainter } from "./chart-particle-paint.js";
/** Own a bounded spring/dust animation. Hidden, offscreen, paused and reduced-motion charts schedule no frames. */
export function particleLayer(
  document,
  scene,
  { element, previousPositions, ...options },
) {
  const canvas = node(document, "canvas", "ag-vt-chart__canvas");
  canvas.width = 1280;
  canvas.height = 600;
  canvas.setAttribute("aria-hidden", "true");
  let context;
  try {
    context = canvas.getContext("2d");
  } catch {
    return null;
  }
  if (
    !context ||
    (options.renderer === "ascii" && typeof context.fillText !== "function")
  )
    return null;
  const view = document.defaultView,
    preference = view?.matchMedia?.("(prefers-reduced-motion: reduce)");
  const field = createParticleField(scene.particles, previousPositions);
  const makePainter =
    options.renderer === "ascii" ? asciiChartPainter : particlePainter;
  const painter = makePainter(
    document,
    element,
    canvas,
    context,
    scene.particles,
    options,
  );
  let frame = 0,
    disposed = false,
    visible = !view?.IntersectionObserver,
    observer,
    intersection,
    resizeObserver,
    last = 0,
    clock = 0,
    started = false;
  element.dataset.particleCount = String(scene.particles.length);
  /** Suspend without a latent RAF; static final data stay visible even when no animation is allowed. */
  function stop() {
    if (frame) view.cancelAnimationFrame(frame);
    frame = 0;
    last = 0;
    field.settle();
    painter.releaseFrames();
    delete element.dataset.particleCachedFrames;
    delete element.dataset.particleCacheBytes;
    painter.paint(field, clock, false);
    painter.releaseFrames();
    element.removeAttribute("data-particles-animating");
  }
  /** Paint flights near 60fps, then drift near 30fps; the timestamp gate also works on 120Hz screens. */
  function tick(now) {
    frame = 0;
    if (disposed || !visible || document.hidden || !element.isConnected) {
      stop();
      return;
    }
    const settled = field.elapsed >= 2000;
    if (!last || now - last >= (settled ? 1000 / 30 : 1000 / 60) - 1) {
      const dt = last ? Math.min(64, now - last) : 16.667;
      last = now;
      clock += dt;
      field.advance(dt);
      painter.paint(field, clock, true);
      element.dataset.particleCachedFrames = String(painter.cachedFrames);
      element.dataset.particleCacheBytes = String(painter.rasterBytes);
    }
    const idleMotion =
      options.renderer === "ascii"
        ? (options.asciiSpeed ?? 0.6) > 0
        : (options.particleJitter ?? 0.75) > 0;
    if (field.elapsed < 2000 || idleMotion)
      frame = view.requestAnimationFrame(tick);
    else element.removeAttribute("data-particles-animating");
  }
  /** Replay scatters, resume keeps the field. Motion permission is shared by controls, preference and visibility. */
  function play({ replay = false } = {}) {
    if (disposed) return false;
    if (replay) field.reset(true);
    if (
      !options.animated ||
      options.paused ||
      preference?.matches ||
      document.hidden ||
      !visible ||
      !view?.requestAnimationFrame
    ) {
      stop();
      return false;
    }
    if (!started) {
      field.reset();
      started = true;
    }
    if (!frame && scene.particles.length) {
      element.dataset.particlesAnimating = "true";
      last = 0;
      frame = view.requestAnimationFrame(tick);
    }
    return Boolean(frame);
  }
  const theme = () => {
    painter.palette();
    painter.paint(field, clock, Boolean(frame));
  };
  /** Resize only invalidates when the fitted cache dimensions change; no layout reads occur during painting. */
  const resized = () => {
    if (!disposed && painter.resize())
      painter.paint(field, clock, Boolean(frame));
  };
  const changed = () => {
    if (preference?.matches || document.hidden) stop();
    else play();
  };
  /** Attach only ancestor palette observers and one visibility observer after insertion; never watch canvas drawing state. */
  function mount() {
    if (disposed) return;
    if (view?.MutationObserver) {
      observer = new view.MutationObserver(theme);
      for (let parent = element; parent; parent = parent.parentElement)
        observer.observe(parent, {
          attributes: true,
          attributeFilter: ["style", "class", "data-theme", "data-palette"],
        });
    }
    if (view?.IntersectionObserver) {
      intersection = new view.IntersectionObserver(
        (entries) => {
          visible = entries.some((entry) => entry.isIntersecting);
          if (visible) play();
          else stop();
        },
        { threshold: 0 },
      );
      intersection.observe(element);
    }
    painter.resize();
    if (view?.ResizeObserver) {
      resizeObserver = new view.ResizeObserver(resized);
      resizeObserver.observe(canvas);
    } else view?.addEventListener?.("resize", resized);
    painter.palette();
    painter.paint(field, clock, false);
    play();
  }
  view?.queueMicrotask?.(mount);
  preference?.addEventListener?.("change", changed);
  document.addEventListener("visibilitychange", changed);
  return {
    canvas,
    play,
    /** Paint an explicit selection once even when paused; ASCII radial motion owns no extra scheduler. */
    select(index) {
      if (!disposed && painter.select?.(index)) {
        painter.paint(field, clock, Boolean(frame));
        if (!frame) painter.releaseFrames();
      }
    },
    snapshot: field.snapshot,
    dispose() {
      if (disposed) return;
      if (frame) view.cancelAnimationFrame(frame);
      frame = 0;
      disposed = true;
      element.removeAttribute("data-particles-animating");
      delete element.dataset.particleCount;
      delete element.dataset.particleCachedFrames;
      delete element.dataset.particleCacheBytes;
      observer?.disconnect();
      intersection?.disconnect();
      resizeObserver?.disconnect();
      view?.removeEventListener?.("resize", resized);
      painter.dispose();
      preference?.removeEventListener?.("change", changed);
      document.removeEventListener("visibilitychange", changed);
      canvas.width = canvas.height = 0;
    },
  };
}
