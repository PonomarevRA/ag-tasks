/** Render the service-derived next gate, distinct from manual status and elapsed work. */
export function renderLifecycle(parent, issue) {
  const labels = {review_pending:'Ожидает независимого рецензента',lease_expired:'Сессия истекла · продолжите работу',work_interrupted:'Работа прервана перезапуском',paused:'Работа приостановлена',completion_pending:'Подзадачи завершены · проверьте критерии плана',awaiting_merge:'Задача выполнена · ожидает разрешения на слияние',stored_cold:'Данные в ZIP · откройте для проверки',working:'Агент работает',children_working:'Подзадачи в работе',children_review:'Подзадачи ожидают проверки',children_need_attention:'Подзадачи требуют внимания',children_pending:'Подзадачи запланированы',awaiting_critic:'Реализация закончена · ожидает критика',awaiting_user:'Проверка пройдена · ожидает пользователя',result_missing:'Работа приостановлена · нужен результат или продолжение',planned:'Работа не начата',rework:'Нужна доработка',done:'Выполнено',cancelled:'Отменено',stale:'Протухла',blocked:'Заблокирована',archived:'В архиве'};
  const phase = issue.lifecycle?.phase;
  if (phase) {
    const description = element('p', labels[phase] || phase, issue.lifecycle.needsAttention ? 'coverage-notice' : 'muted');
    description.dataset.lifecycleDescription = 'true';
    parent.append(description);
    if (issue.work?.nextAction) parent.append(element('p', `Следующий шаг: ${issue.work.nextAction}`, 'muted'));
  }
}

import {renderTaskLabels} from './task-labels.js';
import {element, button, evidenceLink, statusLabel, labelledField} from './dom.js';
import {formatElapsed, roles, workStates} from './task-format.js';

/** Create an editable criterion list; IDs and existing evidence survive unrelated metadata changes. */
export function criteriaEditor(container, criteria = []) {
  container.replaceChildren();
  const rows = element('div', null, 'criteria-editor-rows');
  let sequence = 0;
  /** Add one declared criterion with explicit author evidence; this is not a service test result. */
  function add(criterion = {}) {
    const row = element('fieldset', null, 'criterion-editor');
    row.append(element('legend', `Критерий ${rows.children.length + 1}`));
    row.dataset.criterionId = criterion.id || `criterion-${Date.now()}-${++sequence}`;
    const text = element('input');
    text.value = criterion.text || '';
    text.maxLength = 1024;
    text.required = true;
    text.setAttribute('aria-label', 'Критерий');
    text.dataset.field = 'text';
    const declared = element('label', 'Подтверждено автором', 'criterion-declaration');
    const passed = element('input');
    passed.type = 'checkbox';
    passed.checked = Boolean(criterion.passed);
    passed.dataset.field = 'passed';
    declared.prepend(passed);
    const evidence = element('textarea');
    evidence.value = criterion.evidence || '';
    evidence.maxLength = 2048;
    evidence.rows = 2;
    evidence.placeholder = 'Основание или результат проверки';
    evidence.setAttribute('aria-label', 'Доказательства по критерию');
    evidence.dataset.field = 'evidence';
    evidence.required = passed.checked;
    passed.addEventListener('change', () => { evidence.required = passed.checked; });
    text.addEventListener('input', () => {
      passed.checked = false;
      evidence.required = false;
    });
    row.append(labelledField('Критерий', text), declared, labelledField('Доказательства по критерию', evidence), button('Удалить критерий', () => {
      const target = row.nextElementSibling?.querySelector('[data-field=text]')
        || row.previousElementSibling?.querySelector('[data-field=text]') || addControl;
      row.remove();
      target.focus();
    }, 'criterion-remove'));
    rows.append(row);
    return text;
  }
  for (const criterion of criteria) add(criterion);
  const addControl = button('＋ Критерий', () => {
    if (rows.children.length >= 50) throw new Error('Не более 50 критериев в одном списке');
    add().focus();
  });
  container.append(rows, addControl);
  return () => [...rows.children].map(row => ({
    id:row.dataset.criterionId,
    text:row.querySelector('[data-field=text]').value.trim(),
    passed:row.querySelector('[data-field=passed]').checked,
    evidence:row.querySelector('[data-field=evidence]').value.trim(),
  }));
}

/** Show the declared checklist and its evidence without presenting the check as independently verified. */
function renderCriteria(parent, title, criteria, emptyMessage) {
  const section = element('section', null, 'criteria-section');
  const passed = (criteria || []).filter(item => item.passed && item.evidence?.trim()).length;
  section.append(element('h3', `${title} · ${passed}/${criteria?.length || 0}`));
  if (!criteria?.length) section.append(element('p', emptyMessage, 'muted'));
  for (const criterion of criteria || []) {
    const row = element('div', null, 'criterion');
    row.append(statusLabel(criterion.passed ? 'Подтверждено автором' : 'Не подтверждено', criterion.passed ? 'accent' : 'neutral'));
    row.append(element('strong', criterion.text));
    row.append(element('p', criterion.evidence || 'Доказательство не добавлено', 'description muted'));
    section.append(row);
  }
  parent.append(section);
}

/** Display one immutable producer report and distinguish reported validation from execution evidence. */
function renderResult(parent, result) {
  if (result.workContext) renderTaskLabels(parent, result.workContext);
  parent.append(element('p', result.summary, 'description'));
  for (const [field, title] of [['changes', 'Изменения'], ['validation', 'Заявленные проверки']]) {
    const section = element('div', null, 'result-list');
    section.append(element('h4', title));
    const list = element('ul');
    for (const item of result[field] || []) list.append(element('li', item));
    section.append(list);
    if (!list.children.length) section.append(element('p', 'Не указано', 'muted'));
    parent.append(section);
  }
  parent.append(element('h4', 'Ограничения'), element('p', result.limitations || 'Не указаны', 'description'));
  for (const artifact of result.artifacts || []) {
    const link = evidenceLink(artifact);
    if (link) parent.append(link);
  }
  parent.append(element('p', `Поколение ${result.generation ?? '—'} · версия определения ${result.definitionRevision ?? '—'} · агент ${result.producerAgentId || 'не указан'}`, 'muted'));
}

/**
 * Render actual work, readiness and immutable caller evidence without mutating the task.
 * @param {Element} parent - Default owner when no explicit presentation slots are supplied.
 * @param {object} issue - Current server task; reported checks remain declarations rather than execution proof.
 * @param {Function} onOpen - Host navigation to the addressed related issue.
 * @param {object} options - Optional same-document overview, criteria, result and critic slots; defaults retain legacy composition.
 */
export function renderTaskWorkflow(parent, issue, onOpen, {overviewHost = parent, criteriaHost = parent, resultsHost = parent, criticHost = parent} = {}) {
  const overview = element('section', null, 'workflow-overview section');
  overview.append(element('h3', 'Роль и фактическая работа'));
  const facts = element('dl', null, 'task-facts');
  for (const [label, value] of [
    ['Роль', roles[issue.intendedRole] || 'Не назначена'],
    ['Фактическое время', formatElapsed(issue.work?.elapsedMs)],
    ['Сессия', workStates[issue.work?.state] || 'Нет данных'],
    ['Версия определения', issue.definitionRevision ?? '—'],
  ]) facts.append(element('dt', label), element('dd', value));
  overview.append(facts);
  renderTaskLabels(overview, issue);
  renderLifecycle(overview, issue);
  const session = issue.work?.activeSession;
  if (session) {
    const expired = Date.parse(session.leaseExpiresAt) < Date.now();
    overview.append(element('p', `Агент: ${session.agentId} · сессия: ${session.sessionId}`, 'muted'));
    overview.append(element('p', expired ? 'Срок lease истёк. Обновите данные для состояния, подтверждённого сервером.' : `Lease до ${session.leaseExpiresAt}. Последний heartbeat: ${session.lastHeartbeatAt}`, 'muted'));
  }
  overview.append(element('p', 'Время учитывается сервером по явной рабочей сессии агента. Открытие карточки и назначение исполнителя не запускают таймер.', 'muted'));
  const progress = issue.childProgress;
  if (progress?.total) {
    overview.append(element('h4', `Подзадачи · ${progress.total}`));
    const labels = {backlog:'Бэклог',ready:'Готовы',in_progress:'В работе',in_review:'На ревью',rework:'Доработка',done:'Выполнены',cancelled:'Отменены',blocked:'Заблокированы',stale:'Протухли'};
    overview.append(element('p', Object.entries(progress.byStatus).map(([key,count]) => `${labels[key] || key}: ${count}`).join(' · '), 'muted'));
  }
  const workHistory = issue.workHistory;
  if (workHistory?.items?.length) {
    const history = element('details');
    history.append(element('summary', 'История работы и ролей'));
    for (const item of workHistory.items) {
      history.append(element('p', `${item.agentId} · ${roles[item.intendedRole] || item.intendedRole || 'Роль ранее не фиксировалась'} · ${workStates[item.state] || item.state} · ${formatElapsed(item.elapsedMs)}`, 'muted'));
    }
    if (workHistory.hasMore) history.append(element('p', 'Последние 20 сессий. Полная история доступна через MCP tasks_work_history.', 'muted'));
    overview.append(history);
  }
  overviewHost.append(overview);
  const criteria = element('section', null, 'criteria-grid section');
  renderCriteria(criteria, 'DoR · Готовность к работе', issue.dor, 'DoR не задан. Начало работы требует роли и подтверждённых критериев готовности.');
  renderCriteria(criteria, 'DoD · Критерии завершения', issue.dod, 'DoD не задан. Передача результата и завершение требуют подтверждённых критериев выполнения.');
  criteriaHost.append(criteria);

  const results = element('section', null, 'section');
  results.append(element('h3', 'Результат работы'));
  if (issue.currentResult) {
    renderResult(results, issue.currentResult);
    if (issue.currentResult.definitionRevision !== issue.definitionRevision) results.append(element('p', 'Этот результат относится к более раннему определению задачи. Нужны новый результат и проверка.', 'coverage-notice'));
  }
  else results.append(element('p', 'Результат ещё не отправлен. Выполняющий агент отправляет отчёт через MCP из своей рабочей сессии.', 'muted'));
  const count = issue.resultsCount || 0;
  if (count > 1) {
    const history = element('details');
    history.append(element('summary', `История результатов · ${count}`));
    for (const result of issue.results || []) {
      const entry = element('section', null, 'history');
      renderResult(entry, result);
      history.append(entry);
    }
    if (count > (issue.results?.length || 0)) history.append(element('p', 'Показаны последние 10 записей. Более ранние результаты сохранены сервером.', 'muted'));
    results.append(history);
  }
  results.append(element('p', 'Результаты и проверки — отчёты объявленных агентов. Сервис не подтверждает выполнение тестов.', 'muted'));
  resultsHost.append(results);

  const critic = element('section', null, 'section');
  critic.append(element('h3', issue.codeTask ? 'Ревью кода' : 'Проверка критика'));
  if (issue.codeTask) critic.append(element('p', 'Результат → независимое ревью → при отказе доработка с причинами → новый результат и повторное ревью. После успешного ревью задача закрывается автоматически. Слияние подтверждает пользователь.', 'muted'));
  if (issue.criticReviewId) critic.append(button('Открыть задачу критика', () => onOpen(issue.criticReviewId)));
  const review = issue.criticReview;
  if (review) {
    const current = review.generation === issue.resultGeneration && review.definitionRevision === issue.definitionRevision;
    const label = !current ? 'Устаревшее заключение критика' : review.verdict === 'pass' ? 'Критик: заявлен PASS' : 'Критик: требуется доработка';
    critic.append(statusLabel(label, !current ? 'accent' : review.verdict === 'pass' ? 'neutral' : 'danger'));
    critic.append(element('p', review.summary, 'description'));
    if (review.verdict === 'rework') critic.append(element('h4', 'Причины возврата на доработку'));
    for (const finding of review.findings || []) critic.append(element('p', finding, 'description'));
    critic.append(element('p', review.evidence, 'description'));
    critic.append(element('p', `Агент ${review.agentId} · поколение ${review.generation} · определение ${review.definitionRevision} · SHA ${review.reviewedHead || 'не применяется'}`, 'muted'));
  } else critic.append(element('p', issue.criticReviewId ? 'Заключение по текущему результату ещё не получено.' : 'После отправки результата сервер создаст связанную задачу критика.', 'muted'));
  if (issue.reviewReports?.length) {
    const history = element('details', null, 'review-history');
    history.append(element('summary', 'История ревью и замечаний'));
    for (const report of issue.reviewReports) {
      const entry = element('section', null, 'history');
      entry.append(statusLabel(report.verdict === 'pass' ? 'Пройдено' : 'Возвращена на доработку', report.verdict === 'rework' ? 'danger' : 'neutral'));
      entry.append(element('p', report.summary, 'description'));
      for (const finding of report.findings || []) entry.append(element('p', finding, 'description'));
      entry.append(element('p', report.evidence, 'description'));
      entry.append(element('p', `Агент ${report.agentId} · результат ${report.generation} · определение ${report.definitionRevision} · ${report.createdAt}`, 'muted'));
      history.append(entry);
    }
    if (issue.reviewReportsTruncated) history.append(element('p', 'Показана ограниченная часть истории. Остальные заключения сохранены.', 'muted'));
    critic.append(history);
  }
  criticHost.append(critic);
}
