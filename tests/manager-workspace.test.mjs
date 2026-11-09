import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {renderManagerWorkspace} from '../web/manager-workspace.js';

/** Supply explicit server facts; client tests never recompute or invent manager metrics. */
function managerData(title = '<img src=x onerror=alert(1)>') {
  const common = {kind:'task',spaceId:'p',project:'Project <one>',priority:'normal',
    lastActivityAt:'2026-11-05T12:00:00Z',elapsedMs:1200,active:false,activeAgent:null,
    open:true,overdue:false,dueSoon:false,unassigned:false,attention:false,reasons:[]};
  return {
    asOf:'2026-11-05T12:00:00Z',
    summary:{totalCount:7,openCount:5,overdueCount:1,dueSoonCount:2,unassignedCount:2,
      activeCount:1,attentionCount:2,reviewCount:1},
    facets:{projects:[{id:'p',label:'Project <one>'}],assignees:[{id:'Alice',label:'Alice'}],
      kinds:['task','rnd','review'],statuses:['ready','blocked','in_review','in_progress'],priorities:['normal','urgent']},
    queues:[{id:'open',count:5},{id:'attention',count:2},{id:'unassigned',count:2},
      {id:'overdue',count:1},{id:'review',count:1},{id:'all',count:7}],
    byProject:[{id:'p',label:'Project <one>',openCount:5,activeCount:1,overdueCount:1}],
    byAssignee:[{id:'Alice',label:'Alice',openCount:3,activeCount:1,overdueCount:0},
      {id:'',label:'Без ответственного',openCount:2,activeCount:0,overdueCount:1}],
    items:[
      {...common,id:'late',key:'TASK-1',title,status:'blocked',assignee:null,priority:'urgent',dueDate:'2026-11-04',
        overdue:true,unassigned:true,attention:true,reasons:['blocked','overdue']},
      {...common,id:'today',key:'TASK-2',title:'Today task',status:'ready',assignee:'Alice',dueDate:'2026-11-05',dueSoon:true},
      {...common,id:'review',key:'TASK-3',title:'Review task',status:'in_review',assignee:'Alice',dueDate:null,
        attention:true,reasons:['in_review']},
      {...common,id:'active',key:'TASK-4',title:'Declared active',status:'in_progress',assignee:'Alice',dueDate:'2026-11-12',
        dueSoon:true,active:true,activeAgent:'External runner'},
      {...common,id:'unassigned',key:'TASK-5',title:'No owner',status:'ready',assignee:null,dueDate:null,unassigned:true},
    ],
    totalCount:5,hasMore:false,nextOffset:null,
  };
}

/** Exercise the existing analytical view with a legitimate zero-effort inventory. */
function analyticsData() {
  return {
    work:{totalMs:0,taskCount:0,projectCount:0,sessionCount:0,projects:[{id:'p',name:'Project'}],kinds:['task'],
      tasks:[],byDay:[],byProject:[],byKind:[],nextOffset:null,taskDurationPercentiles:{p50Ms:null,p90Ms:null}},
    execution:{asOf:'2026-11-05T12:00:00Z',snapshot:{totalCount:2,activeSessionCount:0,reviewCount:0,
      attentionCount:0,openProblemCount:0,statuses:[{id:'ready',count:2}],attention:[],attentionLimit:10},
      events:{byDay:[{label:'2026-11-05',createdCount:2,completedCount:0}],byWeek:[],createdCount:2,completedUniqueCount:0}},
  };
}

/** Flush actual response promises without advancing the owned thirty-second polling clock. */
const settled = () => new Promise(setImmediate);

/** Mount real product modules in a disposable DOM with only polling timers intercepted. */
function mount(t, {request,filters={}} = {}) {
  const dom = new JSDOM('<main></main><button id="outside">Другой раздел</button>', {
    url:'http://localhost',pretendToBeVisual:true,
  });
  const previous = {document:globalThis.document,Option:globalThis.Option};
  Object.assign(globalThis,{document:dom.window.document,Option:dom.window.Option});
  const {document,Event,KeyboardEvent}=dom.window;
  const clocks = new Map();
  let sequence = 0;
  const setTimeout = dom.window.setTimeout.bind(dom.window);
  const clearTimeout = dom.window.clearTimeout.bind(dom.window);
  dom.window.setTimeout = (fn,delay) => {
    if (delay !== 30_000) return setTimeout(fn,delay);
    const id = 900_000 + ++sequence;
    clocks.set(id,fn);
    return id;
  };
  dom.window.clearTimeout = id => {if (!clocks.delete(id)) clearTimeout(id);};
  const calls = [];
  const opened = [];
  const pair = analyticsData();
  const read = request || (async path => path.includes('/management?') ? managerData()
    : path.includes('/execution?') ? pair.execution : pair.work);
  const main = document.querySelector('main');
  const dispose = renderManagerWorkspace(main,{filters,openIssue:id=>opened.push(id),request:path=>{
    calls.push(path);
    return read(path);
  }});
  t.after(() => {dispose();dom.window.close();Object.assign(globalThis,previous);});
  return {
    document,Event,KeyboardEvent,main,clocks,calls,opened,dispose,filters,
    tick() {
      const entry = clocks.entries().next().value;
      assert.ok(entry,'A polling timer must be scheduled');
      clocks.delete(entry[0]);
      entry[1]();
    },
    button(label) {return [...main.querySelectorAll('button')].find(node=>node.textContent===label);},
    submit() {main.querySelector('form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}));},
  };
}

test('manager opens by default with actionable table before charts and literal task data',async t=>{
  const f = mount(t);
  await settled();
  const url = new URL(f.calls[0],'http://localhost');
  assert.equal(url.pathname,'/api/v1/analytics/management');
  assert.equal(url.searchParams.get('kind'),'task');
  assert.equal(url.searchParams.get('queue'),'open');
  assert.equal(f.button('Контроль выполнения').getAttribute('aria-pressed'),'true');
  assert.equal(f.button('Аналитика времени').getAttribute('aria-pressed'),'false');
  const table = f.main.querySelector('.manager-table');
  const charts = f.main.querySelector('.manager-chart-grid');
  assert.ok(table.compareDocumentPosition(charts) & f.document.defaultView.Node.DOCUMENT_POSITION_FOLLOWING);
  assert.equal(table.querySelectorAll('tbody tr').length,5);
  assert.equal(table.querySelector('img'),null);
  assert.match(table.textContent,/<img src=x onerror=alert\(1\)>/);
  assert.match(table.textContent,/04\.11\.2026/);
  assert.match(table.textContent,/Не задан/);
  assert.match(table.textContent,/External runner/);
  assert.equal(table.querySelectorAll('thead th[scope=col]').length,7);
  assert.equal(table.querySelectorAll('tbody th[scope=row]').length,5);
  assert.equal(charts.querySelectorAll('.ag-vt-chart__plot > svg').length,2);
  const info = f.main.querySelector('button[aria-label="Пояснение: Открытые"]');
  info.click();
  assert.equal(f.document.activeElement,info);
  assert.equal(info.getAttribute('aria-expanded'),'true');
  info.dispatchEvent(new f.KeyboardEvent('keydown',{key:'Escape',bubbles:true,cancelable:true}));
  assert.equal(info.getAttribute('aria-expanded'),'false');
  table.querySelector('tbody button').click();
  assert.deepEqual(f.opened,['late']);
});

test('queue requests preserve full-scope counters while the response changes only the selected list',async t=>{
  const f = mount(t,{request:async path=>{
    const data = managerData();
    if (new URL(path,'http://localhost').searchParams.get('queue')==='overdue') {
      data.items = data.items.slice(0,1);
      data.totalCount = 1;
    }
    return data;
  }});
  await settled();
  const kpis = f.main.querySelector('.manager-kpis').textContent;
  f.button('Просроченные · 1').click();
  await settled();
  assert.equal(new URL(f.calls.at(-1),'http://localhost').searchParams.get('queue'),'overdue');
  assert.equal(f.main.querySelector('.manager-table tbody').rows.length,1);
  assert.match(f.main.querySelector('.manager-table caption').textContent,/Просроченные.*1/);
  assert.equal(f.main.querySelector('.manager-kpis').textContent,kpis);
  assert.equal(f.button('Просроченные · 1').getAttribute('aria-pressed'),'true');
  assert.match(f.main.querySelector('.manager-report').textContent,/Показатели и графики охватывают всю выборку фильтров/);
});

test('manager failure retains the successful nodes and timestamp until explicit retry succeeds',async t=>{
  let failing = false;
  const f = mount(t,{request:async()=>{
    if (failing) throw Error('Temporary fixture outage');
    return managerData();
  }});
  await settled();
  const original = f.main.querySelector('.manager-results');
  const observed = f.main.querySelector('.manager-refresh-state').textContent;
  failing = true;
  f.button('Обновить контроль').click();
  await settled();
  assert.equal(f.main.querySelector('.manager-results'),original);
  assert.equal(f.main.querySelector('.manager-refresh-state').textContent,observed);
  assert.match(f.main.querySelector('.manager-load-status').textContent,/предыдущий снимок.*Temporary fixture outage/);
  failing = false;
  f.button('Обновить контроль').click();
  await settled();
  assert.notEqual(f.main.querySelector('.manager-results'),original);
  assert.equal(f.main.querySelector('.manager-load-status').textContent,'');
});

test('late responses from previous filters cannot replace a newer manager result',async t=>{
  const pending = [];
  const f = mount(t,{request:path=>new Promise(resolve=>pending.push({path,resolve}))});
  f.main.querySelector('input[type=search]').value = 'current filter';
  f.submit();
  assert.equal(pending.length,2);
  assert.equal(new URL(pending[1].path,'http://localhost').searchParams.get('q'),'current filter');
  pending[1].resolve(managerData('Newest result'));
  await settled();
  const current = f.main.querySelector('.manager-results');
  pending[0].resolve(managerData('Obsolete result'));
  await settled();
  assert.equal(f.main.querySelector('.manager-results'),current);
  assert.match(f.main.querySelector('.manager-table').textContent,/Newest result/);
  assert.doesNotMatch(f.main.querySelector('.manager-table').textContent,/Obsolete result/);
});

test('unapplied manager drafts prevent an automatic request and polling is released on disposal',async t=>{
  const f = mount(t);
  await settled();
  assert.equal(f.clocks.size,1);
  const query = f.main.querySelector('input[type=search]');
  query.value = 'unapplied draft';
  f.tick();
  await settled();
  assert.equal(f.calls.length,1);
  assert.equal(query.value,'unapplied draft');
  assert.match(f.main.querySelector('.manager-refresh-state').textContent,/Изменены фильтры/);
  query.value = '';
  f.tick();
  await settled();
  assert.equal(f.calls.length,2);
  f.dispose();
  assert.equal(f.clocks.size,0);
});

for (const interaction of ['help','filter','hidden','disabled','dialog']) {
  test(`in-flight manager polling cannot replace a newly active ${interaction}`,async t=>{
    let delayed = false;
    const pending = [];
    const f = mount(t,{request:()=>delayed ? new Promise(resolve=>pending.push(resolve)) : Promise.resolve(managerData())});
    await settled();
    const original = f.main.querySelector('.manager-results');
    delayed = true;
    f.tick();
    assert.equal(pending.length,1);
    let focused;
    if (interaction==='help') {
      focused = f.main.querySelector('button[aria-label="Пояснение: Открытые"]');
      focused.click();
    } else if (interaction==='filter') {
      focused = f.main.querySelector('input[type=search]');
      focused.value = 'new unapplied draft';
      focused.focus();
    } else if (interaction==='disabled') {
      focused = f.main.querySelector('input[type=checkbox]');
      focused.checked = false;
      focused.dispatchEvent(new f.Event('change'));
    } else if (interaction==='dialog') {
      const dialog = f.document.createElement('dialog');
      dialog.setAttribute('open','');
      f.document.body.append(dialog);
    } else Object.defineProperty(f.document,'hidden',{value:true,configurable:true});
    pending[0](managerData('Do not replace'));
    await settled();
    assert.equal(f.main.querySelector('.manager-results'),original);
    assert.doesNotMatch(f.main.querySelector('.manager-table').textContent,/Do not replace/);
    if (interaction==='help') assert.equal(focused.getAttribute('aria-expanded'),'true');
    if (interaction==='filter') assert.equal(focused.value,'new unapplied draft');
  });
}

test('switching modes has independent filters and releases old polling/help before historical rendering',async t=>{
  const filters = {q:'historical',manager:{applied:{q:'operational',assignee:'Alice'}}};
  const f = mount(t,{filters});
  await settled();
  const help = f.main.querySelector('button[aria-label="Пояснение: Открытые"]');
  help.click();
  const panel = f.document.getElementById(help.getAttribute('aria-controls'));
  assert.equal(panel.hidden,false);
  f.button('Аналитика времени').click();
  await settled();
  assert.equal(panel.hidden,true);
  assert.equal(f.main.querySelector('.manager-control'),null);
  assert.ok(f.main.querySelector('.execution-dashboard'));
  assert.equal(f.clocks.size,1);
  const analytical = f.calls.filter(path=>!path.includes('/management?'));
  assert.equal(analytical.length,2);
  for (const path of analytical) {
    const query = new URL(path,'http://localhost').searchParams;
    assert.equal(query.get('q'),'historical');
    assert.equal(query.has('assignee'),false);
  }
  f.button('Контроль выполнения').click();
  await settled();
  assert.equal(f.main.querySelector('input[type=search]').value,'operational');
  assert.equal(f.main.querySelector('select[name=assignee]').value,'Alice');
  assert.equal(f.clocks.size,1);
  f.dispose();
  assert.equal(f.clocks.size,0);
});

test('late disposed manager response cannot reappear in the analytical mode',async t=>{
  let resolveManager;
  const pair = analyticsData();
  const f = mount(t,{request:path=>path.includes('/management?')
    ? new Promise(resolve=>{resolveManager=resolve;})
    : Promise.resolve(path.includes('/execution?')?pair.execution:pair.work)});
  f.button('Аналитика времени').click();
  await settled();
  resolveManager(managerData('Disposed response'));
  await settled();
  assert.equal(f.main.querySelector('.manager-control'),null);
  assert.ok(f.main.querySelector('.execution-dashboard'));
  assert.doesNotMatch(f.main.textContent,/Disposed response/);
  assert.equal(f.clocks.size,1);
});

test('unsaved manager filters survive a mode switch without being silently applied',async t=>{
  const filters = {q:'historical',manager:{applied:{q:'applied scope',assignee:'Alice'}}};
  const f = mount(t,{filters});
  await settled();
  const query = f.main.querySelector('input[type=search]');
  query.value = 'unsaved manager draft';
  query.dispatchEvent(new f.Event('input',{bubbles:true}));
  const priority = f.main.querySelector('select[name=priority]');
  priority.value = 'urgent';
  priority.dispatchEvent(new f.Event('change',{bubbles:true}));
  f.button('Аналитика времени').click();
  await settled();
  f.button('Контроль выполнения').click();
  await settled();
  assert.equal(f.main.querySelector('input[type=search]').value,'unsaved manager draft');
  assert.equal(f.main.querySelector('select[name=priority]').value,'urgent');
  const restoredRequest = new URL(f.calls.at(-1),'http://localhost').searchParams;
  assert.equal(restoredRequest.get('q'),'applied scope');
  assert.equal(restoredRequest.get('priority'),'');
  assert.equal(filters.manager.applied.q,'applied scope');
  assert.match(f.main.querySelector('.manager-refresh-state').textContent,/(Изменены фильтры|Фильтры изменены).*Применить/i);
  f.submit();
  await settled();
  const appliedRequest = new URL(f.calls.at(-1),'http://localhost').searchParams;
  assert.equal(appliedRequest.get('q'),'unsaved manager draft');
  assert.equal(appliedRequest.get('priority'),'urgent');
  assert.equal(filters.manager.applied.q,'unsaved manager draft');
});

test('changing a queue preserves unapplied manager drafts until explicit Apply',async t=>{
  const filters = {manager:{applied:{q:'base query',priority:'normal'}}};
  const f = mount(t,{filters,request:async path=>{
    const data = managerData();
    if (new URL(path,'http://localhost').searchParams.get('queue')==='overdue') {
      data.items = data.items.slice(0,1);
      data.totalCount = 1;
    }
    return data;
  }});
  await settled();
  const beforeCounts = f.main.querySelector('.manager-kpis').textContent;
  const query = f.main.querySelector('input[type=search]');
  query.value = 'unapplied queue draft';
  query.dispatchEvent(new f.Event('input',{bubbles:true}));
  const priority = f.main.querySelector('select[name=priority]');
  priority.value = 'urgent';
  priority.dispatchEvent(new f.Event('change',{bubbles:true}));
  f.button('Просроченные · 1').click();
  await settled();
  const queued = new URL(f.calls.at(-1),'http://localhost').searchParams;
  assert.equal(queued.get('queue'),'overdue');
  assert.equal(queued.get('q'),'base query');
  assert.equal(queued.get('priority'),'normal');
  assert.equal(filters.manager.applied.queue,'overdue');
  assert.equal(filters.manager.applied.q,'base query');
  assert.equal(filters.manager.applied.priority,'normal');
  assert.equal(f.main.querySelector('input[type=search]').value,'unapplied queue draft');
  assert.equal(f.main.querySelector('select[name=priority]').value,'urgent');
  assert.equal(f.main.querySelector('.manager-kpis').textContent,beforeCounts);
  assert.match(f.main.querySelector('.manager-refresh-state').textContent,
    /(Изменены фильтры|Фильтры изменены).*Применить/i);
  assert.equal(f.main.querySelector('.manager-table tbody').rows.length,1);
  f.submit();
  await settled();
  const applied = new URL(f.calls.at(-1),'http://localhost').searchParams;
  assert.equal(applied.get('queue'),'overdue');
  assert.equal(applied.get('q'),'unapplied queue draft');
  assert.equal(applied.get('priority'),'urgent');
  assert.equal(filters.manager.applied.q,'unapplied queue draft');
  assert.equal(filters.manager.applied.priority,'urgent');
});
