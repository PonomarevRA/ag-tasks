import { createWindowToolbar } from "./window-toolbar-localized.js";
import { bindResizablePanel } from "./resizable-panel-localized.js";

/** Adapt actual product controls to one header; fixed host configuration owns selectors, commands and layout variables. */
export function bindWindowShell({
  document = globalThis.document,
  identity = "",
  header,
  titleSource,
  detailTitleSource,
  detailSelector = "dialog[open]",
  navigation,
  actions = [],
  panels = [],
  existingBack,
  existingForward,
  notifyNative = () => {},
} = {}) {
  const view = document.defaultView,
    target =
      typeof header === "string" ? document.querySelector(header) : header;
  if (!target) return { dispose() {}, refresh() {} };
  const find = (selector) =>
    selector ? document.querySelector(selector) : null;
  const moved = [],
    disposers = [];
  const hasAnimationFrame = typeof view.requestAnimationFrame === "function";
  const requestFrame = hasAnimationFrame
    ? view.requestAnimationFrame.bind(view)
    : (callback) => view.setTimeout(callback, 16);
  const cancelFrame = hasAnimationFrame
    ? view.cancelAnimationFrame.bind(view)
    : view.clearTimeout.bind(view);
  let frame = 0,
    disposed = false,
    signature = "",
    lastOpener = null,
    restoring = null;
  const stack = [];
  let index = -1;
  const toolbar = createWindowToolbar({
    document,
    identity,
    onBack: () => step(-1),
    onForward: () => step(1),
  });
  const back = toolbar.element.querySelector('[data-ag-window-command="back"]'),
    forward = toolbar.element.querySelector('[data-ag-window-command="forward"]');
  back.dataset.agWindowCommand = "back";
  forward.dataset.agWindowCommand = "forward";
  const commandHost = toolbar.element.querySelector(
    ".ag-window-toolbar__actions",
  );
  for (const action of actions) {
    const element = find(action.selector);
    if (!element) continue;
    moved.push([element, element.parentNode, element.nextSibling]);
    element.dataset.agWindowCommand = action.id;
    commandHost.append(element);
  }
  // Existing product identity/project selection stays actionable; only duplicate text is owned by the host adapter.
  const identityControl = target.querySelector("a.brand,a.workspace-brand");
  if (identityControl) {
    moved.push([
      identityControl,
      identityControl.parentNode,
      identityControl.nextSibling,
    ]);
    toolbar.element
      .querySelector(".ag-window-toolbar__navigation")
      .append(identityControl);
  }
  target.append(toolbar.element);
  target.classList.add("ag-window-shell-host");
  for (const configuration of panels) {
    const panel = find(configuration.selector);
    if (!panel) continue;
    const controller = bindResizablePanel({
      panel,
      edge: configuration.edge,
      minWidth: configuration.minWidth ?? 180,
      maxWidth: configuration.maxWidth ?? 580,
      initialWidth: panel.getBoundingClientRect().width || 220,
      storageKey: identity + ":" + configuration.selector,
      storage: configuration.storage,
      reservedWidth: () =>
        panels
          .filter((other) => other.selector !== configuration.selector)
          .reduce(
            (sum, other) =>
              sum + (find(other.selector)?.getBoundingClientRect().width || 0),
            configuration.chromeWidth ?? 0,
          ),
      onResize: (width) => {
        const layout = find(configuration.layout) || document.documentElement;
        if (configuration.variable)
          layout.style.setProperty(configuration.variable, width + "px");
        configuration.onResize?.(width);
        schedule();
      },
    });
    disposers.push(controller.dispose);
  }
  /** Capture actual selected DOM context; restore functions click existing application controls, never infer dependencies or URLs. */
  function context() {
    const dialog = find(detailSelector),
      titleElement = dialog
        ? detailTitleSource
          ? dialog.querySelector(detailTitleSource)
          : dialog.querySelector("h1,h2")
        : find(titleSource);
    const active = find(navigation),
      title =
        (titleElement?.textContent || active?.textContent || identity)
          .trim()
          .slice(0, 256) || identity;
    const destination = dialog ? lastOpener : active;
    const issue = destination?.closest?.("[data-issue-id]")?.dataset.issueId;
    const targetKey =
      active?.dataset.view ||
      active?.dataset.page ||
      active?.getAttribute("href") ||
      view.location.pathname;
    const key = dialog ? "detail:" + title : "view:" + targetKey;
    const restore = () => {
      const current = find(detailSelector);
      if (current) current.close?.();
      if (dialog && issue) {
        const card = [...document.querySelectorAll("[data-issue-id]")].find(
          (item) => item.dataset.issueId === issue,
        );
        if (card) {
          card.click();
          return;
        }
      }
      if (destination?.isConnected) destination.click();
    };
    return { key, title, restore };
  }
  /** Use the product's map/browser history when supplied, otherwise retain only actually visited restorable view contexts. */
  function step(direction) {
    const original = find(direction < 0 ? existingBack : existingForward);
    if (original) {
      if (!original.disabled) original.click();
      return;
    }
    const next = index + direction;
    if (next < 0 || next >= stack.length) return;
    index = next;
    restoring = stack[index].key;
    stack[index].restore();
    schedule();
  }
  /** Coalesce title, disabled-state and panel changes; no polling or source execution is performed. */
  function refresh() {
    if (disposed) return;
    const current = context();
    if (current.key === restoring) {
      restoring = null;
      stack[index] = current;
    } else if (!restoring && stack[index]?.key !== current.key) {
      stack.splice(index + 1);
      stack.push(current);
      if (stack.length > 100) stack.shift();
      index = stack.length - 1;
    } else if (stack[index]?.key === current.key) stack[index] = current;
    const originalBack = find(existingBack),
      originalForward = find(existingForward);
    const state = {
      title: current.title,
      canBack: originalBack ? !originalBack.disabled : index > 0,
      canForward: originalForward
        ? !originalForward.disabled
        : index < stack.length - 1,
    };
    const next = JSON.stringify({
      ...state,
      // Panel drags can change native alignment without changing title or command state.
      panelWidths: panels.map((configuration) =>
        Math.round(
          find(configuration.selector)?.getBoundingClientRect().width || 0,
        ),
      ),
      commands: [...commandHost.children].map((item) => [
        item.dataset.agWindowCommand,
        item.disabled === true,
        item.hidden === true,
      ]),
    });
    if (next === signature) return;
    signature = next;
    toolbar.element.querySelector(".ag-window-toolbar__title").textContent =
      current.title;
    back.disabled = !state.canBack;
    forward.disabled = !state.canForward;
    document.documentElement.dataset.agWindowTitle = current.title;
    document.title = current.title + " · " + identity;
    notifyNative();
    try {
      const api = view.pywebview?.api;
      if (api?.standalone_sidebar_changed)
        Promise.resolve(api.standalone_sidebar_changed()).catch(() => {});
      else view.webkit?.messageHandlers?.agWindowChanged?.postMessage(null);
    } catch {
      /* The browser fallback is sufficient. */
    }
  }
  function schedule() {
    if (disposed || frame) return;
    frame = requestFrame(() => {
      frame = 0;
      refresh();
    });
  }
  const click = (event) => {
    if (
      !toolbar.element.contains(event.target) &&
      !event.target.closest?.("dialog")
    )
      lastOpener =
        event.target.closest?.("button,a,[data-issue-id]") || lastOpener;
  };
  document.addEventListener("click", click, true);
  const observer = new view.MutationObserver(schedule);
  observer.observe(document.body, {
    subtree: true,
    childList: true,
    characterData: true,
    attributes: true,
    attributeFilter: ["aria-current", "open", "disabled", "hidden", "class"],
  });
  view.addEventListener("popstate", schedule);
  view.addEventListener("pywebviewready", schedule);
  refresh();
  return {
    refresh,
    dispose() {
      if (disposed) return;
      disposed = true;
      if (frame) cancelFrame(frame);
      observer.disconnect();
      document.removeEventListener("click", click, true);
      view.removeEventListener("popstate", schedule);
      view.removeEventListener("pywebviewready", schedule);
      for (const dispose of disposers) dispose();
      for (const [element, parent, next] of moved) {
        if (parent?.isConnected)
          parent.insertBefore(
            element,
            next?.parentNode === parent ? next : null,
          );
        delete element.dataset.agWindowCommand;
      }
      toolbar.dispose();
      target.classList.remove("ag-window-shell-host");
      delete document.documentElement.dataset.agWindowTitle;
    },
  };
}
