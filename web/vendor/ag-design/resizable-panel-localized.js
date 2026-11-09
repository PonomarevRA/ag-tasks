import { bindResizablePanel as implementation } from "./resizable-panel.js";
import { localizeComponent } from "./i18n-component.js";
export * from "./resizable-panel.js";
/** Locale-aware public facade; application data is retained by the original controller. */
export function bindResizablePanel(options = {}) {
  return localizeComponent(implementation(options), "resizable-panel", options);
}
