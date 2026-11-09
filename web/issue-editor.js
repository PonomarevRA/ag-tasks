/** Reveal existing optional values without changing successful form controls or their serialization. */
export function revealIssueEditorGroups(form) {
  for (const group of form.querySelectorAll('[data-editor-group]')) {
    group.open = [...group.querySelectorAll('input,select,textarea')].some(control =>
      control.type === 'checkbox' ? control.checked : Boolean(control.value.trim()));
    if (group.dataset.editorGroup === 'criteria')
      group.open = Boolean(group.querySelector('.criterion-editor'));
  }
}

/** Native validation must reveal an invalid control before the browser tries to focus it. */
export function bindIssueEditorValidation(form) {
  form.addEventListener('invalid', event => {
    let group = event.target.closest('details');
    while (group && form.contains(group)) {
      group.open = true;
      group = group.parentElement.closest('details');
    }
  }, true);
}
