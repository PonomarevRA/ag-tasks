import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderCodexSessions} from '../web/codex-sessions.js';
import {renderAgentSessions} from '../web/agent-sessions.js';

/** Keep each consumer DOM isolated, including its visibility and polling resources. */
function environment(t) {
  const dom=new JSDOM('<body><main></main>',{url:'http://127.0.0.1',pretendToBeVisual:true});
  const previous=globalThis.document;globalThis.document=dom.window.document;
  t.after(()=>{globalThis.document=previous;dom.window.close();});
  return document.querySelector('main');
}
const tick=()=>new Promise(resolve=>setImmediate(resolve));

test('Codex source reports actual states as text and removes closed writers; failure is not an empty success',async t=>{
  const parent=environment(t);let snapshot={available:true,items:[
    {id:'one',title:'<img src=x>',state:'running',activityAt:'now'},
    {id:'two',title:'Waiting',state:'idle',activityAt:'then'}]};
  const view=renderCodexSessions(parent,{request:async()=>snapshot});t.after(view.dispose);
  await view.refresh();assert.equal(parent.querySelector('img'),null);
  assert.match(parent.textContent,/Выполняется · по журналу/);assert.match(parent.textContent,/Ожидает сообщения/);
  snapshot={available:false,items:[]};await view.refresh();
  assert.match(parent.textContent,/Источник Codex недоступен/);
  assert.doesNotMatch(parent.textContent,/Выполняется · по журналу/);
  snapshot={available:true,items:[]};await view.refresh();
  assert.equal(parent.querySelectorAll('.codex-session-row').length,0);
  assert.match(parent.textContent,/нет открытых локальных чатов/);
});

test('background refresh preserves unsaved task link and focus and cannot issue commands',async t=>{
  const parent=environment(t);const oldInterval=globalThis.setInterval,oldClear=globalThis.clearInterval;
  let poll,cleared=false;globalThis.setInterval=callback=>{poll=callback;return 77;};globalThis.clearInterval=id=>{cleared=id===77;};
  t.after(()=>{globalThis.setInterval=oldInterval;globalThis.clearInterval=oldClear;});
  let presence='connected',reads=0;
  const session={id:'session',title:'Codex MCP',agent:'Codex',protocol:'mcp',protocolVersion:1,eventCount:0,revision:1,
    issueId:null,projection:{foregroundState:'unknown'}};
  const request=async path=>{
    reads++;
    if(path==='/api/v1/codex-sessions')return {available:true,items:[]};
    if(path.startsWith('/api/v1/sessions?'))return {items:[{...session,connection:{state:presence}}],hasMore:false};
    if(path.includes('/events?'))return {items:[],hasMore:false};
    return {...session,connection:{state:presence}};
  };
  const dispose=renderAgentSessions(parent,{request,command:()=>{throw Error('Unexpected command');},selection:{},openIssue:()=>{},onError:error=>{throw error;}});
  await tick();await tick();
  const input=parent.querySelector('.session-link-form input');input.value='AGT-draft';input.focus();
  presence='stale';await poll();
  assert.equal(parent.querySelector('.session-link-form input'),input);assert.equal(input.value,'AGT-draft');assert.equal(document.activeElement,input);
  assert.match(parent.querySelector('.agent-session-list').textContent,/Связь MCP не подтверждена/);
  assert.match(parent.querySelector('[data-connection-status]').textContent,/не подтверждена/);
  dispose();const before=reads;await poll();assert.equal(reads,before);assert.equal(cleared,true);
});
