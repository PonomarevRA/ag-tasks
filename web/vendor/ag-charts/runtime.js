/** Build literal, bounded content without HTML or external resource interpretation. */
export function node(document, tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = String(text).slice(0, 500);
  return element;
}
/** Validate stable identities before rendering or releasing current listeners. */
export function identities(items, max = 100) {
  if (!Array.isArray(items) || items.length > max)
    throw new RangeError("Item limit exceeded");
  const ids = new Set();
  for (const item of items) {
    if (
      !item ||
      typeof item.id !== "string" ||
      !item.id ||
      item.id.length > 80 ||
      ids.has(item.id) ||
      typeof item.label !== "string" ||
      !item.label.trim()
    )
      throw new TypeError("Unique IDs and labels required");
    ids.add(item.id);
  }
  return items;
}
/** Own a replaceable literal DOM tree with transactional validation, listeners and stable-key focus restoration. */
export function surface(document, className, initial, validate, draw) {
  const element = node(document, "section", "ag-vt " + className);
  let state,
    disposed = false,
    listeners = [];
  const listen = (target, name, handler) => {
    target.addEventListener(name, handler);
    listeners.push(() => target.removeEventListener(name, handler));
  };
  /** Validate the complete next state first; only then replace owned listeners/DOM and restore a stable focused key. */
  function update(next = {}) {
    if (disposed) return false;
    const candidate = { ...state, ...next };
    validate(candidate);
    const focus = element.contains(document.activeElement)
      ? document.activeElement.dataset.key
      : null;
    for (const remove of listeners) remove();
    listeners = [];
    state = candidate;
    element.replaceChildren(...draw(state, listen));
    if (focus)
      for (const control of element.querySelectorAll("[data-key]"))
        if (control.dataset.key === focus) {
          control.focus();
          break;
        }
    return true;
  }
  update(initial);
  return {
    element,
    update,
    get state() {
      return state;
    },
    dispose() {
      if (disposed) return;
      disposed = true;
      for (const remove of listeners) remove();
      listeners = [];
      element.remove();
    },
  };
}
/** Create a named native control; callbacks stay local and are removed by the owning surface. */
export function button(document, label, listen, onClick) {
  const control = node(document, "button", "ag-vt-button", label);
  control.type = "button";
  listen(control, "click", onClick);
  return control;
}
