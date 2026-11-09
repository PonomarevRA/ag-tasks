import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderAgentQueue} from '../web/agent-queue.js';
import {renderAgentSessions} from '../web/agent-sessions.js';

const tick=()=>new Promise(resolve=>setImmediate(resolve));
const profile={id:'fixture',version:'1.0',protocol:'acp',protocolVersion:1};
const lane={laneId:'one',title:'Очередь <img src=x>',profile,revision:8,state:'ready',draining:false,activeAttempt:null,queuedCount:2};
const queued=id=>({id,laneId:'one',text:`Task ${id}`,state:'queued',revision:3,position:id==='first'?1:2,issueId:null});

/** Own a synthetic consumer DOM and restore globals; no production HTTP request is made. */
function environment(t) {
  const dom=new JSDOM('<body><main></main><div id="notice"></div>',{url:'http://127.0.0.1'});
  const previous=globalThis.document;globalThis.document=dom.window.document;
  t.after(()=>{globalThis.document=previous;dom.window.close();});
  return {parent:document.querySelector('main'),window:dom.window};
}

/** Use explicit bounded API pages; their order intentionally differs from creation history. */
function request(path) {
  if(path.startsWith('/api/v1/queues?'))return Promise.resolve({items:[lane],hasMore:false});
  if(path.includes('/pending?'))return Promise.resolve({items:[queued('second'),queued('first')],hasMore:false});
  if(path.includes('/items?'))return Promise.resolve({items:[queued('first'),queued('second')],hasMore:false});
  return Promise.resolve(lane);
}

async function loaded() {await tick();await tick();}

test('pending display preserves domain order and uses CAS when moving/cancelling',async t=>{
  const {parent}=environment(t),commands=[];
  const dispose=renderAgentQueue(parent,{request,command:async(...args)=>{commands.push(args);},selection:{},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await loaded();
  const rows=[...parent.querySelectorAll('.queue-pending .queue-item')];
  assert.deepEqual(rows.map(row=>row.dataset.itemId),['second','first']);
  assert.equal(parent.querySelector('img'),null);
  [...rows[1].querySelectorAll('button')].find(node=>node.textContent==='В начало').click();await loaded();
  assert.deepEqual(commands[0],['queue.move',{itemId:'first',expectedRevision:3,beforeItemId:'second'}]);
  [...parent.querySelectorAll('.queue-item button')].find(node=>node.textContent==='Отменить').click();await loaded();
  assert.deepEqual(commands[1],['queue.cancel',{itemId:'second',expectedRevision:3}]);
  assert.match(parent.querySelector('[role=status]').textContent,/сохранены/);
});

test('failed enqueue retains draft and optional issue link rather than clearing the intent',async t=>{
  const {parent,window}=environment(t),errors=[],commands=[];
  const dispose=renderAgentQueue(parent,{request,command:async(...args)=>{commands.push(args);throw Error('Связь потеряна');},selection:{},openIssue:()=>{},onError:error=>errors.push(error.message)});t.after(dispose);
  await loaded();const form=parent.querySelector('.queue-detail form');
  form.querySelector('textarea').value='Keep my exact draft';form.querySelector('input').value='AGT-113';
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await loaded();
  assert.equal(form.querySelector('textarea').value,'Keep my exact draft');
  assert.deepEqual(commands[0],['queue.enqueue',{laneId:'one',text:'Keep my exact draft',issueId:'AGT-113'}]);
  assert.match(parent.querySelector('[role=status]').textContent,/Связь потеряна/);
  assert.equal(form.querySelector('[type=submit]').disabled,false);assert.equal(errors.length,1);
});

test('completed foreground remains reserved until explicit operator evidence is submitted',async t=>{
  const {parent,window}=environment(t),commands=[];
  const active={...lane,state:'busy',activeAttempt:{id:'attempt',itemId:'active',state:'finished',revision:12,profile,outcome:'end_turn'}};
  const fetch=async path=>path.startsWith('/api/v1/queue-items/')?{...queued('active'),state:'active'}:path==='/api/v1/queues/one'?active:request(path);
  const dispose=renderAgentQueue(parent,{request:fetch,command:async(...args)=>commands.push(args),selection:{},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await loaded();assert.equal(commands.length,0);
  assert.match(parent.querySelector('.queue-active').textContent,/выход основного процесса этого не подтверждают/);
  const form=parent.querySelector('.queue-active form');form.querySelector('textarea').value='Verified all fixture processes stopped';
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await loaded();
  assert.deepEqual(commands[0],['attempt.reconcile',{attemptId:'attempt',expectedRevision:12,resolution:'owned_runtime_stopped',evidence:'Verified all fixture processes stopped'}]);
});

test('late lane response cannot overwrite a newer selection and dispose ignores pending data',async t=>{
  const {parent}=environment(t);let finish;
  const fetch=path=>path==='/api/v1/queues/one'?new Promise(resolve=>{finish=resolve;}):request(path);
  const dispose=renderAgentQueue(parent,{request:fetch,command:async()=>{},selection:{},openIssue:()=>{},onError:()=>{}});
  await loaded();dispose();parent.replaceChildren();finish(lane);await loaded();assert.equal(parent.textContent,'');
});

test('queue subview survives application refresh without requesting source sessions',async t=>{
  const {parent}=environment(t),selection={view:'queue',laneId:'one'},paths=[];
  const dispose=renderAgentSessions(parent,{request:path=>{paths.push(path);return request(path);},command:async()=>{},selection,openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await loaded();assert.ok(parent.querySelector('.agent-queue'));
  assert.ok(paths.every(path=>!path.startsWith('/api/v1/sessions')));
  assert.equal(parent.querySelector('.agent-session-list').hidden,true);
});

test('late enqueue acknowledgement cannot navigate back from a newer lane selection',async t=>{
  const {parent,window}=environment(t),selection={};let acknowledge;
  const second={...lane,laneId:'two',title:'Second lane',queuedCount:0};
  const fetch=async path=>path.startsWith('/api/v1/queues?')?{items:[lane,second],hasMore:false}:path==='/api/v1/queues/two'?second:request(path);
  const dispose=renderAgentQueue(parent,{request:fetch,command:()=>new Promise(resolve=>{acknowledge=resolve;}),selection,openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await loaded();const form=parent.querySelector('.queue-detail form');form.querySelector('textarea').value='Pending ACK';
  form.dispatchEvent(new window.Event('submit',{bubbles:true,cancelable:true}));await tick();
  parent.querySelector('[data-lane-id="two"]').click();await loaded();assert.equal(selection.laneId,'two');
  acknowledge({id:'enqueued'});await loaded();
  assert.equal(selection.laneId,'two');assert.equal(parent.querySelector('.queue-detail h2').textContent,'Second lane');
});

test('late post-ACK reload cannot take focus from a newer lane draft',async t=>{
  const {parent}=environment(t),selection={};let reload,reads=0;
  const second={...lane,laneId:'two',title:'Second lane',queuedCount:0};
  const fetch=path=>{
    if(path.startsWith('/api/v1/queues?'))return Promise.resolve({items:[lane,second],hasMore:false});
    if(path==='/api/v1/queues/one' && ++reads>1)return new Promise(resolve=>{reload=resolve;});
    if(path==='/api/v1/queues/two')return Promise.resolve(second);
    return request(path);
  };
  const dispose=renderAgentQueue(parent,{request:fetch,command:async()=>({ok:true}),selection,openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await loaded();
  [...parent.querySelectorAll('.queue-item button')].find(node=>node.textContent==='Отменить').click();await loaded();
  assert.equal(typeof reload,'function');parent.querySelector('[data-lane-id="two"]').click();await loaded();
  const draft=parent.querySelector('.queue-detail textarea');draft.focus();draft.value='New lane draft';
  reload(lane);await loaded();
  assert.equal(document.activeElement,draft);assert.equal(draft.value,'New lane draft');assert.equal(selection.laneId,'two');
});
