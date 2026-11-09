import assert from 'node:assert/strict';
import test from 'node:test';
import {JSDOM} from 'jsdom';
import {bindDesktopAutomation} from '../web/desktop-automation.js';

test('desktop navigation reports a superseded action and fences stale revisions', async t => {
  const dom = new JSDOM(`<html class="native-shell"><body><nav id="sidebar"></nav>
    <button id="space-manage"></button><h1 id="heading"></h1><div id="content"></div>
    <dialog id="spaces-dialog"></dialog><button id="retry-view" hidden></button>
    <p id="notice">Unrelated domain error</p></body></html>`);
  const before = {window:globalThis.window, document:globalThis.document};
  Object.assign(globalThis, {window:dom.window, document:dom.window.document});
  t.after(() => { Object.assign(globalThis, before); dom.window.close(); });
  let view = 'sessions';
  bindDesktopAutomation({
    getView:() => view, getSpace:() => 'default', isLoading:() => false,
    navigate:async () => { view = 'analytics'; return false; },
  });
  const bridge = dom.window.agTasksDesktopAutomation;
  assert.equal(bridge.inspect().loadFailed, false);
  const {revision} = bridge.inspect();
  assert.equal((await bridge.execute({action:'open_board', revision})).error, 'desktop_navigation_superseded');
  assert.equal(bridge.inspect().view, 'analytics');
  assert.equal((await bridge.execute({action:'open_board', revision})).error, 'desktop_revision_changed');
});
