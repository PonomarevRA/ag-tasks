import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderTaskConversations} from '../web/task-conversations.js';

/** Mount a private DOM without controlling any user application or harness. */
function setup(t) {
  const dom = new JSDOM('<main></main>');
  globalThis.document = dom.window.document;
  t.after(() => {delete globalThis.document; dom.window.close();});
  return document.querySelector('main');
}

test('chat metadata remains text and does not claim saved connections are active work', t => {
  const root = setup(t);
  renderTaskConversations(root, {id:'task', conversations:{items:[
    {id:'one', title:'<img src=x>', harness:'codex',remoteSessionId:'chat-one'},
    {id:'two', title:'Continuation', harness:'cursor',remoteSessionId:'chat-two',continuedFrom:'one'},
  ],hasMore:false}}, {request:async()=>assert.fail('initial list requires no request')});
  assert.equal(root.querySelectorAll('li').length,2);
  assert.equal(root.querySelectorAll('img').length,0);
  assert.match(root.textContent,/Продолжение: <img src=x>/);
  assert.match(root.textContent,/не подтверждает активную работу/);
  assert.equal(root.querySelector('button').hidden,true);
});

test('failed page retains rows and supports retry while late pages cannot repaint another task', async t => {
  const root = setup(t);
  let current = true, resolve;
  renderTaskConversations(root,{id:'a',conversations:{items:[{id:'one',title:'One',harness:'codex',remoteSessionId:'one'}],hasMore:true,nextCursor:1}}, {
    isCurrent:()=>current, request:async()=>new Promise(r=>{resolve=r;})});
  root.querySelector('button').click();
  await Promise.resolve();
  assert.equal(root.querySelector('button').disabled,true);
  assert.equal(root.querySelector('ul').getAttribute('aria-busy'),'true');
  assert.equal(root.querySelector('[role=status]').parentElement,root);
  current=false;
  resolve({items:[{id:'late',title:'Late',harness:'cursor',remoteSessionId:'late'}],hasMore:false});
  await new Promise(r=>setTimeout(r,0));
  assert.equal(root.querySelectorAll('li').length,1);
});

test('empty, older-server and failed pagination remain distinguishable', async t => {
  const root = setup(t);
  renderTaskConversations(root,{id:'a'}, {request:async()=>({items:[]})});
  assert.match(root.textContent,/недоступен в этой версии/);
  root.replaceChildren();
  renderTaskConversations(root,{id:'a',conversations:{items:[],hasMore:false}}, {request:async()=>({items:[]})});
  assert.match(root.textContent,/пока нет/);
  root.replaceChildren();
  let attempts=0;
  renderTaskConversations(root,{id:'a',conversations:{items:[{id:'one',title:'One'}],hasMore:true,nextCursor:1}}, {
    request:async()=>{attempts++; if(attempts===1) throw new Error('offline'); return {items:[{id:'two',title:'Two'}],hasMore:false};}});
  const more=root.querySelector('button');more.click();
  await new Promise(r=>setTimeout(r,0));
  assert.match(root.querySelector('[role=status]').textContent,/Не удалось/);
  assert.equal(root.querySelectorAll('li').length,1);
  more.click();await new Promise(r=>setTimeout(r,0));
  assert.equal(root.querySelectorAll('li').length,2);
  assert.equal(more.hidden,true);
});
