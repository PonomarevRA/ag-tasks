/**
 * Fit the board or graph to remaining window space, keeping card height and document scrolling natural.
 * @param {object} options - Main content and its existing contextual chrome in the same document.
 * @returns {Function} Idempotent cleanup for observers, resize listener and scheduled measurement.
 */
export function bindWorkAreaFit({ content, chrome = [] }) {
  const view = content.ownerDocument.defaultView;
  let scheduled = null,
    disposed = false;
  function measure() {
    scheduled = null;
    if (disposed) return;
    const target = content.querySelector(".board, .task-graph-viewport");
    if (!target || !target.isConnected) return;
    const top = Math.max(0, target.getBoundingClientRect().top);
    const height = Math.max(280, Math.floor(view.innerHeight - top - 20));
    const value = height + "px";
    if (target.style.getPropertyValue("--task-work-area-height") !== value)
      target.style.setProperty("--task-work-area-height", value);
  }
  // Coalesce real navigation, chrome and window changes; no idle polling or frame loop.
  function schedule() {
    if (!disposed && scheduled === null)
      scheduled = view.requestAnimationFrame(measure);
  }
  const resize = view.ResizeObserver ? new view.ResizeObserver(schedule) : null;
  for (const node of chrome) resize?.observe(node);
  const changes = new view.MutationObserver(schedule);
  changes.observe(content, { childList: true });
  view.addEventListener("resize", schedule);
  schedule();
  return () => {
    if (disposed) return;
    disposed = true;
    resize?.disconnect();
    changes.disconnect();
    view.removeEventListener("resize", schedule);
    if (scheduled !== null) view.cancelAnimationFrame(scheduled);
  };
}
