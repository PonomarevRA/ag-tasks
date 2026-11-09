import { resolveI18n as popupLocale } from "./i18n.js";
import { applyControlGeometry } from "./geometry.js";
import { attachFieldCounter, validateMaxLength } from "./field-counter.js";

import { createButtonIcon } from "./button-icons.js";

const variants = new Set(["primary", "secondary", "quiet", "danger"]);
const tones = new Set(["neutral", "accent", "success", "danger"]);

/** Create a native button with reviewed SVG icons or literal text; supplied markup never executes. */
export function createButton({
  document = globalThis.document,
  label,
  variant = "secondary",
  icon,
  iconOnly = false,
  disabled = false,
  onClick,
  density = "compact",
  shape,
  parentRadius,
  inset = 0,
} = {}) {
  if (typeof label !== "string" || !label.trim())
    throw new TypeError("Button label is required");
  if (!variants.has(variant)) throw new TypeError("Unknown button variant");
  const button = document.createElement("button");
  button.type = "button";
  button.className = `ag-button ag-button--${variant}`;
  button.disabled = disabled;
  applyControlGeometry(button, { density, shape, parentRadius, inset });
  if (icon) {
    const symbol = document.createElement("span");
    symbol.className = "ag-button__icon";
    const graphic = createButtonIcon(document, icon);
    if (graphic) symbol.append(graphic);
    else symbol.textContent = icon;
    symbol.setAttribute("aria-hidden", "true");
    button.append(symbol);
  }
  if (iconOnly) {
    button.setAttribute("aria-label", label);
    button.title = label;
  }
  if (iconOnly && icon) {
    button.classList.add("ag-icon-button");
    button.style.minWidth = button.style.minHeight;
  } else {
    const text = document.createElement("span");
    text.className = "ag-nav__label";
    text.textContent = label;
    button.append(text);
  }
  if (onClick) button.addEventListener("click", onClick);
  return button;
}

/** Create a labelled native field; maxLength adds remaining feedback. Sync after value assignment; dispose on removal. */
export function createField({
  document = globalThis.document,
  id,
  label,
  placeholder = "",
  hint = "",
  type = "text",
  value = "",
  maxLength,
  disabled = false,
  i18n,
  density = "compact",
  readOnly = false,
  required = false,
  error = "",
} = {}) {
  if (!id || !label) throw new TypeError("Field id and label are required");
  validateMaxLength(maxLength);
  if (typeof value !== "string")
    throw new TypeError("Field value must be literal text");
  if (
    maxLength !== undefined &&
    !["text", "search", "url", "tel", "email", "password"].includes(type)
  )
    throw new TypeError("maxLength requires a native text input type");
  const wrapper = document.createElement("label");
  wrapper.className = "ag-field";
  wrapper.htmlFor = id;
  const caption = document.createElement("span");
  caption.className = "ag-field__label";
  caption.textContent = label;
  caption.id = `${id}-label`;
  const input = document.createElement("input");
  input.id = id;
  input.name = id;
  input.type = type;
  input.placeholder = placeholder;
  input.value = value;
  input.defaultValue = value;
  input.disabled = disabled;
  if (maxLength !== undefined) input.maxLength = maxLength;
  input.readOnly = readOnly;
  input.required = required;
  input.setAttribute("aria-labelledby", caption.id);
  applyControlGeometry(input, { density });
  wrapper.append(caption, input);
  if (hint) {
    const help = document.createElement("span");
    help.className = "ag-field__hint";
    help.id = `${id}-hint`;
    help.textContent = hint;
    input.setAttribute("aria-describedby", help.id);
    wrapper.append(help);
  }
  if (error) {
    const message = document.createElement("span");
    message.id = `${id}-error`;
    message.className = "ag-field__error";
    message.textContent = error;
    input.setAttribute("aria-invalid", "true");
    input.setAttribute(
      "aria-describedby",
      [input.getAttribute("aria-describedby"), message.id]
        .filter(Boolean)
        .join(" "),
    );
    wrapper.append(message);
  }
  return attachFieldCounter(wrapper, input, { i18n });
}

/** A status always includes a readable label; colour is supplementary. */
export function createStatus({
  document = globalThis.document,
  label,
  tone = "neutral",
} = {}) {
  if (!tones.has(tone)) throw new TypeError("Unknown status tone");
  const status = document.createElement("span");
  status.className = "ag-status";
  status.dataset.tone = tone;
  status.textContent = label;
  return status;
}

/** Create an honest empty state with an optional existing action element. */
export function createEmptyState({
  document = globalThis.document,
  title,
  description = "",
  action,
} = {}) {
  const section = document.createElement("section");
  section.className = "ag-empty";
  const heading = document.createElement("h2");
  heading.textContent = title;
  section.append(heading);
  if (description) {
    const text = document.createElement("p");
    text.textContent = description;
    section.append(text);
  }
  if (action) section.append(action);
  return section;
}

/** Create a contextual notice; only errors interrupt screen-reader announcements. */
export function createNotice({
  document = globalThis.document,
  text,
  tone = "neutral",
} = {}) {
  if (!tones.has(tone)) throw new TypeError("Unknown notice tone");
  const notice = document.createElement("p");
  notice.className = "ag-notice";
  notice.dataset.tone = tone;
  notice.setAttribute("role", tone === "danger" ? "alert" : "status");
  notice.textContent = text;
  return notice;
}

/** Add optional arrow/Home/End navigation without changing ordinary Tab, activation, or selection semantics. */
export function bindNavigation(nav, { selector = "button" } = {}) {
  const keydown = (event) => {
    if (
      event.defaultPrevented ||
      event.altKey ||
      event.metaKey ||
      event.ctrlKey ||
      event.shiftKey
    )
      return;
    if (!["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
    const buttons = [...nav.querySelectorAll(selector)].filter(
      (button) =>
        !button.disabled &&
        !button.hidden &&
        !button.closest("[hidden],[inert]"),
    );
    const index = buttons.indexOf(nav.ownerDocument.activeElement);
    if (index < 0) return;
    event.preventDefault();
    let next = index + (event.key === "ArrowUp" ? -1 : 1);
    if (event.key === "Home") next = 0;
    if (event.key === "End") next = buttons.length - 1;
    // Let the browser reveal the focused row in a short, scrollable sidebar.
    buttons[(next + buttons.length) % buttons.length].focus();
  };
  nav.addEventListener("keydown", keydown);
  return () => nav.removeEventListener("keydown", keydown);
}

/** Collapsing hides labels only, keeps the leading rail and restores focus before hiding a label subtree. */
export function createSidebarController({
  panel,
  toggle,
  storageKey,
  storage,
  onChange = () => {},
}) {
  let collapsed = panel.dataset.collapsed === "true";
  try {
    if (storageKey)
      collapsed =
        (storage ?? panel.ownerDocument.defaultView.localStorage).getItem(
          storageKey,
        ) === "true";
  } catch {
    /* Navigation also works without storage. */
  }
  const apply = () => {
    panel.dataset.collapsed = String(collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    toggle.setAttribute("aria-controls", panel.id);
    onChange(collapsed);
  };
  const setCollapsed = (value) => {
    collapsed = Boolean(value);
    apply();
    try {
      if (storageKey)
        (storage ?? panel.ownerDocument.defaultView.localStorage).setItem(
          storageKey,
          String(collapsed),
        );
    } catch {
      /* Persistence is optional. */
    }
  };
  const click = () => setCollapsed(!collapsed);
  toggle.addEventListener("click", click);
  apply();
  return {
    setCollapsed,
    dispose: () => toggle.removeEventListener("click", click),
  };
}

/** Create a native dialog with a persistent titlebar and a separately scrollable content region; the supplied node retains its listeners. */
export function createPopup({
  document = globalThis.document,
  id,
  title,
  content,
} = {}) {
  if (!id || !title) throw new TypeError("Popup id and title are required");
  const dialog = document.createElement("dialog");
  dialog.className = "ag-popup";
  dialog.id = id;
  const header = document.createElement("div");
  header.className = "ag-popup__header";
  const heading = document.createElement("h2");
  heading.id = `${id}-title`;
  heading.textContent = title;
  dialog.setAttribute("aria-labelledby", heading.id);
  const close = createButton({
    document,
    label: popupLocale(document).t("common.close"),
    icon: "×",
    iconOnly: true,
    variant: "quiet",
    onClick: () => dialog.close(),
  });
  header.append(heading, close);
  const body = document.createElement("div");
  body.className = "ag-popup__body";
  body.tabIndex = 0;
  body.setAttribute("role", "region");
  body.setAttribute("aria-label", popupLocale(document).t("window.content"));
  if (content) body.append(content);
  dialog.append(header, body);
  return dialog;
}
