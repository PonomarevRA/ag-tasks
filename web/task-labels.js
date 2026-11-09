import {element, statusLabel} from './dom.js';

export const reviewStages = {unknown:'Ревью кода: заключение при открытии архива', pending:'Ревью кода: ожидает результата', in_review:'Ревью кода: на проверке', rework:'Ревью кода: доработка', passed:'Ревью кода: пройдено'};

/** Render literal language/system labels with bounded card density; detail shows every declared label. */
export function renderTaskLabels(parent, issue, {compact = false} = {}) {
  const labels = [];
  if (issue.codeTask) labels.push('Код');
  for (const language of issue.languages || []) labels.push(`Язык: ${language}`);
  for (const system of issue.systems || []) labels.push(`Система: ${system}`);
  if (!labels.length && (compact || !issue.codeReviewStage)) return;
  const row = element('span', null, 'task-labels');
  row.setAttribute('aria-label', 'Метки задачи');
  const visible = compact ? labels.slice(0, 5) : labels;
  for (const label of visible) row.append(statusLabel(label));
  if (visible.length < labels.length) row.append(element('span', `Ещё ${labels.length - visible.length}`, 'muted'));
  if (!compact && issue.codeReviewStage) row.append(statusLabel(reviewStages[issue.codeReviewStage], issue.codeReviewStage === 'rework' ? 'danger' : 'neutral'));
  parent.append(row);
}
