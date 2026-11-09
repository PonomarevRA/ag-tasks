import { builtinMessages } from "./i18n-messages.js";
const documents = new WeakMap();
const categories = new Set(["zero", "one", "two", "few", "many", "other"]);

/** Canonicalize BCP47 tags; invalid or oversized configuration is rejected before mutation. */
function tag(value) {
  if (typeof value !== "string" || value.length > 80)
    throw new TypeError("A bounded BCP47 locale is required");
  return (
    Intl.getCanonicalLocales(value)[0] ??
    (() => {
      throw new TypeError("Locale required");
    })()
  );
}
/** Copy flat literal messages, including CLDR plural forms, without executable interpolation. */
function dictionaries(input = {}) {
  if (!input || typeof input !== "object" || Array.isArray(input))
    throw new TypeError("Message dictionaries required");
  const result = {};
  if (Object.keys(input).length > 32)
    throw new RangeError("At most32 locale dictionaries");
  for (const [locale, entries] of Object.entries(input)) {
    const canonical = tag(locale);
    if (
      !entries ||
      typeof entries !== "object" ||
      Array.isArray(entries) ||
      Object.keys(entries).length > 1000
    )
      throw new TypeError("At most1000 messages per locale");
    const copied = Object.create(null);
    for (const [key, value] of Object.entries(entries)) {
      if (!/^[A-Za-z0-9_.-]{1,160}$/.test(key))
        throw new TypeError("Stable message key required");
      if (typeof value === "string" && value.length <= 6000)
        copied[key] = value;
      else if (
        value &&
        typeof value === "object" &&
        !Array.isArray(value) &&
        typeof value.other === "string"
      ) {
        const forms = Object.create(null);
        for (const [form, message] of Object.entries(value)) {
          if (
            !categories.has(form) ||
            typeof message !== "string" ||
            message.length > 6000
          )
            throw new TypeError("Literal plural forms required");
          forms[form] = message;
        }
        copied[key] = Object.freeze(forms);
      } else throw new TypeError("Literal string or plural message required");
    }
    result[canonical] = Object.freeze(copied);
  }
  return result;
}
/** Find the closest enabled language. Disabled locales never become an implicit fallback. */
function matchLocale(requested, available) {
  if (!requested) return null;
  const canonical = tag(requested);
  if (available.includes(canonical)) return canonical;
  const language = canonical.split("-")[0];
  return available.find((item) => item.split("-")[0] === language) ?? null;
}
/** Create an independent locale context. English-only, custom dictionaries and per-root contexts are first-class. */
export function createI18n(options = {}) {
  let state,
    disposed = false;
  const listeners = new Set();
  /** Validate the entire proposed configuration before publishing a replacement. */
  function nextState(patch) {
    if (
      !patch ||
      typeof patch !== "object" ||
      Array.isArray(patch) ||
      Object.keys(patch).some(
        (key) =>
          ![
            "locale",
            "fallbackLocale",
            "availableLocales",
            "messages",
            "direction",
          ].includes(key),
      )
    )
      throw new TypeError("Unknown locale configuration field");
    const raw = patch.availableLocales ??
      state?.availableLocales ?? ["en", "ru"];
    if (!Array.isArray(raw) || !raw.length || raw.length > 32)
      throw new TypeError("Enable1–32 locales");
    const available = raw.map(tag);
    if (new Set(available).size !== available.length)
      throw new TypeError("Locale IDs must be unique");
    let fallback =
      patch.fallbackLocale !== undefined
        ? tag(patch.fallbackLocale)
        : state?.fallbackLocale;
    if (patch.fallbackLocale !== undefined && !available.includes(fallback))
      throw new TypeError("Fallback must be enabled");
    if (!available.includes(fallback))
      fallback = available.includes("en") ? "en" : available[0];
    const requested = patch.locale ?? state?.locale ?? fallback;
    const locale = matchLocale(requested, available) ?? fallback;
    const custom =
      patch.messages === undefined
        ? (state?.messages ?? {})
        : dictionaries(patch.messages);
    const direction = patch.direction ?? state?.direction ?? "auto";
    if (!["auto", "ltr", "rtl"].includes(direction))
      throw new TypeError("direction must be auto/ltr/rtl");
    return Object.freeze({
      locale,
      fallbackLocale: fallback,
      availableLocales: Object.freeze(available),
      messages: custom,
      direction,
    });
  }
  state = nextState(options);
  /** Look up the selected, base and configured fallback dictionaries; missing keys stay inspectable. */
  function value(key) {
    const locales = [
      state.locale,
      state.locale.split("-")[0],
      state.fallbackLocale,
      state.fallbackLocale.split("-")[0],
    ];
    for (const locale of locales) {
      if (Object.hasOwn(state.messages[locale] ?? {}, key))
        return state.messages[locale][key];
      if (Object.hasOwn(builtinMessages[locale] ?? {}, key))
        return builtinMessages[locale][key];
    }
    return key;
  }
  /** Format literal messages with named values. Numbers use Intl; markup remains ordinary text. */
  function t(key, params = {}) {
    let message = value(key);
    if (typeof message !== "string") {
      if (typeof params.count !== "number" || !Number.isFinite(params.count))
        throw new TypeError("Plural messages need a finite count");
      const form = new Intl.PluralRules(state.locale).select(params.count);
      message = message[form] ?? message.other;
    }
    return message.replace(/\{([A-Za-z][A-Za-z0-9_]*)\}/g, (token, name) => {
      if (!Object.hasOwn(params, name)) return token;
      const item = params[name];
      if (typeof item === "number" && Number.isFinite(item))
        return new Intl.NumberFormat(state.locale).format(item);
      if (typeof item === "string" || typeof item === "boolean")
        return String(item);
      throw new TypeError("Message values must be finite scalars");
    });
  }
  /** Atomically change enabled languages and notify independent owners without remounting their content. */
  function configure(patch = {}) {
    if (disposed) throw new Error("Locale context disposed");
    const next = nextState(patch);
    state = next;
    let firstError;
    for (const listener of [...listeners])
      try {
        listener(api);
      } catch (error) {
        firstError ??= error;
      }
    if (firstError) throw firstError;
    return api.locale;
  }
  const api = {
    t,
    configure,
    get locale() {
      return state.locale;
    },
    get fallbackLocale() {
      return state.fallbackLocale;
    },
    get availableLocales() {
      return [...state.availableLocales];
    },
    get direction() {
      return state.direction !== "auto"
        ? state.direction
        : /^(ar|fa|he|ur)(-|$)/.test(state.locale)
          ? "rtl"
          : "ltr";
    },
    setLocale(locale) {
      if (!matchLocale(locale, state.availableLocales))
        throw new TypeError("Locale is not enabled");
      return configure({ locale });
    },
    number(value, options) {
      return new Intl.NumberFormat(state.locale, options).format(value);
    },
    date(value, options) {
      return new Intl.DateTimeFormat(state.locale, options).format(value);
    },
    list(values, options) {
      return new Intl.ListFormat(state.locale, options).format(values);
    },
    subscribe(listener) {
      if (disposed || typeof listener !== "function")
        throw new TypeError("Active locale listener required");
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispose() {
      disposed = true;
      listeners.clear();
    },
  };
  return Object.freeze(api);
}

/** Resolve explicit context before a document provider; legacy unconfigured documents retain Russian compatibility. */
export function resolveI18n(document, explicit) {
  if (explicit !== undefined) {
    if (
      !explicit ||
      typeof explicit.t !== "function" ||
      typeof explicit.subscribe !== "function"
    )
      throw new TypeError("I18n context required");
    return explicit;
  }
  const existing = documents.get(document);
  if (existing) return existing;
  const locale = document?.documentElement?.lang || "ru";
  const context = createI18n({ locale, availableLocales: ["en", "ru"] });
  if (document) documents.set(document, context);
  return context;
}
/** Bind a provider to one Document. Cleanup restores only attributes this provider still owns. */
export function setDocumentI18n(document, context) {
  resolveI18n(document, context);
  const previous = documents.get(document),
    root = document.documentElement,
    lang = root.getAttribute("lang"),
    dir = root.getAttribute("dir");
  documents.set(document, context);
  const apply = () => {
    root.lang = context.locale;
    root.dir = context.direction;
  };
  apply();
  const off = context.subscribe(apply);
  return () => {
    off();
    if (documents.get(document) !== context) return;
    if (previous) documents.set(document, previous);
    else documents.delete(document);
    if (lang === null) root.removeAttribute("lang");
    else root.setAttribute("lang", lang);
    if (dir === null) root.removeAttribute("dir");
    else root.setAttribute("dir", dir);
  };
}

/** Explicit key bindings for new components. Replacing text never parses HTML or recreates user controls. */
export function createI18nBindings(context) {
  const bindings = new Set();
  let disposed = false;
  const off = context.subscribe(() => {
    for (const binding of bindings) binding();
  });
  function bind(node, key, params = {}, attribute = null) {
    if (
      ["SCRIPT", "STYLE", "IFRAME", "OBJECT", "EMBED"].includes(node.nodeName)
    )
      throw new TypeError("Text binding requires a UI node");
    if (
      attribute &&
      ![
        "title",
        "placeholder",
        "alt",
        "aria-label",
        "aria-description",
        "aria-valuetext",
        "aria-roledescription",
      ].includes(attribute)
    )
      throw new TypeError(
        "Only non-executable UI attributes may be translated",
      );
    if (disposed) throw new Error("Bindings disposed");
    const update = () => {
      const value = context.t(
        key,
        typeof params === "function" ? params() : params,
      );
      if (attribute) {
        if (node.getAttribute(attribute) !== value)
          node.setAttribute(attribute, value);
      } else if (node.textContent !== value) node.textContent = value;
    };
    bindings.add(update);
    update();
    return () => bindings.delete(update);
  }
  return {
    text: (node, key, params) => bind(node, key, params),
    attribute: (node, name, key, params) => bind(node, key, params, name),
    dispose() {
      if (disposed) return;
      disposed = true;
      off();
      bindings.clear();
    },
  };
}

export { builtinMessages } from "./i18n-messages.js";
