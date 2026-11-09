let serial = 0;

/** Fixed controls use host tokens; native inputs remain keyboard operable and never join the transformed scene. */
export function createBoardControls(document, actions) {
  const element = document.createElement('div');
  element.className = 'ag-board__controls';
  element.setAttribute('role', 'group');
  element.setAttribute('aria-label', 'Управление доской');
  const listeners = [];
  const bind = (node, event, callback) => {
    node.addEventListener(event, callback);
    listeners.push(() => node.removeEventListener(event, callback));
  };
  const button = (label, text, callback) => {
    const node = document.createElement('button');
    node.type = 'button'; node.className = 'ag-button ag-button--quiet'; node.textContent = text;
    node.title = label; node.setAttribute('aria-label', label);
    bind(node, 'click', callback); element.append(node); return node;
  };
  button('Уменьшить масштаб (−)', '−', () => actions.zoom(1 / 1.2));
  const reset = button('Масштаб 100% (1)', '100%', actions.reset);
  button('Увеличить масштаб (+)', '+', () => actions.zoom(1.2));
  button('Вместить граф (0)', 'Вместить', actions.fit);
  const settings = document.createElement('details');
  settings.className = 'ag-board__settings';
  const summary = iconSummary(document, 'Настройки фона', 'settings');
  const panel = document.createElement('div'); panel.className = 'ag-board__preferences';
  const colorLabel = document.createElement('label'); colorLabel.textContent = 'Цвет фона';
  const color = document.createElement('input'); color.type = 'color'; color.id = `ag-board-color-${++serial}`;
  colorLabel.htmlFor = color.id; colorLabel.append(color);
  const theme = document.createElement('button'); theme.type = 'button'; theme.className = 'ag-button'; theme.textContent = 'Цвет темы';
  bind(theme, 'click', () => actions.background({color:''}));
  const patternLabel = document.createElement('label'); patternLabel.textContent = 'Заливка';
  const pattern = document.createElement('select'); pattern.setAttribute('aria-label', 'Заливка');
  for (const [value, label] of [['none','Без узора'],['dots','Точки'],['grid','Сетка']]) {
    const option = document.createElement('option'); option.value = value; option.textContent = label; pattern.append(option);
  }
  patternLabel.append(pattern);
  const information = document.createElement('details');
  information.className = 'ag-board__help';
  const informationSummary = iconSummary(document, 'Управление и горячие клавиши', 'info');
  const help = document.createElement('p');
  help.className = 'ag-board__help-text';
  help.textContent = 'Перетащите фон, либо удерживайте пробел и тяните. Колесо — перемещение; Ctrl/⌘ + колесо — масштаб. При фокусе на поле: стрелки — обзор, Shift — быстрее, +/− — масштаб, 0 — вместить, 1 — 100%. Tab — карточки. Esc — отменить жест.';
  bind(color, 'input', () => actions.background({color:color.value}));
  bind(pattern, 'change', () => actions.background({pattern:pattern.value}));
  const disclosures = [[settings, summary], [information, informationSummary]];
  for (const [disclosure, trigger] of disclosures) {
    bind(disclosure, 'keydown', event => {
      if (event.key !== 'Escape' || !disclosure.open) return;
      event.preventDefault();
      event.stopPropagation();
      disclosure.open = false;
      trigger.focus();
    });
    bind(disclosure, 'toggle', () => {
      if (!disclosure.open) return;
      for (const [other] of disclosures) if (other !== disclosure) other.open = false;
    });
  }
  // Dismiss on outside pointer/focus without stealing focus from the user's next target.
  for (const event of ['pointerdown', 'focusin']) bind(document, event, input => {
    for (const [disclosure] of disclosures) {
      if (!disclosure.contains(input.target)) disclosure.open = false;
    }
  });
  panel.append(colorLabel, theme, patternLabel);
  settings.append(summary, panel);
  information.append(informationSummary, help);
  element.append(settings, information);
  return {
    element,
    update(state) {reset.textContent = `${Math.round(state.scale * 100)}%`; color.value = state.color || '#f4f6f8'; pattern.value = state.pattern;},
    dispose() {for (const remove of listeners) remove(); element.remove();},
  };
}


/** Build a named native disclosure with an SVG icon; decorative geometry stays out of the accessible name. */
function iconSummary(document, label, kind) {
  const summary = document.createElement('summary');
  summary.title = label;
  summary.setAttribute('aria-label', label);
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('width', '20');
  svg.setAttribute('height', '20');
  svg.setAttribute('fill', 'none');
  svg.setAttribute('stroke', 'currentColor');
  svg.setAttribute('stroke-width', '1.7');
  svg.setAttribute('stroke-linecap', 'round');
  svg.setAttribute('aria-hidden', 'true');
  const paths = kind === 'info'
    ? ['M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18', 'M12 11v6', 'M12 7h.01']
    : ['M4 7h5m4 0h7M4 17h9m4 0h3', 'M9 4v6m4 4v6'];
  for (const data of paths) {
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', data);
    svg.append(path);
  }
  summary.append(svg);
  return summary;
}
