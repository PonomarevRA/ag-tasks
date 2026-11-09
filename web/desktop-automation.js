const targets = {
  sidebar:'#sidebar', picker:'#space-manage', heading:'#heading',
  content:'#content', spaces:'#spaces-dialog',
};
const views = {open_board:'board', open_sessions:'sessions', open_analytics:'analytics'};

/** Describe only fixed elements, with dimensions in CSS pixels. */
function geometry() {
  const boxes = {};
  for (const [name, selector] of Object.entries(targets)) {
    const node = document.querySelector(selector);
    if (!node?.getClientRects().length) continue;
    const {x,y,width,height} = node.getBoundingClientRect();
    boxes[name] = {x,y,width,height};
  }
  return boxes;
}

/** Return a structural focus location without reading values or arbitrary identifiers. */
function focusLocation() {
  const active = document.activeElement;
  if (active?.matches('#space-manage')) return 'space_picker';
  if (active?.closest('#spaces-dialog')) return 'spaces_dialog';
  if (active?.matches('#sidebar [data-view]')) return 'navigation';
  if (active?.closest('#content')) return 'content';
  return 'other';
}

/** Bind an allowlisted, data-free bridge inside the owned desktop document. */
export function bindDesktopAutomation({getView, getSpace, navigate, isLoading}) {
  if (!document.documentElement.classList.contains('native-shell')) return;
  const epoch = crypto.randomUUID();
  let sequence = 0;
  let previous = '';

  /** Observe live state and advance the revision when its projection changes. */
  function inspect() {
    const view = getView();
    const state = {
      view,
      spaceId: getSpace(),
      viewScope: ['sessions','analytics','repositories'].includes(view) ? 'service' : 'space',
      sidebarCollapsed:document.documentElement.classList.contains('sidebar-collapsed'),
      spacesOpen:document.querySelector('#spaces-dialog').open,
      modalOpen:Boolean(document.querySelector('dialog[open]')),
      loading:isLoading() || Boolean(document.querySelector('#content [aria-busy="true"]:not([hidden])')?.getClientRects().length),
      loadFailed:!document.querySelector('#retry-view').hidden || Boolean(
        document.querySelector('#content [data-load-failed]')?.getClientRects().length),
      focus:focusLocation(),
      geometry:geometry(),
    };
    const key = JSON.stringify(state);
    if (key !== previous) { sequence++; previous = key; }
    return {status:'ok', revision:`${epoch}:${sequence}`, ...state};
  }

  /** Drive the same product actions as navigation controls and observe the result. */
  async function execute({action, revision}) {
    const before = inspect();
    if (revision !== before.revision) return {status:'error', error:'desktop_revision_changed'};
    if (isLoading()) return {status:'error', error:'desktop_view_busy'};
    if (before.modalOpen && !(before.spacesOpen && action === 'close_spaces'))
      return {status:'error', error:'desktop_modal_open'};

    const navigates = Object.hasOwn(views, action);
    if (navigates) {
      const committed = await navigate(views[action]);
      if (!committed || getView() !== views[action])
        return {status:'error', error:'desktop_navigation_superseded'};
    } else if (action === 'open_spaces') {
      const picker = document.querySelector('#space-manage');
      picker.focus();
      picker.click();
    } else if (action === 'close_spaces' && before.spacesOpen) {
      document.querySelector('[data-close="spaces-dialog"]').click();
    } else if (action === 'toggle_sidebar') {
      window.dispatchEvent(new Event('ag-tasks:toggle-sidebar'));
    } else {
      return {status:'error', error:'desktop_action_unavailable'};
    }
    const state = inspect();
    if (navigates && state.loadFailed) return {status:'error', error:'desktop_view_load_failed', state};
    return {status:'ok', state};
  }

  Object.defineProperty(window, 'agTasksDesktopAutomation', {value:Object.freeze({inspect,execute})});
}
