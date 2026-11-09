import { axisDateValues, layoutAxisTicks } from "./chart-axis.js";

/** Reflow only horizontal axis text using actual SVG text metrics; return a complete observer cleanup. */
export function bindSvgAxisLabels(
  svg,
  labels,
  explicit,
  { left, right, width, minFont = 12, unit = "auto", onLayout } = {},
) {
  const view = svg.ownerDocument.defaultView;
  const entries = [...svg.querySelectorAll("[data-axis-index]")];
  const values = axisDateValues(labels, explicit);
  const probe = entries[0];
  const container = svg.parentElement;
  const originalOverflow = container?.style.overflow || "";
  const originalMargin = container?.style.marginBlockEnd || "";
  const originalSvgOverflow = svg.style.overflow;

  function resize() {
    if (!probe) return;
    const actualWidth = svg.getBoundingClientRect().width || width;
    const scale = actualWidth / width;
    // SVG user units scale with the image; keep text at the reader's screen size instead.
    const rootScale =
      (parseFloat(
        view.getComputedStyle(svg.ownerDocument.documentElement).fontSize,
      ) || 16) / 16;
    const parentFont =
      parseFloat(view.getComputedStyle(svg.parentElement || svg).fontSize) ||
      14;
    const font = Math.max(minFont * rootScale, parentFont * 0.75) / scale;
    const family = view.getComputedStyle(container || svg).fontFamily;
    for (const entry of entries) {
      entry.style.fontSize = font + "px";
      entry.style.fontFamily = family;
      entry.style.fill = "var(--ag-text)";
    }
    const measure = (text) => {
      probe.textContent = text;
      const measured = probe.getComputedTextLength?.();
      return measured > 0 ? measured : Array.from(text).length * font * 0.7;
    };
    probe.style.display = "";
    const positions = entries.map((entry) => Number(entry.dataset.axisX));
    const ticks = layoutAxisTicks({
      values,
      labels,
      positions,
      left,
      right,
      measure,
      gap: 12 / scale,
      unit,
    });
    const retained = new Map(ticks.map((tick) => [tick.index, tick]));
    for (let i = 0; i < entries.length; i++) {
      const entry = entries[i],
        tick = retained.get(i);
      entry.replaceChildren();
      entry.style.display = tick ? "" : "none";
      if (!tick) continue;
      entry.setAttribute("x", tick.x);
      entry.setAttribute("text-anchor", tick.anchor);
      for (let line = 0; line < tick.lines.length; line++) {
        const span = svg.ownerDocument.createElementNS(
          svg.namespaceURI,
          "tspan",
        );
        span.setAttribute("x", line ? tick.contextX : tick.x);
        span.setAttribute("dy", line ? "1.1em" : "0");
        span.textContent = tick.lines[line];
        entry.append(span);
      }
    }
    onLayout?.(ticks);
    const svgBox = svg.getBoundingClientRect();
    let extra = 0;
    for (const entry of entries) {
      if (entry.style.display === "none") continue;
      extra = Math.max(
        extra,
        entry.getBoundingClientRect().bottom - svgBox.bottom,
      );
    }
    if (container) {
      container.style.overflow = extra > 0 ? "visible" : originalOverflow;
      container.style.marginBlockEnd =
        extra > 0 ? Math.ceil(extra + 4) + "px" : originalMargin;
      svg.style.overflow = extra > 0 ? "visible" : originalSvgOverflow;
      svg.dataset.axisExtraSpace = String(extra > 0 ? Math.ceil(extra + 4) : 0);
    }
  }
  const observer = view.ResizeObserver ? new view.ResizeObserver(resize) : null;
  observer?.observe(svg);
  const typography = view.MutationObserver
    ? new view.MutationObserver(resize)
    : null;
  typography?.observe(svg.ownerDocument.documentElement, {
    attributes: true,
    attributeFilter: ["style", "class"],
  });
  svg.ownerDocument.fonts?.addEventListener("loadingdone", resize);
  resize();
  return () => {
    observer?.disconnect();
    typography?.disconnect();
    if (container) {
      container.style.overflow = originalOverflow;
      container.style.marginBlockEnd = originalMargin;
    }
    svg.style.overflow = originalSvgOverflow;
    svg.ownerDocument.fonts?.removeEventListener("loadingdone", resize);
  };
}
