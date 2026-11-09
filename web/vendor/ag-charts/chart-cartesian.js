import { axisDateValues } from "./chart-axis.js";
import { chartDomain } from "./chart-scale.js";
const colors = [
  "var(--ag-accent)",
  "var(--ag-success)",
  "var(--ag-text)",
  "var(--ag-danger)",
  "var(--ag-muted)",
  "var(--ag-border)",
];

/** Build Cartesian scales and axes, then dispatch one family of marks. */
export function cartesianScene(state, scene) {
  const {
    width,
    height,
    left,
    right,
    top,
    bottom,
    marks,
    particles,
    add,
    dot,
    segment,
  } = scene;
  const { type, labels, series, arrangement = "grouped" } = state;
  const points = type === "scatter" || type === "bubble";
  let all = [];
  for (const item of series)
    for (const value of item.values)
      if (value !== null) all.push(points ? value.y : value);
  if (type === "bar" && arrangement === "stacked") {
    all = [];
    for (let i = 0; i < labels.length; i++) {
      let positive = 0,
        negative = 0;
      for (const item of series) {
        const value = item.values[i] ?? 0;
        if (value > 0) positive += value;
        else negative += value;
      }
      all.push(positive, negative);
    }
  }
  const [min, max] = chartDomain(all, state.min, state.max),
    y = (value) => bottom - ((value - min) / (max - min)) * (bottom - top),
    category = (i) =>
      left + ((i + 0.5) / Math.max(1, labels.length)) * (right - left),
    number = new Intl.NumberFormat("ru-RU", { maximumFractionDigits: 2 });
  const horizontal = type === "bar" && arrangement === "horizontal",
    xValue = (value) => left + ((value - min) / (max - min)) * (right - left),
    rowHeight = (bottom - top) / Math.max(1, labels.length);
  for (let i = 0; i <= 4; i++) {
    const value = min + ((max - min) * i) / 4,
      position = horizontal ? xValue(value) : y(value);
    add(
      "line",
      horizontal
        ? {
            x1: position,
            x2: position,
            y1: top,
            y2: bottom,
            stroke: "var(--ag-border)",
          }
        : {
            x1: left,
            x2: right,
            y1: position,
            y2: position,
            stroke: "var(--ag-border)",
          },
    );
    add(
      "text",
      horizontal
        ? {
            x: position,
            y: bottom + 24,
            "text-anchor": "middle",
            fill: "var(--ag-muted)",
          }
        : {
            x: left - 8,
            y: position + 4,
            "text-anchor": "end",
            fill: "var(--ag-muted)",
          },
      number.format(value),
    );
  }
  const dates = axisDateValues(labels, state.axisDates);
  // Reserve a second calendar-context row only on a horizontal temporal axis.
  if (dates && !horizontal && !points) scene.height = 344;
  const labelStep = Math.max(1, Math.ceil(labels.length / 8));
  labels.forEach((label, index) => {
    if (horizontal && index % labelStep) return;
    add(
      "text",
      horizontal
        ? {
            x: left - 8,
            y: top + (index + 0.5) * rowHeight + 4,
            "text-anchor": "end",
            fill: "var(--ag-muted)",
          }
        : {
            x: category(index),
            y: bottom + 24,
            "data-axis-index": index,
            "data-axis-x": category(index),
            "text-anchor": "middle",
            fill: "var(--ag-muted)",
          },
      label,
    );
  });
  const mapping = {
    min,
    max,
    y,
    category,
    number,
    horizontal,
    xValue,
    rowHeight,
  };
  if (points) drawPointMarks(state, scene, mapping);
  else if (type === "composed") {
    drawBarMarks(state, scene, mapping);
    drawLineMarks(state, scene, mapping);
  } else if (type === "bar") drawBarMarks(state, scene, mapping);
  else drawLineMarks(state, scene, mapping);
  return { width, height, marks, particles };
}

/** Render numeric x/y points and area-scaled bubbles with numeric x ticks. */
function drawPointMarks(state, scene, mapping) {
  const {
    width,
    height,
    left,
    right,
    top,
    bottom,
    marks,
    particles,
    add,
    dot,
    segment,
  } = scene;
  const { type, labels, series, arrangement = "grouped" } = state;
  const { min, max, y, category, number, horizontal, xValue, rowHeight } =
    mapping;
  const xs = [];
  let sizeMax = 1;
  for (const item of series)
    for (const value of item.values) {
      if (value === null) continue;
      xs.push(value.x);
      sizeMax = Math.max(sizeMax, value.size ?? 1);
    }
  const [xMin, xMax] = chartDomain(xs);
  series.forEach((item, si) => {
    for (const [pointIndex, value] of item.values.entries()) {
      if (!value) continue;
      const cx = left + ((value.x - xMin) / (xMax - xMin)) * (right - left),
        cy = y(value.y),
        r = type === "bubble" ? 4 + 20 * Math.sqrt(value.size / sizeMax) : 4,
        color = colors[si];
      add("circle", {
        "data-point": pointIndex,
        "data-series": si,
        cx,
        cy,
        r,
        fill: color,
        "fill-opacity": 0.7,
        stroke: color,
      });
      scene.region({
        kind: "disk",
        x: cx,
        y: cy,
        radius: r,
        color,
        index: pointIndex,
        series: si,
      });
      for (let radius = 0; radius <= r; radius += 4) {
        const count = Math.max(1, Math.ceil((radius * Math.PI * 2) / 5));
        for (let j = 0; j < count; j++)
          dot(
            cx + Math.cos((j / count) * Math.PI * 2) * radius,
            cy + Math.sin((j / count) * Math.PI * 2) * radius,
            color,
          );
      }
    }
  });
  // Scatter has a numeric horizontal domain; replace categorical labels with actual x ticks.
  for (let i = marks.length - 1; i >= 0; i--)
    if (marks[i].tag === "text" && marks[i].attrs.y === bottom + 24)
      marks.splice(i, 1);
  for (let i = 0; i <= 4; i++)
    add(
      "text",
      {
        x: left + (i / 4) * (right - left),
        y: bottom + 24,
        "text-anchor": "middle",
        fill: "var(--ag-muted)",
      },
      number.format(xMin + (i / 4) * (xMax - xMin)),
    );
}

/** Render grouped, stacked or horizontal signed bars while keeping each zero baseline. */
function drawBarMarks(state, scene, mapping) {
  const {
    width,
    height,
    left,
    right,
    top,
    bottom,
    marks,
    particles,
    add,
    dot,
    segment,
  } = scene;
  const { type, labels, series, arrangement = "grouped" } = state;
  const { min, max, y, category, number, horizontal, xValue, rowHeight } =
    mapping;
  const positive = labels.map(() => 0),
    negative = labels.map(() => 0),
    band = (right - left) / Math.max(1, labels.length);
  const barCount =
    type === "composed"
      ? series.filter((item) => item.type === "bar").length
      : series.length;
  let barIndex = -1;
  series.forEach((item, si) => {
    if (type === "composed" && item.type !== "bar") return;
    barIndex++;
    item.values.forEach((value, index) => {
      if (value === null) return;
      const color = colors[si];
      let x, heightRect, widthRect, topRect;
      if (horizontal) {
        const slot = rowHeight / (series.length + 1);
        x = Math.min(xValue(0), xValue(value));
        widthRect = Math.abs(xValue(value) - xValue(0));
        topRect = top + index * rowHeight + slot * (si + 0.5);
        heightRect = slot * 0.85;
      } else {
        let baseline = 0;
        if (arrangement === "stacked") {
          baseline = value >= 0 ? positive[index] : negative[index];
          if (value >= 0) positive[index] += value;
          else negative[index] += value;
        }
        const slot = band / (arrangement === "stacked" ? 1 : barCount);
        widthRect = slot * 0.72;
        x =
          left +
          index * band +
          (arrangement === "stacked" ? band * 0.14 : slot * (barIndex + 0.14));
        topRect = Math.min(y(baseline), y(baseline + value));
        heightRect = Math.abs(y(baseline + value) - y(baseline));
      }
      add("rect", {
        "data-point": index,
        "data-series": si,
        x,
        y: topRect,
        width: widthRect,
        height: heightRect,
        fill: color,
      });
      scene.region({
        kind: "rect",
        x,
        y: topRect,
        width: widthRect,
        height: heightRect,
        color,
        index,
        series: si,
      });
      const step = Math.max(
        5,
        Math.sqrt(
          (widthRect * heightRect) /
            Math.max(1, 2400 / (series.length * Math.max(1, labels.length))),
        ),
      );
      for (let px = x + step / 2; px < x + widthRect; px += step)
        for (let py = topRect + step / 2; py < topRect + heightRect; py += step)
          dot(px, py, color);
    });
  });
}

/** Flush contiguous non-null runs independently so missing data never becomes an interpolated point. */
function drawLineMarks(state, scene, mapping) {
  const {
    width,
    height,
    left,
    right,
    top,
    bottom,
    marks,
    particles,
    add,
    dot,
    segment,
  } = scene;
  const { type, labels, series, arrangement = "grouped" } = state;
  const { min, max, y, category, number, horizontal, xValue, rowHeight } =
    mapping;
  series.forEach((item, si) => {
    if (type === "composed" && item.type === "bar") return;
    const seriesType = type === "composed" ? item.type : type;
    const color = colors[si];
    let run = [];
    /** Finish one contiguous run; an explicit null starts a new run instead of drawing across missing observations. */
    const flush = () => {
      if (!run.length) return;
      const d = run.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" ");
      if (seriesType === "area")
        add("path", {
          d: d + ` L${run.at(-1).x},${y(0)} L${run[0].x},${y(0)} Z`,
          fill: color,
          "fill-opacity": 0.12,
        });
      add("path", {
        d,
        fill: "none",
        stroke: color,
        "stroke-width": 2,
        "stroke-dasharray": si % 2 ? "5 3" : "none",
      });
      scene.region({ kind: "line", points: [...run], color, series: si });
      if (seriesType === "area" && run.length > 1) {
        scene.region({
          kind: "polygon",
          points: [
            ...run,
            { x: run.at(-1).x, y: y(0) },
            { x: run[0].x, y: y(0) },
          ],
          color,
          series: si,
          edge: run,
        });
      }
      for (let i = 1; i < run.length; i++) segment(run[i - 1], run[i], color);
      run = [];
    };
    item.values.forEach((value, index) => {
      if (value === null) {
        flush();
        return;
      }
      const p = { x: category(index), y: y(value), index };
      run.push(p);
      add("circle", {
        cx: p.x,
        cy: p.y,
        r: 3,
        fill: color,
        "data-point": index,
        "data-series": si,
      });
      dot(p.x, p.y, color, 3);
    });
    flush();
  });
}
