import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {orderSessions} from '../web/session-order.js';
import {renderAgentSessions} from '../web/agent-sessions.js';
import {renderCodexSessions} from '../web/codex-sessions.js';

/**
 * Install a disposable document and restore the prior environment after each test.
 * @param {object} t - Node test context owning teardown callbacks.
 * @returns {object} Parent container and its isolated window.
 */
function environment(t) {
  const dom=new JSDOM('<body><main></main>',{pretendToBeVisual:true,url:'http://127.0.0.1'});
  const previous=globalThis.document;globalThis.document=dom.window.document;
  t.after(()=>{globalThis.document=previous;dom.window.close();});
  return {parent:document.querySelector('main'),window:dom.window};
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));
const old={id:'old',title:'Old',agent:'test',protocol:'mcp',protocolVersion:1,revision:1,eventCount:0,
  updatedAt:'2026-10-28T12:00:00Z',projection:{foregroundState:'unknown'}};
const fresh={...old,id:'fresh',title:'Fresh',updatedAt:'2026-10-29T12:00:00Z'};

test('activity ordering handles timezone offsets, malformed dates and source metadata fallback',()=>{
  const entries=[{id:'none',activityAt:'bad'},old,fresh,{id:'offset',activityAt:'2026-10-29T14:30:00+03:00'}];
  assert.deepEqual(orderSessions(entries).map(x=>x.id),['fresh','offset','old','none']);
  assert.deepEqual(orderSessions(entries,'oldest').map(x=>x.id),['old','offset','fresh','none']);
  assert.equal(entries[0].id,'none');
});

test('submillisecond activity takes precedence over the stable identity tie breaker',()=>{
  const entries=[{id:'z',updatedAt:'2026-10-29T12:00:00.000001Z'},
    {id:'a',updatedAt:'2026-10-29T12:00:00.000002Z'}];
  assert.deepEqual(orderSessions(entries).map(item=>item.id),['a','z']);
});

test('newest default and switching order preserve the selected form, unsaved input and focus',async t=>{
  const {parent,window}=environment(t),selection={},paths=[];
  const request=async path=>{
    paths.push(path);
    if(path.startsWith('/api/v1/codex-sessions'))return {available:true,items:[]};
    if(path.startsWith('/api/v1/sessions?'))return {items:path.includes('sort=oldest')?[old,fresh]:[fresh,old],hasMore:false};
    if(path.includes('/events?'))return {items:[],hasMore:false};
    return path.endsWith('fresh')?fresh:old;
  };
  const dispose=renderAgentSessions(parent,{request,selection,command:()=>{throw Error('Unexpected mutation');},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();await tick();
  const ids=()=>[...parent.querySelectorAll('[data-session-id]')].map(n=>n.dataset.sessionId);
  assert.deepEqual(ids(),['fresh','old']);assert.equal(selection.id,'fresh');
  const input=parent.querySelector('.session-link-form input');input.value='UNSAVED';input.focus();
  const select=parent.querySelector('[data-session-sort]');
  assert.equal(select.value,'newest');assert.equal(select.closest('label').textContent.includes('Сортировка'),true);
  select.value='oldest';select.dispatchEvent(new window.Event('change',{bubbles:true}));
  await tick();await tick();
  assert.deepEqual(ids(),['old','fresh']);assert.equal(selection.sort,'oldest');
  assert.equal(parent.querySelector('.session-link-form input'),input);
  assert.equal(input.value,'UNSAVED');assert.equal(document.activeElement,input);assert.equal(selection.id,'fresh');
  assert.equal(paths.filter(p=>p==='/api/v1/sessions/fresh').length,1);
});

test('activity refresh reorders reused nodes and removes expired history without replacing the detail form',async t=>{
  const {parent}=environment(t),previousInterval=globalThis.setInterval,previousClear=globalThis.clearInterval;
  let poll,items=[fresh,old];globalThis.setInterval=callback=>(poll=callback,88);globalThis.clearInterval=()=>{};
  t.after(()=>{globalThis.setInterval=previousInterval;globalThis.clearInterval=previousClear;});
  const request=async path=>path==='/api/v1/codex-sessions'?{available:true,items:[]}:
    path.startsWith('/api/v1/sessions?')?{items,hasMore:false}:path.includes('/events?')?{items:[],hasMore:false}:fresh;
  const dispose=renderAgentSessions(parent,{request,selection:{history:true},command:async()=>{},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();await tick();
  const control=parent.querySelector('[data-session-id="old"]'), input=parent.querySelector('.session-link-form input');
  input.value='draft';control.focus();
  items=[{...old,updatedAt:'2026-10-29T13:00:00Z'},fresh];await poll();
  assert.equal(parent.querySelector('[data-session-id]'),control);assert.equal(document.activeElement,control);
  assert.equal(parent.querySelector('.session-link-form input'),input);
  input.focus();items=[fresh];await poll();
  assert.equal(parent.querySelector('[data-session-id="old"]'),null);assert.equal(input.value,'draft');
});

test('sort invalidates a delayed page and keeps opaque continuation tokens intact',async t=>{
  const {parent,window}=environment(t);let resolvePage;const paths=[];
  const request=async path=>{
    paths.push(path);
    if(path==='/api/v1/codex-sessions')return {available:true,items:[]};
    if(path.startsWith('/api/v1/sessions?')) {
      if(path.includes('after=opaque%2B%2F%3D'))return new Promise(resolve=>{resolvePage=resolve;});
      return path.includes('sort=oldest')?{items:[old],hasMore:false}:{items:[fresh],hasMore:true,nextCursor:'opaque+/='};
    }
    if(path.includes('/events?'))return {items:[],hasMore:false};
    return fresh;
  };
  const dispose=renderAgentSessions(parent,{request,selection:{history:true},command:async()=>{},openIssue:()=>{},onError:error=>{throw error;}});t.after(dispose);
  await tick();await tick();parent.querySelector('[data-sessions-more]').click();await tick();
  const sort=parent.querySelector('[data-session-sort]');sort.value='oldest';sort.dispatchEvent(new window.Event('change'));
  await tick();resolvePage({items:[fresh],hasMore:false});await tick();await tick();
  assert.deepEqual([...parent.querySelectorAll('[data-session-id]')].map(n=>n.dataset.sessionId),['old']);
  assert.ok(paths.some(p=>p.includes('after=opaque%2B%2F%3D')));
});

test('Codex lists reorder already mounted rows in both directions on source refresh',async t=>{
  const {parent}=environment(t);let order='newest',items=[{...old,state:'running'},{...fresh,state:'running'}];
  const view=renderCodexSessions(parent,{request:async()=>({available:true,items}),getSort:()=>order});t.after(view.dispose);
  await view.refresh();
  const titles=()=>[...parent.querySelectorAll('.codex-session-row h3')].map(n=>n.textContent);
  assert.deepEqual(titles(),['Fresh','Old']);
  order='oldest';view.reorder();assert.deepEqual(titles(),['Old','Fresh']);
  const node=parent.querySelector('.codex-session-row');
  items=[{...old,updatedAt:'2026-10-29T13:00:00Z',state:'running'},{...fresh,state:'running'}];
  await view.refresh();assert.deepEqual(titles(),['Fresh','Old']);
  assert.equal(parent.querySelectorAll('.codex-session-row')[1],node);
});
