import {element, button} from './dom.js';

/**
 * Show a task's durable chat list without treating a saved connection as running work.
 * @param {Element} container - Task-owned slot using the host's native disclosure and palette.
 * @param {object} issue - Current task with a bounded conversations page, if supported by its server.
 * @param {object} options - Read-only request function and isCurrent ownership check for late pages.
 * @returns {void} Renders metadata only; chat execution and navigation remain with each harness.
 */
export function renderTaskConversations(container, issue, {request, isCurrent = () => true}) {
  const list = element('ul', null, 'task-conversations');
  const status = element('p');
  status.setAttribute('role', 'status');
  const seen = new Set();
  const labels = new Map();
  let cursor = null;
  const more = button('Показать ещё чаты', async () => {
    status.textContent = 'Загружаем чаты…';
    list.setAttribute('aria-busy', 'true');
    try {
      const page = await request(`/api/v1/conversations?issueId=${encodeURIComponent(issue.id)}&limit=20&after=${encodeURIComponent(cursor)}`);
      if (!isCurrent()) return;
      append(page);
      status.textContent = 'Список чатов обновлён.';
    } catch {
      if (isCurrent()) status.textContent = 'Не удалось загрузить чаты. Повторите загрузку.';
    } finally {
      if (isCurrent()) list.setAttribute('aria-busy', 'false');
    }
  });

  /**
   * Append one creation-ordered page without duplicate IDs or interpreted remote HTML.
   * @param {object} page - Validated server page; the task remains the authoritative filter.
   * @returns {void} Updates pagination while retaining existing list rows and focus.
   */
  function append(page) {
    for (const chat of page.items || []) {
      if (seen.has(chat.id)) continue;
      seen.add(chat.id);
      labels.set(chat.id, chat.title);
      const row = element('li');
      row.append(element('strong', chat.title), element('p', `Система: ${chat.harness}`));
      row.append(element('p', `Чат: ${chat.remoteSessionId}`));
      if (chat.continuedFrom) row.append(element('p', `Продолжение: ${labels.get(chat.continuedFrom) || 'ранее связанный чат'}`));
      list.append(row);
    }
    cursor = page.nextCursor;
    more.hidden = !page.hasMore;
  }

  container.append(element('p', 'Задачу можно продолжить в другом чате или передать другому агенту. Сохранённая привязка не подтверждает активную работу.'));
  // The status stays outside the busy list, so page-load feedback can be announced.
  container.append(status, list, more);
  if (issue.conversations) {
    append(issue.conversations);
    if (!seen.size) status.textContent = 'Связанных чатов пока нет.';
  } else {
    more.hidden = true;
    status.textContent = 'Список чатов недоступен в этой версии сервиса.';
  }
}
