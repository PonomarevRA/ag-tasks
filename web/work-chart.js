import {dashboardBlock} from './dashboard-block.js';
import {dashboardHelp} from './dashboard-help.js';
import {createChart} from './vendor/ag-charts/chart.js';
import {element, labelledField} from './dom.js';

const storageKey = 'ag-tasks.work-chart.v1';
const kindNames = {task:'Задача', rnd:'Исследование', review:'Ревью'};
const choices = {
  metric: [['percentiles','P50 и P90'], ['p50Ms','P50 · медиана'], ['p90Ms','P90'], ['meanMs','Среднее на задачу'], ['elapsedMs','Сумма времени']],
  group: [['overall','Вся выборка'], ['day','По дням'], ['week','По неделям'], ['project','По проектам'], ['kind','По типам задач']],
  type: [['bar','Столбцы'], ['horizontal','Горизонтальные столбцы'], ['line','Линия'], ['area','Область']],
  unit: [['minutes','Минуты'], ['hours','Часы'], ['seconds','Секунды']],
};
const defaults = {metric:'percentiles', group:'overall', type:'bar', unit:'minutes'};
const unitScale = {minutes:60_000, hours:3_600_000, seconds:1000};

/** Restore only known preference values; unavailable local storage leaves usable defaults. */
function readPreferences(view) {
  const saved = {...defaults};
  try {
    const value = JSON.parse(view.localStorage.getItem(storageKey));
    for (const key of Object.keys(choices)) {
      if (choices[key].some(([id]) => id === value?.[key])) saved[key] = value[key];
    }
  } catch { /* Storage is optional in restricted webviews and private browsers. */ }
  return saved;
}

/** Prepare a bounded categorical projection without recalculating server percentiles in the browser. */
function chartRows(data, group) {
  if (group === 'day') return data.byDay;
  if (group === 'week') return data.byWeek || [];
  if (group === 'project') return data.byProject;
  if (group === 'kind') return data.byKind.map(row => ({...row, label:kindNames[row.id] || row.id}));
  return [{
    label:'Все задачи', taskCount:data.taskCount, elapsedMs:data.totalMs,
    ...data.taskDurationPercentiles, meanMs:data.meanTaskMs,
  }];
}

/** Mount AG SVG charts with host-owned metrics, grouping and real time-unit conversions. */
export function mountWorkChart(data) {
  const panel = element('section', null, 'work-panel work-chart');
  const heading = element('h2', null, 'dashboard-block-info');
  const help = dashboardHelp('Время карточек и P50/P90', 'Перцентили считаются по сумме записанного времени одной карточки внутри каждого разреза. Незавершённые карточки участвуют с уже учтённым временем. Выбор метрики и вида меняет только эту панель. Пустые интервалы остаются без измерений.');
  help.element.classList.add('dashboard-block-help');
  heading.append(help.element);
  panel.append(heading);
  const description = element('p', 'P50 — медиана, P90 — оценка порога для 90% задач. В каждом разрезе сначала складывается время сессий одной задачи за период, затем рассчитывается метрика.');
  panel.append(description);
  const settings = readPreferences(document.defaultView);
  const controls = {};
  const toolbar = element('div', null, 'work-chart-controls ag-form-tracks');
  toolbar.dataset.agSelectIndicators = '';
  const labels = {metric:'Метрика', group:'Разрез', type:'Вид графика', unit:'Единицы времени'};
  for (const key of Object.keys(choices)) {
    const select = element('select');
    select.dataset.chartOption = key;
    for (const [value, label] of choices[key]) {
      const option = element('option', label);
      option.value = value;
      select.append(option);
    }
    select.value = settings[key];
    controls[key] = select;
    toolbar.append(labelledField(labels[key], select, {sharedTracks:true}));
  }
  const note = element('p', null, 'work-chart-note');
  note.setAttribute('role', 'status');
  const plot = element('div', null, 'work-chart-host');
  const sample = element('details', null, 'work-chart-samples');
  sample.append(element('summary', 'Точные значения и размер выборки'));
  const counts = element('div');
  sample.append(counts);
  panel.append(toolbar, note, plot, sample);
  let chart;

  /** Keep unsupported plot/group combinations explicit, with one scale for every visible series. */
  function draw() {
    let message = '';
    if (settings.group === 'day' && data.byDay.length > 120) {
      settings.group = controls.group.value = 'week';
      message = 'Для периода длиннее 120 дней показаны недели. ';
    }
    const temporal = settings.group === 'day' || settings.group === 'week';
    for (const option of controls.type.options) {
      option.disabled = !temporal && ['line','area'].includes(option.value);
    }
    if (!temporal && ['line','area'].includes(settings.type)) {
      settings.type = controls.type.value = 'bar';
    }
    const allRows = chartRows(data, settings.group);
    const rows = allRows.slice(0, temporal ? 120 : 20);
    if (allRows.length > rows.length) message += 'Показаны 20 разрезов с наибольшей суммой времени. ';
    if (settings.group === 'week') message += 'Недели начинаются в понедельник (UTC), время ограничено выбранным периодом. ';
    const count = new Intl.NumberFormat('ru-RU').format(data.taskCount);
    note.textContent = message + `Карточек с учтённым временем: ${count}. Пустой разрез означает отсутствие измерений. График округлён до 2 знаков, точные миллисекунды — в таблице.`;
    const metricKeys = settings.metric === 'percentiles' ? ['p50Ms','p90Ms'] : [settings.metric];
    const series = [];
    for (const key of metricKeys) {
      const label = choices.metric.find(([id]) => id === key)[1];
      series.push({id:key, label, values:rows.map(row => row[key] == null ? null : row[key] / unitScale[settings.unit])});
    }
    const options = {
      label:`${labels.metric}: ${choices.metric.find(([id]) => id === settings.metric)[1]} · ${choices.unit.find(([id]) => id === settings.unit)[1].toLowerCase()}`,
      renderer:'svg', animated:false,
      type:settings.type === 'horizontal' ? 'bar' : settings.type,
      arrangement:settings.type === 'horizontal' ? 'horizontal' : 'grouped',
      labels:rows.map(row => row.label), series,
    };
    if (chart) chart.update(options);
    else {
      chart = createChart({document, ...options});
      plot.append(chart.element);
    }
    const table = element('table');
    const header = table.createTHead().insertRow();
    for (const label of ['Разрез', 'Задач', ...series.map(row => row.label + ', мс')]) {
      const cell = element('th', label);
      cell.scope = 'col';
      header.append(cell);
    }
    const body = table.createTBody();
    const number = new Intl.NumberFormat('ru-RU');
    for (const row of rows) {
      const record = body.insertRow();
      const label = element('th', row.label);
      label.scope = 'row';
      record.append(label, element('td', row.taskCount ?? '—'));
      for (const key of metricKeys) {
        record.append(element('td', row[key] == null ? 'Нет данных' : number.format(row[key])));
      }
    }
    counts.replaceChildren(table);
    try { document.defaultView.localStorage.setItem(storageKey, JSON.stringify(settings)); }
    catch { /* The controls still work when preferences cannot be persisted. */ }
  }
  for (const key of Object.keys(controls)) {
    controls[key].addEventListener('change', () => {
      settings[key] = controls[key].value;
      draw();
    });
  }
  const block = dashboardBlock('time-chart', 'Время карточек и P50/P90');
  block.body.append(...panel.childNodes);
  panel.append(block.element);
  draw();
  return {element:panel, dispose() { chart?.dispose(); help.dispose(); block.dispose(); }};
}
