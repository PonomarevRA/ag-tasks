/** UI boundary tests keep incident evidence inert and user action tokens out of read requests. */
import test from 'node:test';
import assert from 'node:assert/strict';
import {element, evidenceLink, button, report} from '../web/dom.js';
import {connect, request} from '../web/api.js';

class Element {
  /** Observe plain text and attributes without providing an HTML parser. */
  constructor(tag) { this.tagName=tag; this.children=[]; this.handlers={}; this.dataset={}; this.style={}; }
  set innerHTML(value) { throw new Error('HTML insertion is forbidden'); }
  append(...nodes) { this.children.push(...nodes); }
  addEventListener(name,handler) { this.handlers[name]=handler; }
  setAttribute(name,value) {this[name]=value;}
  getAttribute(name) {return this[name]??null;}
}
globalThis.document={createElement: tag => new Element(tag)};

test('untrusted issue/evidence content remains plain text', () => {
  const title=element('strong','<img src=x onerror=alert(1)>');
  assert.equal(title.textContent,'<img src=x onerror=alert(1)>');
  const link=evidenceLink({url:'http://127.0.0.1:8890/?incidentId=abc',title:'<script>bad</script>'});
  assert.equal(link.textContent,'<script>bad</script>');
  assert.equal(link.rel,'noopener noreferrer');
  assert.equal(evidenceLink({url:'javascript:alert(1)'}),null);
  assert.equal(evidenceLink({url:'https://secret:password@example.com/'}),null);
});

test('UI actions disable their own control until completion', async () => {
  let release;
  const pending=new Promise(resolve => {release=resolve;});
  const control=button('Run',() => pending);
  const action=control.handlers.click();
  assert.equal(control.disabled,true);
  release(); await action;
  assert.equal(control.disabled,false);
});

test('UI token appears only on intentional mutation and server messages are surfaced', async t => {
  const cache=new Map();globalThis.sessionStorage={getItem:key=>cache.get(key)??null,setItem:(key,value)=>cache.set(key,value)};
  t.after(()=>{delete globalThis.sessionStorage;});
  const calls=[];
  globalThis.fetch=async (path,init) => {
    calls.push({path,init});
    return {ok:true,json:async()=> path.endsWith('ui-session')?{token:'first-party'}:{items:[]}};
  };
  await connect(); await request('/api/v1/issues'); await request('/api/v1/issues','POST',{title:'Example'});
  assert.equal(calls[1].init.headers['X-Ag-Tasks-UI'],undefined);
  assert.equal(calls[2].init.headers['X-Ag-Tasks-UI'],'first-party');
  assert.equal(calls[2].path,'/api/v1/commands');
  const sent=JSON.parse(calls[2].init.body);
  assert.equal(sent.operation,'issue.create');
  assert.deepEqual(sent.payload,{title:'Example'});
  assert.ok(sent.commandId && sent.clientId);
  globalThis.fetch=async()=>({ok:false,status:409,json:async()=>({error:{message:'stale_review'}})});
  await assert.rejects(request('/api/v1/issues/x/approve-merge','POST',{}),/stale_review/);
});


test('nested editor failures are reported in the focused modal even when DOM order differs', () => {
  const previous=globalThis.document;
  const notice={}, active=new Element('dialog'), underlying=new Element('dialog');
  active.querySelector=()=>null;
  globalThis.document={createElement:tag=>new Element(tag),querySelector:()=>notice,querySelectorAll:()=>[active,underlying],activeElement:{closest:()=>active}};
  report(new Error('invalid_field'));
  assert.equal(active.children[0].textContent,'invalid_field');
  assert.equal(underlying.children.length,0);
  globalThis.document=previous;
});

test('stalled HTTP is aborted with a finite visible timeout and timer cleanup', async () => {
  const previousSet=globalThis.setTimeout, previousClear=globalThis.clearTimeout;
  const cleared=[];
  globalThis.setTimeout=callback=>{queueMicrotask(callback);return 42;};
  globalThis.clearTimeout=timer=>cleared.push(timer);
  globalThis.fetch=async(path,init)=>new Promise((resolve,reject)=>{
    init.signal.addEventListener('abort',()=>reject(new DOMException('aborted','AbortError')));
  });
  try {
    await assert.rejects(request('/api/v1/issues'),/не ответил вовремя/);
    assert.deepEqual(cleared,[42]);
  } finally {globalThis.setTimeout=previousSet;globalThis.clearTimeout=previousClear;}
});

test('sidebar toolbar, shortcut and browser button share persisted icon-rail state', async () => {
  const {bindSidebar}=await import('../web/sidebar.js');
  const classes=new Set(), handlers={}, fields={}, storage=new Map();
  const toggle=new Element('button');
  const documentRef={documentElement:{classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)}},querySelector:()=>toggle};
  const windowRef={setTimeout:()=>1,clearTimeout:()=>{},localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value)},addEventListener:(name,handler)=>{handlers[name]=handler;}};
  bindSidebar(documentRef,windowRef);
  assert.equal(toggle['aria-expanded'],'true');
  handlers['ag-tasks:toggle-sidebar']();
  assert(classes.has('sidebar-collapsed'));
  assert.equal(toggle['aria-expanded'],'false');
  assert.equal(storage.get('ag-tasks.sidebar-collapsed'),'1');
  let prevented=false;
  handlers.keydown({metaKey:true,shiftKey:true,key:'S',preventDefault:()=>{prevented=true;}});
  assert(prevented && !classes.has('sidebar-collapsed'));
  classes.add('native-toolbar'); handlers.keydown({metaKey:true,shiftKey:true,code:'KeyS',key:'s',preventDefault:()=>assert.fail('Native owns this shortcut')});
  assert(!classes.has('sidebar-collapsed')); classes.delete('native-toolbar');
  toggle.handlers.click(); assert(classes.has('sidebar-collapsed'));
  bindSidebar(documentRef,windowRef); assert(classes.has('sidebar-collapsed'));
});

test('native presentation notification is zero-argument, debounced and disposed on page exit', async () => {
  const {bindSidebar}=await import('../web/sidebar.js');
  const classes=new Set(), handlers={}, timers=[], calls=[], cleared=[];
  const toggle=new Element('button'); let observerDisconnected=false;
  class Resize {
    constructor(callback) {this.callback=callback;}
    observe() {}
    disconnect() {observerDisconnected=true;}
  }
  const documentRef={documentElement:{classList:{toggle:(name,on)=>on?classes.add(name):classes.delete(name),contains:name=>classes.has(name)}},querySelector:()=>toggle};
  const windowRef={localStorage:{getItem:()=>null,setItem:()=>{}},ResizeObserver:Resize,
    setTimeout:callback=>{timers.push(callback);return timers.length;},clearTimeout:timer=>cleared.push(timer),
    addEventListener:(name,callback)=>{handlers[name]=callback;},pywebview:{api:{standalone_sidebar_changed:(...args)=>{calls.push(args);return Promise.resolve();}}}};
  bindSidebar(documentRef,windowRef);
  handlers['ag-tasks:toggle-sidebar'](); handlers.pywebviewready();
  assert.equal(timers.length,1);
  timers[0](); await Promise.resolve(); assert.deepEqual(calls,[[]]);
  handlers['ag-tasks:toggle-sidebar'](); assert.equal(timers.length,2);
  handlers.pagehide(); assert.deepEqual(cleared,[2]);assert(observerDisconnected);
});
