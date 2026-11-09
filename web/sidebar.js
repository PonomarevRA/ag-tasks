/** Persistent icon rail shared by the browser button, keyboard shortcut and native toolbar command. */
export function bindSidebar(documentRef = document, windowRef = window) {
  const root = documentRef.documentElement;
  const toggle = documentRef.querySelector("#sidebar-toggle");
  const key = "ag-tasks.sidebar-collapsed";
  let timer;

  /** Notify only presentation changes; native code pulls and validates its own fixed layout fields. */
  function notifyNative() {
    if (timer) return;
    timer = windowRef.setTimeout(() => {
      timer = null;
      try {
        const callback = windowRef.pywebview?.api?.standalone_sidebar_changed;
        if (callback) Promise.resolve(callback()).catch(() => {});
      } catch { /* Native presentation is optional; browser controls remain usable. */ }
    }, 150);
  }

  /** Render accessible state first; denied local storage must never disable navigation. */
  function apply(collapsed) {
    root.classList.toggle("sidebar-collapsed", collapsed);
    toggle.setAttribute("aria-expanded", String(!collapsed));
    const label = `${collapsed ? "Развернуть" : "Свернуть"} меню`;
    toggle.setAttribute("aria-label", label); toggle.title = `${label} (⌘⇧S / Ctrl+Shift+S)`;
    try { windowRef.localStorage.setItem(key, collapsed ? "1" : "0"); } catch { /* Ephemeral state still works. */ }
    notifyNative();
  }

  /** Toggle only the sidebar label width; keep every navigation action available. */
  function change() { apply(!root.classList.contains("sidebar-collapsed")); }
  toggle.addEventListener("click", change);
  windowRef.addEventListener("ag-tasks:toggle-sidebar", change);
  windowRef.addEventListener("pywebviewready", notifyNative);
  let observer;
  if (windowRef.ResizeObserver) {
    observer = new windowRef.ResizeObserver(notifyNative);
    observer.observe(documentRef.querySelector("#sidebar"));
  }
  windowRef.addEventListener("pagehide", () => {
    if (timer) windowRef.clearTimeout(timer);
    timer = null; observer?.disconnect();
  });
  windowRef.addEventListener("keydown", event => {
    if (root.classList.contains("native-toolbar") || event.repeat || event.altKey) return;
    const commandModifier = event.metaKey || event.ctrlKey;
    if (!commandModifier || !event.shiftKey) return;
    const sidebarKey = event.code ? event.code === "KeyS" : event.key?.toLowerCase() === "s";
    if (sidebarKey) {
      event.preventDefault(); change();
    }
  });
  let collapsed = false;
  try { collapsed = windowRef.localStorage.getItem(key) === "1"; } catch { /* Browser privacy mode keeps the default. */ }
  apply(collapsed);
}
