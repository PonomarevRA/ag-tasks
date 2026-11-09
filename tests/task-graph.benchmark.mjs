/** Report actual Node/jsdom model and DOM costs; no browser layout, browser heap or native RSS claim. */
import {JSDOM} from 'jsdom';
import {performance} from 'node:perf_hooks';
import {readFile} from 'node:fs/promises';
import {createHash} from 'node:crypto';
import {renderTaskGraph, groupTaskGraph} from '../web/task-graph.js';

const dom = new JSDOM('<main id="content"></main>', {pretendToBeVisual:true,url:'http://127.0.0.1:8895'});
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
const container = document.querySelector('#content');
const nodes = Array.from({length:200}, (_, index) => ({
  id:`task-${index}`,key:`TASK-${index}`,title:`Measured task ${index}`,spaceId:'default',
  parentId:index % 20 ? `task-${index - index % 20}` : null,
  intendedRole:'developer',kind:'task',status:'ready',work:{elapsedMs:0},
}));
const edges = nodes.filter(node => node.parentId).map(node => ({from:node.parentId,to:node.id,kind:'parent'}));
for (let index=20; index<200; index+=20) edges.push({from:`task-${index-20}`,to:`task-${index}`,kind:'blocks'});
const graph = {nodes,edges,stubs:[]};
const options = {statuses:{ready:'Готово к работе'},onOpen:() => {}};

/** Report nearest-rank percentiles from the actual trial population, including the exact count. */
function distribution(values) {
  const sorted = [...values].sort((a,b) => a-b);
  return {samples:sorted.length,p50:sorted[Math.ceil(sorted.length*.5)-1],p95:sorted[Math.ceil(sorted.length*.95)-1],max:sorted.at(-1)};
}

/** Force repeated full GC only when the caller explicitly enables the Node measurement flag. */
function collect() {
  for (let count=0; count<3; count++) globalThis.gc?.();
}

const modelTimes = [], mountTimes = [], disposalTimes = [];
for (let trial=0; trial<35; trial++) {
  let started = performance.now();
  groupTaskGraph(nodes);
  const model = performance.now() - started;
  started = performance.now();
  let dispose = renderTaskGraph(container, graph, options);
  const mount = performance.now() - started;
  started = performance.now();
  dispose();
  dispose = null;
  container.replaceChildren();
  const disposal = performance.now() - started;
  // Warm-up trials are disclosed and excluded consistently from every distribution.
  if (trial >= 5) { modelTimes.push(model); mountTimes.push(mount); disposalTimes.push(disposal); }
  await new Promise(resolve => setTimeout(resolve,0));
}
const memory = [];
if (globalThis.gc) {
  for (let trial=0; trial<4; trial++) {
    collect();
    const baseline = process.memoryUsage().heapUsed;
    let dispose = renderTaskGraph(container,graph,options);
    collect();
    const live = process.memoryUsage().heapUsed;
    dispose();
    dispose = null;
    container.replaceChildren();
    await new Promise(resolve => setTimeout(resolve,0));
    collect();
    const after = process.memoryUsage().heapUsed;
    memory.push({liveHeapDeltaBytes:live-baseline,postDisposalSignedDeltaBytes:after-baseline});
  }
}
const source = await readFile(new URL('../web/task-graph.js',import.meta.url));
const shared = JSON.parse(await readFile(new URL('../web/vendor/ag-design/manifest.json',import.meta.url),'utf8'));
const report = {
  contractVersion:'ag-tasks-workflow/v2',sharedComponentVersion:shared.version,
  graphSourceSha256:createHash('sha256').update(source).digest('hex'),
  scenario:'ag-tasks-v2 task graph: 200 real fixture cards, 199 actual declared edges, mount/dispose',
  environment:{runtime:process.version,engine:'Node/V8 + jsdom 26.1.0',platform:process.platform,architecture:process.arch,gc:typeof globalThis.gc==='function'},
  scope:'Model and DOM creation only. jsdom has no layout engine; SVG geometry, browser heap, native RSS and leak absence are not established.',
  nodes:nodes.length,edges:edges.length,warmupTrials:5,
  modelMs:distribution(modelTimes),mountDomMs:distribution(mountTimes),disposalMs:distribution(disposalTimes),
  memoryTrials:memory,memoryNote:'Raw signed deltas are retained; negative/noisy values are not clamped or represented as browser retained memory.',
};
dom.window.close();
console.log(JSON.stringify(report,null,2));
