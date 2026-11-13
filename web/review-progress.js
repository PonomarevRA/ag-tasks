import {element} from './dom.js';

const reasons = {
  reviewer_required: 'Рецензент не назначен',
  assigned: 'Рецензент назначен · проверка ещё не началась',
  queued: 'Ревью в очереди · ожидает исполнителя',
  inspect_worker: 'Проверьте состояние исполнителя',
  active: 'Рецензент проверяет результат',
};

/** Render the same service-owned review facts in the inbox and task detail. */
export function renderReviewProgress(parent, progress, now = Date.now()) {
  if (!progress) return;
  const group = element('div', null, 'review-progress');
  group.dataset.agWidth = 'fill';
  group.append(element('p', reasons[progress.reason] || 'Состояние проверки неизвестно'));
  const since = Date.parse(progress.waitingSince);
  const minutes = Math.max(0, Math.floor((now - since) / 60000));
  let age = 'неизвестно';
  if (Number.isFinite(minutes)) {
    age = minutes < 60 ? `${minutes} мин`
      : minutes < 1440 ? `${Math.floor(minutes / 60)} ч`
      : `${Math.floor(minutes / 1440)} д ${Math.floor(minutes % 1440 / 60)} ч`;
  }
  group.append(element('p', `Ожидание с момента сдачи: ${age}${progress.reviewerId ? ` · Рецензент: ${progress.reviewerId}` : ''}`, 'muted'));
  if (progress.lastAttemptExpired) {
    const notice = element('p', 'Аренда последней сессии критика истекла', 'coverage-notice');
    if (Number.isFinite(Date.parse(progress.lastAttemptEndedAt))) {
      notice.append(' · ', reviewTime(progress.lastAttemptEndedAt));
    }
    group.append(notice);
  }
  parent.append(group);
}

/** Show an observed UTC instant as both readable text and a machine-readable timestamp. */
export function reviewTime(value) {
  const time = element('time', new Intl.DateTimeFormat('ru-RU', {
    dateStyle:'short', timeStyle:'short', timeZone:'UTC',
  }).format(new Date(value)) + ' UTC');
  time.dateTime = value;
  return time;
}
