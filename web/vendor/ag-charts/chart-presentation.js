import { asciiSelectionShape } from "./chart-ascii-selection.js";
import { node, button } from "./runtime.js";
const ns = "http://www.w3.org/2000/svg";
/** Render fixed SVG geometry with separate axes, data and selection layers; supplied values never become markup or URLs. */
export function svgScene(document, scene) {
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", `0 0 ${scene.width} ${scene.height}`);
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("ag-vt-chart__svg");
  const axes = document.createElementNS(ns, "g"),
    group = document.createElementNS(ns, "g"),
    selection = document.createElementNS(ns, "g");
  group.classList.add("ag-vt-chart__marks");
  selection.classList.add("ag-vt-chart__selection");
  for (const mark of scene.marks) {
    const shape = document.createElementNS(ns, mark.tag);
    for (const [key, value] of Object.entries(mark.attrs))
      shape.setAttribute(key, String(value));
    if (mark.text !== undefined) shape.textContent = mark.text;
    const isData =
      ["path", "rect", "circle"].includes(mark.tag) ||
      (mark.tag === "line" && mark.attrs.stroke !== "var(--ag-border)");
    (isData ? group : axes).append(shape);
  }
  svg.append(axes, group, selection);
  return { svg, group, selection };
}
/** Draw a fine semantic outline/halo over the chosen real marks, including above particle canvas, without inventing null values. */
export function highlight(
  { svg, selection, overlay, ascii, boundaries },
  index,
) {
  selection.replaceChildren();
  const matches = [];
  for (const shape of svg.querySelectorAll("[data-point]")) {
    const active = Number(shape.getAttribute("data-point")) === index;
    shape.setAttribute("data-selected", String(active));
    if (active) matches.push(shape);
  }
  for (const shape of matches) {
    if (ascii) {
      const glyphs = asciiSelectionShape(svg.ownerDocument, shape, ascii);
      if (glyphs) selection.append(glyphs);
      continue;
    }
    const copy = shape.cloneNode(false);
    copy.removeAttribute("data-point");
    copy.removeAttribute("data-series");
    copy.removeAttribute("data-selected");
    copy.setAttribute("class", "ag-vt-chart__selected");
    copy.setAttribute("stroke", "var(--ag-text)");
    copy.setAttribute("stroke-width", "1.25");
    copy.setAttribute("vector-effect", "non-scaling-stroke");
    if (copy.tagName === "circle") {
      copy.setAttribute("r", String(Number(copy.getAttribute("r")) + 3));
      copy.setAttribute("fill", "none");
    } else copy.setAttribute("fill", "none");
    selection.append(copy);
  }
  if (overlay) {
    const layers = boundaries
      ? [boundaries, selection.cloneNode(true)]
      : [selection.cloneNode(true)];
    overlay.replaceChildren(...layers);
  }
}
/** Compact native arrow/replay controls retain full accessible names and expose the selected category next to the readout. */
export function chartControls(document, listen, { previous, next, replay }) {
  const footer = node(document, "div", "ag-vt-chart__footer"),
    controls = node(document, "div", "ag-vt-chart__navigation"),
    prev = button(document, "", listen, previous),
    nextButton = button(document, "", listen, next),
    replayButton = button(document, "", listen, replay),
    counter = node(document, "span", "ag-vt-chart__counter"),
    readout = node(document, "output", "ag-vt-chart__readout");
  for (const [control, label, path] of [
    [prev, "Предыдущая точка", "m12 5-6 5 6 5"],
    [nextButton, "Следующая точка", "m8 5 6 5-6 5"],
    [replayButton, "Повторить анимацию", "M16 8a6 6 0 1 0 .2 4 M16 3v5h-5"],
  ]) {
    control.classList.add("ag-vt-chart__icon-button");
    control.setAttribute("aria-label", label);
    control.title = label;
    const svg = document.createElementNS(ns, "svg");
    for (const [key, value] of Object.entries({
      viewBox: "0 0 20 20",
      width: 18,
      height: 18,
      fill: "none",
      stroke: "currentColor",
      "stroke-width": 1.7,
      "stroke-linecap": "round",
      "stroke-linejoin": "round",
      "aria-hidden": true,
    }))
      svg.setAttribute(key, String(value));
    const line = document.createElementNS(ns, "path");
    line.setAttribute("d", path);
    svg.append(line);
    control.append(svg);
  }
  readout.setAttribute("aria-live", "polite");
  controls.append(prev, counter, nextButton, replayButton);
  footer.append(readout, controls);
  return {
    footer,
    readout,
    prev,
    next: nextButton,
    replay: replayButton,
    counter,
  };
}
