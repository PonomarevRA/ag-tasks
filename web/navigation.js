import { bindNavigation } from './vendor/ag-design/index.js';

/** Keep selected issue view explicit for screen readers; keyboard movement never activates it. */
export function selectTaskView(nav, view) {
  for (const control of nav.querySelectorAll('button[data-view]')) {
    const selected = control.dataset.view === view;
    control.classList.toggle('active', selected);
    if (selected) control.setAttribute('aria-current', 'page'); else control.removeAttribute('aria-current');
  }
}

/** Bind shared focus keys and the application's own activation; return complete listener cleanup. */
export function bindTaskNavigation(nav, onNavigate) {
  const disposeKeyboard = bindNavigation(nav, { selector: 'button[data-view]' });
  const bindings = [];
  for (const control of nav.querySelectorAll('button[data-view]')) {
    // Activate this fixed application view only after the operator clicks its control.
    const activate = () => onNavigate(control.dataset.view);
    control.addEventListener('click', activate); bindings.push([control, activate]);
  }
  return () => {
    disposeKeyboard();
    for (const [control, activate] of bindings) control.removeEventListener('click', activate);
  };
}
