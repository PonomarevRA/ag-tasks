import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountExecutionDashboard} from '../web/execution-dashboard.js';

/** Supply execution facts without a work-time payload, including a repeated completion across days. */
function executionData() {
  return {
    asOf:'2026-11-04T12:00:00+00:00',
    snapshot:{
      totalCount:52, activeSessionCount:4, reviewCount:3, attentionCount:12, openProblemCount:4,
      statuses:[
        {id:'backlog',count:11}, {id:'ready',count:7}, {id:'in_progress',count:4},
        {id:'blocked',count:2}, {id:'in_review',count:3}, {id:'rework',count:1},
        {id:'done',count:13}, {id:'cancelled',count:6}, {id:'stale',count:5},
      ],
      attention:[
        {id:'zero-time',key:'TASK-17',title:'<em>Без записанного времени</em>',status:'blocked',
          reasons:['blocked','open_problem'],lastActivityAt:'2026-11-03T09:30:00+00:00'},
        {id:'expired-lease',key:'TASK-18',title:'Проверить аренду',status:'in_progress',
          reasons:['expired_session'],lastActivityAt:null},
      ],
      attentionLimit:10,
    },
    events:{
      byDay:[
        {label:'2026-11-02',createdCount:3,completedCount:2},
        {label:'2026-11-03',createdCount:0,completedCount:1},
      ],
      byWeek:[{label:'2026-11-02',createdCount:3,completedCount:3}],
      createdCount:3, completedUniqueCount:2,
    },
  };
}

/** Mount real AG charts in an isolated browser document and restore globals after every test. */
function mountDashboard(t, data = executionData()) {
  const dom = new JSDOM('<main></main><button id="outside">Другое действие</button>', {
    url:'http://localhost', pretendToBeVisual:true,
  });
  const previous = {document:globalThis.document, Option:globalThis.Option};
  Object.assign(globalThis, {document:dom.window.document, Option:dom.window.Option});
  const opened = [];
  const dashboard = mountExecutionDashboard(data, {openIssue:id => { opened.push(id); }});
  dom.window.document.querySelector('main').append(dashboard.element);
  t.after(() => {
    dashboard.dispose();
    dom.window.close();
    Object.assign(globalThis, previous);
  });
  return {dom, document:dom.window.document, dashboard, opened};
}

/** Read visible labels and values from a definition list without hidden explanatory text. */
function definitionValues(list) {
  const entries = {};
  for (const name of list.querySelectorAll('dt')) {
    const label = name.querySelector('.ag-inline-help__label')?.textContent || name.textContent;
    entries[label] = name.nextElementSibling.textContent;
  }
  return entries;
}

/** Read the chart's accessible data table so assertions verify plotted values, not only its caption. */
function chartTableRows(panel) {
  const rows = [];
  for (const row of panel.querySelectorAll('.ag-vt-chart__data tbody tr')) {
    const cells = [];
    for (const cell of row.children) cells.push(cell.textContent);
    rows.push(cells);
  }
  return rows;
}

test('execution inventory and two real charts render without any work-time payload', t => {
  const {dashboard} = mountDashboard(t);
  const root = dashboard.element;
  assert.deepEqual(definitionValues(root.querySelector('.execution-kpis')), {
    'Карточки сейчас':'52', 'Действующие сессии':'4', 'Ожидают ревью':'3', 'Требуют внимания':'12',
  });
  assert.equal(root.querySelectorAll('.execution-chart .ag-vt-chart').length, 2);
  assert.equal(root.querySelectorAll('.execution-chart .ag-vt-chart__plot > svg').length, 2);
  assert.deepEqual(definitionValues(root.querySelector('.execution-status-values dl')), {
    'Бэклог':'11', 'Готовы к запуску':'7', 'В работе':'4', 'Заблокированы':'2',
    'Ожидают ревью':'3', 'На доработке':'1', 'Завершены':'13', 'Отменены':'6', 'Требуют актуализации':'5',
  });
  assert.match(root.querySelector('.execution-scope').textContent, /04\.11\.2026.*12:00:00 UTC/);
  assert.match(root.querySelector('.execution-scope').textContent, /Даты задают период событий/);
  const charts = root.querySelectorAll('.execution-chart');
  assert.match(charts[0].textContent, /Статусы сейчас/);
  assert.match(charts[0].querySelector('.ag-vt-chart').textContent, /Бэклог.*11/);
  assert.deepEqual(chartTableRows(charts[0]).map(row => row[1]), ['11','7','4','2','3','1','13','6','5']);
  assert.match(charts[1].textContent, /Создание и автозавершение/);
  assert.deepEqual(chartTableRows(charts[1]), [['2026-11-02','3','2'], ['2026-11-03','0','1']]);
  assert.match(charts[1].textContent, /За период создано: 3\. С автозавершением: 2 уникальных карточек/);
});

test('attention rows show the bounded population and literal titles before opening the exact task', t => {
  const {dashboard, opened} = mountDashboard(t);
  const attention = dashboard.element.querySelector('.execution-attention');
  assert.match(attention.textContent, /Показано 2 из 12 карточек\. Открытых проблем: 4/);
  const rows = attention.querySelectorAll('li');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].querySelector('button').textContent, 'TASK-17 · <em>Без записанного времени</em>');
  assert.equal(rows[0].querySelector('em'), null);
  assert.match(rows[0].textContent, /Есть блокировка · Есть открытые проблемы/);
  assert.match(rows[1].textContent, /Рабочая аренда истекла/);
  assert.equal(rows[0].querySelector('time').dateTime, '2026-11-03T09:30:00+00:00');
  rows[0].querySelector('button').click();
  assert.deepEqual(opened, ['zero-time']);
});

test('help opens by its named button, Escape restores focus, and outside focus closes it', t => {
  const {dashboard, document, dom} = mountDashboard(t);
  const trigger = dashboard.element.querySelector('button[aria-label="Пояснение: Карточки сейчас"]');
  const panel = document.getElementById(trigger.getAttribute('aria-controls'));
  assert.ok(trigger.querySelector('svg[aria-hidden="true"] path'));
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(panel.hidden, true);
  trigger.focus();
  trigger.click();
  assert.equal(trigger.getAttribute('aria-expanded'), 'true');
  assert.equal(trigger.getAttribute('aria-describedby'), panel.id);
  assert.equal(panel.hidden, false);
  assert.match(panel.textContent, /Даты не ограничивают этот показатель/);
  const escape = new dom.window.KeyboardEvent('keydown', {key:'Escape',bubbles:true,cancelable:true});
  trigger.dispatchEvent(escape);
  assert.equal(escape.defaultPrevented, true);
  assert.equal(panel.hidden, true);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  assert.equal(trigger.hasAttribute('aria-describedby'), false);
  assert.equal(document.activeElement, trigger);
  trigger.click();
  const outside = document.querySelector('#outside');
  outside.focus();
  assert.equal(panel.hidden, true);
  assert.equal(document.activeElement, outside);
});

test('non-focusing help activation moves focus to its trigger so Escape closes it', t => {
  const {dashboard, document, dom} = mountDashboard(t);
  const trigger = dashboard.element.querySelector('button[aria-label="Пояснение: Карточки сейчас"]');
  const panel = document.getElementById(trigger.getAttribute('aria-controls'));
  assert.equal(document.activeElement, document.body);
  trigger.click();
  assert.equal(document.activeElement, trigger);
  assert.equal(panel.hidden, false);
  document.activeElement.dispatchEvent(new dom.window.KeyboardEvent('keydown', {
    key:'Escape', bubbles:true, cancelable:true,
  }));
  assert.equal(panel.hidden, true);
  assert.equal(document.activeElement, trigger);
  document.querySelector('#outside').focus();
  dashboard.dispose();
  trigger.click();
  assert.equal(document.activeElement.id, 'outside');
  assert.equal(panel.hidden, true);
});

test('disposing an open dashboard removes both charts and its document help listeners', t => {
  const {dashboard, document} = mountDashboard(t);
  const trigger = dashboard.element.querySelector('button[aria-label="Пояснение: Статусы сейчас"]');
  const panel = document.getElementById(trigger.getAttribute('aria-controls'));
  const eventTypes = new Set(['keydown','pointerdown','focusin']);
  const listeners = new Map();
  const originalAdd = document.addEventListener;
  const originalRemove = document.removeEventListener;
  document.addEventListener = function(type, listener, options) {
    if (eventTypes.has(type)) {
      const active = listeners.get(type) || new Set();
      active.add(listener);
      listeners.set(type, active);
    }
    return originalAdd.call(this, type, listener, options);
  };
  document.removeEventListener = function(type, listener, options) {
    listeners.get(type)?.delete(listener);
    return originalRemove.call(this, type, listener, options);
  };
  t.after(() => {
    document.addEventListener = originalAdd;
    document.removeEventListener = originalRemove;
  });
  trigger.click();
  assert.equal(panel.hidden, false);
  assert.deepEqual([...listeners.keys()].sort(), ['focusin','keydown','pointerdown']);
  for (const active of listeners.values()) assert.equal(active.size, 1);
  dashboard.dispose();
  assert.equal(dashboard.element.querySelectorAll('.ag-vt-chart').length, 0);
  assert.equal(panel.hidden, true);
  for (const active of listeners.values()) assert.equal(active.size, 0);
  trigger.click();
  assert.equal(panel.hidden, true);
  assert.equal(trigger.getAttribute('aria-expanded'), 'false');
  for (const active of listeners.values()) assert.equal(active.size, 0);
});

test('empty attention and zero event counts keep inventory and chart panels visible', t => {
  const data = executionData();
  data.snapshot.attention = [];
  data.snapshot.attentionCount = data.snapshot.openProblemCount = 0;
  data.events.byDay = [{label:'2026-11-04',createdCount:0,completedCount:0}];
  data.events.createdCount = data.events.completedUniqueCount = 0;
  const {dashboard} = mountDashboard(t, data);
  assert.equal(dashboard.element.querySelectorAll('.execution-chart .ag-vt-chart').length, 2);
  assert.match(dashboard.element.querySelector('.execution-attention').textContent, /Нет карточек, требующих внимания/);
  assert.equal(dashboard.element.querySelector('.execution-attention-list'), null);
  assert.match(dashboard.element.textContent, /За период создано: 0\. С автозавершением: 0 уникальных карточек/);
});

for (const days of [120,121]) {
  test(`activity uses ${days === 120 ? 'daily buckets at' : 'weekly sums above'} the 120-day boundary`, t => {
    const data = executionData();
    data.events.byDay = Array.from({length:days}, (_, index) => ({
      label:`day-${index}`,createdCount:index === 0 ? 3 : 0,completedCount:index === 1 ? 2 : 0,
    }));
    data.events.byWeek = [{label:'2026-11-02',createdCount:3,completedCount:2}];
    const {dashboard} = mountDashboard(t, data);
    const activity = dashboard.element.querySelectorAll('.execution-chart')[1];
    const chart = activity.querySelector('.ag-vt-chart');
    if (days === 120) {
      assert.match(chart.textContent, /События по дням UTC/);
      assert.match(chart.textContent, /day-119/);
      assert.doesNotMatch(activity.textContent, /Для периода длиннее 120 дней показаны/);
    } else {
      assert.match(chart.textContent, /Недельные суммы дневных значений/);
      assert.match(chart.textContent, /2026-11-02/);
      assert.doesNotMatch(chart.textContent, /day-0/);
      assert.match(activity.textContent, /Одна карточка может учитываться в разные дни/);
    }
  });
}
