import {element} from './dom.js';

const storageKey = 'ag-tasks.dashboard.blocks.v1';

/** Mount a native disclosure and persist only the addressed block's expanded state. */
export function dashboardBlock(key, title) {
  const view = document.defaultView;
  const details = element('details', null, 'dashboard-block');
  details.dataset.dashboardBlock = key;
  details.open = true;
  try {
    const stored = JSON.parse(view.localStorage.getItem(storageKey));
    if (typeof stored?.[key] === 'boolean') details.open = stored[key];
  } catch { /* Disclosure still works when local preferences are unavailable. */ }
  const summary = element('summary', title);
  const body = element('div', null, 'dashboard-block-body');
  details.append(summary, body);

  /** Merge one preference so sibling blocks and other page mounts keep their choices. */
  function save() {
    try {
      const stored = JSON.parse(view.localStorage.getItem(storageKey));
      const states = stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
      states[key] = details.open;
      view.localStorage.setItem(storageKey, JSON.stringify(states));
    } catch { /* A failed preference write must not undo the visible toggle. */ }
  }
  details.addEventListener('toggle', save);
  return {
    element:details,
    body,
    dispose() { details.removeEventListener('toggle', save); },
  };
}
