import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {JSDOM} from 'jsdom';
import {groupTaskGraph} from '../web/task-graph.js';
import {formatElapsed, visibleIssues} from '../web/task-format.js';

/** Supply actual contract fields without inventing a running session or pre-approved result. */
function issue(id, values = {}) {
  return {id,key:`TASK-${id}`,title:`Task ${id}`,description:'',spaceId:'default',parentId:null,
    kind:'task',intendedRole:null,status:'backlog',priority:'normal',assignee:null,repoId:null,
    dor:[],dod:[],definitionRevision:0,resultGeneration:0,currentResult:null,results:[],resultsCount:0,
    work:{state:'never_started',elapsedMs:0,activeSession:null},criticReviewId:null,criticReview:null,
    archivedAt:null,deletedAt:null,externalRefs:[],links:[],comments:[],history:[],...values};
}

test('task graph caps nodes, groups actual ancestry and leaves unknown parents/cycles explicit', () => {
  const root = issue('root');
  const child = issue('child', {parentId:'root'});
  const missing = issue('missing', {parentId:'unloaded'});
  const model = groupTaskGraph([root, child, missing]);
  assert.equal(model.groups.length, 2);
  assert.deepEqual(model.groups[0].tasks.map(entry => entry.depth), [0,1]);
  assert.deepEqual(model.edges, [{source:'root',target:'child',kind:'parent'}]);
  assert.equal(model.missingParents, 1);
  const cycle = groupTaskGraph([issue('a',{parentId:'b'}),issue('b',{parentId:'a'})]);
  assert.equal(cycle.cycles, 2);
  assert.equal(cycle.groups.length, 2);
  const bounded = groupTaskGraph(Array.from({length:201}, (_,i) => issue(String(i))));
  assert.equal(bounded.tasks.length, 200);
  assert.equal(bounded.omitted, 1);
});

test('actual duration has no extrapolation and archive filters never include deleted or active records', () => {
  assert.equal(formatElapsed(3661000), '01:01:01');
  assert.equal(formatElapsed(0), '00:00:00');
  assert.equal(formatElapsed(undefined), 'Неизвестно');
  assert.equal(formatElapsed(-1), 'Неизвестно');
  const rows = [issue('active'),issue('old',{archivedAt:'2026-10-22T00:00:00Z'}),issue('gone',{archivedAt:'x',deletedAt:'x'})];
  assert.deepEqual(visibleIssues(rows).map(row => row.id), ['active']);
  assert.deepEqual(visibleIssues(rows,{archived:true}).map(row => row.id), ['old']);
});

let generation = 0;
/** Load the real application module and its actual HTML against a bounded in-memory HTTP fixture. */
async function application(t, initial, override, options = {}) {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const dom = new JSDOM(html, {url:'http://127.0.0.1:8895',pretendToBeVisual:true});
  const window = dom.window;
  window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  window.HTMLDialogElement.prototype.close = function () {
    this.open = false;
    const deliver = () => this.dispatchEvent(new window.Event('close'));
    if (options.queuedClose) setTimeout(deliver, 0); else deliver();
  };
  window.HTMLElement.prototype.getBoundingClientRect = function () {
    const x = this.dataset.issueId === 'child' ? 400 : 100;
    return {left:x,top:100,right:x+235,bottom:220,width:235,height:120};
  };
  const globals = {
    window, document:window.document, Option:window.Option, FormData:window.FormData,sessionStorage:window.sessionStorage,
    requestAnimationFrame:window.requestAnimationFrame.bind(window),
    cancelAnimationFrame:window.cancelAnimationFrame.bind(window),
    ResizeObserver:class { observe() {} disconnect() {} },
  };
  const before = new Map();
  for (const [key, value] of Object.entries(globals)) { before.set(key,globalThis[key]); globalThis[key]=value; }
  let refreshCaptured=false;
  const previousSetInterval = globalThis.setInterval;
  if (options.captureRefresh) globalThis.setInterval = (callback, delay, ...args) => {
    if (delay === 10000) {
      if(!refreshCaptured) {refreshCaptured=true;options.captureRefresh(callback);}
      return -1;
    }
    return previousSetInterval(callback, delay, ...args);
  };
  const previousFetch = globalThis.fetch;
  const records = initial.map(row => structuredClone(row));
  const calls = [];
  const spaces = [{id:'default',name:'Main'},{id:'beta',name:'Beta'}];
  const response = value => ({ok:true,json:async () => structuredClone(value)});
  /** Route only the frozen API surface and record each mutation for independent assertions. */
  function respond(path, init) {
    const url = new URL(path, window.location.origin);
    const method = init.method;
    const data = init.body ? JSON.parse(init.body) : null;
    const segments = url.pathname.split('/');
    if (url.pathname === '/api/v1/ui-session') return response({token:'fixture-token'});
    if (url.pathname === '/api/v1/codex-sessions') return response({items:[],available:true,truncated:false});
    if (url.pathname === '/api/v1/sessions') return response({items:[],hasMore:false,nextCursor:null});
    if (url.pathname === '/api/v1/commands') {
      assert.ok(data.commandId && data.clientId);
      if (data.operation === 'issue.create') return respond('/api/v1/issues',{method:'POST',body:JSON.stringify(data.payload)});
      if (data.operation === 'issue.update') return respond('/api/v1/issues/'+data.payload.issueId,{method:'PATCH',body:JSON.stringify(data.payload.changes)});
      throw new Error('Uncovered domain command: '+data.operation);
    }
    if (url.pathname === '/api/v1/spaces') {
      if (method === 'POST') { const created={id:'new-space',name:data.name}; spaces.push(created); return response(created); }
      return response({items:spaces});
    }
    if (url.pathname === '/api/v1/repositories') return response({items:[]});
    if (url.pathname === '/api/v1/issues/graph') {
      const nodes = records.filter(row => row.spaceId === url.searchParams.get('spaceId') && !row.archivedAt && !row.deletedAt);
      return response({nodes,edges:[{from:'root',to:'child',kind:'parent'},{from:'child',to:'root',kind:'related'}],stubs:[{id:'outside',key:'TASK-outside',title:'Actual outside-page task'}],nodeCount:nodes.length+1,edgeCount:3,truncated:true});
    }
    if (url.pathname === '/api/v1/issues') {
      if (method === 'POST') {
        const created = issue('created', data);
        records.push(created);
        return response(created);
      }
      const items = records.filter(row => !row.deletedAt && row.spaceId === url.searchParams.get('spaceId') && (!row.archivedAt || url.searchParams.get('includeArchived') === 'true') && (!url.searchParams.has('parentId') || row.parentId === url.searchParams.get('parentId')));
      return response({items,hasMore:false,nextOffset:items.length});
    }
    const record = records.find(row => row.id === segments[4]);
    if (record) {
      if (segments[5] === 'archive') record.archivedAt = '2026-10-22T01:00:00Z';
      if (segments[5] === 'restore') record.archivedAt = null;
      if (method === 'DELETE') { record.deletedAt = '2026-10-22T01:00:00Z'; return response({id:record.id,deleted:true}); }
      if (method === 'PATCH') Object.assign(record, data);
      return response(record);
    }
    throw new Error(`Uncovered fixture route: ${method} ${path}`);
  }
  globalThis.fetch = async (path, init) => {
    calls.push({path,method:init.method,headers:init.headers,body:init.body ? JSON.parse(init.body) : null});
    return override?.(path,init,respond,response) ?? respond(path,init);
  };
  t.after(() => {
    window.dispatchEvent(new window.Event('pagehide'));
    dom.window.close();
    globalThis.fetch = previousFetch;
    globalThis.setInterval = previousSetInterval;
    for (const [key,value] of before) {
      if (value === undefined) delete globalThis[key]; else globalThis[key]=value;
    }
  });
  if (options.includeHistory) window.document.querySelector('#include-history').checked = true;
  await import(`../web/app.js?test=${++generation}`);
  await until(() => window.document.querySelector('[data-view="sessions"]').getAttribute('aria-current')==='page');
  assert.equal(window.document.querySelector('#heading').textContent,'Агенты и сессии');
  // These regression scenarios explicitly exercise task views after the new session landing page.
  window.document.querySelector('[data-view="graph"]').click();
  await until(() => window.document.querySelectorAll('.task-graph-card').length > 0);
  return {window,document:window.document,calls,records};
}

/** Wait for visible application effects, failing with a finite deadline rather than sleeping blindly. */
async function until(predicate) {
  for (let attempt=0; attempt<100; attempt++) {
    if (predicate()) return;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  assert.fail('Application effect did not complete within 500ms');
}

test('real graph pipeline shows bounded coverage, inert hostile content and only directed dependency arrows', async t => {
  const app = await application(t,[issue('root',{title:'<img src=x onerror=bad()>',intendedRole:'developer'}),issue('child',{parentId:'root'})]);
  await until(() => app.document.querySelector('path[data-kind=parent]'));
  assert.equal(app.document.querySelectorAll('.task-graph-card').length, 2);
  assert.equal(app.document.querySelectorAll('img').length, 0);
  assert.match(app.document.querySelector('#coverage').textContent,/Ответ сокращён сервером/);
  assert.match(app.document.querySelector('.graph-stubs').textContent,/Actual outside-page task/);
  assert.ok(app.document.querySelector('path[data-kind=parent]').hasAttribute('marker-end'));
  assert.equal(app.document.querySelector('path[data-kind=related]').hasAttribute('marker-end'),false);
  assert.equal(app.calls.some(call => /work|results|review-report/.test(call.path)),false);
});

test('task details show server time and declared result/critic evidence without any agent session writes', async t => {
  const root = issue('root',{definitionRevision:1,resultGeneration:1,intendedRole:'developer',work:{state:'expired',elapsedMs:3661000,activeSession:null},dor:[{id:'ready',text:'Ready input',passed:true,evidence:'Input supplied'}],currentResult:{summary:'<script>unsafe</script>',changes:['Actual change'],validation:['Caller says checks passed'],limitations:'No browser proof',artifacts:[],generation:1,definitionRevision:1},criticReview:{verdict:'pass',summary:'Declared independent review',findings:[],evidence:'Read source',generation:1,definitionRevision:1,reviewedHead:null,agentId:'critic-1'}});
  const app = await application(t,[root]);
  app.document.querySelector('.task-graph-card').click();
  await until(() => app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  const text = app.document.querySelector('#detail-body').textContent;
  assert.match(text,/01:01:01/);
  assert.match(text,/Срок сессии истёк/);
  assert.match(text,/Критик: заявлен PASS/);
  assert.match(text,/Caller says checks passed/);
  assert.equal(app.document.querySelector('#detail-body script'),null);
  assert.ok([...app.document.querySelectorAll('#detail-body option')].find(option => option.value==='in_progress').disabled);
  assert.equal(app.calls.some(call => call.method !== 'GET'),false);
});

test('criteria edit submits drafts and evidence explicitly; changing text clears the declared pass', async t => {
  const app = await application(t,[issue('root')]);
  app.document.querySelector('#create').click();
  const form = app.document.querySelector('#issue-form');
  form.elements.title.value = 'New draft';
  form.elements.intendedRole.value = 'developer';
  app.document.querySelector('#dor-editor>button').click();
  const row = app.document.querySelector('.criterion-editor');
  row.querySelector('[data-field=text]').value = 'Input supplied';
  row.querySelector('[data-field=passed]').checked = true;
  row.querySelector('[data-field=evidence]').value = 'Reference available';
  row.querySelector('[data-field=text]').dispatchEvent(new app.window.Event('input'));
  assert.equal(row.querySelector('[data-field=passed]').checked,false);
  row.querySelector('[data-field=passed]').checked = true;
  form.dispatchEvent(new app.window.Event('submit',{cancelable:true,bubbles:true}));
  await until(() => app.calls.some(call => call.body?.operation==='issue.create'));
  const create = app.calls.find(call => call.body?.operation==='issue.create').body.payload;
  assert.equal(create.spaceId,'default');
  assert.equal(create.intendedRole,'developer');
  assert.equal(create.dor[0].evidence,'Reference available');
  assert.equal(create.dor[0].passed,true);
  assert.equal(create.kind,'task');
  assert.equal(create.estimatedComplexity,null);
  await until(() => app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
});

test('terminal archive, restore and tombstone require explicit UI actions and delete confirmation', async t => {
  const app = await application(t,[issue('root',{status:'cancelled'})],undefined,{includeHistory:true});
  const clickText = text => [...app.document.querySelectorAll('#detail-body button')].find(control=>control.textContent===text).click();
  app.document.querySelector('.task-graph-card').click();
  await until(() => app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  assert.ok([...app.document.querySelectorAll('#detail-body button')].find(control=>control.textContent==='＋ Подзадача').disabled);
  clickText('В архив');
  await until(() => [...app.document.querySelectorAll('#detail-body button')].some(control=>control.textContent==='Восстановить из архива'));
  clickText('Восстановить из архива');
  await until(() => [...app.document.querySelectorAll('#detail-body button')].some(control=>control.textContent==='В архив'));
  clickText('В архив');
  await until(() => [...app.document.querySelectorAll('#detail-body button')].some(control=>control.textContent==='Удалить…'));
  clickText('Удалить…');
  const form = app.document.querySelector('#delete-form');
  form.dispatchEvent(new app.window.Event('submit',{cancelable:true}));
  assert.equal(app.calls.some(call=>call.method==='DELETE'),false);
  app.document.querySelector('#delete-consent').checked=true;
  form.dispatchEvent(new app.window.Event('submit',{cancelable:true}));
  await until(() => app.calls.some(call=>call.method==='DELETE'));
  await until(() => !app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  assert.equal(app.calls.find(call=>call.method==='DELETE').headers['X-Ag-Tasks-UI'],'fixture-token');
  assert.ok(app.records[0].deletedAt);
  await until(() => app.document.activeElement === app.document.querySelector('#heading'));
});

test('a delayed previous-space refresh cannot replace a newer selected space', async t => {
  let release, delayed=false;
  const app = await application(t,[issue('root'),issue('beta',{spaceId:'beta',title:'Beta task'})],(path,init,respond) => {
    if (delayed && path.includes('/issues?') && path.includes('spaceId=default')) {
      delayed=false;
      const value=respond(path,init);
      return new Promise(resolve=>{ release=()=>resolve(value); });
    }
  });
  delayed=true;
  app.document.querySelector('#refresh').click();
  await until(()=>release);
  app.document.querySelector('#space-manage').click();
  app.document.querySelector('#space-list [data-space-id="beta"] .space-choice').click();
  await until(()=>app.document.querySelector('.task-graph-card')?.textContent.includes('Beta task'));
  release();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.match(app.document.querySelector('#space-picker-label').textContent,/Beta/);
  assert.match(app.document.querySelector('.task-graph-card').textContent,/Beta task/);
  assert.match(app.document.querySelector('#space-breadcrumb').textContent,/Beta/);
});


test('creating a space survives the asynchronous event lifetime and selects its actual ID', async t => {
  const app=await application(t,[issue('root')]);
  app.document.querySelector('#space-manage').click();
  const form=app.document.querySelector('#space-form');
  form.elements.name.value='New actual space';
  form.dispatchEvent(new app.window.Event('submit',{cancelable:true,bubbles:true}));
  await until(()=>app.document.querySelector('#space-picker-label').textContent==='New actual space');
  assert.equal(app.document.querySelector('#spaces-dialog').open,false);
  assert.equal(app.document.querySelector('#notice').textContent,'');
});

test('a parent outside the loaded list remains the explicit parent when a graph card creates a child', async t => {
  const app=await application(t,[issue('root'),issue('outside')],(path,init,respond,response)=>{
    if (path.includes('/issues?') && !path.includes('parentId=')) return response({items:[issue('root')],hasMore:true,nextOffset:1});
  });
  [...app.document.querySelectorAll('.task-graph-card')].find(card=>card.dataset.issueId==='outside').click();
  await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  [...app.document.querySelectorAll('#detail-body button')].find(control=>control.textContent==='＋ Подзадача').click();
  assert.equal(app.document.querySelector('#issue-form').elements.parentId.value,'outside');
});

test('prior-generation critic PASS remains visible as stale evidence and never as the current acceptance', async t => {
  const app=await application(t,[issue('root',{definitionRevision:2,resultGeneration:2,criticReview:{verdict:'pass',generation:1,definitionRevision:1,summary:'Earlier review',evidence:'Earlier evidence',agentId:'critic-old'}})]);
  app.document.querySelector('.task-graph-card').click();
  await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  const text=app.document.querySelector('#detail-body').textContent;
  assert.match(text,/Устаревшее заключение критика/);
  assert.equal(text.includes('Критик: заявлен PASS'),false);
});


test('failed new-space loading never leaves previous-space cards actionable under the new selector', async t => {
  const app=await application(t,[issue('root')],path=>{
    if (path.includes('spaceId=beta')) throw new Error('fixture unavailable');
  });
  app.document.querySelector('#space-manage').click();
  app.document.querySelector('#space-list [data-space-id="beta"] .space-choice').click();
  await until(()=>app.document.querySelector('#view-load-status').textContent.includes('fixture unavailable'));
  assert.equal(app.document.querySelectorAll('.task-graph-card').length,0);
  assert.match(app.document.querySelector('#content').textContent,/Данные не загружены/);
  assert.equal(app.document.querySelector('#content').inert,false);
});


test('replacing archive actions keeps focus inside the task dialog and closing returns to a reachable card', async t => {
  const app = await application(t,[issue('root',{status:'cancelled'})],undefined,{includeHistory:true});
  const card = app.document.querySelector('.task-graph-card');
  card.focus(); card.click();
  await until(() => app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  const archive = [...app.document.querySelectorAll('#detail-body button')].find(node=>node.textContent==='В архив');
  archive.focus(); archive.click();
  await until(() => [...app.document.querySelectorAll('#detail-body button')].some(node=>node.textContent==='Восстановить из архива'));
  assert.ok(app.document.querySelector('#detail').contains(app.document.activeElement));
  app.document.querySelector('#detail [data-close]').click();
  assert.equal(app.document.activeElement, app.document.querySelector('#heading'));
});

test('closing an unchanged task dialog restores its initiating card', async t => {
  const app = await application(t,[issue('root')]);
  const card = app.document.querySelector('.task-graph-card');
  card.focus(); card.click();
  await until(() => app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  app.document.querySelector('#detail [data-close]').click();
  assert.equal(app.document.activeElement, card);
});


test('code labels and rejected review reasons remain visible in cards, detail and history', async t => {
  const report={verdict:'rework',summary:'Missing empty input handling',findings:['Handle an empty query result'],evidence:'Inspected query.py',agentId:'critic',generation:1,definitionRevision:0,createdAt:'2026-10-22'};
  const app=await application(t,[issue('root',{codeTask:true,languages:['Python','SQL'],systems:['Tasks','Observer'],codeReviewStage:'rework',status:'rework',criticReview:report,resultGeneration:1,reviewReports:[report]})]);
  const card=app.document.querySelector('.task-graph-card');
  assert.match(card.textContent,/Код/); assert.match(card.textContent,/Язык: Python/); assert.match(card.textContent,/Система: Observer/);
  card.click(); await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  const body=app.document.querySelector('#detail-body');
  assert.match(body.textContent,/Ревью кода/); assert.match(body.textContent,/Причины возврата на доработку/);
  assert.match(body.textContent,/Handle an empty query result/);
  const history=body.querySelector('.review-history'); history.open=true;
  assert.match(history.textContent,/Inspected query.py/);
});

test('editor saves only deliberate system-label edits without resending code classification or languages', async t => {
  const app=await application(t,[issue('root',{codeTask:true,languages:['Python','SQL'],systems:['Tasks'],codeReviewStage:'pending'})]);
  app.document.querySelector('.task-graph-card').click(); await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  [...app.document.querySelectorAll('#detail-body button')].find(node=>node.textContent==='Редактировать').click();
  const form=app.document.querySelector('#issue-form');
  assert.equal(form.elements.codeTask.checked,true); assert.equal(form.elements.languages.value,'Python, SQL');
  form.elements.systems.value='Tasks, Observer';
  form.dispatchEvent(new app.window.Event('submit',{bubbles:true,cancelable:true}));
  await until(()=>app.calls.some(call=>call.body?.operation==='issue.update'));
  const body=app.calls.find(call=>call.body?.operation==='issue.update').body.payload.changes;
  assert.deepEqual(body,{systems:['Tasks','Observer']});
  await until(()=>!app.document.querySelector('#editor').open && app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  const saved=app.document.querySelector('#detail-body').textContent;
  assert.match(saved,/Язык: Python/); assert.match(saved,/Язык: SQL/); assert.match(saved,/Система: Observer/);
});

test('task search matches code, languages and systems without interpreting label text', () => {
  const rows=[issue('code',{codeTask:true,languages:['Python','SQL'],systems:['Observer']}),issue('other')];
  for (const query of ['код','Python','Observer']) assert.deepEqual(visibleIssues(rows,{query}).map(row=>row.id),['code']);
});


test('details show captured session roles and child status counts as literal read-only evidence', async () => {
  const {renderTaskWorkflow}=await import('../web/task-workflow.js');
  const dom=new JSDOM('<main></main>');
  const previous=globalThis.document;globalThis.document=dom.window.document;
  try {
    const host=document.querySelector('main');
    renderTaskWorkflow(host,issue('parent',{childProgress:{total:3,byStatus:{in_review:2,in_progress:1}},workHistory:{items:[{agentId:'<img src=x>',intendedRole:'qa',state:'paused',elapsedMs:2000},{agentId:'legacy',intendedRole:null,state:'interrupted',elapsedMs:0}],hasMore:true}}),()=>{});
    assert.match(host.textContent,/На ревью: 2/);
    assert.match(host.textContent,/В работе: 1/);
    assert.match(host.textContent,/Тестировщик/);
    assert.match(host.textContent,/Роль ранее не фиксировалась/);
    assert.equal(host.querySelector('img'),null);
    assert.match(host.textContent,/00:00:02/);
  } finally {globalThis.document=previous;dom.window.close();}
});

test('lifecycle renderer distinguishes implementation from approval without interpreting text as HTML', async () => {
  const dom = new JSDOM('<main></main>');
  globalThis.document = dom.window.document;
  const {renderLifecycle} = await import('../web/task-workflow.js');
  const host = document.querySelector('main');
  renderLifecycle(host, {lifecycle:{phase:'awaiting_user',needsAttention:true}});
  assert.match(host.textContent, /ожидает пользователя/);
  renderLifecycle(host, {lifecycle:{phase:'<img src=x>',needsAttention:true}});
  assert.equal(host.querySelector('img'), null);
  dom.window.close();
});

test('graph cards show derived parent review phase independently of backlog', async t => {
  const app = await application(t, [issue('root',{lifecycle:{phase:'children_review',needsAttention:true}})]);
  const card = document.querySelector('[data-issue-id="root"]');
  assert.match(card.textContent, /Подзадачи ожидают проверки/);
  assert.match(card.textContent, /Бэклог/);
});

test('working views retain pending reviews and pending merge while hiding finished work', () => {
  const rows = [issue('active'),issue('critic',{kind:'review'}),issue('done',{status:'done'}),issue('merge',{status:'done',mergePending:true})];
  assert.deepEqual(visibleIssues(rows).map(x=>x.id), ['active','critic','merge']);
  assert.equal(visibleIssues(rows,{includeHistory:true}).length,4);
  assert.deepEqual(visibleIssues(rows,{status:'done'}).map(x=>x.id),['done','merge']);
});

test('history control reveals service review cards without creating or completing work', async t => {
  const app = await application(t,[issue('root'),issue('critic',{kind:'review',status:'done'})]);
  assert.equal(app.document.querySelectorAll('.task-graph-card').length,1);
  const toggle = app.document.querySelector('#include-history');
  toggle.checked = true;
  toggle.dispatchEvent(new app.window.Event('change'));
  await until(() => app.document.querySelectorAll('.task-graph-card').length === 2);
  assert.equal(app.calls.filter(x => x.method === 'POST').length,0);
});


test('graph summary puts title and real lifecycle before secondary labels and retains one native action', async t => {
  const app = await application(t, [issue('root', {status:'in_review',intendedRole:'developer',codeTask:true,languages:['Python'],systems:['Observer'],codeReviewStage:'in_review',lifecycle:{phase:'awaiting_critic'}})]);
  const card = app.document.querySelector('.task-graph-card');
  assert.equal(card.tagName,'BUTTON');
  const text = card.textContent;
  assert.ok(text.indexOf('Task root') < text.indexOf('ожидает критика'));
  assert.ok(text.indexOf('ожидает критика') < text.indexOf('Язык: Python'));
  assert.equal(card.querySelectorAll('button,a').length,0);
  card.click(); await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  assert.match(app.document.querySelector('#detail-body').textContent,/Роль и фактическая работа/);
});


test('a delayed save never closes or reports errors into a newer editor draft', async t => {
  let release;
  const app = await application(t, [issue('root')], (path, init, respond) => {
    if (path === '/api/v1/commands' && JSON.parse(init.body).operation === 'issue.create') {
      const result = respond(path, init);
      return new Promise(resolve => { release = () => resolve(result); });
    }
  });
  app.document.querySelector('#create').click();
  const form = app.document.querySelector('#issue-form');
  form.elements.title.value = 'Draft A';
  form.dispatchEvent(new app.window.Event('submit', {cancelable:true,bubbles:true}));
  await until(() => release);
  app.document.querySelector('#editor').close();
  app.document.querySelector('#create').click();
  form.elements.title.value = 'Unsaved draft B';
  release();
  await new Promise(resolve => setTimeout(resolve, 30));
  assert.equal(app.document.querySelector('#editor').open, true);
  assert.equal(form.elements.title.value, 'Unsaved draft B');
  assert.equal(form.querySelector('[type=submit]').disabled, false);
  assert.equal(app.document.querySelector('#detail').open, false);
});

test('old save failure cannot attach an error or disable the newer editor', async t => {
  let release;
  const app=await application(t,[issue('root')],(path,init)=>{
    if(path==='/api/v1/commands'&&JSON.parse(init.body).operation==='issue.create')
      return new Promise(resolve=>{release=()=>resolve({ok:false,status:400,json:async()=>({error:{message:'Old draft rejected'}})});});
  });
  app.document.querySelector('#create').click();const form=app.document.querySelector('#issue-form');
  form.elements.title.value='A';form.dispatchEvent(new app.window.Event('submit',{cancelable:true,bubbles:true}));await until(()=>release);
  app.document.querySelector('#editor').close();app.document.querySelector('#create').click();form.elements.title.value='Draft B';release();
  await until(()=>app.document.querySelector('#notice').textContent.includes('Old draft rejected'));
  assert.equal(app.document.querySelector('#editor').open,true);assert.equal(form.elements.title.value,'Draft B');
  assert.equal(form.querySelector('[type=submit]').disabled,false);assert.equal(app.document.querySelector('#editor .error'),null);
});

test('optional editor groups preserve serialization and reveal existing and invalid values', async t => {
  const app=await application(t,[issue('root',{codeTask:true,languages:['python'],assignee:'Agent',estimatedComplexity:{points:3,reason:'Known scope'},dor:[{id:'input',text:'Ready',passed:false,evidence:''}]})]);
  app.document.querySelector('#create').click();const form=app.document.querySelector('#issue-form');
  const groups=[...form.querySelectorAll('[data-editor-group]')];assert.equal(groups.length,3);assert.ok(groups.every(group=>!group.open));
  form.elements.title.value='Simple draft';form.elements.languages.value='python';form.elements.assignee.value='Fixture agent';
  const before=[...new app.window.FormData(form)];groups.forEach(group=>{group.open=true;});assert.deepEqual([...new app.window.FormData(form)],before);
  const code=form.querySelector('[data-editor-group=code]');code.open=false;form.elements.languages.dispatchEvent(new app.window.Event('invalid'));assert.equal(code.open,true);
  app.document.querySelector('#editor').close();app.document.querySelector('.task-graph-card').click();await until(()=>app.document.querySelector('#detail').open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  [...app.document.querySelectorAll('#detail-body button')].find(button=>button.textContent==='Редактировать').click();
  assert.ok(groups.every(group=>group.open));assert.equal(form.elements.languages.value,'python');assert.equal(form.elements.assignee.value,'Agent');
  assert.equal(form.elements.estimatedPoints.value,'3');assert.equal(form.querySelectorAll('#dor-editor .criterion-editor').length,1);
});

test('native task dialogs put the close header outside their one scrolling body', async () => {
  const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');const dom=new JSDOM(html);
  try {
    for(const dialog of dom.window.document.querySelectorAll('dialog')){
      const heading=dialog.querySelector('.dialog-heading'),body=dialog.querySelector('.dialog-body');assert.ok(heading&&body,dialog.id);
      assert.equal(body.contains(heading),false,dialog.id);assert.ok(heading.querySelector('[data-close]'),dialog.id);
    }
  } finally {dom.window.close();}
});

test('dialog footer text controls never use a fixed icon-only hit area', async () => {
  const html=await readFile(new URL('../web/index.html',import.meta.url),'utf8');const dom=new JSDOM(html);
  try {
    for(const button of dom.window.document.querySelectorAll('.dialog-actions button')) {
      assert.equal(button.classList.contains('ag-icon-button'),false,button.textContent);
      assert.ok(button.textContent.trim().length>1);
    }
  } finally {dom.window.close();}
});


test('passive refresh preserves document/main scroll, appended pages and session readers; idle top overview can refresh',async t=>{
  let poll;
  const records=Array.from({length:201},(_,i)=>issue(String(i)));
  const f=await application(t,records,null,{captureRefresh:callback=>{poll=callback;}});
  const reads=()=>f.calls.filter(x=>x.path.startsWith('/api/v1/issues')).length;
  // More than one bounded page is already loaded: polling must never collapse it to page one.
  let before=reads();poll();await new Promise(resolve=>setImmediate(resolve));assert.equal(reads(),before);
  f.records.splice(1);
  f.document.querySelector('#refresh').click();await until(()=>f.document.querySelectorAll('.task-graph-card').length===1);
  await until(()=>f.document.querySelector('#content').getAttribute('aria-busy')==='false');
  const scroller=f.document.scrollingElement || f.document.documentElement;
  Object.defineProperty(f.document,'scrollingElement',{value:scroller,configurable:true});
  scroller.scrollTop=200;
  before=reads();poll();await new Promise(resolve=>setImmediate(resolve));assert.equal(reads(),before);
  scroller.scrollTop=0;f.document.querySelector('#main').scrollTop=100;
  poll();await new Promise(resolve=>setImmediate(resolve));assert.equal(reads(),before);
  f.document.querySelector('#main').scrollTop=0;
  f.document.activeElement.blur();
  poll();await until(()=>reads()>before);
  await until(()=>f.document.querySelector('#content').getAttribute('aria-busy')==='false');
  f.document.querySelector('[data-view=sessions]').click();
  await until(()=>f.document.querySelector('.agent-sessions'));
  const root=f.document.querySelector('.agent-sessions');
  poll();await new Promise(resolve=>setImmediate(resolve));
  assert.equal(f.document.querySelector('.agent-sessions'),root);
});

test('queued close(A) cannot cancel pending detail B and normal close restores B focus', async t => {
  let release;
  const app = await application(t, [issue('root'), issue('other')], (path, init, respond) => {
    if (path === '/api/v1/issues/other') {
      const value = respond(path, init);
      return new Promise(resolve => { release = () => resolve(value); });
    }
  }, {queuedClose:true});
  const detail = app.document.querySelector('#detail');
  const ready = () => detail.open && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false';
  app.document.querySelector('[data-issue-id=root]').click();
  await until(ready);
  const other = app.document.querySelector('[data-issue-id=other]');
  detail.close(); other.focus(); other.click();
  await until(() => release);
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(detail.open, true);
  assert.match(app.document.querySelector('#detail-title').textContent, /Загрузка/);
  const skeleton = app.document.querySelector('.task-detail-skeleton');
  assert.equal(skeleton.getAttribute('aria-hidden'), 'true');
  assert.equal(skeleton.querySelectorAll('button, input, [tabindex]').length, 0);
  assert.equal(app.document.querySelector('#detail-body').getAttribute('aria-busy'), 'true');
  assert.match(app.document.querySelector('#detail-feedback').textContent, /Загружаем задачу/);
  assert.ok(app.document.querySelector('#detail-feedback').textContent);
  release(); await until(ready);
  assert.match(app.document.querySelector('#detail-title').textContent, /TASK-other/);
  assert.equal(app.document.querySelector('.task-detail-skeleton'), null);
  assert.equal(app.document.querySelector('#detail-feedback').textContent, '');
  detail.close();
  await until(() => app.document.activeElement === other);
  assert.equal(app.calls.some(call => call.method !== 'GET'), false);
});

test('a successfully created task opens after its queued editor close', async t => {
  const app = await application(t, [issue('root')], undefined, {queuedClose:true});
  app.document.querySelector('#create').click();
  const form = app.document.querySelector('#issue-form');
  form.elements.title.value = 'Created after queued close';
  form.dispatchEvent(new app.window.Event('submit', {cancelable:true, bubbles:true}));
  await until(() => app.document.querySelector('#detail-title').textContent.includes('Created after queued close'));
  await until(() => app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false');
  assert.equal(app.document.querySelector('#editor').open, false);
  assert.equal(app.document.querySelector('#detail').open, true);
  assert.equal(app.calls.filter(call => call.body?.operation === 'issue.create').length, 1);
});

test('same-task refresh retains an unsaved comment; a failed read retries without crossing tasks', async t => {
  let fail = false;
  const app = await application(t, [issue('root')], path => {
    if (path === '/api/v1/issues/root' && fail) return Promise.reject(new Error('Synthetic read failure'));
  });
  app.document.querySelector('[data-issue-id=root]').click();
  const ready = () => app.document.querySelector('.task-detail-layout') && app.document.querySelector('#detail-body').getAttribute('aria-busy') === 'false';
  await until(ready);
  const comment = app.document.querySelector('[data-detail-draft=comment]');
  comment.closest('.task-detail-disclosure').open = true;
  comment.value = 'Unsaved evidence draft';
  app.document.querySelector('[data-detail-section=criteria]').open = false;
  fail = true;
  [...app.document.querySelectorAll('#detail-body button')].find(b => b.textContent === 'Обновить карточку').click();
  await until(() => app.document.querySelector('#detail-body .error'));
  assert.equal(app.document.querySelector('.task-detail-skeleton'), null);
  assert.equal(app.document.querySelector('#detail').open, true);
  fail = false;
  [...app.document.querySelectorAll('#detail-body button')].find(b => b.textContent === 'Повторить загрузку задачи').click();
  await until(ready);
  const retained = app.document.querySelector('[data-detail-draft=comment]');
  assert.equal(retained.value, 'Unsaved evidence draft');
  assert.equal(app.document.querySelector('[data-detail-section=criteria]').open, false);
  assert.equal(retained.closest('.task-detail-disclosure').open, true);
  assert.equal(app.calls.some(call => call.method !== 'GET'), false);
});

test('queued approval close cannot retire a new pending review or enable merge before its response', async t => {
  let reads = 0, release;
  const app = await application(t, [issue('root', {status:'in_review', branch:'codex/fixture', repoId:'fixture'})], (path, init, respond, response) => {
    if (path.endsWith('/root/review')) {
      const value = response({issueId:'root', head:'frozen-head', base:'frozen-base', baseBranch:'main', changedFiles:[], diff:'Synthetic immutable diff', canMerge:false, reasons:['Independent approval still required']});
      reads++;
      return reads === 2 ? new Promise(resolve => { release = () => resolve(value); }) : value;
    }
  }, {queuedClose:true});
  app.document.querySelector('[data-issue-id=root]').click();
  await until(() => app.document.querySelector('.task-detail-layout'));
  const open = [...app.document.querySelectorAll('#detail-body button')].find(b => b.textContent === 'Просмотреть изменения');
  open.click();
  await until(() => app.document.querySelector('#review-body pre'));
  await new Promise(resolve => setTimeout(resolve, 1));
  app.document.querySelector('#approval').close(); open.click();
  await until(() => release);
  await new Promise(resolve => setTimeout(resolve, 5));
  assert.equal(app.document.querySelector('#approval').open, true);
  assert.equal(app.document.querySelector('#merge').disabled, true);
  assert.equal(app.document.querySelector('#merge-consent').checked, false);
  release();
  await until(() => app.document.querySelector('#review-body pre'));
  app.document.querySelector('#merge-consent').checked = true;
  app.document.querySelector('#merge-consent').dispatchEvent(new app.window.Event('change'));
  assert.equal(app.document.querySelector('#merge').disabled, true);
  assert.equal(app.calls.some(call => /approve-merge/.test(call.path)), false);
});

test('comment acknowledgement retires only its submitted snapshot and preserves newer typing', async t => {
  let release;
  const app = await application(t, [issue('root')], (path, init, respond, response) => {
    const sent = init.body ? JSON.parse(init.body) : null;
    if (path === '/api/v1/commands' && sent?.operation === 'issue.comment')
      return new Promise(resolve => { release = () => resolve(response({accepted:true})); });
  });
  app.document.querySelector('[data-issue-id=root]').click();
  await until(() => app.document.querySelector('.task-detail-layout'));
  const comment = app.document.querySelector('[data-detail-draft=comment]');
  comment.closest('details').open = true;
  comment.value = 'Submitted snapshot';
  [...app.document.querySelectorAll('#detail-body button')].find(b => b.textContent === 'Добавить комментарий').click();
  await until(() => release);
  comment.value = 'Newer typing while request is pending';
  release();
  await until(() => app.document.querySelector('[data-detail-draft=comment]') !== comment);
  const retained = app.document.querySelector('[data-detail-draft=comment]');
  assert.equal(retained.value, 'Newer typing while request is pending');
  assert.equal(retained.closest('details').open, true);
  const commands = app.calls.filter(call => call.body?.operation === 'issue.comment');
  assert.equal(commands.length, 1);
  assert.equal(commands[0].body.payload.text, 'Submitted snapshot');
});

test('passive overview refresh preserves board horizontal and individual column scroll', async t => {
  let poll;
  const app = await application(t, [issue('root')], undefined, {captureRefresh:value => {poll=value;}});
  app.document.querySelector('nav [data-view=board]').click();
  await until(() => app.document.querySelector('.board'));
  await until(() => app.document.querySelector('#content').getAttribute('aria-busy') === 'false');
  app.document.querySelector('#heading').focus();
  const board = app.document.querySelector('.board'), column = board.querySelector('.column');
  const reads = () => app.calls.filter(call => call.path.startsWith('/api/v1/issues?')).length;
  const before = reads();
  column.scrollTop = 120;
  poll(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads(), before);assert.equal(app.document.querySelector('.board'), board);
  column.scrollTop = 0;board.scrollLeft = 150;
  poll(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(reads(), before);assert.equal(board.scrollLeft, 150);
  board.scrollLeft = 0;
  poll(); await until(() => reads() > before);
});


test('board hides empty lanes by default and keeps an explicit native option for the whole workflow', async t => {
  const app = await application(t, [issue('root'), issue('review', {status:'in_review'})]);
  app.document.querySelector('[data-view=board]').click();
  await until(() => app.document.querySelector('.board'));
  assert.equal(app.document.querySelectorAll('.column').length, 2);
  assert.match(app.document.querySelector('.board').textContent, /На проверке/);
  const toggle = app.document.querySelector('#show-empty-columns');
  toggle.checked = true; toggle.dispatchEvent(new app.window.Event('change'));
  assert.equal(app.document.querySelectorAll('.column').length, 9);
  assert.equal(app.document.querySelectorAll('.card').length, 2);
});

test('filtered empty results reset query/status without changing history, space or data on the server', async t => {
  const app = await application(t, [issue('root')]);
  app.document.querySelector('[data-view=list]').click();
  await until(() => app.document.querySelector('.issue-list'));
  const query = app.document.querySelector('#issue-query');
  query.value = '<no match>'; query.dispatchEvent(new app.window.Event('input'));
  assert.match(app.document.querySelector('#content').textContent, /Задачи не найдены/);
  assert.equal(app.document.querySelector('#reset-filters').hidden, false);
  app.document.querySelector('#reset-filters').click();
  assert.equal(query.value, '');
  assert.equal(app.document.activeElement, query);
  assert.equal(app.document.querySelectorAll('.issue-row').length, 1);
  assert.equal(app.calls.filter(call => call.method !== 'GET').length, 0);
});

test('failed same-scope refresh preserves actual nodes and scroll; retry is read-only and clears stale feedback', async t => {
  let fail = false;
  const app = await application(t, [issue('root')], path => {
    if (fail && path.startsWith('/api/v1/issues?')) throw new Error('<offline>');
  });
  app.document.querySelector('[data-view=board]').click();
  await until(() => app.document.querySelector('.board'));
  const board = app.document.querySelector('.board'), card = board.querySelector('.card');
  board.scrollLeft = 120;
  fail = true; app.document.querySelector('#refresh').click();
  await until(() => !app.document.querySelector('#retry-view').hidden);
  assert.equal(app.document.querySelector('.board'), board);
  assert.equal(app.document.querySelector('.card'), card);
  assert.equal(board.scrollLeft, 120);
  assert.equal(app.document.querySelector('#content').inert, false);
  assert.match(app.document.querySelector('#view-load-status').textContent, /устаревшими.*<offline>/);
  assert.equal(app.document.querySelector('#view-load-status offline'), null);
  fail = false; app.document.querySelector('#retry-view').click();
  await until(() => app.document.querySelector('#view-feedback').hidden);
  assert.equal(app.document.querySelectorAll('.card').length, 1);
  assert.equal(app.calls.filter(call => call.method !== 'GET').length, 0);
});

test('failed navigation cannot expose previous-context data through local filter changes', async t => {
  const app = await application(t, [issue('root')], path => {
    if (path.includes('spaceId=beta')) throw new Error('Unavailable');
  });
  app.document.querySelector('#space-manage').click();
  app.document.querySelector('#space-list [data-space-id="beta"] .space-choice').click();
  await until(() => !app.document.querySelector('#retry-view').hidden);
  const query = app.document.querySelector('#issue-query');
  query.value = 'root'; query.dispatchEvent(new app.window.Event('input'));
  assert.equal(app.document.querySelectorAll('[data-issue-id]').length, 0);
  assert.match(app.document.querySelector('#content').textContent, /Данные не загружены/);
});

test('nested review detail retains the original source after the view replaces its row', async t => {
  const reviewProgress = {reason:'reviewer_required',waitingSince:'2026-10-22T12:00:00Z',
    lastAttemptExpired:true,nextAction:{kind:'open_critic',issueId:'critic',key:'AGT-2'}};
  const app = await application(t, [issue('root', {status:'in_review', reviewProgress}), issue('critic', {kind:'review', reviewProgress})]);
  const source = app.document.querySelector('[data-issue-id=root]');
  source.click();
  await until(() => app.document.querySelector('.task-detail-next'));
  const action = app.document.querySelector('.task-detail-next');
  assert.match(action.textContent, /AGT-2/);
  assert.equal(app.document.querySelector('#detail-body button'), action);
  assert.equal(app.document.querySelector('[data-detail-section=criteria]').open, false);
  action.click();
  await until(() => app.document.querySelector('.task-detail-next')?.textContent === 'Открыть очередь ревью');
  app.document.querySelector('#refresh').click();
  await until(() => !source.isConnected && !app.document.querySelector('#content').inert);
  app.document.querySelector('#detail [data-close]').click();
  assert.equal(app.document.activeElement, app.document.querySelector('[data-issue-id=root]'));
});
