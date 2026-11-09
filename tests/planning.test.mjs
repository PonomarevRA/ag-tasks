import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {nameColor, applyIdentity} from '../web/task-colors.js';
import {markdownBody, openDocument, renderDocuments} from '../web/task-documents.js';
import {renderPlanning} from '../web/task-planning.js';

/** Use real DOM semantics with narrow network/clipboard fixtures; no browser-heap claims derive from jsdom. */
function environment(t) {
  const dom = new JSDOM('<body><p id="notice"></p><button id="opener">File</button><main></main>', {url:'http://127.0.0.1:8895'});
  const previous = new Map();
  const values = {document:dom.window.document, Option:dom.window.Option, navigator:{clipboard:{writeText:async () => {}}}};
  for (const [key,value] of Object.entries(values)) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis,key));
    Object.defineProperty(globalThis,key,{configurable:true,writable:true,value});
  }
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open=true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open=false; this.dispatchEvent(new dom.window.Event('close')); };
  const originalFetch=globalThis.fetch;
  t.after(() => {
    dom.window.close(); globalThis.fetch=originalFetch;
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis,key,descriptor); else delete globalThis[key];
    }
  });
  return {document:dom.window.document, window:dom.window, parent:dom.window.document.querySelector('main')};
}

test('colours are deterministic, Unicode-normalized and scoped by space; text does not rely on colour', t => {
  const {parent}=environment(t);
  assert.equal(nameColor('Portfolio'),nameColor('portfolio'));
  assert.equal(nameColor('é'),nameColor('e\u0301'));
  assert.notEqual(nameColor('Implement','Portfolio'),nameColor('Implement','Calculator'));
  const node=document.createElement('button'); node.textContent='HP-12 · Implement';
  applyIdentity(node,'Implement','Portfolio'); parent.append(node);
  assert.equal(node.textContent,'HP-12 · Implement');
  assert.match(node.style.getPropertyValue('--identity-color'),/^hsl\(/);
});

test('Markdown view wraps wide tables and adds explicit code-copy without changing source', async t => {
  const {parent}=environment(t);
  let copied;
  navigator.clipboard.writeText=async value => {copied=value;};
  parent.append(markdownBody('<h1>Goal</h1><table><tr><td>Wide data</td></tr></table><pre><code>print(&quot;safe&quot;)</code></pre>'));
  assert.equal(parent.querySelector('.markdown-table').tabIndex,0);
  parent.querySelector('.copy-code').click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(copied,'print("safe")');
});

test('Markdown chips open an expandable file viewer with source, download and focus return', async t => {
  const {parent}=environment(t);
  const calls=[];
  globalThis.fetch=async (path, init) => {
    calls.push([path,init.method]);
    return {ok:true,json:async()=>({id:'doc',name:'plan.md',content:'# Goal',html:'<h1>Goal</h1>',revision:2})};
  };
  const issue={id:'task',key:'HP-12',kind:'rnd',documents:[{id:'doc',name:'plan.md',purpose:'plan',revision:2}]};
  renderDocuments(parent,issue,async()=>{});
  const chip=parent.querySelector('.document-chip'); chip.focus(); chip.click();
  await new Promise(resolve=>setImmediate(resolve));
  const dialog=document.querySelector('.document-dialog');
  assert.equal(dialog.open,true);
  assert.equal(dialog.querySelector('h1').textContent,'Goal');
  [...dialog.querySelectorAll('button')].find(node=>node.textContent==='Развернуть').click();
  assert.equal(dialog.classList.contains('document-expanded'),true);
  [...dialog.querySelectorAll('button')].find(node=>node.textContent==='Исходник').click();
  assert.equal(dialog.querySelector('.markdown-source').hidden,false);
  assert.match(dialog.querySelector('a[download]').href,/documents\/doc\/download$/);
  dialog.close();
  assert.equal(document.activeElement,chip);
  assert.equal(document.querySelector('#task-document'),null);
  assert.ok(calls.every(entry=>entry[1]==='GET'));
});

test('stale tasks show explicit human revalidation and findings keep hostile text inert', async t => {
  const {parent}=environment(t);
  const issue={id:'task',key:'HP-12',kind:'task',status:'stale',definitionRevision:1,problemsCount:1,
    problems:{items:[{id:'problem',title:'<img src=x onerror=alert(1)>',details:'Failure',state:'open',severity:'high',actor:'agent'}]}};
  renderPlanning(parent,issue,async()=>{},async()=>{});
  assert.match(parent.textContent,/Протухла · выполнение запрещено/);
  assert.equal(parent.querySelector('img'),null);
  assert.ok([...parent.querySelectorAll('button')].some(node=>node.textContent==='Переактуализировать'));
  assert.ok([...parent.querySelectorAll('button')].some(node=>node.textContent==='План исправления'));
});


test('closing the task during a delayed file read cannot open a late orphan viewer', async t => {
  const {parent}=environment(t);
  const host=document.createElement('dialog'); document.body.append(host); host.append(parent); host.showModal();
  let finish;
  globalThis.fetch=()=>new Promise(resolve=>{finish=()=>resolve({ok:true,json:async()=>({name:'plan.md',content:'# Goal',html:'<h1>Goal</h1>',revision:1})});});
  renderDocuments(parent,{id:'task',key:'HP-1',kind:'rnd',documents:[{id:'doc',name:'plan.md'}]},async()=>{});
  const chip=parent.querySelector('.document-chip'); chip.focus(); chip.click(); host.close(); finish();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(document.querySelector('#task-document'),null);
});
