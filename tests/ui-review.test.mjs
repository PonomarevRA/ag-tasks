import test from 'node:test';
import assert from 'node:assert/strict';
import {JSDOM} from 'jsdom';
import {criteriaEditor} from '../web/task-workflow.js';
import {renderCardSummary} from '../web/task-card.js';
import {createTaskDetailLayout} from '../web/task-detail-layout.js';

/** Keep synthetic edits in an isolated DOM; never write to the live task store. */
function fixture(t) {
  const dom=new JSDOM('<body><section></section>',{pretendToBeVisual:true});
  const previous=globalThis.document;
  globalThis.document=dom.window.document;
  t.after(()=>{globalThis.document=previous;dom.window.close();});
  return dom.window.document;
}

test('criterion add/remove keeps focus within a predictable editing path and retains IDs/evidence',async t=>{
  const document=fixture(t),container=document.querySelector('section');
  const read=criteriaEditor(container,[{id:'a',text:'A',passed:true,evidence:'Original'},{id:'b',text:'B'}]);
  const rows=()=>[...container.querySelectorAll('.criterion-editor')];
  rows()[0].querySelector('button').click();
  assert.equal(document.activeElement,rows()[0].querySelector('input'));
  assert.equal(read()[0].id,'b');
  const add=container.lastElementChild;add.click();
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(document.activeElement,rows()[1].querySelector('input'));
  rows()[1].querySelector('button').click();
  assert.equal(document.activeElement,rows()[0].querySelector('input'));
  rows()[0].querySelector('button').click();
  assert.equal(document.activeElement,add);
  assert.deepEqual(read(),[]);
});

test('card accessible name carries the rendered status and lifecycle without duplicating its mapping',t=>{
  const document=fixture(t),card=document.createElement('button');
  document.body.append(card);
  renderCardSummary(card,{key:'TEST-1',title:'Synthetic task',status:'blocked',lifecycle:{phase:'rework',needsAttention:true}});
  assert.match(card.getAttribute('aria-label'),/TEST-1: Synthetic task/);
  assert.ok(card.getAttribute('aria-label').includes(card.querySelector('[data-lifecycle-description]').textContent));
  assert.match(card.getAttribute('aria-label'),/Нужна доработка/);
});

test('criterion fields retain visible labels and evidence requirements after adding and editing', t => {
  const document = fixture(t), container = document.querySelector('section');
  const read = criteriaEditor(container, [{id:'criterion',text:'Actual condition',passed:true,evidence:'Caller evidence'}]);
  const row = container.querySelector('.criterion-editor');
  const text = row.querySelector('[data-field=text]'), evidence = row.querySelector('[data-field=evidence]');
  assert.equal(row.tagName, 'FIELDSET');
  assert.ok(row.querySelector('legend').textContent);
  assert.ok(text.labels[0].textContent.includes('Критерий'));
  assert.ok(evidence.labels[0].textContent.includes('Доказательства по критерию'));
  assert.equal(evidence.required, true);
  text.value = 'Changed condition'; text.dispatchEvent(new document.defaultView.Event('input'));
  assert.equal(evidence.required, false);
  assert.deepEqual(read(), [{id:'criterion',text:'Changed condition',passed:false,evidence:'Caller evidence'}]);
});

test('detail composition distinguishes stored status from derived next action and retains disclosure controls', t => {
  const document = fixture(t);
  const layout = createTaskDetailLayout({status:'backlog',intendedRole:'developer',lifecycle:{phase:'awaiting_critic',needsAttention:true},work:{nextAction:'Find independent reviewer'},currentResult:{summary:'Caller report'},codeTask:true});
  document.body.append(layout.root);
  assert.match(layout.root.textContent, /Бэклог/);
  assert.match(layout.root.textContent, /ожидает критика/);
  assert.match(layout.root.textContent, /Find independent reviewer/);
  const input = document.createElement('textarea');input.value = 'Retained draft';layout.history.append(input);
  const disclosure = layout.history.closest('details');disclosure.open = true;disclosure.open = false;disclosure.open = true;
  assert.equal(layout.history.querySelector('textarea'), input);
  assert.equal(input.value, 'Retained draft');
  assert.equal(layout.results.closest('details').open, true);
});
