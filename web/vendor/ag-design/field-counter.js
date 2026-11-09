import { resolveI18n } from "./i18n.js";

const resets = new WeakMap();

/** Reject impossible native maxlength values before creating a field. */
export function validateMaxLength(maxLength) {
  if (
    maxLength !== undefined &&
    (!Number.isInteger(maxLength) || maxLength < 0 || maxLength > 2147483647)
  )
    throw new TypeError("maxLength must be a native nonnegative32-bit integer");
}

/** One reset listener per document; its WeakMap never retains detached fields. */
function registerReset(control, sync) {
  const document = control.ownerDocument;
  let state = resets.get(document);
  if (!state) {
    const fields = new WeakMap();
    const listener = (event) => {
      // The reset event precedes the browser's default value restoration.
      const controls = Array.from(event.target.elements ?? []);
      queueMicrotask(() => {
        for (const field of controls) fields.get(field)?.();
      });
    };
    state = { fields, listener, count: 0 };
    document.addEventListener("reset", listener, true);
    resets.set(document, state);
  }
  state.count++;
  state.fields.set(control, sync);
  return () => {
    state.fields.delete(control);
    if (--state.count === 0) {
      document.removeEventListener("reset", state.listener, true);
      resets.delete(document);
    }
  };
}

/**
 * Add remaining/native-UTF-16-length feedback without intercepting editing or paste.
 * Returns sync/dispose on the original label, preserving native field APIs and identity.
 * Programmatic value/maxlength assignments need sync(); normal input and form reset do not.
 */
export function attachFieldCounter(element, control, { i18n } = {}) {
  let disposed = false;
  let timer;
  let offLocale = () => {};
  let offReset = () => {};
  let counter, announcement, context;
  /** Preserve host-managed descriptions while adding/removing only this counter's reference. */
  function describe(enabled) {
    const references = (control.getAttribute("aria-describedby") ?? "")
      .split(/\s+/)
      .filter(Boolean);
    const retained = references.filter((id) => id !== counter.id);
    if (enabled) retained.push(counter.id);
    if (retained.length)
      control.setAttribute("aria-describedby", retained.join(" "));
    else control.removeAttribute("aria-describedby");
  }

  /** Refresh from the actual accepted value; never truncate host-owned data. */
  function sync() {
    if (disposed || !counter) return;
    // A new value, reset or locale must retire any older spoken count.
    announcement.textContent = "";
    const max = control.maxLength;
    counter.hidden = max < 0;
    describe(max >= 0);
    if (max < 0) {
      counter.textContent = "";
      announcement.textContent = "";
      return;
    }
    counter.lang = context.locale;
    announcement.lang = context.locale;
    const remaining = max - control.value.length;
    const key =
      remaining < 0 ? "field.characters.over" : "field.characters.remaining";
    counter.textContent = context.t(key, {
      remaining: Math.abs(remaining),
      max,
    });
    counter.dataset.state =
      remaining < 0 ? "over" : remaining === 0 ? "limit" : "available";
  }

  /** Announce after typing settles, rather than interrupting every keystroke or IME update. */
  function edited(event) {
    sync();
    clearTimeout(timer);
    if (!event.isComposing && !control.disabled)
      timer = setTimeout(() => {
        if (!disposed && control.isConnected)
          announcement.textContent = counter.hidden ? "" : counter.textContent;
      }, 700);
  }

  /** Remove owned subscriptions/timer while leaving the field and accepted value intact. */
  function dispose() {
    if (disposed) return;
    disposed = true;
    clearTimeout(timer);
    control.removeEventListener("input", edited);
    control.removeEventListener("compositionend", edited);
    control.removeEventListener("change", sync);
    control.removeEventListener("focus", sync);
    offLocale();
    offReset();
  }

  if (control.hasAttribute("maxlength")) {
    context = resolveI18n(control.ownerDocument, i18n);
    const document = control.ownerDocument;
    const footer = document.createElement("span");
    footer.className = "ag-field__feedback";
    const messages = document.createElement("span");
    messages.className = "ag-field__messages";
    for (const message of element.querySelectorAll(
      ".ag-field__hint,.ag-field__error",
    ))
      messages.append(message);
    counter = document.createElement("span");
    counter.className = "ag-field__counter";
    counter.id = `${control.id}-count`;
    announcement = document.createElement("span");
    announcement.className = "ag-text-feedback__sr";
    announcement.setAttribute("aria-live", "polite");
    announcement.setAttribute("aria-atomic", "true");
    footer.append(messages, counter);
    element.append(footer, announcement);
    sync();
    control.addEventListener("input", edited);
    control.addEventListener("compositionend", edited);
    control.addEventListener("change", sync);
    control.addEventListener("focus", sync);
    offLocale = context.subscribe(sync);
    offReset = registerReset(control, sync);
  }
  Object.assign(element, { sync, dispose });
  return element;
}
