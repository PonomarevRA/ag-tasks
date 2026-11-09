import {normalizeBoardState, zoomBoardAt, fitBoardState} from './board-camera.js';
import {createBoardControls} from './board-controls.js';

const INTERACTIVE = 'button,a,input,textarea,select,summary,[contenteditable]:not([contenteditable="false"]),[role="textbox"],[data-board-no-pan]';

/**
 * Bind a camera to a host-owned scene. Geometry and business data stay in the product.
 * Call dispose before removing the viewport. Preferences are local per supplied storageKey;
 * denied/corrupt storage falls back to memory. onChange receives CSS-pixel x/y and scale.
 * Camera updates are coalesced into one frame; no idle loop or per-card listeners are created.
 */
export function createBoardViewport({viewport, scene, storageKey, storage, initialState, onChange = () => {}} = {}) {
  if (!viewport || !scene || scene.parentElement !== viewport) throw new TypeError('A viewport and its direct child scene are required');
  const document = viewport.ownerDocument, view = document.defaultView;
  let state = normalizeBoardState(initialState), disposed = false, frame = null, saveTimer = null;
  let drag = null, space = false, suppressClick = false, clickDeadline = 0;
  const removers = [];
  const oldTabIndex = viewport.getAttribute('tabindex');
  const oldRole = viewport.getAttribute('role');
  const oldLabel = viewport.getAttribute('aria-label');
  const oldTransform = scene.style.transform, oldOrigin = scene.style.transformOrigin;
  const hadViewportClass = viewport.classList.contains('ag-board'), hadSceneClass = scene.classList.contains('ag-board__scene');
  const properties = ['--ag-board-color','--ag-board-dot','--ag-board-pattern-size','--ag-board-x','--ag-board-y'];
  const previousProperties = properties.map(name => [name, viewport.style.getPropertyValue(name)]);
  const previousPattern = viewport.getAttribute('data-board-pattern');
  if (!storage) {try {storage = view.localStorage;} catch { /* Sandboxed browser: camera still works. */ }}
  if (storageKey) {try {const saved = storage?.getItem(storageKey); if (saved) state = normalizeBoardState(JSON.parse(saved));} catch { /* Do not replace invalid storage with guessed state. */ }}
  viewport.classList.add('ag-board'); scene.classList.add('ag-board__scene');
  viewport.tabIndex = 0; viewport.setAttribute('role', 'region');
  if (!oldLabel) viewport.setAttribute('aria-label', 'Рабочая доска');
  scene.style.transformOrigin = '0 0';
  const controls = createBoardControls(document, {zoom, reset, fit, background});
  viewport.append(controls.element);

  /** Register every listener with symmetrical cleanup, including global gesture termination. */
  function listen(target, event, callback, options) {
    target.addEventListener(event, callback, options);
    removers.push(() => target.removeEventListener(event, callback, options));
  }
  /** Store only bounded preferences, throttled across wheel/pointer bursts and flushed on dispose/pagehide. */
  function persist() {
    if (saveTimer !== null) view.clearTimeout(saveTimer);
    saveTimer = null;
    if (storageKey) {try {storage?.setItem(storageKey, JSON.stringify(state));} catch { /* Storage is optional. */ }}
  }
  /** Paint one shared transform and one CSS pattern; never measure all cards on camera movement. */
  function paint() {
    frame = null;
    scene.style.transform = `translate(${state.x}px, ${state.y}px) scale(${state.scale})`;
    viewport.dataset.boardPattern = state.pattern;
    viewport.style.setProperty('--ag-board-color', state.color || 'var(--ag-surface-alt)');
    // Dots remain distinguishable on explicit light/dark custom backgrounds.
    if (state.color) {
      const rgb = [1,3,5].map(i => parseInt(state.color.slice(i,i+2),16));
      const light = rgb[0]*.2126 + rgb[1]*.7152 + rgb[2]*.0722 > 140;
      viewport.style.setProperty('--ag-board-dot', light ? '#00000028' : '#ffffff38');
    } else viewport.style.removeProperty('--ag-board-dot');
    viewport.style.setProperty('--ag-board-pattern-size', `${24 * state.scale}px`);
    viewport.style.setProperty('--ag-board-x', `${state.x}px`);
    viewport.style.setProperty('--ag-board-y', `${state.y}px`);
    controls.update(state); onChange({...state});
  }
  /** Keep event handling cheap; state changes are synchronous, DOM writes wait for the next frame. */
  function setState(value) {
    if (disposed) return;
    state = normalizeBoardState({...state, ...value});
    if (frame === null) frame = view.requestAnimationFrame(paint);
    if (saveTimer !== null) view.clearTimeout(saveTimer);
    saveTimer = view.setTimeout(persist, 250);
  }
  /** Zoom at the pointer, or the visual center when invoked from controls/keyboard. */
  function zoom(factor, point) {
    const anchor = point || {x:viewport.clientWidth/2, y:viewport.clientHeight/2};
    setState(zoomBoardAt(state, factor, anchor));
  }
  /** Restore readable size without silently recentering the user's current point of interest. */
  function reset() {zoom(1 / state.scale);}
  /** Fit is an explicit action; resize/refresh must not silently reset the camera. */
  function fit() {
    setState(fitBoardState(state, {width:viewport.clientWidth,height:viewport.clientHeight}, {width:scene.offsetWidth,height:scene.offsetHeight}));
  }
  /** Merge bounded appearance settings without changing the host palette or card colors. */
  function background(value) {setState({color:value.color ?? state.color,pattern:value.pattern ?? state.pattern});}
  /** End pointer capture on cancel, blur, lost capture, release and disposal. */
  function endDrag(event) {
    const previous = drag; drag = null;
    if (event?.type === "pointercancel") suppressClick = false;
    if (previous && suppressClick) clickDeadline = Date.now() + 500;
    if (previous && viewport.hasPointerCapture?.(previous.id)) viewport.releasePointerCapture(previous.id);
    viewport.classList.remove('ag-board--dragging');
  }
  /** Controls/text own their gestures; Space deliberately enables panning over a card button. */
  function pointerDown(event) {
    if (controls.element.contains(event.target) || (event.button !== 0 && event.button !== 1)) return;
    const editable = event.target.closest?.('input,textarea,select,[contenteditable]:not([contenteditable="false"])');
    if (editable || (!space && event.target.closest?.(INTERACTIVE))) return;
    event.preventDefault(); viewport.focus({preventScroll:true});
    suppressClick = false;
    drag = {id:event.pointerId,clientX:event.clientX,clientY:event.clientY,x:state.x,y:state.y};
    viewport.setPointerCapture?.(event.pointerId); viewport.classList.add('ag-board--dragging');
  }
  function pointerMove(event) {
    if (!drag || event.pointerId !== drag.id) return;
    const dx = event.clientX - drag.clientX, dy = event.clientY - drag.clientY;
    if (Math.hypot(dx,dy) > 4) suppressClick = true;
    setState({x:drag.x+dx,y:drag.y+dy});
  }
  /** Wheel pans naturally; modifier-wheel/pinch zooms only within this workspace. */
  function wheel(event) {
    if (controls.element.contains(event.target) || event.target.closest?.('input,textarea,select,[contenteditable]')) return;
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
    if (event.ctrlKey || event.metaKey) {
      const rect = viewport.getBoundingClientRect();
      zoom(Math.exp(-Math.max(-100, Math.min(100,event.deltaY*unit)) * .008), {x:event.clientX-rect.left,y:event.clientY-rect.top});
    } else {
      setState({x:state.x-event.deltaX*unit,y:state.y-event.deltaY*unit});
    }
  }
  /** Shortcuts belong only to the focused board itself; Tab/card activation and text editing stay native. */
  function keyDown(event) {
    if (event.target !== viewport || event.defaultPrevented || event.isComposing || event.metaKey || event.ctrlKey || event.altKey) return;
    const step = event.shiftKey ? 192 : 64;
    const directions = {ArrowLeft:[step,0],ArrowRight:[-step,0],ArrowUp:[0,step],ArrowDown:[0,-step]};
    const delta = directions[event.key];
    if (delta) setState({x:state.x+delta[0],y:state.y+delta[1]});
    else if (event.code === 'Space' || event.key === ' ') {space = true; viewport.classList.add('ag-board--grab');}
    else if (['+','='].includes(event.key)) zoom(1.2);
    else if (['-','−'].includes(event.key)) zoom(1/1.2);
    else if (event.key === '0') fit();
    else if (event.key === '1') reset();
    else if (event.key === 'Escape') {space=false;endDrag();viewport.classList.remove('ag-board--grab');}
    else return;
    event.preventDefault();
  }
  function clearKeys() {space=false;viewport.classList.remove('ag-board--grab');endDrag();}
  /** Bring tab-focused cards back on screen without altering scale or exposing an inaccessible offscreen focus. */
  function revealFocus(event) {
    if (!scene.contains(event.target)) return;
    const bounds = viewport.getBoundingClientRect(), rect = event.target.getBoundingClientRect();
    const dx = rect.left < bounds.left+12 ? bounds.left+12-rect.left : rect.right > bounds.right-12 ? bounds.right-12-rect.right : 0;
    const dy = rect.top < bounds.top+12 ? bounds.top+12-rect.top : rect.bottom > bounds.bottom-76 ? bounds.bottom-76-rect.bottom : 0;
    if (dx || dy) setState({x:state.x+dx,y:state.y+dy});
  }
  listen(viewport,'pointerdown',pointerDown); listen(viewport,'pointermove',pointerMove);
  for (const type of ['pointerup','pointercancel','lostpointercapture']) listen(viewport,type,endDrag);
  listen(viewport,'click',event => {
    if(suppressClick && event.detail !== 0 && Date.now() <= clickDeadline) {event.preventDefault();event.stopPropagation();}
    suppressClick=false;
  },true);
  listen(viewport,'wheel',wheel,{passive:false}); listen(viewport,'keydown',keyDown);
  listen(viewport,'focusin',revealFocus); listen(viewport,'focusout',event => {if (!viewport.contains(event.relatedTarget)) clearKeys();});
  listen(view,'keyup',event => {if(event.code==='Space' || event.key===' ') clearKeys();});
  listen(view,'blur',clearKeys); listen(view,'pagehide',persist);
  paint();
  return {
    get state() {return {...state};}, setState, zoom, fit, reset,
    dispose() {
      if(disposed) return;
      disposed=true;clearKeys();persist();
      if(frame!==null) view.cancelAnimationFrame(frame);
      for(const remove of removers) remove(); controls.dispose();
      scene.style.transform=oldTransform;scene.style.transformOrigin=oldOrigin;
      if(!hadViewportClass) viewport.classList.remove('ag-board');
      if(!hadSceneClass) scene.classList.remove('ag-board__scene');
      for(const [name,value] of previousProperties) {if(value)viewport.style.setProperty(name,value);else viewport.style.removeProperty(name);}
      for(const [name,value] of [['tabindex',oldTabIndex],['role',oldRole],['aria-label',oldLabel],['data-board-pattern',previousPattern]]) {
        if(value===null)viewport.removeAttribute(name);else viewport.setAttribute(name,value);
      }
    },
  };
}
