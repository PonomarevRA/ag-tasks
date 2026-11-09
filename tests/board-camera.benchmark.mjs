/** Measure the vendored viewport binding over a pre-existing 200-card scene. jsdom is not browser paint or retained memory. */
import {JSDOM} from 'jsdom';
import {performance} from 'node:perf_hooks';
import {createBoardViewport} from '../web/vendor/ag-design/index.js';
import {readFile} from 'node:fs/promises';
const dom=new JSDOM('<div id="v"><div id="s"></div></div>',{url:'http://localhost'});
const viewport=dom.window.document.querySelector('#v'),scene=dom.window.document.querySelector('#s');
for(let n=0;n<200;n++)scene.append(dom.window.document.createElement('button'));
let serial=0;const frames=new Map();dom.window.requestAnimationFrame=cb=>{frames.set(++serial,cb);return serial};dom.window.cancelAnimationFrame=id=>frames.delete(id);
const mount=[],update=[],dispose=[];
for(let trial=0;trial<45;trial++) {
 const begin=performance.now();const board=createBoardViewport({viewport,scene});const mounted=performance.now();
 board.zoom(1.2,{x:400,y:250});for(const callback of frames.values())callback();frames.clear();const updated=performance.now();
 board.dispose();const ended=performance.now();
 if(trial>=5){mount.push(mounted-begin);update.push(updated-mounted);dispose.push(ended-updated)}
}
const p95=values=>[...values].sort((a,b)=>a-b)[Math.ceil(values.length*.95)-1];
const manifest=JSON.parse(await readFile(new URL('../web/vendor/ag-design/manifest.json',import.meta.url),'utf8'));
console.log(JSON.stringify({scenario:'consumer-viewport-binding-jsdom-v1',version:manifest.version,sourceSha256:manifest.files['board-viewport.js'],environment:{runtime:process.version,platform:process.platform,architecture:process.arch,engine:'jsdom26.1'},samples:40,warmups:5,sceneCards:200,mountP95Ms:p95(mount),interactionP95Ms:p95(update),disposeP95Ms:p95(dispose),remainingFrames:frames.size,limitations:'Viewport binding/update only over existing cards; no actual CSS layout, browser paint, INP, native RSS or retained heap.'},null,2));
dom.window.close();
