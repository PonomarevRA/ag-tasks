import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {renderTaskGraph} from '../web/task-graph.js';

/** Render actual graph/controller with deterministic geometry, without pretending jsdom performs browser layout. */
function environment(t) {
 const dom=new JSDOM('<main></main>',{url:'http://localhost',pretendToBeVisual:true});
 const previous=new Map();let sequence=0;const frames=new Map();
 const raf=cb=>{frames.set(++sequence,cb);return sequence};const cancel=id=>frames.delete(id);
 dom.window.requestAnimationFrame=raf;dom.window.cancelAnimationFrame=cancel;
 for(const [key,value] of Object.entries({window:dom.window,document:dom.window.document,requestAnimationFrame:raf,cancelAnimationFrame:cancel})) {
  previous.set(key,Object.getOwnPropertyDescriptor(globalThis,key));Object.defineProperty(globalThis,key,{value,writable:true,configurable:true});
 }
 t.after(()=>{dom.window.close();for(const [key,descriptor]of previous){if(descriptor)Object.defineProperty(globalThis,key,descriptor);else delete globalThis[key]}});
 return {document:dom.window.document,window:dom.window,frames,flush(){const batch=[...frames.values()];frames.clear();for(const cb of batch)cb()}};
}
const nodes=[{id:'a',key:'T-1',title:'Parent',status:'ready',work:{elapsedMs:0}},{id:'b',key:'T-2',title:'Child',status:'ready',parentId:'a',work:{elapsedMs:0}}];
const graph={nodes,edges:[{from:'a',to:'b',kind:'parent'}]};

test('connectors use rendered scene scale and preserve original card action',t=>{
 const x=environment(t),container=x.document.querySelector('main');let opened;
 x.window.localStorage.setItem('board:a',JSON.stringify({x:-80,y:30,scale:2}));
 const dispose=renderTaskGraph(container,graph,{storageKey:'board:a',onOpen:id=>opened=id});
 const scene=container.querySelector('.task-graph-scene');
 Object.defineProperties(scene,{offsetWidth:{value:800},clientWidth:{value:800},clientHeight:{value:400}});
 scene.getBoundingClientRect=()=>({left:-80,top:30,width:1600,height:800});
 const cards=container.querySelectorAll('.task-graph-card');
 cards[0].getBoundingClientRect=()=>({left:0,right:400,top:130,height:200,width:400});
 cards[1].getBoundingClientRect=()=>({left:520,right:920,top:230,height:200,width:400});
 x.flush();
 assert.equal(container.querySelector('path[data-kind]').getAttribute('d'),'M240,100 C270,100 270,150 300,150');
 cards[1].click();assert.equal(opened,'b');
 dispose();assert.equal(x.frames.size,0);
});

test('camera and pattern survive graph replacement and stay scoped to a space',t=>{
 const x=environment(t),container=x.document.querySelector('main');
 let dispose=renderTaskGraph(container,graph,{storageKey:'board:a',onOpen:()=>{}});
 container.querySelector('[aria-label="Увеличить масштаб (+)"]').click();
 const pattern=container.querySelector('[aria-label="Заливка"]');pattern.value='grid';pattern.dispatchEvent(new x.window.Event('change'));
 x.flush();dispose();container.replaceChildren();
 dispose=renderTaskGraph(container,graph,{storageKey:'board:a',onOpen:()=>{}});
 assert.match(container.querySelector('.task-graph-scene').style.transform,/scale\(1.2\)/);
 assert.equal(container.querySelector('.ag-board').dataset.boardPattern,'grid');dispose();container.replaceChildren();
 dispose=renderTaskGraph(container,graph,{storageKey:'board:b',onOpen:()=>{}});
 assert.match(container.querySelector('.task-graph-scene').style.transform,/scale\(1\)/);
 assert.equal(container.querySelector('.ag-board').dataset.boardPattern,'dots');dispose();
});

test('initial camera frame sizes the board after the final header layout',t=>{
 const x=environment(t),container=x.document.querySelector('main');
 const dispose=renderTaskGraph(container,graph,{onOpen:()=>{}});
 const viewport=container.querySelector('.ag-board');viewport.getBoundingClientRect=()=>({top:300});
 x.flush();assert.equal(viewport.style.height,`${x.window.innerHeight-300-24}px`);dispose();
});
