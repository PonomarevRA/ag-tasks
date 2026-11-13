import {element, button, emptyState} from './dom.js';
import {statuses, priorities, formatElapsed} from './task-format.js';
import {dashboardBlock} from './dashboard-block.js';
import {dashboardHelp} from './dashboard-help.js';
import {createChart} from './vendor/ag-charts/chart.js';

export const queues = {open:'Открытые',attention:'Требуют внимания',unassigned:'Без ответственного',overdue:'Просроченные',review:'На проверке',all:'Все, включая историю'};
export const kinds = {task:'Задача',rnd:'Исследование',review:'Ревью'};
const reasons = {blocked:'Блокировка',rework:'Доработка',stale:'Нужна актуализация',in_review:'Ожидает ревью',expired_session:'Аренда истекла',open_problem:'Открытая проблема',overdue:'Срок прошёл'};

/** Format an explicit server instant in UTC without inferring agent activity. */
export function observedAt(value) {
  return new Intl.DateTimeFormat('ru-RU', {dateStyle:'short',timeStyle:'short',timeZone:'UTC'}).format(new Date(value)) + ' UTC';
}

/** Format calendar dates directly so local timezone offsets cannot move a deadline. */
function calendar(value) {
  return value ? value.split('-').reverse().join('.') : 'Не задан';
}

/** Build a native header cell with an explicit column scope. */
function header(row, text) {
  const cell = element('th', text);
  cell.scope = 'col';
  row.append(cell);
}

/** Render complete operational facts and bounded AG charts; host callbacks own navigation. */
export function mountManagerPanels(data, {openIssue, onQueue, onPage, queue, offset}) {
  const root = element('section', null, 'manager-results');
  root.dataset.agWidth='fill';
  const resources = [];
  root.append(element('p', `Снимок ${observedAt(data.asOf)}. Неархивированные карточки. Сроки — календарные даты UTC; сегодня ещё не просрочено.`, 'execution-scope'));
  const summary = dashboardBlock('manager-summary', 'Показатели по выбранным фильтрам');
  resources.push(summary);
  summary.body.append(element('p', 'Очередь меняет список контроля. Показатели и графики охватывают всю выборку фильтров.', 'execution-scope'));
  const kpis = element('dl', null, 'work-kpis manager-kpis');
  kpis.dataset.agWidth='fill';
  const definitions = [
    ['Открытые',data.summary.openCount,'Карточки вне статусов «Выполнено» и «Отменено». Тип карточки задаётся фильтром; по умолчанию считаются задачи.'],
    ['Просроченные',data.summary.overdueCount,'Открытые карточки с сохранённым сроком раньше сегодняшней даты UTC. Отсутствующий срок не считается просрочкой.'],
    ['Срок в ближайшие 7 дней',data.summary.dueSoonCount,'Открытые карточки со сроком от сегодняшней даты до семи дней вперёд включительно, UTC.'],
    ['Без ответственного',data.summary.unassignedCount,'Открытые карточки без назначенного ответственного. Назначение человека или команды не является рабочей сессией агента.'],
    ['Действующие сессии',data.summary.activeCount,'Сохранённые рабочие аренды, действующие строго после времени серверного снимка. Подтверждение исполнения внешней модели требует данных её агента.'],
    ['Требуют внимания',data.summary.attentionCount,'Открытые карточки с блокировкой, доработкой, неактуальностью, ревью, открытой проблемой, истёкшей арендой или просроченным сроком. Каждая карточка считается один раз.'],
  ];
  for (const [label,count,text] of definitions) {
    const card = element('div');
    card.dataset.agWidth='content';card.dataset.agWidthMinEm='8';
    const name = element('dt');
    const help = dashboardHelp(label,text);
    resources.push(help);
    name.append(help.element);
    card.append(name,element('dd',count));
    kpis.append(card);
  }
  summary.body.append(kpis);
  root.append(summary.element);

  const control = dashboardBlock('manager-table','Контроль задач');
  resources.push(control);
  const queueBar = element('div',null,'manager-queues');
  queueBar.setAttribute('role','group');
  queueBar.setAttribute('aria-label','Очередь списка контроля');
  for (const row of data.queues) {
    const choose = button(`${queues[row.id]} · ${row.count}`,() => onQueue(row.id));
    choose.setAttribute('aria-pressed',String(row.id === queue));
    queueBar.append(choose);
  }
  control.body.append(queueBar);
  const viewport = element('div',null,'manager-table-scroll');
  viewport.dataset.agWidth='fill';
  viewport.tabIndex=0;
  viewport.setAttribute('role','region');
  viewport.setAttribute('aria-label','Список контроля задач с прокруткой');
  const table = element('table',null,'manager-table');
  table.append(element('caption',`Очередь «${queues[queue]}»: ${data.totalCount}. Показано ${data.items.length ? offset + 1 : 0}–${data.items.length ? offset + data.items.length : 0}. Порядок: внимание, приоритет, срок, давность активности.`));
  const head = table.createTHead().insertRow();
  for (const name of ['Карточка и причина внимания','Проект','Статус','Приоритет','Ответственный','Срок (UTC)','Учтено / аренда']) header(head,name);
  const body = table.createTBody();
  for (const card of data.items) {
    const row = body.insertRow();
    const title = element('th');
    title.scope = 'row';
    const task = button(`${card.key} · ${card.title}`, () => { void openIssue(card.id, task); });
    task.dataset.issueId = card.id;
    title.append(task);
    title.append(element('small',kinds[card.kind] || card.kind));
    if (card.reasons.length) title.append(element('p',card.reasons.map(reason => reasons[reason] || reason).join(' · '),'manager-reasons'));
    if (card.lastActivityAt) title.append(element('small',`Активность: ${observedAt(card.lastActivityAt)}`));
    row.append(title,element('td',card.project));
    const status = element('td');
    const tag = element('span',statuses[card.status] || card.status,'manager-status');
    tag.dataset.status = card.status;
    status.append(tag);
    row.append(status,element('td',priorities[card.priority] || card.priority),element('td',card.assignee || 'Не назначен'));
    const deadline = element('td',calendar(card.dueDate));
    if (card.overdue) deadline.append(element('small','Просрочено'));
    deadline.classList.toggle('manager-overdue',card.overdue);
    const work = element('td',formatElapsed(card.elapsedMs));
    if (card.active) work.append(element('small',`Аренда: ${card.activeAgent}`));
    row.append(deadline,work);
    if (body.rows.length===1) {
      const minimums=[12,8,6,4,8,6,6];
      for (let index=0;index<row.cells.length;index++) {
        row.cells[index].dataset.agWidth='content';
        row.cells[index].dataset.agWidthMinEm=String(minimums[index]);
      }
    }
  }
  viewport.append(table);
  control.body.append(viewport);
  if (!data.items.length) control.body.append(emptyState('Нет карточек в этой очереди','Измените очередь или фильтры. Отсутствующий срок и неизвестная активность не считаются просрочкой.'));
  const pager = element('div',null,'manager-pager');
  if (offset) pager.append(button('Назад',() => onPage(Math.max(0,offset - 50))));
  if (data.hasMore) pager.append(button('Следующие 50',() => onPage(data.nextOffset)));
  control.body.append(pager,element('p','Откройте карточку для назначения ответственного, срока и приоритета. Изменение статуса подчиняется общим правилам DoR, результатов и независимого ревью.','execution-scope'));
  root.append(control.element);

  const plots = element('div',null,'manager-chart-grid');
  plots.dataset.agWidth='fill';
  /** Reuse the pinned AG chart for the largest eight groups, with all exact group counts below. */
  function workload(key,title,rows) {
    const block = dashboardBlock(key,title);
    block.element.dataset.agWidth='content';block.element.dataset.agWidthMinEm='12';
    resources.push(block);
    const explanation = dashboardHelp(title,'Открытые карточки всей выборки фильтров до очереди списка. Назначение не доказывает исполнение модели. Показаны восемь крупнейших групп; полные значения находятся ниже.');
    resources.push(explanation);
    explanation.element.classList.add('dashboard-block-help');
    const helpHeading=element('h3',null,'dashboard-block-info');
    helpHeading.append(explanation.element);
    block.body.append(helpHeading);
    if (!rows.length) {
      block.body.append(emptyState('Нет открытых карточек','В выбранной области нет открытой нагрузки. История доступна в списке.'));
      return block.element;
    }
    const visible = rows.slice(0,8);
    const chart = createChart({document,renderer:'svg',animated:false,type:'bar',arrangement:'horizontal',
      label:`${title} · открытые карточки`,labels:visible.map((row,index) => String(index+1)),
      series:[{id:'open',label:'Открытые',values:visible.map(row => row.openCount)}]});
    resources.push(chart);
    const navigation=chart.element.querySelector('.ag-vt-chart__navigation');
    if (navigation) {navigation.dataset.agWidth='content';navigation.dataset.agWidthMinEm='4';}
    const chartHost = element('div',null,'manager-plot');
    chartHost.append(chart.element);
    block.body.append(chartHost,element('p',`На графике ${visible.length} из ${rows.length} групп. Номера соответствуют списку ниже.`, 'execution-scope'));
    const keys=element('ol',null,'manager-chart-keys');
    for (const row of visible) {
      const item=element('li');
      item.append(element('span',row.label),element('strong',row.openCount));
      keys.append(item);
    }
    block.body.append(keys);
    const exact = element('details',null,'manager-exact');
    exact.append(element('summary','Полные значения по группам'));
    const values = element('table');
    const heading = values.createTHead().insertRow();
    for (const name of ['Группа','Открытые','Действующая аренда','Просроченные']) header(heading,name);
    const valueBody = values.createTBody();
    for (const row of rows) {
      const line = valueBody.insertRow();
      for (const value of [row.label,row.openCount,row.activeCount,row.overdueCount]) line.append(element('td',value));
    }
    exact.append(values);
    block.body.append(exact);
    return block.element;
  }
  plots.append(workload('manager-projects','Открытые карточки по проектам',data.byProject),workload('manager-owners','Назначение и нагрузка',data.byAssignee));
  root.append(plots);
  return {element:root,dispose() { for (const resource of resources) resource.dispose(); }};
}
