import { axisPointLabel } from "./chart-axis.js";
import {
  chartAnchor,
  chartLocalPlacement,
  chartTooltipPlacement,
} from "./chart-tooltip-placement.js";
import { node } from "./runtime.js";
/** Find the real category under a pointer. Radial/point shapes use their geometry, not categorical x bins. */
export function hitChart(scene, state, x, y) {
  let nearest = null,
    distance = Infinity;
  for (const region of scene.regions) {
    let inside = false;
    if (region.kind === "rect")
      inside =
        x >= region.x &&
        x <= region.x + region.width &&
        y >= region.y &&
        y <= region.y + region.height;
    else if (region.kind === "disk")
      inside = Math.hypot(x - region.x, y - region.y) <= region.radius + 8;
    else if (region.kind === "sector") {
      const radius = Math.hypot(x - region.x, y - region.y);
      let angle = Math.atan2(y - region.y, x - region.x);
      while (angle < region.start) angle += Math.PI * 2;
      inside =
        radius >= region.inner &&
        radius <= region.radius &&
        angle <= region.end;
    }
    if (inside && region.index !== undefined) return region.index;
    if (region.points && !["pie", "donut"].includes(state.type)) {
      for (const point of region.points) {
        if (point.index === undefined) continue;
        const next = Math.hypot(x - point.x, y - point.y);
        if (next < distance) {
          nearest = point.index;
          distance = next;
        }
      }
    }
  }
  if (state.type === "radar") return distance < 100 ? nearest : null;
  if (["scatter", "bubble", "pie", "donut"].includes(state.type)) return null;
  if (state.arrangement === "horizontal") {
    if (y < scene.top || y > scene.bottom) return null;
    return Math.min(
      state.labels.length - 1,
      Math.floor(
        ((y - scene.top) / (scene.bottom - scene.top)) * state.labels.length,
      ),
    );
  }
  if (x < scene.left || x > scene.right) return null;
  return Math.min(
    state.labels.length - 1,
    Math.floor(
      ((x - scene.left) / (scene.right - scene.left)) * state.labels.length,
    ),
  );
}
/** Own an anchored literal tooltip and fine crosshair. Keyboard and hover share selection without stealing focus. */
export function chartInspector(
  document,
  plot,
  state,
  scene,
  listen,
  select,
  pointText,
) {
  const tooltip = node(document, "div", "ag-vt-chart__tooltip"),
    title = node(document, "strong"),
    values = node(document, "div");
  const crosshair = node(document, "div", "ag-vt-chart__crosshair");
  tooltip.setAttribute("role", "tooltip");
  tooltip.hidden = crosshair.hidden = true;
  tooltip.append(title, values);
  plot.append(crosshair, tooltip);
  const center = node(document, "div", "ag-vt-chart__center"),
    amount = node(document, "strong"),
    caption = node(document, "span");
  center.setAttribute("aria-hidden", "true");
  center.append(amount, caption);
  if (state.type === "donut") plot.append(center);
  let active = -1;
  /** Repeat the selected literal donut amount in the open centre; table/readout remain the accessible source. */
  function centerValue(index) {
    if (state.type !== "donut") return;
    amount.textContent = pointText(state.series[0].values[index]);
    caption.textContent = axisPointLabel(state.labels, index, state.axisDates);
  }
  /** Measure the actual tooltip and avoid the whole selected mark and cursor; dock below the plot if no free side fits. */
  function show(index, x, y) {
    if (
      state.showTooltip === false ||
      index < 0 ||
      index >= state.labels.length
    )
      return;
    title.textContent = axisPointLabel(state.labels, index, state.axisDates);
    values.replaceChildren();
    for (let si = 0; si < state.series.length; si++) {
      const series = state.series[si],
        row = node(document, "div", "ag-vt-chart__tooltip-row"),
        swatch = node(document, "span", "ag-vt-chart__swatch");
      swatch.dataset.series = String(
        ["pie", "donut"].includes(state.type) ? index % 6 : si,
      );
      row.append(
        swatch,
        node(document, "span", "", series.label),
        node(document, "b", "", pointText(series.values[index])),
      );
      values.append(row);
    }
    plot.append(tooltip);
    tooltip.dataset.placement = "floating";
    tooltip.style.visibility = "hidden";
    tooltip.style.left = tooltip.style.top = "0px";
    tooltip.hidden = false;
    const rect = plot.getBoundingClientRect(),
      box = tooltip.getBoundingClientRect(),
      view = document.defaultView;
    const logical = chartAnchor(
      scene,
      index,
      Number.isFinite(x) && Number.isFinite(y) ? { x, y } : null,
    );
    const anchor = {
      left: rect.left + (logical.left * rect.width) / 640,
      right: rect.left + (logical.right * rect.width) / 640,
      top: rect.top + (logical.top * rect.height) / 300,
      bottom: rect.top + (logical.bottom * rect.height) / 300,
    };
    const placement = chartTooltipPlacement(anchor, box, {
      left: Math.max(rect.left, 0) + 8,
      right: Math.min(rect.right, view?.innerWidth || rect.right) - 8,
      top: Math.max(rect.top, 0) + 8,
      bottom: Math.min(rect.bottom, view?.innerHeight || rect.bottom) - 8,
    });
    const local = chartLocalPlacement(plot, rect, box, placement);
    if (local) {
      tooltip.style.left = local.x + "px";
      tooltip.style.top = local.y + "px";
      tooltip.dataset.side = local.side;
    } else {
      // Normal flow cannot obscure the source, including when the popup is larger than the plot.
      plot.after(tooltip);
      tooltip.dataset.placement = "docked";
      delete tooltip.dataset.side;
    }
    tooltip.style.visibility = "";
    crosshair.hidden = ["pie", "donut", "radar"].includes(state.type);
    const cursorX = Number.isFinite(x) ? x : (logical.left + logical.right) / 2;
    crosshair.style.left =
      Math.max(0, Math.min(100, (cursorX / 640) * 100)) + "%";
  }
  /** Convert client pixels to the fixed logical scene before testing any geometry. */
  function locate(event) {
    const rect = plot.getBoundingClientRect();
    if (!rect.width || !rect.height) return null;
    const x = ((event.clientX - rect.left) / rect.width) * 640,
      y = ((event.clientY - rect.top) / rect.height) * 300;
    return { x, y, index: hitChart(scene, state, x, y) };
  }
  listen(plot, "pointermove", (event) => {
    const hit = locate(event);
    if (hit?.index === null || !hit) {
      hide();
      return;
    }
    if (active !== hit.index) {
      active = hit.index;
      select(active, { animate: false, notify: false });
    }
    show(hit.index, hit.x, hit.y);
  });
  listen(plot, "pointerleave", hide);
  if (document.defaultView) listen(document.defaultView, "resize", hide);
  listen(document, "scroll", hide, true);
  listen(plot, "blur", hide);
  listen(plot, "keydown", (event) => {
    if (event.key === "Escape") hide();
  });
  /** Close without moving focus; resize/scroll invalidate the last measured geometry. */
  function hide() {
    tooltip.hidden = crosshair.hidden = true;
    active = -1;
  }
  return { show, hide, locate, centerValue };
}
