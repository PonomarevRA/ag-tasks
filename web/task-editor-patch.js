/** Compare bounded JSON form values without mistaking object key order for an edit. */
function sameValue(left,right) {
  if (left===right) return true;
  if (!left || !right || typeof left!=='object' || typeof right!=='object') return false;
  if (Array.isArray(left)!==Array.isArray(right)) return false;
  const keys=Object.keys(left);
  if (keys.length!==Object.keys(right).length) return false;
  for (const key of keys) if (!Object.hasOwn(right,key) || !sameValue(left[key],right[key])) return false;
  return true;
}

/** Return only deliberate edits, so changing an owner/deadline cannot resend an obsolete task definition. */
export function changedIssueFields(fields,issue,observedBaseline=null) {
  const baseline=observedBaseline || {...issue,dueDate:issue.dueDate || null,
    estimatedComplexity:issue.estimatedComplexity ? {points:issue.estimatedComplexity.points,reason:issue.estimatedComplexity.reason} : null};
  const patch={};
  for (const [key,value] of Object.entries(fields)) if (!sameValue(value,baseline[key])) patch[key]=value;
  return patch;
}

/**
 * Read exactly the values represented by the current native task form.
 * @param {HTMLFormElement} form - The populated issue editor, including disabled identity controls.
 * @param {object} context - Original issue (null for creation) and current criteria readers.
 * @returns {object} Validated-by-server payload candidates; no task or source object is mutated.
 * Capture once after population as the edit baseline: native single-line inputs and criteria
 * readers normalize legal stored text. Comparing against that same representation prevents
 * an unrelated owner/date edit from accidentally resubmitting a normalized task definition.
 */
export function readIssueEditorFields(form,{issue,readDor,readDod}) {
  const fields=Object.fromEntries(new form.ownerDocument.defaultView.FormData(form));
  fields.codeTask=form.elements.codeTask.checked || Boolean(form.elements.repoId.value);
  fields.languages=fields.languages.split(',').map(value=>value.trim()).filter(Boolean);
  fields.systems=fields.systems.split(',').map(value=>value.trim()).filter(Boolean);
  const evidence=fields.evidence;
  delete fields.evidence;
  fields.estimatedComplexity=fields.estimatedPoints ? {points:Number(fields.estimatedPoints),reason:fields.estimatedReason} : null;
  delete fields.estimatedPoints;
  delete fields.estimatedReason;
  for (const key of ['parentId','repoId','assignee','intendedRole']) fields[key] ||= null;
  fields.dueDate ||= null;
  fields.spaceId=form.elements.spaceId.value;
  fields.dor=readDor();
  fields.dod=readDod();
  fields.externalRefs=[...(issue?.externalRefs || [])];
  if (evidence) fields.externalRefs.push({url:evidence,title:'Доказательства',kind:'incident'});
  if (issue?.branch) fields.repoId=issue.repoId;
  return fields;
}
