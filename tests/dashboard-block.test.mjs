import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {dashboardBlock} from '../web/dashboard-block.js';

/** Isolate saved preferences and release every mounted block after each test. */
function browser(t, {storageAvailable = true} = {}) {
  const options = storageAvailable ? {url:'http://localhost'} : {};
  const dom = new JSDOM('<main></main>', options);
  const previous = globalThis.document;
  globalThis.document = dom.window.document;
  const blocks = [];
  t.after(() => {
    for (const block of blocks) block.dispose();
    dom.window.close();
    globalThis.document = previous;
  });
  return {
    document:dom.window.document,
    mount(key, title = 'Раздел') {
      const block = dashboardBlock(key, title);
      dom.window.document.querySelector('main').append(block.element);
      blocks.push(block);
      return block;
    },
  };
}

/** Dispatch the native disclosure event explicitly because jsdom queues automatic toggle events. */
function toggle(block, open) {
  block.element.open = open;
  block.element.dispatchEvent(new block.element.ownerDocument.defaultView.Event('toggle'));
}

test('dashboard blocks start open and expose a native summary with a reusable content body', t => {
  const {mount, document} = browser(t);
  const block = mount('overview', '<b>Обзор исполнения</b>');
  assert.equal(block.element.tagName, 'DETAILS');
  assert.equal(block.element.open, true);
  const summary = block.element.querySelector(':scope > summary');
  assert.equal(summary.textContent, '<b>Обзор исполнения</b>');
  assert.equal(summary.querySelector('b'), null);
  assert.equal(block.body.parentElement, block.element);
  const content = document.createElement('p');
  content.textContent = '52 карточки';
  block.body.append(content);
  assert.equal(block.body.textContent, '52 карточки');
});

test('a block restores both collapsed and expanded choices across remounts', t => {
  const {mount} = browser(t);
  const first = mount('status');
  toggle(first, false);
  first.dispose();
  first.element.remove();
  const restored = mount('status');
  assert.equal(restored.element.open, false);
  toggle(restored, true);
  restored.dispose();
  restored.element.remove();
  assert.equal(mount('status').element.open, true);
});

test('saving one block preserves independent choices of sibling block keys', t => {
  const {mount} = browser(t);
  const overview = mount('overview');
  const ledger = mount('ledger');
  toggle(overview, false);
  assert.equal(ledger.element.open, true);
  toggle(ledger, false);
  toggle(overview, true);
  assert.equal(mount('overview').element.open, true);
  assert.equal(mount('ledger').element.open, false);
  assert.equal(mount('new-panel').element.open, true);
});

test('unavailable local storage keeps native disclosure usable with open defaults', t => {
  const {mount, document} = browser(t, {storageAvailable:false});
  assert.throws(() => document.defaultView.localStorage, {name:'SecurityError'});
  const block = mount('filters');
  assert.equal(block.element.open, true);
  assert.doesNotThrow(() => toggle(block, false));
  assert.equal(block.element.open, false);
  assert.equal(mount('filters').element.open, true);
});

test('disposal removes the toggle listener and prevents old nodes from rewriting saved state', t => {
  const {mount} = browser(t);
  const block = mount('activity');
  toggle(block, false);
  const removed = [];
  const originalRemove = block.element.removeEventListener;
  block.element.removeEventListener = function(type, listener, options) {
    removed.push(type);
    return originalRemove.call(this, type, listener, options);
  };
  block.dispose();
  assert.deepEqual(removed, ['toggle']);
  toggle(block, true);
  assert.equal(block.element.open, true);
  assert.equal(mount('activity').element.open, false);
});
