/** The tray's live log renders bounded metadata only, with no executable source text. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { renderSnapshot, startActivity } from '../web/desktop-activity.js';

function documentFixture() {
  const parts = new Map();
  const element = () => ({ children: [], textContent: '', append(node) { this.children.push(node); }, replaceChildren(...nodes) { this.children = nodes; }, set innerHTML(value) { throw new Error('HTML forbidden'); } });
  return { hidden: false, createElement: element, getElementById(id) { if (!parts.has(id)) parts.set(id, element()); return parts.get(id); } };
}
test('tray request view checks app identity and keeps unknown/empty/capture time visible', () => {
  const document = documentFixture();
  const snapshot = { schemaVersion: 'ag-desktop/widget-v1', appId: 'ag-tasks', status: 'ok', capturedAt: '2026-10-22T00:00:00Z', source: 'process-local', requests: [{ operation: '<script>unsafe</script>', actorLabel: 'unidentified', requestSummary: 'x'.repeat(700) }] };
  renderSnapshot(document, snapshot);
  assert.equal(document.getElementById('requests').children[0].children[1].textContent, '<script>unsafe</script>');
  assert.equal(document.getElementById('requests').children[0].children[4].textContent.length, 512);
  assert.match(document.getElementById('coverage').textContent, /2026-10-22/);
  assert.throws(() => renderSnapshot(document, { ...snapshot, appId: 'other' }));
  renderSnapshot(document, { ...snapshot, requests: [] });
  assert.match(document.getElementById('coverage').textContent, /ещё не/);
});
test('hidden tray log never polls; page disposal removes listener and timer', () => {
  const document = documentFixture(); document.hidden = true;
  let calls = 0, removed = 0, cleared = 0;
  const window = { setTimeout: () => 1, clearTimeout() { cleared++; }, addEventListener() {}, removeEventListener() { removed++; } };
  const dispose = startActivity(document, window, () => { calls++; });
  assert.equal(calls, 0); dispose(); assert.equal(removed, 1); assert.equal(cleared, 1);
});
