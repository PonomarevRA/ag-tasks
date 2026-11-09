import {dashboardBlock} from './dashboard-block.js';
import {createChart} from './vendor/ag-charts/chart.js';
import {button, element, emptyState} from './dom.js';
import {dashboardHelp} from './dashboard-help.js';

const statusNames = {
  backlog:'Бэклог', ready:'Готовы к запуску', in_progress:'В работе', blocked:'Заблокированы',
  in_review:'Ожидают ревью', rework:'На доработке', done:'Завершены', cancelled:'Отменены', stale:'Требуют актуализации',
};
const axisNames = {
  backlog:'Бэклог', ready:'Старт', in_progress:'Работа', blocked:'Блок', in_review:'Ревью',
  rework:'Правки', done:'Готово', cancelled:'Отмена', stale:'Неакт.',
};
const reasonNames = {
  blocked:'Есть блокировка', rework:'Нужна доработка', stale:'Требуется актуализация',
  in_review:'Нужен результат ревью', expired_session:'Рабочая аренда истекла', open_problem:'Есть открытые проблемы',
};

/** Format the server's observation time explicitly in UTC, including the date. */
function observedAt(value) {
  return new Intl.DateTimeFormat('ru-RU', {
    dateStyle:'short', timeStyle:'medium', timeZone:'UTC',
  }).format(new Date(value)) + ' UTC';
}

/** Mount the current-state and recorded-event panels; all counts come from the read-only execution API. */
export function mountExecutionDashboard(data, {openIssue}) {
  const root = element('section', null, 'execution-dashboard');
  const resources = [];
  const {snapshot, events} = data;
  root.append(element('h2', 'Исполнение задач'));
  const observation = element('p', `Состояние на ${observedAt(data.asOf)}. Проект, тип и поиск применяются к снимку. Даты задают период событий и учтённого времени.`, 'execution-scope');

  const kpis = element('dl', null, 'work-kpis execution-kpis');
  const definitions = [
    ['Карточки сейчас', snapshot.totalCount, 'Все неархивированные и неудалённые карточки выбранного проекта, типа и поиска на момент снимка. Даты не ограничивают этот показатель. Исследования, контейнеры и ревью считаются отдельными карточками.'],
    ['Действующие сессии', snapshot.activeSessionCount, 'Сохранённые рабочие сессии с действующей арендой на момент серверного снимка. Показывает объявленную работу с задачей. Подтверждение выполнения внешней модели требует данных её агента.'],
    ['Ожидают ревью', snapshot.reviewCount, 'Карточки в текущем статусе «Ожидают ревью». Назначение проверяющего само по себе не запускает агента.'],
    ['Требуют внимания', snapshot.attentionCount, 'Карточки с блокировкой, доработкой, неактуальностью, ожидающим ревью, истёкшей рабочей арендой или открытой проблемой. Каждая карточка считается один раз, даже при нескольких причинах.'],
  ];
  for (const [label, count, text] of definitions) {
    const card = element('div');
    const name = element('dt');
    const help = dashboardHelp(label, text);
    resources.push(help);
    name.append(help.element);
    card.append(name, element('dd', count));
    kpis.append(card);
  }
  const overview = dashboardBlock('overview', 'Показатели сейчас');
  resources.push(overview);
  overview.body.append(observation, kpis);
  root.append(overview.element);

  /** Build one independent AG chart and retain its help and disposal with this dashboard. */
  function panel(key, title, explanation, chartOptions) {
    const section = element('section', null, 'work-panel execution-chart');
    const heading = element('h3', null, 'dashboard-block-info');
    const help = dashboardHelp(title, explanation);
    resources.push(help);
    help.element.classList.add('dashboard-block-help');
    heading.append(help.element);
    const host = element('div', null, 'work-chart-host');
    const chart = createChart({document, renderer:'svg', animated:false, ...chartOptions});
    resources.push(chart);
    host.append(chart.element);
    const block = dashboardBlock(key, title);
    resources.push(block);
    block.body.append(heading, host);
    section.append(block.element);
    return {element:section, body:block.body};
  }

  const statuses = snapshot.statuses;
  const statusPanel = panel('statuses', 'Статусы сейчас',
    'Распределение всех текущих карточек по сохранённым статусам. Карточки без учтённого времени входят в распределение. Период дат на снимок не влияет. Полные названия статусов приведены в таблице.',
    {label:'Текущие карточки · количество', type:'bar', arrangement:'horizontal',
      labels:statuses.map(row => axisNames[row.id] || row.id),
      series:[{id:'cards', label:'Карточки', values:statuses.map(row => row.count)}]});
  const statusDetails = element('div', null, 'execution-status-values');
  statusDetails.append(element('p', 'Все статусы и количество'));
  const statusList = element('dl', null, 'work-values');
  for (const row of statuses) statusList.append(element('dt', statusNames[row.id] || row.id), element('dd', row.count));
  statusDetails.append(statusList);
  statusPanel.body.append(statusDetails);

  const weekly = events.byDay.length > 120;
  const activityRows = weekly ? events.byWeek : events.byDay;
  const activityPanel = panel('events', 'Создание и автозавершение',
    'Создание считается по дате создания сохранённой карточки. Автозавершение — разные карточки с событием auto_completed в пределах UTC-дня. После повторного открытия карточка может попасть в несколько дней. Ручные и старые завершения без этого события не учитываются. Архивные карточки входят в события, удалённые исключены. Проект, тип и поиск берутся из текущих метаданных.',
    {label:weekly ? 'Недельные суммы дневных значений · карточки' : 'События по дням UTC · карточки', type:'bar',
      labels:activityRows.map(row => row.label),
      series:[
        {id:'created', label:'Создано', values:activityRows.map(row => row.createdCount)},
        {id:'completed', label:'Автозавершение', values:activityRows.map(row => row.completedCount)},
      ]});
  const periodSummary = element('p', `За период создано: ${events.createdCount}. С автозавершением: ${events.completedUniqueCount} уникальных карточек.`, 'execution-scope');
  activityPanel.body.append(periodSummary);
  if (weekly) activityPanel.body.append(element('p', 'Для периода длиннее 120 дней показаны недельные суммы дневных значений. Одна карточка может учитываться в разные дни.'));
  const charts = element('div', null, 'execution-chart-grid');
  charts.append(statusPanel.element, activityPanel.element);
  root.append(charts);

  const attention = element('section', null, 'work-panel execution-attention');
  const heading = element('h3', null, 'dashboard-block-info');
  const help = dashboardHelp('Требуют внимания',
    'Откройте карточку для проверки причины. Сначала показаны истёкшие аренды, затем блокировки и серьёзные открытые проблемы. Последняя активность включает активность дочерних карточек. Чтение дашборда не меняет статус и не возобновляет рабочую сессию.');
  resources.push(help);
  help.element.classList.add('dashboard-block-help');
  heading.append(help.element);
  attention.append(heading, element('p', `Показано ${snapshot.attention.length} из ${snapshot.attentionCount} карточек. Открытых проблем: ${snapshot.openProblemCount}.`));
  if (!snapshot.attention.length) {
    attention.append(emptyState('Нет карточек, требующих внимания', 'По выбранному проекту, типу и поиску.'));
  } else {
    const list = element('ul', null, 'execution-attention-list');
    for (const card of snapshot.attention) {
      const item = element('li');
      item.append(button(`${card.key} · ${card.title}`, () => openIssue(card.id)));
      const reasons = card.reasons.map(reason => reasonNames[reason] || reason);
      item.append(element('p', reasons.join(' · ')));
      if (card.lastActivityAt) {
        const timestamp = element('time', `Последняя активность: ${observedAt(card.lastActivityAt)}`);
        timestamp.dateTime = card.lastActivityAt;
        item.append(timestamp);
      }
      list.append(item);
    }
    attention.append(list);
  }
  const attentionBlock = dashboardBlock('attention', 'Требуют внимания');
  resources.push(attentionBlock);
  attentionBlock.body.append(...attention.childNodes);
  attention.append(attentionBlock.element);
  root.append(attention);
  return {
    element:root,
    attentionElement:attention,
    /** Release chart and document-level disclosure listeners before replacing or leaving the view. */
    dispose() {
      for (const resource of resources) resource.dispose();
    },
  };
}
