import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {bindWorkAreaFit} from '../web/work-area-fit.js';

test('work area fit coalesces resize, preserves cards and releases every observer/frame', () => {
  const dom = new JSDOM('<main><header></header><div id="content"><div class="board"><button>Natural card</button></div></div></main>', {pretendToBeVisual:true});
  const view = dom.window, frames = new Map();let sequence = 0, disconnects = 0, resized;
  view.requestAnimationFrame = callback => {const id=++sequence;frames.set(id,callback);return id;};
  view.cancelAnimationFrame = id => frames.delete(id);
  view.ResizeObserver = class {constructor(callback){resized=callback;} observe(){} disconnect(){disconnects++;}};
  const board = view.document.querySelector('.board'), card = board.firstElementChild;
  board.getBoundingClientRect = () => ({top:200});
  const dispose = bindWorkAreaFit({content:view.document.querySelector('#content'),chrome:[view.document.querySelector('header')]});
  resized();resized();view.dispatchEvent(new view.Event('resize'));
  assert.equal(frames.size, 1);
  const callback = frames.values().next().value;frames.clear();callback();
  assert.equal(board.style.getPropertyValue('--task-work-area-height'), '548px');
  assert.equal(board.firstElementChild, card);
  view.dispatchEvent(new view.Event('resize'));assert.equal(frames.size, 1);
  dispose();dispose();assert.equal(frames.size, 0);assert.equal(disconnects, 1);
  resized();view.dispatchEvent(new view.Event('resize'));assert.equal(frames.size, 0);
  dom.window.close();
});
