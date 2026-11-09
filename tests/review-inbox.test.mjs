import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderReviewInbox} from '../web/review-inbox.js';
import {renderLifecycle} from '../web/task-workflow.js';

/** Mount only a synthetic local root; no user browser or production server is controlled. */
function root(t) {
  const dom = new JSDOM('<main></main>', {url:'http://127.0.0.1:8895'});
  globalThis.document = dom.window.document; globalThis.Option = dom.window.Option;
  t.after(() => { delete globalThis.document; delete globalThis.Option; dom.window.close(); });
  return {dom, container:document.querySelector('main')};
}

test('current review shows owner, age, target and an explicit assignment without automatically posting', async t => {
  const {dom, container} = root(t);
  const calls = [], opens = [];
  const issue = {id:'critic',key:'AGT-2',title:'Review <img onerror=x>',status:'ready',kind:'review',createdAt:'2026-10-22T12:00:00Z',
    lifecycle:{phase:'review_pending',needsAttention:true},handoff:{state:'reviewer_required'},reviewTarget:{id:'producer',key:'AGT-1',generation:2}};
  const dispose = renderReviewInbox(container,[issue],{spaceId:'default',onOpen:id=>opens.push(id),onRefresh:async()=>{},
    request:async(path,method='GET',body)=>{calls.push({path,method,body});return{items:[]};}});
  t.after(dispose);
  await Promise.resolve();
  assert.equal(calls.filter(x=>x.method==='POST').length,0);
  assert.match(container.textContent,/Рецензент: не назначен/);
  assert.match(container.textContent,/Ожидание:/);
  assert.equal(container.querySelectorAll('img').length,0);
  [...container.querySelectorAll('button')].find(b=>b.textContent.includes('Результат AGT-1')).click();
  await Promise.resolve();
  assert.deepEqual(opens,['producer']);
  container.querySelector('input').value='independent-critic';
  [...container.querySelectorAll('button')].find(b=>b.textContent.includes('Передать выбранному')).click();
  await new Promise(resolve=>setTimeout(resolve,0));
  const assigned = calls.find(c=>c.method==='POST');
  assert.equal(assigned.path,'/api/v1/issues/critic/review-assignment');
  assert.deepEqual(assigned.body,{reviewerId:'independent-critic',laneId:null});
});

test('failed assignment retains entered identity and surfaces recovery; disposed queue load has no effects', async t => {
  const {container} = root(t);
  let resolveQueues;
  const dispose=renderReviewInbox(container,[{id:'critic',key:'AGT-2',title:'Review',kind:'review',status:'ready',handoff:{state:'reviewer_required'}}],{
    spaceId:'default',onOpen:()=>{},onRefresh:async()=>assert.fail('failed assignment cannot refresh'),
    request:(path,method='GET')=>method==='GET'?new Promise(resolve=>{resolveQueues=resolve;}):Promise.reject(new Error('Assignment unavailable'))});
  container.querySelector('input').value='critic';
  [...container.querySelectorAll('button')].find(b=>b.textContent.includes('Передать выбранному')).click();
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(container.querySelector('input').value,'critic');
  assert.match(container.querySelector('[role=status]').textContent,/Assignment unavailable/);
  dispose(); resolveQueues({items:[{id:'late',title:'Late queue'}]});
  await Promise.resolve();
  assert.equal(container.querySelector('select').options.length,1);
});

test('blocked and expired work give distinct recovery context', t => {
  const {container}=root(t);
  renderLifecycle(container,{lifecycle:{phase:'blocked',needsAttention:true},work:{nextAction:'Restore test access'}});
  renderLifecycle(container,{lifecycle:{phase:'lease_expired',needsAttention:true},work:{}});
  assert.match(container.textContent,/Restore test access/);
  assert.match(container.textContent,/Сессия истекла/);
  assert.doesNotMatch(container.textContent,/Нужна доработка/);
});

test('route save remains owned and disabled until its POST completes', async t => {
  const {container}=root(t);
  let finish;
  let posts=0;
  const dispose=renderReviewInbox(container,[],{spaceId:'default',onOpen:()=>{},onRefresh:async()=>{},request:(path,method='GET')=>{
    if(method==='GET') return Promise.resolve({items:[]});
    posts++; return new Promise(resolve=>{finish=resolve;});
  }});
  t.after(dispose);
  container.querySelector('input').value='critic';
  const control=[...container.querySelectorAll('button')].find(b=>b.textContent.includes('Применять'));
  control.click();
  await Promise.resolve();
  assert.equal(control.disabled,true);
  control.click();
  assert.equal(posts,1);
  finish({});
  await new Promise(resolve=>setTimeout(resolve,0));
  assert.equal(control.disabled,false);
});
