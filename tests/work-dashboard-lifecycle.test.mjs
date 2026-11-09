import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {renderWorkAnalytics} from '../web/work-analytics.js';

function responsePair() {
  return {
    work:{totalMs:0,taskCount:0,projectCount:0,sessionCount:0,projects:[{id:'p',name:'Проект'}],kinds:['task'],
      tasks:[],byDay:[],byProject:[],byKind:[],nextOffset:null,taskDurationPercentiles:{p50Ms:null,p90Ms:null}},
    execution:{asOf:'2026-11-04T12:00:00Z',
      snapshot:{totalCount:2,activeSessionCount:1,reviewCount:0,attentionCount:0,openProblemCount:0,
        statuses:[{id:'in_progress',count:1},{id:'ready',count:1}],attention:[],attentionLimit:10},
      events:{byDay:[{label:'2026-11-04',createdCount:2,completedCount:0}],byWeek:[],createdCount:2,completedUniqueCount:0}},
  };
}

/** Flush actual response promises while keeping the owned refresh clock deterministic. */
const settled = () => new Promise(setImmediate);

function fixture(request) {
  const dom = new JSDOM('<main></main>', {url:'http://localhost',pretendToBeVisual:true});
  const {document,Event}=dom.window;
  Object.assign(globalThis,{document,Option:dom.window.Option});
  const clocks=new Map();
  let sequence=0;
  const nativeSetTimeout=dom.window.setTimeout.bind(dom.window);
  const nativeClearTimeout=dom.window.clearTimeout.bind(dom.window);
  dom.window.setTimeout=(fn,delay)=>{
    if(delay!==30_000)return nativeSetTimeout(fn,delay);
    const id=900_000+ ++sequence;clocks.set(id,{fn,delay});return id;
  };
  dom.window.clearTimeout=id=>{if(!clocks.delete(id))nativeClearTimeout(id);};
  const main=document.querySelector('main');
  const dispose=renderWorkAnalytics(main,{request,openIssue:()=>{},filters:{start:'2026-11-04',end:'2026-11-04'}});
  return {dom,document,main,clocks,Event,close(){dispose();dom.window.close();}};
}

test('automatic refresh pauses for unapplied filters and disposal cancels the owned clock',async()=>{
  const data=responsePair();let reads=0;
  const f=fixture(async path=>{reads++;return path.includes('/execution?')?data.execution:data.work;});
  await settled();
  assert.equal(reads,2);
  assert.match(f.main.textContent,/Карточки сейчас/);
  assert.match(f.main.textContent,/Нет учтённого времени/);
  assert.equal(f.clocks.size,1);
  f.main.querySelector('input[type=search]').value='не применено';
  const timer=[...f.clocks.values()][0];f.clocks.clear();timer.fn();
  await settled();
  assert.equal(reads,2);
  assert.match(f.main.querySelector('.work-refresh-state').textContent,/Фильтры изменены/);
  f.main.querySelector('input[type=search]').value='';
  const next=[...f.clocks.values()][0];f.clocks.clear();next.fn();
  await settled();
  assert.equal(reads,4);
  f.close();
  assert.equal(f.clocks.size,0);
});

test('a failed refresh retains the successful snapshot, timestamp and a retry path',async()=>{
  const data=responsePair();let failing=false;
  const f=fixture(async path=>{if(failing)throw Error('Временно недоступно');return path.includes('/execution?')?data.execution:data.work;});
  await settled();
  const before=f.main.querySelector('.work-refresh-state').textContent;
  failing=true;
  const timer=[...f.clocks.values()][0];f.clocks.clear();timer.fn();
  await settled();
  assert.match(f.main.textContent,/Временно недоступно/);
  assert.match(f.main.textContent,/предыдущего успешного обновления/);
  assert.match(f.main.textContent,/Карточки сейчас/);
  assert.equal(f.main.querySelector('.work-refresh-state').textContent,before);
  assert.equal(f.main.querySelector('.work-results').dataset.loadFailed,'true');
  failing=false;
  [...f.main.querySelectorAll('button')].find(node=>node.textContent==='Обновить дашборд').click();
  await settled();
  assert.equal(f.main.querySelector('.work-results').dataset.loadFailed,undefined);
  assert.equal(f.main.querySelector('.work-status').textContent,'');
  f.close();
});

test('obsolete cross-filter responses cannot replace the current zero-time inventory',async()=>{
  const data=responsePair();const pending=[];
  const f=fixture(path=>new Promise(resolve=>pending.push({path,resolve})));
  assert.equal(pending.length,2);
  f.main.querySelector('input[type=search]').value='current';
  f.main.querySelector('form').dispatchEvent(new f.Event('submit',{cancelable:true}));
  assert.equal(pending.length,4);
  const newest={...data.execution,snapshot:{...data.execution.snapshot,totalCount:7}};
  pending[2].resolve(data.work);pending[3].resolve(newest);await settled();
  const current=f.main.querySelector('.execution-kpis').textContent;
  assert.equal(f.main.querySelector('.execution-kpis dd').textContent,'7');
  pending[0].resolve(data.work);pending[1].resolve(data.execution);await settled();
  assert.equal(f.main.querySelector('.execution-kpis').textContent,current);
  f.close();
});

for (const interaction of ['help','filter','hidden','disabled']) {
  test('in-flight automatic refresh preserves a newly active '+interaction,async()=>{
    const data=responsePair();const delayed=[];
    let defer=false;
    const f=fixture(path=>{
      if(defer)return new Promise(resolve=>delayed.push({path,resolve}));
      return Promise.resolve(path.includes('/execution?')?data.execution:data.work);
    });
    await settled();defer=true;
    const original=f.main.querySelector('.execution-dashboard');
    const timer=[...f.clocks.values()][0];f.clocks.clear();timer.fn();
    assert.equal(delayed.length,2);
    let focused;
    if(interaction==='help'){
      focused=f.main.querySelector('button[aria-label="Пояснение: Карточки сейчас"]');
      focused.focus();focused.click();
    } else if(interaction==='filter'){
      focused=f.main.querySelector('select');
      focused.value='p';focused.focus();
    } else if(interaction==='disabled'){
      focused=f.main.querySelector('input[type=checkbox]');
      focused.checked=false;focused.focus();
      focused.dispatchEvent(new f.Event('change'));
    } else Object.defineProperty(f.document,'hidden',{value:true,configurable:true});
    delayed[0].resolve(data.work);
    delayed[1].resolve({...data.execution,snapshot:{...data.execution.snapshot,totalCount:9}});
    await settled();
    assert.equal(f.main.querySelector('.execution-dashboard'),original);
    if(focused) assert.equal(f.document.activeElement,focused);
    if(interaction==='help') assert.equal(focused.getAttribute('aria-expanded'),'true');
    if(interaction==='filter') assert.equal(focused.value,'p');
    assert.equal(f.main.querySelector('.execution-kpis dd').textContent,'2');
    f.close();
  });
}
