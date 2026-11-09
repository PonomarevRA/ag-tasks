import {dashboardBlock} from './dashboard-block.js';
import {mountExecutionDashboard} from './execution-dashboard.js';
import {dashboardHelp} from './dashboard-help.js';
import {mountWorkChart} from './work-chart.js';
import {button, element, emptyState, labelledField} from './dom.js';

const kindNames = {task:'Задача', rnd:'Исследование', review:'Ревью'};
/** Format committed time without rounding a nonempty result down to zero. */
function duration(ms) {
  const seconds = Math.floor(ms / 1000);
  if (ms > 0 && !seconds) return '< 1 с';
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor(seconds / 60) % 60;
  return [hours && `${hours} ч`, minutes && `${minutes} мин`, seconds % 60 && `${seconds % 60} с`].filter(Boolean).join(' ') || '0 с';
}

/** Display a native filter with the active value preserved across response updates. */
function fillOptions(select, rows, emptyLabel) {
  const current = select.value;
  select.replaceChildren(new Option(emptyLabel, ''));
  for (const row of rows) select.add(new Option(row.label, row.id));
  select.value = current;
}

/** Build task rows as text and use the host issue dialog for drill-down. */
function appendTasks(body, tasks, openIssue) {
  for (const task of tasks) {
    const row = element('tr');
    const name = element('th');
    name.scope = 'row';
    const title = `${task.key} · ${task.deleted ? "Удалённая задача" : task.title}`;
    name.append(task.deleted ? element('span', title) : button(title, () => openIssue(task.id)));
    row.append(name, element('td', task.project), element('td', kindNames[task.kind] || task.kind),
      element('td', duration(task.elapsedMs)), element('td', task.sessionCount));
    body.append(row);
  }
}

/** Mount the read-only view and ignore responses from obsolete filters or navigation. */
export function renderWorkAnalytics(target, {request, openIssue, filters}) {
  const today = new Date();
  const firstDay = new Date(today.getTime() - 29 * 86400000);
  const form = element('form', null, 'work-filters ag-form-tracks');
  form.dataset.agSelectIndicators = '';
  const controls = {start:element('input'), end:element('input'), spaceId:element('select'), kind:element('select'), q:element('input')};
  controls.start.type = controls.end.type = 'date';
  controls.start.required = controls.end.required = true;
  controls.q.type = 'search';
  controls.q.maxLength = 200;
  controls.q.value = filters.q || '';
  controls.start.value = filters.start || firstDay.toISOString().slice(0, 10);
  controls.end.value = filters.end || today.toISOString().slice(0, 10);
  fillOptions(controls.spaceId, [], 'Все проекты');
  fillOptions(controls.kind, [], 'Все типы');
  const fields = {start:'С (UTC)', end:'По (UTC)', spaceId:'Проект', kind:'Тип задачи', q:'Ключ или название'};
  for (const [name, control] of Object.entries(controls)) form.append(labelledField(fields[name], control, {sharedTracks:true}));
  const apply = element('button', 'Применить', 'ag-button ag-button--primary');
  apply.type = 'submit';
  const action = element('div', null, 'work-filter-action');
  action.dataset.agFormField = '';
  const captionSlot = element('span', '', 'ag-form-track__label');
  captionSlot.setAttribute('aria-hidden', 'true');
  apply.classList.add('ag-form-track__control');
  action.append(captionSlot, apply);
  form.append(action);

  const status = element('p', null, 'work-status');
  status.setAttribute('role', 'status');
  const result = element('div', null, 'work-results');
  const description = element('p', 'Проект, тип и поиск применяются ко всем панелям. Период UTC ограничивает события и учтённое время. Текущее состояние карточек показано на момент обновления.');
  const refreshBar = element('div', null, 'work-refresh-bar');
  const automatic = element('input');
  automatic.type = 'checkbox';
  automatic.checked = filters.autoRefresh !== false;
  const automaticLabel = element('label', null, 'work-auto-refresh');
  automaticLabel.append(automatic, document.createTextNode('Обновлять каждые 30 с'));
  const refreshState = element('span', null, 'work-refresh-state');
  const refresh = button('Обновить дашборд', () => {
    if (filterDraftChanged()) { form.requestSubmit(); return; }
    return load(pageOffset);
  });
  refreshBar.append(automaticLabel, refresh, refreshState);
  const filterBlock = dashboardBlock('filters', 'Фильтры');
  filterBlock.element.classList.add('dashboard-filter-block');
  filterBlock.body.append(description, form);
  target.append(filterBlock.element, refreshBar, status, result);

  let generation = 0;
  let disposed = false;
  let timer = null;
  let pageOffset = 0;
  let pending = false;
  let disposeReport = () => {};
  let active = {
    start:controls.start.value, end:controls.end.value, spaceId:filters.spaceId || '',
    kind:filters.kind || '', q:controls.q.value,
  };

  /** Detect unapplied filter edits so background refresh never silently overwrites them. */
  function filterDraftChanged() {
    return Object.keys(controls).some(name => controls[name].value !== (active[name] || ''));
  }

  /** Recheck before both requesting and committing automatic updates; interaction may begin during a request. */
  function backgroundPauseReason() {
    if (!automatic.checked) return 'Автообновление выключено';
    if (document.hidden) return 'Автообновление приостановлено в скрытом окне';
    if (filterDraftChanged()) return 'Фильтры изменены — нажмите «Применить»';
    const focused = document.activeElement;
    const editing = form.contains(focused) && focused.matches('input,select,textarea');
    const interacting = result.contains(focused) || result.querySelector('[aria-expanded="true"]') || document.querySelector('dialog[open]');
    return editing || interacting ? 'Автообновление приостановлено на время взаимодействия' : '';
  }

  /** Refresh after the preceding request finishes; hidden views and active interactions keep their current DOM. */
  function scheduleRefresh() {
    document.defaultView.clearTimeout(timer);
    timer = null;
    if (disposed || pending || !automatic.checked) return;
    timer = document.defaultView.setTimeout(() => {
      timer = null;
      if (disposed) return;
      const pause = backgroundPauseReason();
      if (pause) {
        if (!document.hidden) refreshState.textContent = pause;
        scheduleRefresh();
        return;
      }
      load(pageOffset, false, true);
    }, 30_000);
  }
  automatic.addEventListener('change', () => {
    filters.autoRefresh = automatic.checked;
    refreshState.textContent = automatic.checked ? 'Автообновление включено' : 'Автообновление выключено';
    scheduleRefresh();
  });

  /** Fetch one read-only page using the submitted filters for both charts and task rows. */
  async function load(offset = 0, focusPage = false, background = false) {
    if (disposed) return;
    document.defaultView.clearTimeout(timer);
    timer = null;
    const version = ++generation;
    const previousStatus = status.textContent;
    pending = true;
    status.textContent = background ? 'Обновляем дашборд…' : offset ? 'Загружаем карточки…' : 'Загружаем дашборд…';
    result.setAttribute('aria-busy', 'true');
    try {
      const params = new URLSearchParams({...active, offset});
      const [data, execution] = await Promise.all([
        request(`/api/v1/analytics/work?${params}`),
        request(`/api/v1/analytics/execution?${new URLSearchParams(active)}`),
      ]);
      if (version !== generation) return;
      const pause = background && backgroundPauseReason();
      if (pause) {
        status.textContent = previousStatus;
        if (!document.hidden) refreshState.textContent = pause;
        return;
      }
      delete result.dataset.loadFailed;
      fillOptions(controls.spaceId, data.projects.map(row => ({...row, label:row.name})), 'Все проекты');
      fillOptions(controls.kind, data.kinds.map(id => ({id, label:kindNames[id] || id})), 'Все типы');
      controls.spaceId.value = active.spaceId || '';
      controls.kind.value = active.kind || '';
      // Replacing chart DOM must not move the reader back to the top of the page.
      const scrollPositions = [];
      for (let node = target; node; node = node.parentElement) {
        if (node.scrollTop || node.scrollLeft) {
          scrollPositions.push({node, top:node.scrollTop, left:node.scrollLeft});
        }
      }
      const plotScrolls = [];
      for (const plot of result.querySelectorAll('.work-chart-host')) {
        plotScrolls.push({top:plot.scrollTop, left:plot.scrollLeft});
      }
      disposeReport();
      disposeReport = renderReport(result, data, execution, openIssue);
      for (const [index, plot] of result.querySelectorAll('.work-chart-host').entries()) {
        const saved = plotScrolls[index];
        if (saved) { plot.scrollTop = saved.top; plot.scrollLeft = saved.left; }
      }
      pageOffset = offset;
      refreshState.textContent = 'Обновлено ' + new Intl.DateTimeFormat('ru-RU', {
        dateStyle:'short', timeStyle:'medium', timeZone:'UTC',
      }).format(new Date(execution.asOf)) + ' UTC';
      let pagerFocus = null;
      if (data.taskCount) {
        const pager = element('div', null, 'work-pager');
        const first = data.tasks.length ? offset + 1 : 0;
        pager.append(element('span', `${first}–${offset + data.tasks.length} из ${data.taskCount} карточек с временем`));
        if (offset) pager.append(button('Назад', () => load(Math.max(0, offset - 50), true)));
        if (data.nextOffset !== null) pager.append(button('Далее', () => load(data.nextOffset, true)));
        const ledgerBody = result.querySelector('[data-dashboard-block="ledger"] .dashboard-block-body');
        (ledgerBody || result).append(pager);
        if (focusPage) {
          pager.tabIndex = -1;
          pagerFocus = pager;
        }
      }
      for (const saved of scrollPositions) {
        saved.node.scrollTop = saved.top;
        saved.node.scrollLeft = saved.left;
      }
      if (pagerFocus) pagerFocus.focus();
      status.textContent = '';
    } catch (error) {
      if (version === generation) {
        const message = error.message || 'Не удалось загрузить дашборд.';
        status.textContent = result.childElementCount
          ? message + ' Показаны данные предыдущего успешного обновления.'
          : message;
        result.dataset.loadFailed = 'true';
      }
    } finally {
      if (version === generation) {
        pending = false;
        result.removeAttribute('aria-busy');
        scheduleRefresh();
      }
    }
  }
  form.addEventListener('submit', event => {
    event.preventDefault();
    active = Object.fromEntries(Object.entries(controls).map(([name, control]) => [name, control.value]));
    Object.assign(filters, active);
    disposeReport();
    disposeReport = () => {};
    result.replaceChildren();
    load();
  });
  load();
  return () => {
    disposed = true;
    generation++;
    document.defaultView.clearTimeout(timer);
    disposeReport();
    filterBlock.dispose();
  };
}

/** Compose independent execution panels and effort details, including a useful zero-effort dashboard. */
function renderReport(result, data, execution, openIssue) {
  result.replaceChildren();
  const resources = [];
  const monitoring = mountExecutionDashboard(execution, {openIssue});
  resources.push(monitoring);
  result.append(monitoring.element);
  let ledger = null;
  const period = element('section', null, 'work-time-summary');
  period.append(element('h2', 'Учтённое время за период'));
  const totals = element('dl', null, 'work-kpis');
  const definitions = [
    ['Учтено за период', duration(data.totalMs), 'Сумма зафиксированного времени сессий внутри выбранного периода UTC. Параллельная работа складывается. Записи удалённых карточек сохраняются с обезличенным названием.'],
    ['Карточки с временем', data.taskCount, 'Карточки с положительным зафиксированным временем за период. Карточки без записанного времени видны в текущем снимке выше.'],
    ['Проекты с временем', data.projectCount, 'Проекты, у карточек которых есть учтённое время за выбранный период. Принадлежность взята из текущих метаданных.'],
    ['Рабочие сессии', data.sessionCount, 'Сессии с положительным учтённым временем, пересекающим выбранный период. Это исторические записи, включая завершённые сессии. Действующие аренды показаны отдельно в текущем снимке.'],
  ];
  for (const [label, value, explanation] of definitions) {
    const item = element('div');
    const term = element('dt');
    const help = dashboardHelp(label, explanation);
    resources.push(help);
    term.append(help.element);
    item.append(term, element('dd', value));
    totals.append(item);
  }
  const summaryBlock = dashboardBlock('time-summary', 'Итоги периода');
  resources.push(summaryBlock);
  summaryBlock.body.append(totals);
  period.append(summaryBlock.element);
  if (!data.totalMs) {
    period.append(emptyState('Нет учтённого времени', 'Для этого периода нет записанных рабочих сессий. Текущие карточки и события показаны выше.'));
  } else {
    const chart = mountWorkChart(data);
    resources.push(chart);
    period.append(chart.element);
    const panel = element('section', null, 'work-panel work-table');
    const ledgerBlock = dashboardBlock('ledger', 'Карточки по учтённому времени');
    resources.push(ledgerBlock);
    const table = element('table');
    const header = table.createTHead().insertRow();
    for (const label of ['Карточка', 'Проект', 'Тип', 'Время', 'Сессии']) {
      const cell = element('th', label);
      cell.scope = 'col';
      header.append(cell);
    }
    appendTasks(table.createTBody(), data.tasks, openIssue);
    ledgerBlock.body.append(table);
    panel.append(ledgerBlock.element);
    ledger = panel;
  }
  result.append(period, monitoring.attentionElement);
  if (ledger) result.append(ledger);
  return () => { for (const resource of resources) resource.dispose(); };
}
