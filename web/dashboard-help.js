import {createInlineHelp} from './vendor/ag-design/inline-help.js';

/** Reuse AG's viewport-aware i-disclosure, with Russian host labels and owned disposal. */
export function dashboardHelp(label, text) {
  const help = createInlineHelp({document, label, text});
  help.element.classList.add('dashboard-help');
  help.panel.classList.add('dashboard-help-panel');
  // WebKit can activate buttons without focusing them. Keep Escape inside AG's
  // trigger/panel scope even for native Accessibility and non-focusing clicks.
  const focusTrigger = () => help.trigger.focus({preventScroll:true});
  help.trigger.addEventListener('click', focusTrigger, true);
  const dispose = help.dispose;
  help.dispose = () => {
    help.trigger.removeEventListener('click', focusTrigger, true);
    dispose();
  };
  return help;
}
