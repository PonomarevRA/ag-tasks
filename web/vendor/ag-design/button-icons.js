const paths = Object.freeze({
  search: "M17 17l4 4M18 10.5a7.5 7.5 0 1 1-15 0 7.5 7.5 0 0 1 15 0Z",
  "wrap-text": "M3 6h18M3 12h15a3 3 0 0 1 0 6h-4m2-2-2 2 2 2M3 18h4",
  "list-ordered": "M9 6h12M9 12h12M9 18h12M3 4h1v4M3 10h2v2H3v2h2M3 16h2v4H3m2-2H3",
  copy: "M9 5V4a1 1 0 0 1 1-1h9a1 1 0 0 1 1 1v9a1 1 0 0 1-1 1h-1M5 8h9a2 2 0 0 1 2 2v9a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-9a2 2 0 0 1 2-2Z",
  close: "M6 6l12 12M18 6 6 18",
  minimize: "M5 12h14",
  expand: "M6 18 18 6M8 6h10v10",
  restore: "M18 6 6 18M6 8v10h10",
  "arrow-up": "M12 19V5m-6 6 6-6 6 6",
  "arrow-down": "M12 5v14m-6-6 6 6 6-6",
  dot: "M20 12a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z",
  back: "M19 12H5m6-6-6 6 6 6",
  forward: "M5 12h14m-6-6 6 6-6 6",
  "panel-left": "M9 4v16M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z",
  "panel-right": "M15 4v16M5 4h14a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V5a1 1 0 0 1 1-1Z",
  refresh: "M20 5v5h-5m5 0a8 8 0 1 0 0 5",
  plus: "M12 5v14M5 12h14",
});
const aliases = Object.freeze({
  "×": "close", "−": "minimize", "↗": "expand", "↙": "restore",
  "↑": "arrow-up", "↓": "arrow-down", "●": "dot",
});

/** Render only reviewed paths on one square grid; unknown icons stay literal text in createButton. */
export function createButtonIcon(document, name) {
  const key = Object.hasOwn(aliases, name) ? aliases[name] : name;
  if (!Object.hasOwn(paths, key)) return null;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", "20");
  svg.setAttribute("height", "20");
  svg.setAttribute("fill", key === "dot" ? "currentColor" : "none");
  svg.setAttribute("stroke", key === "dot" ? "none" : "currentColor");
  svg.setAttribute("stroke-width", "1.75");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.setAttribute("focusable", "false");
  const path = document.createElementNS(svg.namespaceURI, "path");
  path.setAttribute("d", paths[key]);
  svg.append(path);
  return svg;
}
