/** Bind an inner-edge splitter without moving the panel to another side; pointer and keyboard share clamped sizing. */
export function bindResizablePanel({
  panel,
  edge = "right",
  minWidth = 120,
  maxWidth = 520,
  initialWidth = 220,
  storageKey,
  storage,
  reservedWidth = () => 0,
  onResize = () => {},
} = {}) {
  if (
    !panel ||
    !["left", "right"].includes(edge) ||
    ![minWidth, maxWidth, initialWidth].every(Number.isFinite) ||
    minWidth < 48 ||
    maxWidth < minWidth
  )
    throw new TypeError("Panel, edge and bounded widths required");
  const document = panel.ownerDocument,
    view = document.defaultView;
  const handle = document.createElement("div");
  handle.className = "ag-panel-resizer";
  handle.dataset.edge = edge;
  handle.setAttribute("role", "separator");
  handle.setAttribute("aria-orientation", "vertical");
  handle.setAttribute(
    "aria-label",
    edge === "right"
      ? "Изменить ширину левой панели"
      : "Изменить ширину правой панели",
  );
  handle.tabIndex = 0;
  handle.title =
    "Потяните границу или используйте стрелки; Home / End — минимальная / максимальная ширина";
  panel.classList.add("ag-resizable-panel");
  panel.append(handle);
  let width = initialWidth,
    start = null,
    disposed = false;
  let persistence = storage;
  try {
    persistence ??= view?.localStorage;
  } catch {
    /* Sandboxed/opaque-origin pages may deny storage access. */
  }
  try {
    if (storageKey) {
      const value = Number(persistence?.getItem(storageKey));
      if (Number.isFinite(value) && value >= minWidth) width = value;
    }
  } catch {
    /* Width persistence is optional. */
  }
  /** Keep keyboard, pointer, ARIA and host layout synchronized through one bounded update. */
  function setWidth(value, { remember = true } = {}) {
    if (disposed || !Number.isFinite(value)) return;
    const available = panel.parentElement?.clientWidth || maxWidth + 200;
    const upper = Math.max(
      minWidth,
      Math.min(
        maxWidth,
        available - Math.max(0, Number(reservedWidth()) || 0) - 160,
      ),
    );
    width = Math.round(Math.max(minWidth, Math.min(upper, value)));
    panel.style.width = width + "px";
    handle.setAttribute("aria-valuemin", String(minWidth));
    handle.setAttribute("aria-valuemax", String(upper));
    handle.setAttribute("aria-valuenow", String(width));
    onResize(width);
    if (remember && storageKey)
      try {
        persistence?.setItem(storageKey, String(width));
      } catch {
        /* Private mode must not break layout. */
      }
  }
  const move = (event) => {
    if (start && event.pointerId === start.id) {
      event.preventDefault();
      setWidth(
        start.width + (event.clientX - start.x) * (edge === "right" ? 1 : -1),
        { remember: false },
      );
    }
  };
  const stop = () => {
    if (!start) return;
    start = null;
    view.removeEventListener("pointermove", move);
    view.removeEventListener("pointerup", stop);
    view.removeEventListener("pointercancel", stop);
    panel.classList.remove("ag-panel-resizing");
    setWidth(width);
  };
  const down = (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    start = { id: event.pointerId, x: event.clientX, width };
    panel.classList.add("ag-panel-resizing");
    view.addEventListener("pointermove", move);
    view.addEventListener("pointerup", stop);
    view.addEventListener("pointercancel", stop);
    handle.focus();
  };
  const key = (event) => {
    let next = width;
    const step = event.shiftKey ? 32 : 8;
    if (event.key === "Home") next = minWidth;
    else if (event.key === "End") next = maxWidth;
    else if (["ArrowLeft", "ArrowRight"].includes(event.key))
      next +=
        (event.key === "ArrowRight" ? 1 : -1) *
        (edge === "right" ? 1 : -1) *
        step;
    else return;
    event.preventDefault();
    setWidth(next);
  };
  const resize = () => setWidth(width, { remember: false });
  handle.addEventListener("pointerdown", down);
  handle.addEventListener("keydown", key);
  view.addEventListener("resize", resize);
  setWidth(width, { remember: false });
  return {
    handle,
    setWidth,
    get width() {
      return width;
    },
    dispose() {
      if (disposed) return;
      stop();
      disposed = true;
      handle.removeEventListener("pointerdown", down);
      handle.removeEventListener("keydown", key);
      view.removeEventListener("resize", resize);
      handle.remove();
      panel.classList.remove("ag-resizable-panel");
    },
  };
}
