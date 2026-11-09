import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {JSDOM} from 'jsdom';
import {changedIssueFields,readIssueEditorFields} from '../web/task-editor-patch.js';
import {criteriaEditor} from '../web/task-workflow.js';

/** Model the existing task snapshot, including fields that belong to server audit provenance. */
function currentIssue() {
  return {
    id:'task-id',title:'Existing definition',description:'Acceptance context',priority:'normal',
    assignee:'Team A',dueDate:'2026-12-01',parentId:null,repoId:null,intendedRole:'developer',
    spaceId:'project',kind:'task',codeTask:true,languages:['Python','JavaScript'],systems:['ag-tasks','MCP'],
    dor:[{id:'ready',text:'Inputs exist',passed:true,evidence:'Reviewed inputs'}],
    dod:[{id:'verified',text:'Contract checked',passed:true,evidence:'Observed checks'}],
    externalRefs:[{url:'https://example.com/evidence',title:'Evidence',kind:'reference'}],
    estimatedComplexity:{points:3,reason:'Bounded change',actor:'agent',recordedAt:'2026-11-05T10:00:00Z'},
    definitionRevision:7,resultGeneration:2,
  };
}

/** Build exactly the editable values a populated form sends, without server-owned audit fields. */
function editorFields(issue) {
  const fields = {};
  for (const name of ['title','description','priority','assignee','parentId','repoId','intendedRole',
    'spaceId','kind','codeTask','languages','systems','dor','dod','externalRefs']) {
    fields[name] = structuredClone(issue[name]);
  }
  fields.dueDate = issue.dueDate || null;
  fields.estimatedComplexity = issue.estimatedComplexity
    ? {points:issue.estimatedComplexity.points,reason:issue.estimatedComplexity.reason} : null;
  return fields;
}

test('owner and deadline changes send only scheduling metadata, preserving an obsolete definition snapshot',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  fields.assignee = 'Team B';
  fields.dueDate = '2026-12-20';
  assert.deepEqual(changedIssueFields(fields,issue),{assignee:'Team B',dueDate:'2026-12-20'});
});

test('estimate audit actor and timestamp do not count as edits while a changed point estimate does',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  issue.estimatedComplexity.actor = 'another recorder';
  issue.estimatedComplexity.recordedAt = '2026-11-05T12:00:00Z';
  assert.deepEqual(changedIssueFields(fields,issue),{});
  fields.estimatedComplexity.points = 5;
  assert.deepEqual(changedIssueFields(fields,issue),{
    estimatedComplexity:{points:5,reason:'Bounded change'},
  });
});

test('object key order is irrelevant while intentional array order changes remain edits',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  fields.dor = [{evidence:'Reviewed inputs',passed:true,text:'Inputs exist',id:'ready'}];
  fields.externalRefs = [{kind:'reference',title:'Evidence',url:'https://example.com/evidence'}];
  assert.deepEqual(changedIssueFields(fields,issue),{});
  fields.languages = ['JavaScript','Python'];
  assert.deepEqual(changedIssueFields(fields,issue),{languages:['JavaScript','Python']});
});

test('intentional title and acceptance-definition edits remain in the patch',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  fields.title = 'Revised definition';
  fields.dod[0].passed = false;
  fields.dod[0].evidence = '';
  assert.deepEqual(changedIssueFields(fields,issue),{
    title:'Revised definition',
    dod:[{id:'verified',text:'Contract checked',passed:false,evidence:''}],
  });
});

test('clearing an existing deadline remains explicit while an empty legacy deadline is a no-op',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  fields.dueDate = null;
  assert.deepEqual(changedIssueFields(fields,issue),{dueDate:null});
  delete issue.dueDate;
  assert.deepEqual(changedIssueFields(fields,issue),{});
});

test('an unchanged full form produces no patch and neither input snapshot is mutated',()=>{
  const issue = currentIssue();
  const fields = editorFields(issue);
  const beforeIssue = structuredClone(issue);
  const beforeFields = structuredClone(fields);
  const patch = changedIssueFields(fields,issue);
  assert.deepEqual(patch,{});
  assert.equal(Object.keys(patch).length,0);
  assert.deepEqual(issue,beforeIssue);
  assert.deepEqual(fields,beforeFields);
});


/** Populate the real issue form and compose its production checklist editors without running app.js. */
function populatedEditor(t,issue) {
  const html = readFileSync(new URL('../web/index.html',import.meta.url),'utf8');
  const dom = new JSDOM(html,{url:'http://localhost',pretendToBeVisual:true});
  const previous = {document:globalThis.document,Option:globalThis.Option};
  Object.assign(globalThis,{document:dom.window.document,Option:dom.window.Option});
  t.after(()=>{dom.window.close();Object.assign(globalThis,previous);});
  const form = dom.window.document.querySelector('#issue-form');
  for (const name of ['title','description','priority','assignee','parentId','repoId','spaceId','intendedRole','dueDate']) {
    const control = form.elements[name];
    const value = issue[name] || '';
    if (control.tagName==='SELECT' && ![...control.options].some(option=>option.value===value)) {
      control.add(new dom.window.Option(value,value));
    }
    control.value = value;
  }
  form.elements.kind.value = issue.kind;
  form.elements.kind.disabled = true;
  form.elements.codeTask.checked = issue.codeTask;
  form.elements.languages.value = issue.languages.join(', ');
  form.elements.systems.value = issue.systems.join(', ');
  form.elements.estimatedPoints.value = String(issue.estimatedComplexity.points);
  form.elements.estimatedReason.value = issue.estimatedComplexity.reason;
  const readDor = criteriaEditor(dom.window.document.querySelector('#dor-editor'),issue.dor);
  const readDod = criteriaEditor(dom.window.document.querySelector('#dod-editor'),issue.dod);
  return {form,dom,read:()=>readIssueEditorFields(form,{issue,readDor,readDod})};
}

/** Legal API whitespace remains stored even though single-line inputs/checklist readers normalize it. */
function whitespaceIssue() {
  const issue = currentIssue();
  issue.title = ' First line\nsecond line ';
  issue.dor[0].text = '  Inputs exist  ';
  issue.dor[0].evidence = '\n Reviewed inputs  \n';
  issue.dod[0].text = '  Contract checked  ';
  issue.dod[0].evidence = '\n Observed checks  \n';
  return issue;
}

test('real form baseline isolates owner/date changes from checklist trimming and title sanitization',t=>{
  const issue = whitespaceIssue();
  const before = structuredClone(issue);
  const editor = populatedEditor(t,issue);
  const baseline = editor.read();
  assert.notEqual(baseline.title,issue.title);
  assert.equal(baseline.title,editor.form.elements.title.value);
  assert.equal(baseline.dor[0].text,'Inputs exist');
  assert.equal(baseline.dor[0].evidence,'Reviewed inputs');
  assert.equal(baseline.dod[0].text,'Contract checked');
  assert.deepEqual(changedIssueFields(baseline,issue,baseline),{});
  editor.form.elements.assignee.value = 'New manager owner';
  editor.form.elements.dueDate.value = '2026-12-22';
  const submitted = editor.read();
  assert.deepEqual(changedIssueFields(submitted,issue,baseline),{
    assignee:'New manager owner',dueDate:'2026-12-22',
  });
  assert.deepEqual(issue,before);
  assert.equal(baseline.dor[0].passed,true);
});

test('real criterion edits after baseline capture remain definition changes with normalized evidence',t=>{
  const issue = whitespaceIssue();
  const before = structuredClone(issue);
  const editor = populatedEditor(t,issue);
  const baseline = editor.read();
  const criterion = editor.form.querySelector('#dor-editor [data-field=text]');
  criterion.value = '  Newly required input  ';
  criterion.dispatchEvent(new editor.dom.window.Event('input',{bubbles:true}));
  const submitted = editor.read();
  assert.deepEqual(changedIssueFields(submitted,issue,baseline),{
    dor:[{id:'ready',text:'Newly required input',passed:false,evidence:'Reviewed inputs'}],
  });
  assert.equal(baseline.dor[0].text,'Inputs exist');
  assert.equal(baseline.dor[0].passed,true);
  assert.deepEqual(issue,before);
});
