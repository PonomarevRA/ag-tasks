import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {mountWorkChart} from '../web/work-chart.js';
import {renderWorkAnalytics} from '../web/work-analytics.js';

test('analytics binds filters, labels the chart and opens the selected task', async () => {
  const dom = new JSDOM('<main></main>');
  const {document, Option, Event} = dom.window;
  Object.assign(globalThis, {document, Option});
  const main = document.querySelector('main');
  const reads = [];
  const opened = [];
  const filters = {start:'2026-11-01',end:'2026-11-01',q:'работа'};
  const data = {
    totalMs:2700, taskCount:1, projectCount:1, sessionCount:2,
    taskDurationPercentiles:{p50Ms:2700, p90Ms:2700},
    projects:[{id:'p',name:'Проект'}], kinds:['task'], nextOffset:null,
    byDay:[{label:'2026-11-01',elapsedMs:2700}],
    byProject:[{id:'p',label:'Проект',elapsedMs:2700}], byKind:[{id:'task',elapsedMs:2700}],
    tasks:[{id:'issue',key:'T-1',title:'<em>работа</em>',project:'Проект',kind:'task',elapsedMs:2700,sessionCount:2}],
  };
  const execution = {
    asOf:'2026-11-04T10:00:00Z',
    snapshot:{totalCount:1,activeSessionCount:0,reviewCount:0,attentionCount:0,openProblemCount:0,statuses:[{id:'ready',count:1}],attention:[],attentionLimit:10},
    events:{byDay:[{label:'2026-11-01',createdCount:1,completedCount:0}],byWeek:[],createdCount:1,completedUniqueCount:0},
  };
  const empty = {...data, totalMs:0, taskCount:0, taskDurationPercentiles:{p50Ms:null, p90Ms:null}};
  const dispose = renderWorkAnalytics(main, {
    request:async path => {
      const params = new URL(path, 'http://localhost').searchParams;
      if (path.startsWith('/api/v1/analytics/execution')) return execution;
      reads.push(params);
      return params.get('spaceId') ? empty : data;
    },
    openIssue:async id => { opened.push(id); }, filters,
  });
  await new Promise(setImmediate);
  assert.equal(reads[0].get('q'), 'работа');
  assert.equal(main.querySelectorAll('.work-chart .ag-vt-chart__marks rect').length, 2);
  assert.match(main.querySelector('.ag-vt-chart__data').textContent, /Данные графика/);
  assert.match(main.querySelector('.work-table tbody').textContent, /<em>работа<\/em>/);
  assert.equal(main.querySelector('.work-table tbody em'), null);
  const percentiles = main.querySelector('.work-chart');
  assert.match(percentiles.textContent, /P50 · медиана/);
  assert.match(percentiles.textContent, /P90/);
  assert.match(percentiles.textContent, /Карточек с учтённым временем: 1/);
  main.querySelector('.work-table tbody button').click();
  assert.deepEqual(opened, ['issue']);
  main.querySelector('select').value = 'p';
  main.querySelector('form').dispatchEvent(new Event('submit', {cancelable:true}));
  await new Promise(setImmediate);
  assert.equal(reads[1].get('spaceId'), 'p');
  assert.equal(filters.spaceId, 'p');
  assert.equal(main.querySelector('.work-chart'), null);
  assert.match(main.textContent, /Нет учтённого времени/);
  dispose();
  dom.window.close();
});

test('chart settings convert units, preserve options and constrain categorical line plots', () => {
  const dom = new JSDOM('<main></main>', {url:'http://localhost', pretendToBeVisual:true});
  Object.assign(globalThis, {document:dom.window.document, Option:dom.window.Option});
  const bucket = {label:'Проект',elapsedMs:600_000,p50Ms:60_000,p90Ms:180_000,meanMs:120_000,taskCount:5};
  const data = {
    totalMs:600_000,taskCount:5,meanTaskMs:120_000,
    taskDurationPercentiles:{p50Ms:60_000,p90Ms:180_000},
    byDay:Array.from({length:121}, (_,i)=>({...bucket,label:String(i)})),
    byWeek:[{...bucket,label:'2026-11-02'}],byProject:[bucket],byKind:[{...bucket,id:'task'}],
  };
  const chart = mountWorkChart(data);
  document.querySelector('main').append(chart.element);
  const change = (key,value) => {
    const select = chart.element.querySelector('[data-chart-option="'+key+'"]');
    select.value=value;
    select.dispatchEvent(new dom.window.Event('change'));
  };
  assert.match(chart.element.querySelector('.ag-vt-chart__readout')?.textContent || chart.element.textContent,/1.*3/);
  change('unit','seconds');
  assert.match(chart.element.textContent,/P50 · медиана 60/);
  change('group','day');
  assert.equal(chart.element.querySelector('[data-chart-option=group]').value,'week');
  assert.match(chart.element.textContent,/длиннее 120 дней/);
  change('type','line');
  change('group','project');
  assert.equal(chart.element.querySelector('[data-chart-option=type]').value,'bar');
  assert.equal(chart.element.querySelector('option[value=line]').disabled,true);
  const saved=JSON.parse(dom.window.localStorage.getItem('ag-tasks.work-chart.v1'));
  assert.equal(saved.unit,'seconds');
  change('metric','meanMs');
  assert.match(chart.element.textContent,/Среднее на задачу 120/);
  chart.dispose();
  assert.equal(chart.element.querySelector('.ag-vt-chart'),null);
  dom.window.close();
});
