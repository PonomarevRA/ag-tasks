const colors = [
  "var(--ag-accent)",
  "var(--ag-success)",
  "var(--ag-text)",
  "var(--ag-danger)",
  "var(--ag-muted)",
  "var(--ag-border)",
];

/** Own one finite plot coordinate system, marks and a 2,400-point sampling budget. */
export function makeChartScene() {
  const width = 640,
    height = 300,
    left = 56,
    right = 620,
    top = 24,
    bottom = 252,
    marks = [],
    particles = [],
    regions = [];
  const add = (tag, attrs, text) => marks.push({ tag, attrs, text });
  const dot = (x, y, color, r = 2) => {
    if (particles.length < 2400) particles.push({ x, y, color, r });
  };
  const segment = (a, b, color) => {
    const count = Math.max(1, Math.ceil(Math.hypot(b.x - a.x, b.y - a.y) / 5));
    for (let step = 0; step <= count; step++)
      dot(
        a.x + ((b.x - a.x) * step) / count,
        a.y + ((b.y - a.y) * step) / count,
        color,
      );
  };

  return {
    width,
    height,
    left,
    right,
    top,
    bottom,
    marks,
    particles,
    regions,
    region: (shape) => regions.push(shape),
    add,
    dot,
    segment,
  };
}
