import { axisPointLabel } from "./chart-axis.js";
import { bindSvgAxisLabels } from "./chart-axis-svg.js";
import { asciiSectorBoundaries } from "./chart-ascii-selection.js";
import { chartInspector } from "./chart-inspect.js";
import { node } from "./runtime.js";
import { validateChart, chartScene } from "./chart-geometry.js";
import { particleLayer } from "./chart-particles.js";
import { svgScene, highlight, chartControls } from "./chart-presentation.js";
import { chartMotion } from "./chart-motion.js";
let sequence = 0;
/** Express one supplied observation literally; a null remains missing instead of becoming a point or zero. */
function pointText(value) {
  if (value === null) return "нет данных";
  if (typeof value === "object")
    return `x ${value.x}, y ${value.y}${value.size !== undefined ? ", размер " + value.size : ""}`;
  return String(value);
}
/** Build a native expandable data table; graphics and animation never become the sole representation of the dataset. */
function dataTable(document, state) {
  const details = node(document, "details", "ag-vt-chart__data"),
    summary = node(document, "summary", "", "Данные графика"),
    table = node(document, "table", "ag-vt-table"),
    head = node(document, "tr");
  head.append(node(document, "th", "", "Категория"));
  for (const series of state.series)
    head.append(node(document, "th", "", series.label));
  const thead = node(document, "thead"),
    body = node(document, "tbody");
  thead.append(head);
  table.append(thead, body);
  for (let index = 0; index < state.labels.length; index++) {
    const row = node(document, "tr"),
      heading = node(
        document,
        "th",
        "",
        axisPointLabel(state.labels, index, state.axisDates),
      );
    row.dataset.row = String(index);
    heading.scope = "row";
    row.append(heading);
    for (const series of state.series)
      row.append(
        node(
          document,
          "td",
          "",
          series.values[index] === null
            ? "Нет данных"
            : pointText(series.values[index]),
        ),
      );
    body.append(row);
  }
  details.append(summary, table);
  return details;
}
/** Local SVG/particle charts with visible selection, visibility-aware animation and compact keyboard/pointer controls. */
export function createChart({
  document = globalThis.document,
  ...options
} = {}) {
  let state,
    scene,
    disposed = false,
    layer,
    motion,
    presentation,
    controls,
    table,
    inspector,
    axisCleanup,
    listeners = [],
    selected = 0;
  const element = node(document, "section", "ag-vt ag-vt-chart"),
    id = "ag-chart-readout-" + ++sequence;
  const listen = (target, name, handler, options) => {
    target.addEventListener(name, handler, options);
    listeners.push(() => target.removeEventListener(name, handler, options));
  };
  /** Change the real mark/sector outline, live value, row state and edge controls together; focus stays on the user's control. */
  function select(index, { animate = true, notify = true } = {}) {
    if (disposed) return false;
    if (!Number.isInteger(index) || index < 0 || index >= state.labels.length)
      return false;
    selected = index;
    element.dataset.selectedIndex = String(index);
    highlight(presentation, index);
    layer?.select?.(index);
    inspector?.centerValue(index);
    controls.readout.textContent =
      axisPointLabel(state.labels, index, state.axisDates) +
      ": " +
      state.series
        .map((series) => series.label + " " + pointText(series.values[index]))
        .join("; ");
    controls.counter.textContent = index + 1 + " / " + state.labels.length;
    controls.prev.disabled = index === 0;
    controls.next.disabled = index === state.labels.length - 1;
    for (const row of table.querySelectorAll("[data-row]"))
      row.classList.toggle(
        "ag-vt-chart__row--selected",
        Number(row.dataset.row) === index,
      );
    if (animate) motion?.selected();
    if (notify) {
      inspector?.show(index);
      state.onSelect?.({ index, label: state.labels[index] });
    }
    return true;
  }
  /** Replay the currently supplied data. Particle fields scatter then settle; SVG data reveal while labels/controls remain static. */
  function replay() {
    if (disposed) return false;
    const started = motion?.play() ?? false;
    const particles = layer?.play({ replay: true }) ?? false;
    return started || particles;
  }
  /** Resolve native click/touch to a rendered mark; line charts also accept a category position in the plot. */
  function choose(event) {
    const mark = event.target.closest?.("[data-point]");
    if (mark) {
      select(Number(mark.getAttribute("data-point")));
      return;
    }
    const hit = inspector?.locate(event);
    if (hit?.index !== null && hit) select(hit.index);
  }
  /** Keyboard navigation uses the same selection state as pointer/touch and native arrow buttons. */
  function key(event) {
    let index = selected;
    if (event.key === "ArrowLeft" || event.key === "ArrowUp") index--;
    else if (event.key === "ArrowRight" || event.key === "ArrowDown") index++;
    else if (event.key === "Home") index = 0;
    else if (event.key === "End") index = state.labels.length - 1;
    else return;
    event.preventDefault();
    select(Math.max(0, Math.min(state.labels.length - 1, index)));
  }
  /** Validate/compute before teardown so an invalid update leaves the prior chart and interaction handlers intact. */
  function update(next = {}) {
    if (disposed) return false;
    const candidate = {
      type: "line",
      renderer: "svg",
      arrangement: "grouped",
      animated: true,
      ...state,
      ...next,
    };
    validateChart(candidate);
    if (Array.isArray(candidate.axisDates))
      candidate.axisDates = [...candidate.axisDates];
    const nextScene = chartScene(candidate),
      previousPositions = layer?.snapshot();
    axisCleanup?.();
    inspector = undefined;
    layer?.dispose();
    layer = undefined;
    motion?.dispose();
    for (const remove of listeners) remove();
    listeners = [];
    state = candidate;
    scene = nextScene;
    selected = Math.min(selected, Math.max(0, state.labels.length - 1));
    element.replaceChildren();
    element.append(node(document, "h3", "", state.label));
    const plot = node(document, "div", "ag-vt-chart__plot");
    plot.style.aspectRatio = scene.width + " / " + scene.height;
    plot.tabIndex = 0;
    plot.setAttribute("role", "group");
    plot.setAttribute("aria-label", state.label + " — стрелки выбирают точку");
    plot.setAttribute("aria-describedby", id);
    presentation = svgScene(document, scene);
    plot.append(presentation.svg);
    if (state.renderer === "ascii" && ["pie", "donut"].includes(state.type))
      plot.style.backgroundColor = "var(--ag-surface)";
    element.dataset.renderer = state.renderer;
    element.append(plot);
    listen(plot, "click", choose);
    listen(plot, "keydown", key);
    if (state.renderer === "particles" || state.renderer === "ascii") {
      layer = particleLayer(document, scene, {
        ...state,
        element,
        previousPositions,
      });
      if (layer) {
        plot.append(layer.canvas);
        presentation.svg.classList.add("ag-vt-chart__svg--axes");
        const overlay = document.createElementNS(
          "http://www.w3.org/2000/svg",
          "svg",
        );
        overlay.setAttribute("viewBox", "0 0 640 300");
        overlay.setAttribute("aria-hidden", "true");
        overlay.classList.add("ag-vt-chart__overlay");
        plot.append(overlay);
        presentation.overlay = overlay;
        if (state.renderer === "ascii") {
          presentation.ascii = { scene, options: state };
          presentation.boundaries = asciiSectorBoundaries(
            document,
            scene,
            presentation.svg,
          );
        }
      } else
        element.append(
          node(document, "p", "ag-vt-muted", "Canvas недоступен: показан SVG."),
        );
    }
    motion = chartMotion({
      element,
      group: presentation.group,
      selection: presentation.selection,
      animated:
        Boolean(state.animated) &&
        !state.paused &&
        !["particles", "ascii"].includes(state.renderer),
    });
    inspector = chartInspector(
      document,
      plot,
      state,
      scene,
      listen,
      select,
      pointText,
    );
    const legend = node(document, "ul", "ag-vt-chart__legend");
    for (const [index, series] of state.series.entries())
      legend.append(node(document, "li", "", `${index + 1}. ${series.label}`));
    element.append(legend);
    controls = chartControls(document, listen, {
      previous: () => select(selected - 1),
      next: () => select(selected + 1),
      replay,
    });
    controls.readout.id = id;
    controls.replay.hidden = !state.animated;
    table = dataTable(document, state);
    element.append(controls.footer, table);
    if (state.labels.length)
      select(selected, { animate: false, notify: false });
    else {
      controls.readout.textContent = "Нет данных";
      controls.counter.textContent = "0 / 0";
      controls.prev.disabled = controls.next.disabled = true;
    }
    if (
      !state.series.some((series) =>
        series.values.some((value) => value !== null),
      )
    )
      element.append(
        node(document, "p", "ag-vt-muted", "Нет переданных значений"),
      );
    axisCleanup = bindSvgAxisLabels(
      presentation.svg,
      state.labels,
      state.axisDates,
      { left: scene.left, right: scene.right, width: scene.width },
    );
    motion.play();
    return true;
  }
  update(options);
  return {
    element,
    update,
    select,
    replay,
    dispose() {
      if (disposed) return;
      disposed = true;
      axisCleanup?.();
      layer?.dispose();
      motion?.dispose();
      for (const remove of listeners) remove();
      listeners = [];
      element.remove();
    },
  };
}
