import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderAgentSessions} from '../web/agent-sessions.js';

/** Own one consumer DOM and its listeners; restore globals when the test ends. */
function environment(t){
  const dom=new JSDOM('<body><main></main>',{url:'http://127.0.0.1'});
  const old=globalThis.document;globalThis.document=dom.window.document;
  t.after(()=>{globalThis.document=old;dom.window.close();});
  return {parent:document.querySelector('main'),window:dom.window};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const session={id:'one',title:'<img src=x onerror=alert(1)>',agent:'Claude',protocol:'acp',protocolVersion:2,issueId:null,revision:1,
  projection:{foregroundState:'idle'},eventCount:2};

test('session UI renders untrusted source data as text, preserves identical observations and cursor pagination',async t=>{
  const {parent}=environment(t);const calls=[];
  const request=async path=>{calls.push(path);if(path.startsWith('/api/v1/sessions?'))return {items:[session],hasMore:false};
    if(!path.includes('/events?'))return session;
    const second=path.endsWith('after=8');
    return {items:[{sequence:second?2:1,direction:'agent_to_client',origin:'live',receivedAt:'now',redactions:[],payload:{jsonrpc:'2.0',method:'future',text:'<script>bad()</script>'}}],hasMore:!second,nextCursor:8};};
  const dispose=renderAgentSessions(parent,{request,command:async()=>{},selection:{id:null},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();await tick();
  assert.equal(parent.querySelector('img'),null);assert.equal(parent.querySelector('script'),null);
  assert.match(parent.textContent,/Фоновая активность: не подтверждена/);
  [...parent.querySelectorAll('button')].find(node=>node.textContent==='Следующие события').click();await tick();
  assert.equal(parent.querySelectorAll('.session-event').length,2);
  assert.ok(calls.some(path=>path.endsWith('after=8')));
});

test('linking a session uses the universal command contract and an expected revision',async t=>{
  const {parent,window}=environment(t);const commands=[];
  const request=async path=>path.startsWith('/api/v1/sessions?')?{items:[session],hasMore:false}:path.includes('/events?')?{items:[],hasMore:false}:session;
  const dispose=renderAgentSessions(parent,{request,command:async(...args)=>commands.push(args),selection:{id:null},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();await tick();parent.querySelector('.session-link-form input').value='AGT-113';
  parent.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  assert.deepEqual(commands[0],['session.link',{sessionId:'one',issueId:'AGT-113',expectedRevision:1}]);
});

test('disposed view ignores a late network reply',async t=>{
  const {parent}=environment(t);let finish;
  const request=()=>new Promise(resolve=>{finish=resolve;});
  const dispose=renderAgentSessions(parent,{request,command:async()=>{},selection:{},openIssue:()=>{},onError:()=>{}});
  dispose();parent.replaceChildren();finish({items:[session]});await tick();
  assert.equal(parent.textContent,'');
});

test('universal service facts are available without an ACP source or runtime authority',async t=>{
  const {parent}=environment(t);
  const request=async path=>path.startsWith('/api/v1/sessions?')?{items:[],hasMore:false}:{items:[{operation:'issue.create',type:'command.committed',createdAt:'now',commandId:'one'}],hasMore:false};
  const dispose=renderAgentSessions(parent,{request,command:async()=>{throw Error('Must not mutate');},selection:{},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();[...parent.querySelectorAll('button')].find(node=>node.textContent==='События сервиса').click();await tick();
  assert.match(parent.querySelector('.service-facts').textContent,/Создание задачи/);
  assert.equal(parent.querySelector('.agent-session-list').hidden,true);
});

test('selected service journal survives application refresh instead of resetting to sessions',async t=>{
  const {parent}=environment(t);const selection={};const paths=[];
  const options={request:async path=>{paths.push(path);return {items:[],hasMore:false};},command:async()=>{},selection,openIssue:()=>{},onError:()=>{}};
  const first=renderAgentSessions(parent,options);await tick();
  [...parent.querySelectorAll('button')].find(node=>node.textContent==='События сервиса').click();await tick();first();
  paths.length=0;const second=renderAgentSessions(parent,options);t.after(second);await tick();
  assert.equal(selection.view,'facts');assert.equal(parent.querySelector('.service-facts').hidden,false);
  assert.ok(paths.every(path=>path.startsWith('/api/v1/events?')));
});

/** Delay one transport outcome so navigation and focus can change before it settles. */
function deferred() {
  let resolve, reject;
  const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});
  return {promise,resolve,reject};
}

/** Select a section through the actual consumer button instead of calling internal handlers. */
function section(parent,view) { return parent.querySelector(`[data-session-view="${view}"]`); }

/** Provide two observed sessions; transport stubs never execute or retry a domain command. */
function sessionReads(path) {
  const two={...session,id:'two',title:'Вторая сессия'};
  if(path.startsWith('/api/v1/sessions?'))return {items:[session,two],hasMore:false};
  if(path.includes('/events?') || path.startsWith('/api/v1/events?') || path.startsWith('/api/v1/queues?'))return {items:[],hasMore:false};
  return path.endsWith('/two')?two:session;
}

test('section buttons expose exactly one selected state, including a restored service journal',async t=>{
  const {parent}=environment(t);
  const dispose=renderAgentSessions(parent,{request:async path=>sessionReads(path),command:async()=>{},selection:{view:'facts'},openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();
  assert.equal(section(parent,'facts').getAttribute('aria-pressed'),'true');
  for(const view of ['sessions','queue','facts']) {
    section(parent,view).click();await tick();await tick();
    assert.equal(parent.querySelectorAll('.session-sections [aria-pressed="true"]').length,1);
    assert.equal(section(parent,view).getAttribute('aria-pressed'),'true');
    assert.equal(parent.querySelectorAll('.session-sections .ag-button--primary').length,0);
    assert.equal(section(parent,view).classList.contains('ag-button--quiet'),true);
  }
});

test('late link save neither selects its old session nor steals focus from a new selection',async t=>{
  const {parent,window}=environment(t);const pending=deferred(),commands=[],selection={};let oldReads=0;
  const dispose=renderAgentSessions(parent,{request:async path=>{if(path==='/api/v1/sessions/one')oldReads++;return sessionReads(path);},command:async(...args)=>{commands.push(args);await pending.promise;},selection,openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();await tick();
  const form=parent.querySelector('form');form.querySelector('input').value='AGT-113';form.querySelector('input').focus();
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
  const next=parent.querySelector('[data-session-id="two"]');next.focus();next.click();await tick();await tick();
  const before=document.activeElement;pending.resolve();await tick();await tick();
  assert.equal(selection.id,'two');assert.equal(document.activeElement,before);assert.equal(oldReads,1);
  assert.deepEqual(commands,[['session.link',{sessionId:'one',issueId:'AGT-113',expectedRevision:1}]]);
});

test('late link save does not mutate or refocus a different activity section',async t=>{
  const {parent,window}=environment(t);const pending=deferred(),selection={};let oldReads=0;
  const dispose=renderAgentSessions(parent,{request:async path=>{if(path==='/api/v1/sessions/one')oldReads++;return sessionReads(path);},command:()=>pending.promise,selection,openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();await tick();parent.querySelector('.session-link-form input').focus();
  parent.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));
  const control=section(parent,'facts');control.focus();control.click();await tick();
  pending.resolve();await tick();await tick();
  assert.equal(selection.view,'facts');assert.equal(oldReads,1);
  assert.equal(parent.querySelector('.service-facts').hidden,false);assert.equal(document.activeElement,control);
});

test('a delayed save-triggered reload cannot focus the input of a newer session',async t=>{
  const {parent,window}=environment(t);const reload=deferred(),selection={};let oldReads=0;
  const dispose=renderAgentSessions(parent,{request:async path=>path==='/api/v1/sessions/one' && ++oldReads===2?reload.promise:sessionReads(path),command:async()=>{},selection,openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();await tick();parent.querySelector('.session-link-form input').focus();
  parent.querySelector('form').dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  const next=parent.querySelector('[data-session-id="two"]');next.focus();next.click();await tick();await tick();
  reload.resolve(session);await tick();await tick();
  assert.equal(selection.id,'two');assert.equal(document.activeElement,next);
  assert.equal(parent.querySelector('.agent-session-detail h2').textContent,'Вторая сессия');
});

test('old session-list reply cannot append or auto-select after leaving and re-entering the section',async t=>{
  const {parent}=environment(t);const first=deferred(),second=deferred(),selection={};let lists=0;
  const dispose=renderAgentSessions(parent,{request:async path=>path.startsWith('/api/v1/sessions?')?(++lists===1?first.promise:second.promise):sessionReads(path),command:async()=>{},selection,openIssue:()=>{},onError:()=>{}});t.after(dispose);
  section(parent,'facts').click();await tick();section(parent,'sessions').click();
  second.resolve({items:[{...session,id:'two',title:'Вторая сессия'}],hasMore:false});await tick();await tick();
  first.resolve({items:[session],hasMore:false});await tick();await tick();
  assert.deepEqual([...parent.querySelectorAll('[data-session-id]')].map(node=>node.dataset.sessionId),['two']);
  assert.equal(selection.id,'two');
});

test('initial service-journal failure is visible, not an empty journal, and Retry owns loading state',async t=>{
  const {parent}=environment(t);const retry=deferred(),errors=[];let reads=0;
  const dispose=renderAgentSessions(parent,{request:async()=>{if(++reads===1)throw Error('Service offline');return retry.promise;},command:async()=>{throw Error('Must not mutate');},selection:{view:'facts'},openIssue:()=>{},onError:error=>errors.push(error)});t.after(dispose);
  await tick();const facts=parent.querySelector('.service-facts');
  assert.equal(facts.hidden,false);assert.equal(facts.getAttribute('aria-busy'),'false');
  assert.match(facts.textContent,/События не загружены/);assert.doesNotMatch(facts.textContent,/Событий пока нет/);assert.equal(errors.length,1);
  [...facts.querySelectorAll('button')].find(node=>node.textContent==='Повторить загрузку').click();
  assert.equal(facts.getAttribute('aria-busy'),'true');assert.match(facts.textContent,/Загрузка событий/);
  retry.resolve({items:[{operation:'issue.create',type:'command.committed',createdAt:'now'}],hasMore:false});await tick();await tick();
  assert.equal(facts.getAttribute('aria-busy'),'false');assert.match(facts.textContent,/Создание задачи/);assert.equal(facts.querySelector('[data-facts-feedback]'),null);
});

test('journal pagination failure preserves rows and retries exactly the failed cursor',async t=>{
  const {parent}=environment(t);const paths=[];let nextReads=0;
  const request=async path=>{paths.push(path);if(path.endsWith('after=7') && ++nextReads===1)throw Error('Temporary');return {items:[{operation:'issue.create',type:'command.committed',createdAt:'now'}],hasMore:!path.endsWith('after=7'),nextCursor:7};};
  const dispose=renderAgentSessions(parent,{request,command:async()=>{},selection:{view:'facts'},openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();const facts=parent.querySelector('.service-facts');
  facts.querySelector('[data-facts-more]').click();await tick();
  assert.equal(facts.querySelectorAll('.session-event').length,1);assert.match(facts.textContent,/События не загружены/);
  [...facts.querySelectorAll('button')].find(node=>node.textContent==='Повторить загрузку').click();await tick();await tick();
  assert.equal(facts.querySelectorAll('.session-event').length,2);
  assert.deepEqual(paths,['/api/v1/events?limit=20&after=0','/api/v1/events?limit=20&after=7','/api/v1/events?limit=20&after=7']);
});

test('a service-journal failure after navigation cannot paint an error in the newer section',async t=>{
  const {parent}=environment(t);const pending=deferred(),errors=[];
  const dispose=renderAgentSessions(parent,{request:async path=>path.startsWith('/api/v1/events?')?pending.promise:sessionReads(path),command:async()=>{},selection:{view:'facts'},openIssue:()=>{},onError:error=>errors.push(error)});t.after(dispose);
  section(parent,'sessions').click();await tick();await tick();
  pending.reject(Error('Late failure'));await tick();await tick();
  assert.equal(errors.length,0);assert.equal(parent.querySelector('.agent-session-detail h2').textContent,session.title);
  assert.equal(parent.querySelector('.service-facts').getAttribute('aria-busy'),'false');
});

test('a failed superseded session list cannot report an error over the current service journal',async t=>{
  const {parent}=environment(t);const pending=deferred(),errors=[];
  const dispose=renderAgentSessions(parent,{request:async path=>path.startsWith('/api/v1/sessions?')?pending.promise:sessionReads(path),command:async()=>{},selection:{},openIssue:()=>{},onError:error=>errors.push(error)});t.after(dispose);
  section(parent,'facts').click();await tick();pending.reject(Error('Old list offline'));await tick();
  assert.equal(errors.length,0);assert.equal(parent.querySelector('.service-facts').hidden,false);
  assert.equal(parent.querySelector('.agent-session-list h2').textContent,'MCP и наблюдаемые сессии');
});

test('an ambiguous link failure keeps the current input and never automatically resends the command',async t=>{
  const {parent,window}=environment(t);const commands=[],errors=[];
  const error=Object.assign(Error('Исход команды неизвестен'),{code:'unknown'});
  const dispose=renderAgentSessions(parent,{request:async path=>sessionReads(path),command:async(...args)=>{commands.push(args);throw error;},selection:{},openIssue:()=>{},onError:error=>errors.push(error)});t.after(dispose);
  await tick();await tick();const form=parent.querySelector('form');form.querySelector('input').value='AGT-113';
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();await tick();
  assert.equal(commands.length,1);assert.equal(errors[0],error);assert.equal(parent.querySelector('form'),form);
  assert.equal(form.querySelector('input').value,'AGT-113');assert.equal(form.querySelector('button').disabled,false);
  assert.deepEqual(commands[0],['session.link',{sessionId:'one',issueId:'AGT-113',expectedRevision:1}]);
  assert.match(parent.querySelector('.session-sections').textContent,/Список обновляется каждые 10 секунд/);
});


test('failed event read keeps the loaded session, unsaved link draft and focus; Retry only reads events',async t=>{
  const {parent}=environment(t),pending=deferred();let eventReads=0,commands=0;
  const request=async path=>{
    if(path.startsWith('/api/v1/sessions?'))return {items:[session],hasMore:false};
    if(!path.includes('/events?'))return session;
    eventReads++;
    if(eventReads===1)return pending.promise;
    return {items:[],hasMore:false};
  };
  const dispose=renderAgentSessions(parent,{request,command:async()=>{commands++;},selection:{},openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();await tick();
  const input=parent.querySelector('.session-link-form input');input.value='UNSAVED-42';input.focus();
  pending.reject(new Error('read unavailable'));await tick();await tick();
  assert.equal(parent.querySelector('.session-link-form input'),input);assert.equal(input.value,'UNSAVED-42');
  assert.equal(document.activeElement,input);
  assert.match(parent.querySelector('.session-events').textContent,/События не загружены/);
  [...parent.querySelectorAll('button')].find(x=>x.textContent==='Повторить загрузку событий').click();await tick();
  assert.equal(eventReads,2);assert.equal(commands,0);assert.equal(parent.querySelector('.session-link-form input').value,'UNSAVED-42');
  assert.equal(parent.querySelector('[data-events-error]'),null);
});


test('failed event page Retry and the retained More button cannot issue overlapping reads or duplicate rows',async t=>{
  const {parent}=environment(t),pending=deferred();let pageReads=0;
  const event=n=>({sequence:n,direction:'agent_to_client',origin:'live',receivedAt:'now',redactions:[],payload:{}});
  const request=async path=>{
    if(path.startsWith('/api/v1/sessions?'))return {items:[session],hasMore:false};
    if(!path.includes('/events?'))return session;
    if(path.endsWith('after=0'))return {items:[event(1)],hasMore:true,nextCursor:5};
    pageReads++;
    if(pageReads===1)throw Error('page unavailable');
    return pending.promise;
  };
  const dispose=renderAgentSessions(parent,{request,command:async()=>{},selection:{},openIssue:()=>{},onError:()=>{}});t.after(dispose);
  await tick();await tick();
  const more=parent.querySelector('[data-events-more]');more.click();await tick();
  [...parent.querySelectorAll('button')].find(x=>x.textContent==='Повторить загрузку событий').click();
  more.click();await tick();assert.equal(pageReads,2);
  pending.resolve({items:[event(2)],hasMore:false});await tick();await tick();
  assert.equal(parent.querySelectorAll('.session-event').length,2);
});
