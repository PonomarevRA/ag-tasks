import { createWindowToolbar as implementation } from "./window-toolbar.js";
import { localizeComponent } from "./i18n-component.js";
export * from "./window-toolbar.js";
/** Public locale-aware facade; source/user data and the implementation's state remain unchanged. */
export function createWindowToolbar(options = {}) {
  return localizeComponent(implementation(options), "window-toolbar", options);
}
