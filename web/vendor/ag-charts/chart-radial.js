const colors = [
  "var(--ag-accent)",
  "var(--ag-success)",
  "var(--ag-text)",
  "var(--ag-danger)",
  "var(--ag-muted)",
  "var(--ag-border)",
];

/** Draw nonnegative part-to-whole sectors and their bounded particle field. */
export function pieScene(state, scene) {
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
  const values = series[0].values.map((value) => value ?? 0),
    total = values.reduce((sum, value) => sum + value, 0);
  let angle = -Math.PI / 2;
  const cx = 320,
    cy = 142,
    r = 110,
    inner = type === "donut" ? 65 : 0;
  for (let index = 0; index < values.length; index++) {
    const fraction = total ? values[index] / total : 0;
    if (!fraction) continue;
    const end = angle + fraction * Math.PI * 2,
      color = colors[index % colors.length],
      point = (a, radius) => [
        cx + Math.cos(a) * radius,
        cy + Math.sin(a) * radius,
      ];
    const [x, y] = point(angle, r),
      [ex, ey] = point(end, r);
    let d;
    if (fraction >= 1 - 1e-10) {
      add("circle", {
        cx,
        cy,
        r,
        fill: color,
        "data-point": index,
        "data-series": 0,
      });
      if (inner) add("circle", { cx, cy, r: inner, fill: "var(--ag-surface)" });
    } else {
      const [ix, iy] = point(end, inner),
        [sx, sy] = point(angle, inner);
      d = `M${x},${y} A${r},${r} 0 ${fraction > 0.5 ? 1 : 0} 1 ${ex},${ey} L${ix},${iy} ${inner ? `A${inner},${inner} 0 ${fraction > 0.5 ? 1 : 0} 0 ${sx},${sy}` : ""} Z`;
      add("path", {
        "data-point": index,
        "data-series": 0,
        d,
        fill: color,
        stroke: "var(--ag-surface)",
        "stroke-width": 2,
      });
    }
    scene.region({
      kind: "sector",
      x: cx,
      y: cy,
      radius: r,
      inner,
      start: angle,
      end,
      color,
      index,
      series: 0,
    });
    for (let radius = inner + 3; radius < r; radius += 6) {
      const count = Math.max(1, Math.ceil(((end - angle) * radius) / 6));
      for (let j = 0; j < count; j++)
        dot(
          cx + Math.cos(angle + ((end - angle) * (j + 0.5)) / count) * radius,
          cy + Math.sin(angle + ((end - angle) * (j + 0.5)) / count) * radius,
          color,
        );
    }
    angle = end;
  }
  if (!total)
    add(
      "text",
      { x: 320, y: 145, "text-anchor": "middle", fill: "var(--ag-muted)" },
      "Нет положительных значений",
    );
  return { width, height, marks, particles };
}

/** Draw independent radar axes and explicitly supplied values, preserving gaps. */
export function radarScene(state, scene) {
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
  const all = [];
  for (const item of series)
    for (const value of item.values) if (value !== null) all.push(value);
  const maximum = state.max ?? Math.max(1, ...all),
    cx = 320,
    cy = 142,
    r = 105,
    count = labels.length;
  if (count < 3) throw new RangeError("Radar needs at least three categories");
  const position = (i, value) => ({
    x:
      cx +
      (Math.cos(-Math.PI / 2 + (i * Math.PI * 2) / count) * r * value) /
        maximum,
    y:
      cy +
      (Math.sin(-Math.PI / 2 + (i * Math.PI * 2) / count) * r * value) /
        maximum,
  });
  for (let level = 1; level <= 4; level++) {
    const path = labels.map((_, i) => position(i, (maximum * level) / 4));
    add("polygon", {
      points: path.map((p) => `${p.x},${p.y}`).join(" "),
      fill: "none",
      stroke: "var(--ag-border)",
    });
  }
  labels.forEach((label, i) => {
    const p = position(i, maximum * 1.15);
    add(
      "text",
      { x: p.x, y: p.y, "text-anchor": "middle", fill: "var(--ag-muted)" },
      label.slice(0, 16),
    );
  });
  series.forEach((item, index) => {
    const color = colors[index];
    const polygon = [];
    let previous;
    item.values.forEach((value, i) => {
      if (value === null) {
        previous = undefined;
        return;
      }
      const p = { ...position(i, value), index: i };
      polygon.push(p);
      add("circle", {
        cx: p.x,
        cy: p.y,
        r: 4,
        fill: color,
        "data-point": i,
        "data-series": index,
      });
      if (previous) {
        add("line", {
          x1: previous.x,
          y1: previous.y,
          x2: p.x,
          y2: p.y,
          stroke: color,
          "stroke-width": 2,
        });
        scene.region({
          kind: "line",
          points: [previous, p],
          color,
          series: index,
        });
        segment(previous, p, color);
      }
      previous = p;
    });
    if (item.values.every((value) => value !== null)) {
      const a = position(count - 1, item.values[count - 1]),
        b = position(0, item.values[0]);
      add("line", {
        x1: a.x,
        y1: a.y,
        x2: b.x,
        y2: b.y,
        stroke: color,
        "stroke-width": 2,
      });
      scene.region({
        kind: "polygon",
        points: polygon,
        color,
        series: index,
        edge: polygon,
      });
      segment(a, b, color);
    }
  });
  return { width, height, marks, particles };
}
