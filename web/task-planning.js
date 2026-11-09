import {element, button, statusLabel} from './dom.js';
import {request} from './api.js';

/** Display relative effort separately from measured time, including the assessment's author and basis. */
export function renderPlanning(parent, issue, reload, onOpen) {
  const section = element('section', null, 'section planning-section');
  section.append(element('h3', issue.kind === 'rnd' ? 'RnD · Планирование' : 'Сложность'));
  const facts = element('div', null, 'complexity-comparison');
  for (const [key, label] of [['estimatedComplexity', 'Предварительно'], ['actualComplexity', 'После реализации']]) {
    const item = issue[key];
    const cell = element('div', null, 'complexity-cell');
    cell.append(element('span', label, 'muted'), element('strong', item ? `${item.points} балл.` : 'Не оценено'));
    if (item) {
      cell.append(element('p', item.reason, 'description'), element('small', item.actor, 'muted'));
      if (key === 'actualComplexity' && (item.definitionRevision !== issue.definitionRevision || item.resultGeneration !== issue.resultGeneration)) cell.append(element('p', 'Оценка относится к прошлой версии задачи.', 'muted'));
    }
    facts.append(cell);
  }
  section.append(facts, element('p', 'Относительная сложность: 1, 2, 3, 5, 8, 13. Это оценка объёма и неопределённости, а не часы.', 'muted'));
  if (issue.kind === 'rnd') section.append(element('p', 'План хранится в файле задачи. Декомпозиция по ролям выполняется отдельным действием по запросу пользователя.', 'muted'));
  if (issue.status === 'stale' && !issue.archivedAt) {
    const reason = element('textarea'); reason.rows = 2; reason.maxLength = 2048;
    reason.setAttribute('aria-label', 'Почему протухшая задача снова актуальна');
    section.append(statusLabel('Протухла · выполнение запрещено', 'danger'), reason,
      button('Переактуализировать', async () => {
        await request(`/api/v1/issues/${issue.id}/revalidate`, 'POST', {reason:reason.value});
        await reload();
      }));
  }
  if (issue.currentResult && !issue.archivedAt && issue.kind !== 'review') {
    const box = element('details'); box.append(element('summary', 'Оценить сложность по итогам реализации'));
    const points = element('select'); points.setAttribute('aria-label', 'Фактическая сложность');
    for (const value of [1,2,3,5,8,13]) points.append(new Option(String(value), value));
    const reason = element('textarea'); reason.rows = 2; reason.maxLength = 2048;
    reason.setAttribute('aria-label', 'Основание итоговой оценки сложности');
    box.append(points, reason, button('Сохранить итоговую оценку', async () => {
      await request(`/api/v1/issues/${issue.id}/actual-complexity`, 'POST', {points:Number(points.value), reason:reason.value});
      await reload();
    }));
    section.append(box);
  }
  parent.append(section);
  renderProblems(parent, issue, reload, onOpen);
}

/** Keep agent difficulties as structured reusable findings, with explicit resolution and follow-up RnD actions. */
function renderProblems(parent, issue, reload, onOpen) {
  const section = element('section', null, 'section problems-section');
  section.append(element('h3', `Проблемы и находки · ${issue.problemsCount || 0}`));
  const list = element('div');
  /** Append one bounded findings page; untrusted content always remains text. */
  function append(items) {
    for (const problem of items) {
      const row = element('article', null, 'task-problem');
      row.append(statusLabel(problem.state === 'resolved' ? 'Решена' : 'Открыта', problem.state === 'resolved' ? 'neutral' : 'accent'));
      row.append(element('h4', problem.title), element('p', problem.details, 'description'), element('small', `${problem.actor} · ${problem.severity}`, 'muted'));
      if (problem.resolution) row.append(element('p', problem.resolution, 'description'));
      row.append(button('План исправления', async () => {
        const value = await request('/api/v1/plans', 'POST', {title:`Исследовать: ${problem.title}`.slice(0,256), spaceId:issue.spaceId,
          markdown:`# ${problem.title}\n\nИсточник: ${issue.key}, проблема ${problem.id}\n\n## Наблюдение\n${problem.details}\n\n## Исследование\n- Проверить причину\n- Согласовать решение\n\n## Проверки\n- Подтвердить устранение исходной проблемы\n`});
        await onOpen(value.id);
      }));
      if (problem.state !== 'resolved' && !issue.archivedAt) {
        const resolution = element('textarea'); resolution.rows = 2; resolution.maxLength = 4096;
        resolution.setAttribute('aria-label', `Решение: ${problem.title}`);
        row.append(resolution, button('Зафиксировать решение', async () => {
          await request(`/api/v1/issues/${issue.id}/problems/${problem.id}`, 'PATCH', {state:'resolved',resolution:resolution.value});
          await reload();
        }));
      }
      list.append(row);
    }
  }
  append(issue.problems?.items || []);
  section.append(list);
  if (!issue.problemsCount) section.append(element('p', 'Агенты ещё не зафиксировали затруднений.', 'muted'));
  if (issue.problems?.hasMore) {
    let offset = issue.problems.nextOffset;
    const more = button('Ещё проблемы', async () => {
      const page = await request(`/api/v1/issues/${issue.id}/problems?offset=${offset}&limit=50`);
      append(page.items); offset = page.nextOffset; more.hidden = !page.hasMore;
    });
    section.append(more);
  }
  if (!issue.archivedAt) {
    const box = element('details'); box.append(element('summary', 'Зафиксировать проблему'));
    const title = element('input'); title.maxLength = 256; title.setAttribute('aria-label', 'Название проблемы');
    const details = element('textarea'); details.rows = 3; details.maxLength = 8192; details.setAttribute('aria-label', 'Условия, влияние и наблюдение');
    const severity = element('select'); severity.setAttribute('aria-label', 'Важность проблемы');
    for (const [value, label] of [['low','Низкая'],['medium','Средняя'],['high','Высокая'],['critical','Критическая']]) severity.append(new Option(label, value));
    severity.value = 'medium';
    box.append(title, severity, details, button('Сохранить проблему', async () => {
      await request(`/api/v1/issues/${issue.id}/problems`, 'POST', {title:title.value, details:details.value, severity:severity.value});
      await reload();
    }));
    section.append(box);
  }
  parent.append(section);
}
