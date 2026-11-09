import { createButton, createEmptyState, createNotice, createStatus } from './vendor/ag-design/index.js';
/** Create text-only UI nodes; issue titles and log evidence never become HTML. */
export function element(tag, text, className) {
  const value = document.createElement(tag);
  if (text != null) value.textContent = String(text);
  if (className) value.className = className;
  return value;
}

/** Bind one UI action and keep promise failures visible in the page status. */
export function button(text, action, className = "") {
  const value = createButton({ document, label: text, variant: className.includes("primary") ? "primary" : "secondary", onClick: async () => {
    value.disabled = true;
    try { await action(); } catch (error) { report(error); } finally { value.disabled = false; }
  } });
  value.className += ` ${className}`;
  return value;
}

/** Show a safe server/domain message using textContent. */
export function report(error) {
  const message = document.querySelector("#notice");
  message.textContent = error?.message || "Операция не выполнена";
  const dialogs = document.querySelectorAll("dialog[open]");
  const active = document.activeElement?.closest("dialog[open]") || dialogs[dialogs.length - 1];
  if (active) {
    let inline = active.querySelector(".error");
    if (!inline) {
      inline = createNotice({ document, text: "", tone: "danger" });
      inline.className += " error";
      active.append(inline);
    }
    inline.textContent = message.textContent;
  }
}

/** Allow evidence hyperlinks only after checking the HTTP scheme again at display time. */
export function evidenceLink(ref) {
  try {
    const url = new URL(ref.url);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password) return null;
    const link = element("a", ref.title || ref.url, "external");
    link.href = url.href;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    return link;
  } catch { return null; }
}

/** Keep empty lists honest while sharing spacing and typography with the other AG applications. */
export function emptyState(title, description) { return createEmptyState({ document, title, description }); }

/** Show an issue state as text as well as colour; the application owns its status vocabulary. */
export function statusLabel(label, tone = "neutral") { return createStatus({ document, label, tone }); }

let trackedFieldSequence = 0;

/**
 * Associate a persistent visible caption with an existing native control without changing its value or serialization.
 * @param {string} caption - Human field name retained while typing or after validation failure.
 * @param {Element} control - Same-document input/select/textarea owned by its existing feature.
 * @param {object} [options] - Opt into shared AG label/control tracks with sharedTracks.
 * @returns {HTMLLabelElement} Native label containing the supplied control.
 */
export function labelledField(caption, control, {sharedTracks = false} = {}) {
  const label = element('label', null, 'ag-field task-field');
  const title = element('span', caption, 'task-field-label');
  if (sharedTracks) {
    label.dataset.agFormField = '';
    title.classList.add('ag-form-track__label');
    title.id = `task-field-caption-${++trackedFieldSequence}`;
    control.classList.add('ag-form-track__control');
    if (control.tagName === 'SELECT') control.classList.add('ag-select-indicator');
    control.setAttribute('aria-labelledby', title.id);
  }
  label.append(title, control);
  return label;
}
