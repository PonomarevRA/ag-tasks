import { createButton } from "./primitives.js";
import { helpPositioner } from "./inline-help-position.js";
import { bindHintBehavior } from "./hint-behavior.js";
import { defaultHoverDelayMs, validateHintDelay } from "./hint-timing.js";
let sequence = 0;

/** Compose literal explanatory text with an accessible i-button and a viewport-aware, non-modal note. */
export function createInlineHelp({
  document = globalThis.document,
  label,
  text,
  icon = "info",
  placement = "auto",
  hoverDelayMs = defaultHoverDelayMs,
  focusDelayMs = 0,
} = {}) {
  if (
    typeof label !== "string" ||
    !label.trim() ||
    typeof text !== "string" ||
    !text.trim()
  )
    throw new TypeError("Visible label and explanatory text are required");
  if (
    !["info", "question"].includes(icon) ||
    !["auto", "right", "bottom"].includes(placement)
  )
    throw new TypeError("Unknown help icon or placement");
  validateHintDelay(hoverDelayMs);
  validateHintDelay(focusDelayMs, "focusDelayMs");
  const element = document.createElement("span");
  element.className = "ag-inline-help";
  const caption = document.createElement("span");
  caption.className = "ag-inline-help__label";
  caption.textContent = bounded(label, 180);
  const panel = document.createElement("span");
  let id;
  do {
    id = "ag-inline-help-" + ++sequence;
  } while (document.getElementById(id));
  panel.id = id;
  panel.className = "ag-inline-help__panel";
  panel.setAttribute("role", "note");
  panel.hidden = true;
  const content = document.createElement("span");
  content.className = "ag-inline-help__content";
  content.textContent = bounded(text, 1200);
  panel.append(content);
  const trigger = createButton({
    document,
    label: "Пояснение: " + caption.textContent,
    icon: icon === "question" ? "?" : "i",
    iconOnly: true,
    variant: "quiet",
  });
  if (icon === "info") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    for (const [key, value] of Object.entries({
      viewBox: "0 0 20 20",
      width: "16",
      height: "16",
      fill: "none",
      stroke: "currentColor",
      "stroke-width": "2",
      "stroke-linecap": "round",
      "aria-hidden": "true",
      focusable: "false",
    }))
      svg.setAttribute(key, value);
    const path = document.createElementNS(svg.namespaceURI, "path");
    path.setAttribute("d", "M10 9v5M10 5.5v.1");
    svg.append(path);
    trigger.querySelector(".ag-button__icon").replaceChildren(svg);
  }
  trigger.removeAttribute("title");
  trigger.classList.add("ag-inline-help__trigger");
  trigger.setAttribute("aria-controls", id);
  trigger.setAttribute("aria-expanded", "false");
  element.append(caption, trigger, panel);
  const position = helpPositioner({
    document,
    element,
    trigger,
    panel,
    preferred: placement,
  });
  const behavior = bindHintBehavior({
    trigger,
    panel,
    position,
    disclosure: true,
    hoverDelayMs,
    focusDelayMs,
  });
  return {
    element,
    trigger,
    panel,
    setOpen: behavior.setOpen,
    setDelays: behavior.setDelays,
    get isOpen() {
      return behavior.isOpen;
    },
    dispose: behavior.dispose,
  };
}

/** Keep explanatory text literal and make truncation visible. */
function bounded(value, limit) {
  return value.length > limit ? value.slice(0, limit - 1) + "…" : value;
}
