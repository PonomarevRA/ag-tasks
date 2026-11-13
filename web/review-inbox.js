import {element, button, emptyState} from './dom.js';
import {renderCardSummary} from './task-card.js';
import {renderReviewProgress} from './review-progress.js';

/** Render actionable reviews with owned async effects; the host supplies a complete bounded page. */
export function renderReviewInbox(container, issues, {request, onOpen, onRefresh, spaceId}) {
  let disposed = false, saving = false;
  const setup = element('form', null, 'section review-routing-form');
  const title = element('h2', 'Передача независимому рецензенту');
  const reviewerLabel = element('label', 'ID рецензента', 'ag-field');
  const reviewer = element('input');
  reviewer.required = true; reviewer.maxLength = 128;
  reviewerLabel.append(reviewer);
  const laneLabel = element('label', 'Очередь исполнения', 'ag-field');
  const lane = element('select');
  lane.append(new Option('Без очереди · рецензент заберёт вручную', ''));
  laneLabel.append(lane);
  const notice = element('p', '', 'coverage-notice');
  notice.setAttribute('role', 'status');
  const save = button('Применять к новым результатам', () => configure());
  setup.append(title, element('p', 'Назначьте отдельного агента. Передача в очередь сохраняет задание; запуск выполняет подключённый исполнитель.', 'muted'), reviewerLabel, laneLabel, save, notice);
  container.append(setup);
  // Queue loading cannot replace navigation or erase entered reviewer text.
  request('/api/v1/queues?limit=50').then(value => {
    if (disposed) return;
    for (const item of value.items) lane.append(new Option(item.title, item.id));
  }).catch(error => { if (!disposed) notice.textContent = error.message; });

  /** Retain fields and exact review identity on failures; never replay an uncertain assignment automatically. */
  async function assign(issue, control) {
    if (!setup.reportValidity()) return;
    control.disabled = true;
    try {
      await request(`/api/v1/issues/${encodeURIComponent(issue.id)}/review-assignment`, 'POST', {reviewerId:reviewer.value.trim(), laneId:lane.value || null});
      if (!disposed) await onRefresh();
    } catch (error) {
      if (!disposed) notice.textContent = `${error.message} Обновите очередь перед повтором.`;
    } finally { if (!disposed) control.disabled = false; }
  }

  /** Saving a default is an explicit operator choice and does not replay historical reviews. */
  async function configure(event) {
    event?.preventDefault();
    if (saving || !setup.reportValidity()) return;
    saving = true;
    save.disabled = true;
    try {
      await request(`/api/v1/spaces/${encodeURIComponent(spaceId)}/review-route`, 'POST', {reviewerId:reviewer.value.trim(), laneId:lane.value || null});
      if (!disposed) notice.textContent = 'Новые результаты будут передаваться этому рецензенту. Текущие проверки назначаются отдельно.';
    } catch (error) { if (!disposed) notice.textContent = error.message; }
    finally { saving = false; if (!disposed) save.disabled = false; }
  }
  setup.addEventListener('submit', configure);
  if (!issues.length) container.append(emptyState('Нет ожидающих ревью', 'Здесь появятся актуальные результаты, которым нужна независимая проверка.'));
  const list = element('div', null, 'issue-list review-issue-list');
  for (const issue of issues) {
    const row = element('section', null, 'section review-inbox-row');
    const open = button(`${issue.key}: ${issue.title}`, () => { void onOpen(issue.id, open); }, 'issue-row');
    open.dataset.issueId = issue.id;
    renderCardSummary(open, issue);
    row.append(open);
    const target = issue.reviewTarget;
    if (target) {
      const producer = button(`Результат ${target.key} · поколение ${target.generation}`, () => { void onOpen(target.id, producer); });
      producer.dataset.issueId = target.id;
      row.append(producer);
    }
    renderReviewProgress(row, issue.reviewProgress);
    const handoff = issue.handoff;
    if (handoff?.assignedAt && issue.reviewProgress?.reason !== 'active') {
      row.append(button('Снять назначение', async () => {
        try {
          await request(`/api/v1/issues/${encodeURIComponent(issue.id)}/review-assignment/release`, 'POST', {expectedAssignedAt:handoff.assignedAt});
          if (!disposed) await onRefresh();
        } catch (error) { if (!disposed) notice.textContent = error.message; }
      }));
    }
    if (issue.reviewProgress?.reason === 'reviewer_required') {
      const assignControl = button('Передать выбранному рецензенту', () => assign(issue, assignControl));
      row.append(assignControl);
    }
    list.append(row);
  }
  container.append(list);
  return () => { disposed = true; setup.removeEventListener('submit', configure); };
}
