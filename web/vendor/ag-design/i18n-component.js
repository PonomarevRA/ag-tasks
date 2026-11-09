import { createI18n, resolveI18n } from "./i18n.js";
import { builtinMessages } from "./i18n-messages.js";

// This allowlist covers owned chrome only. Source text, Markdown, row cells and caller content are excluded.
const selectors = {
  breadcrumbs: [".ag-breadcrumbs"],
  skeleton: [".ag-skeleton__status"],
  "render-window": [".ag-render-window"],
  "resizable-panel": [".ag-panel-resizer"],
  "issue-summary": [".ag-issue-summary summary"],

  "application-shell": [
    ".skip",
    ".title-context",
    ".title-actions button",
    "#sidebar nav",
    "#sidebar [data-view]",
    "#sidebar .ag-nav__label",
    ".footer-label",
    "#connection",
    "#heading",
    "#description",
    "#catalog-tools .ag-field__label",
    "#catalog-tools summary",
    "#catalog-tools option",
    "#search",
    "#sidebar-toggle",
  ],
  "style-lab-shell": [
    ".lab-heading p",
    ".lab-heading h2",
    ".lab-marker",
    ".lab-styles",
    ".lab-status",
    ".lab-footer button .ag-nav__label",
    ".ag-tabs__list",
    ".ag-tabs__tab",
    ".lab-form .ag-field__label",
    ".lab-form .ag-field__hint",
    ".lab-form input",
    ".lab-form .ag-select>label",
    ".lab-form .ag-select option",
    ".lab-form .ag-button .ag-nav__label",
    ".lab-form .ag-status",
    ".lab-form .ag-progress label",
    ".lab-form .ag-progress__description",
    ".lab-table caption",
    ".lab-table th",
    ".lab-table th button",
  ],
  "loading-indicator": [".ag-skeleton__status"],

  "code-viewer": [
    ".ag-code-viewer",
    ".ag-code-viewer__title",
    ".ag-code-viewer__toolbar",
    ".ag-code-viewer__action",
    ".ag-code-viewer__status",
    ".ag-code-viewer .cm-content",
  ],
  "markdown-viewer": [
    ".ag-code-viewer .cm-content",
    ".ag-markdown-viewer__toolbar",
    ".ag-markdown-viewer__toolbar button",
    ".ag-markdown-viewer__status",
    ".ag-markdown-viewer",
  ],
  "markdown-editor": [
    ".ag-code-viewer .cm-content",
    ".ag-markdown-editor__toolbar",
    ".ag-markdown-editor__toolbar button",
    ".ag-markdown-editor__status",
    ".ag-markdown-editor__preview-status",
    ".ag-markdown-editor__file-choice p",
    ".ag-markdown-editor__file-choice button",
    ".ag-code-viewer",
    ".ag-code-viewer__title",
    ".ag-code-viewer__toolbar",
    ".ag-code-viewer__action",
    ".ag-code-viewer__status",
    ".ag-markdown-viewer__toolbar",
    ".ag-markdown-viewer__toolbar button",
    ".ag-markdown-viewer__status",
    ".ag-markdown-viewer",
  ],
  history: [
    ".ag-history",
    ".ag-history__title",
    ".ag-history__search > span",
    ".ag-history__search input",
    ".ag-history__toolbar .ag-select > label",
    ".ag-history__toolbar option[value=all]",
    ".ag-history__legend",
    ".ag-history__details",
    ".ag-history__eyebrow",
    ".ag-history__action",
    ".ag-history__boundary",
    ".ag-history__empty",
    ".ag-history__count",
  ],
  "data-table": [
    ".ag-data-table__pager",
    ".ag-data-table__pager button",
    ".ag-data-table__pager span",
    ".ag-data-table__status",
    ".ag-data-table__empty",
    ".ag-data-table__sort",
    ".ag-data-table input[type=checkbox]",
  ],
  "data-grid": [
    ".ag-data-grid__toolbar",
    ".ag-data-grid__toolbar button",
    ".ag-data-grid__toolbar label",
    ".ag-data-grid__toolbar summary",
    ".ag-data-grid__toolbar option",
    ".ag-data-grid__pager",
    ".ag-data-grid__pager label",
    ".ag-data-grid__pager button",
    ".ag-data-grid__pager span",
    ".ag-data-grid__status",
    ".ag-data-grid__status button",
    ".ag-data-grid__button",
    ".ag-data-grid__viewport",
  ],
  "dropdown-select": [
    ".ag-dropdown-select__toggle",
    ".ag-dropdown-select__input",
    ".ag-dropdown-select__empty",
  ],
  progress: [".ag-progress__value"],
  "window-toolbar": [
    ".ag-window-toolbar",
    ".ag-window-toolbar__navigation button",
    ".ag-window-toolbar__navigation .ag-nav__label",
    ".ag-window-toolbar__actions button",
  ],
  "agent-conversation": [
    ".ag-conversation__viewport",
    ".ag-conversation__empty",
    ".ag-conversation__notice",
    ".ag-conversation__footer button",
    ".ag-conversation__composer label",
    ".ag-conversation__composer textarea",
    ".ag-conversation__composer button",
    ".ag-conversation__actions button",
    ".ag-conversation__muted",
  ],
  "agent-activity-chat": [
    ".ag-agent-chat__control",
    ".ag-agent-chat__control .ag-nav__label",
    ".ag-agent-chat__markers",
    ".ag-agent-chat__empty",
    ".ag-agent-chat__footer span",
    ".ag-agent-chat__log",
    ".ag-agent-chat__options > button",
  ],
  tree: [".ag-tree button[aria-expanded]"],
  "settings-panel": [".ag-settings-panel nav"],
  "inline-help": [".ag-inline-help__trigger"],
  "numeric-badge": [".ag-numeric-badge"],
  "card-connections": [".ag-card-connections__list"],
  "ambient-chart": [
    ".ag-ambient-chart__tag",
    ".ag-ambient-chart__empty",
    ".ag-ambient-chart__controls button",
    ".ag-ambient-chart__controls .ag-nav__label",
  ],
  "board-viewport": [
    ".ag-board__controls",
    ".ag-board__controls button",
    ".ag-board__preferences label",
    ".ag-board__preferences select",
    ".ag-board__preferences option",
    ".ag-board__settings summary",
    ".ag-board__help summary",
    ".ag-board__help-text",
  ],
};
const attributes = ["aria-label", "title", "placeholder"];
const patterns = [];
/** Escape literal dictionary text; only named placeholders become bounded captures. */
function escaped(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}
for (const [key, message] of Object.entries(builtinMessages.ru)) {
  if (typeof message !== "string") continue;
  const names = [];
  let source = "",
    offset = 0;
  for (const token of message.matchAll(/\{([A-Za-z][A-Za-z0-9_]*)\}/g)) {
    source += escaped(message.slice(offset, token.index)) + "([\\s\\S]*?)";
    names.push(token[1]);
    offset = token.index + token[0].length;
  }
  source += escaped(message.slice(offset));
  patterns.push({ key, message, names, regex: new RegExp("^" + source + "$") });
}
patterns.sort(
  (a, b) =>
    b.message.replace(/\{[^}]*\}/g, "").length -
    a.message.replace(/\{[^}]*\}/g, "").length,
);
/** Translate one explicitly owned source message. Host values captured by placeholders stay literal. */
export function translateBuiltin(
  context,
  source,
  protectedStrings = new Set(),
) {
  if (source.length > 12000 || protectedStrings.has(source)) return source;
  for (const pattern of patterns) {
    const match = pattern.regex.exec(source);
    if (!match) continue;
    const params = {};
    for (let i = 0; i < pattern.names.length; i++) {
      const name = pattern.names[i],
        raw = match[i + 1];
      params[name] =
        ["count", "total"].includes(name) && /^\d{1,12}$/.test(raw)
          ? Number(raw)
          : raw;
      if (
        name === "name" &&
        pattern.key.startsWith("md.") &&
        !protectedStrings.has(raw)
      )
        params[name] = translateBuiltin(context, raw, protectedStrings);
    }
    return context.t(pattern.key, params);
  }
  return source;
}
/** Attach live localization to a legacy component's declared chrome; never inspect arbitrary document content. */
export function localizeComponent(handle, kind, options = {}) {
  const element =
    options.localizationRoot ??
    handle.element ??
    handle.root ??
    handle.handle ??
    options.element;
  if (!element) return handle;
  const rules = selectors[kind] ?? [];
  if (!rules.length) return handle;
  const doc = element.ownerDocument;
  const ownContext = options.locale !== undefined && !options.i18n;
  const localeContext = ownContext
    ? createI18n({ locale: options.locale })
    : resolveI18n(doc, options.i18n);
  const originalLanguage = element.getAttribute("lang"),
    originalDirection = element.getAttribute("dir");
  const protectedStrings = new Set();
  let currentOptions = {};
  for (const key of [
    "label",
    "title",
    "placeholder",
    "searchPlaceholder",
    "emptyText",
    "hint",
    "message",
    "description",
    "status",
    "filename",
    "labels",
    "columns",
    "actions",
    "bulkActions",
  ])
    if (options[key] !== undefined) currentOptions[key] = options[key];
  /** Track caller-owned labels after option updates so matching built-in words stay literal. */
  function rememberLabels(patch) {
    currentOptions = { ...currentOptions, ...patch };
    protectedStrings.clear();
    for (const key of [
      "label",
      "title",
      "placeholder",
      "searchPlaceholder",
      "emptyText",
      "hint",
      "message",
      "description",
      "status",
      "filename",
    ])
      if (typeof currentOptions[key] === "string")
        protectedStrings.add(currentOptions[key]);
    for (const value of Object.values(currentOptions.labels ?? {}))
      if (typeof value === "string") protectedStrings.add(value);
    for (const action of [
      ...(currentOptions.actions ?? []),
      ...(currentOptions.bulkActions ?? []),
    ])
      if (typeof action.label === "string") protectedStrings.add(action.label);
    for (const item of currentOptions.columns ?? [])
      if (typeof item.label === "string") protectedStrings.add(item.label);
  }

  rememberLabels({});
  let disposed = false,
    queued = false,
    applying = false;
  const original = new WeakMap();
  /** Retain source text per target and field; renderer updates replace that source without guessing a reverse translation. */
  function update(target, field, value, write) {
    let fields = original.get(target);
    if (!fields) {
      fields = new Map();
      original.set(target, fields);
    }
    let record = fields.get(field);
    if (!record || record.rendered !== value) {
      record = { source: value, rendered: value };
      fields.set(field, record);
    }
    const next = translateBuiltin(
      localeContext,
      record.source,
      protectedStrings,
    );
    record.rendered = next;
    if (next !== value) write(next);
  }
  /** Update existing text/attributes in place so focus, editor models, files and task state remain owned by the component. */
  function refresh() {
    if (disposed || applying) return;
    applying = true;
    try {
      handle.refreshLocaleContent?.(localeContext);
      element.lang = localeContext.locale;
      // Source code keeps its own direction; the surrounding chrome follows the interface locale.
      element.dir = localeContext.direction;
      const targets = new Set();
      for (const selector of rules) {
        if (element.matches(selector)) targets.add(element);
        for (const node of element.querySelectorAll(selector))
          targets.add(node);
      }
      for (const node of targets) {
        for (const attr of attributes) {
          const value = node.getAttribute(attr);
          if (value !== null)
            update(node, attr, value, (next) => node.setAttribute(attr, next));
        }
        if (node.matches(".cm-content")) continue;
        for (const child of node.childNodes)
          if (child.nodeType === 3)
            update(child, "text", child.nodeValue, (next) => {
              child.nodeValue = next;
            });
      }
    } finally {
      applying = false;
    }
  }
  const off = localeContext.subscribe(refresh);
  const Observer = doc.defaultView?.MutationObserver;
  const observer = Observer
    ? new Observer(() => {
        if (disposed || queued) return;
        queued = true;
        queueMicrotask(() => {
          queued = false;
          refresh();
        });
      })
    : null;
  observer?.observe(element, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: attributes,
  });
  const oldDispose = handle.dispose;
  /** Release the provider subscription and observer before the implementation tears down child resources. */
  function dispose() {
    if (disposed) return;
    disposed = true;
    off();
    observer?.disconnect();
    if (element.getAttribute("lang") === localeContext.locale) {
      if (originalLanguage === null) element.removeAttribute("lang");
      else element.setAttribute("lang", originalLanguage);
    }
    if (element.getAttribute("dir") === localeContext.direction) {
      if (originalDirection === null) element.removeAttribute("dir");
      else element.setAttribute("dir", originalDirection);
    }
    currentOptions = {};
    protectedStrings.clear();
    if (ownContext) localeContext.dispose();
    oldDispose?.call(handle);
  }
  refresh();
  // Public methods also refresh synchronously. Async renderer changes are handled by the bounded observer.
  const result = {};
  for (const key of Reflect.ownKeys(handle)) {
    const descriptor = Object.getOwnPropertyDescriptor(handle, key);
    if (key === "dispose") continue;
    if (typeof descriptor.value === "function") {
      const method = descriptor.value;
      Object.defineProperty(result, key, {
        ...descriptor,
        value(...args) {
          const value = method.apply(handle, args);
          if (
            ["setOptions", "update", "setContext"].includes(key) &&
            args[0] &&
            typeof args[0] === "object" &&
            !Array.isArray(args[0])
          )
            rememberLabels(args[0]);
          if (key === "setColumns") rememberLabels({ columns: args[0] });
          refresh();
          return value;
        },
      });
    } else Object.defineProperty(result, key, descriptor);
  }
  result.dispose = dispose;
  result.refreshLocale = refresh;
  return result;
}
