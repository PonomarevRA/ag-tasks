import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {agentActivity, spaceOptionLabel, renderSpaceSummary, taskGraphPath} from '../web/space-activity.js';

const now = Date.parse('2026-10-30T09:00:00Z');

test('activity distinguishes live work, recent history, no work and unknown data', () => {
  assert.equal(agentActivity({activeTasks:2}, now).state, 'active');
  assert.equal(agentActivity({lastAgentWorkAt:'2026-10-30T08:55:00Z'}, now).state, 'recent');
  assert.equal(agentActivity({lastAgentWorkAt:'2026-10-30T08:45:00Z'}, now).state, 'idle');
  assert.equal(agentActivity({}, now).state, 'none');
  assert.equal(agentActivity(undefined, now).state, 'unknown');
  assert.equal(agentActivity({lastAgentWorkAt:'bad'}, now).state, 'unknown');
  assert.equal(agentActivity({lastAgentWorkAt:'2026-10-30T10:00:00Z'}, now).state, 'unknown');
});

test('native options and rich summaries keep numbers and labels without color or markup', () => {
  const dom = new JSDOM();
  globalThis.document = dom.window.document;
  try {
    const space = {prefix:'TASK',name:'<img src=x>',summary:{backlog:1250,inProgress:3,activeTasks:1}};
    const label = spaceOptionLabel(space, now);
    assert.match(label, /Бэклог 1250/);
    assert.match(label, /В работе 3/);
    assert.match(label, /Агенты сейчас · задач: 1/);
    const summary = renderSpaceSummary(space, now);
    assert.match(summary.textContent, /Бэклог 1250/);
    assert.equal(summary.querySelector('img'), null);
    assert.equal(summary.querySelector('.space-activity--active').textContent, '● Агенты сейчас · задач: 1');
    assert.match(spaceOptionLabel({prefix:'OLD',name:'Old server'}), /Сводка недоступна/);
  } finally {
    delete globalThis.document;
    dom.window.close();
  }
});

test('active graph request remains scoped and always filters on the server', () => {
  const url = new URL(taskGraphPath('a & b', true), 'http://localhost');
  assert.equal(url.searchParams.get('spaceId'), 'a & b');
  assert.equal(url.searchParams.get('activeOnly'), 'true');
  assert.equal(url.searchParams.get('limit'), '200');
  assert.equal(new URL(taskGraphPath('a', false), url).searchParams.has('activeOnly'), false);
});
