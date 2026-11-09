export const statuses = {
  backlog:'Бэклог', ready:'Готово к работе', in_progress:'В работе', blocked:'Заблокировано',
  in_review:'На проверке', rework:'На доработке', done:'Выполнено', cancelled:'Отменено', stale:'Протухла',
};
export const priorities = {low:'Низкий', normal:'Обычный', high:'Высокий', urgent:'Срочный'};
export const roles = {
  analyst:'Аналитик', architect:'Архитектор', developer:'Разработчик', qa:'Тестировщик',
  critic:'Критик', documentation:'Документация', operator:'Оператор',
};
export const workStates = {
  never_started:'Работа не начиналась', running:'Работа начата', paused:'Приостановлено',
  expired:'Срок сессии истёк', interrupted:'Прервано перезапуском',
};

/** Format only server-accounted milliseconds; never infer work from wall-clock time or status. */
export function formatElapsed(value) {
  if (!Number.isFinite(value) || value < 0) return 'Неизвестно';
  const total = Math.floor(value / 1000);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total % 3600 / 60);
  const seconds = total % 60;
  return [hours, minutes, seconds].map(part => String(part).padStart(2, '0')).join(':');
}

/** Build the exact list-page query independently of UI labels and never include deleted tasks. */
export function issuePagePath(spaceId, archived, offset = 0) {
  const query = new URLSearchParams({spaceId, includeArchived:String(archived), limit:'200', offset:String(offset)});
  return `/api/v1/issues?${query}`;
}

/** Keep archive navigation separate even when the server includes both active and archived records. */
export function visibleIssues(issues, {archived = false, query = '', status = '', includeHistory = false} = {}) {
  const text = query.trim().toLocaleLowerCase();
  return issues.filter(issue => {
    if (issue.deletedAt || Boolean(issue.archivedAt) !== archived) return false;
    if (status && issue.status !== status) return false;
    if (!archived && !includeHistory && issue.kind === 'review' && ['done','cancelled'].includes(issue.status)) return false;
    if (!archived && !includeHistory && !status && ['done','cancelled'].includes(issue.status) && !issue.mergePending) return false;
    const labels = [...(issue.languages || []), ...(issue.systems || [])].join(' ');
    const searchable = `${issue.key} ${issue.title} ${issue.assignee || ''} ${issue.intendedRole || ''} ${issue.codeTask ? 'Код code' : ''} ${labels}`;
    return !text || searchable.toLocaleLowerCase().includes(text);
  });
}
