import {element} from './dom.js';

/** Describe confirmed task work; recency is a 15-minute hint, not an active lease. */
export function agentActivity(summary, now = Date.now()) {
  if (!summary) return {text:'Активность неизвестна', state:'unknown'};
  if (summary.activeTasks > 0) return {text:`Агенты сейчас · задач: ${summary.activeTasks}`, state:'active'};
  if (!summary.lastAgentWorkAt) return {text:'Нет записей о работе агентов', state:'none'};
  const time = Date.parse(summary.lastAgentWorkAt);
  if (!Number.isFinite(time) || time > now) return {text:'Время активности неизвестно', state:'unknown'};
  const minutes = Math.floor((now - time) / 60000);
  let age = 'меньше минуты назад';
  if (minutes >= 1440) age = `${Math.floor(minutes / 1440)} дн. назад`;
  else if (minutes >= 60) age = `${Math.floor(minutes / 60)} ч. назад`;
  else if (minutes >= 1) age = `${minutes} мин. назад`;
  return {text:`Агенты: ${age}`, state:minutes < 15 ? 'recent' : 'idle'};
}

/** Keep native option meaning readable without relying on platform-specific option colors. */
export function spaceOptionLabel(space, now = Date.now()) {
  const name = `${space.prefix} · ${space.name}`;
  if (!space.summary) return `${name} · Сводка недоступна`;
  const summary = space.summary;
  return `${name} · ○ Бэклог ${summary.backlog} · ● В работе ${summary.inProgress} · ${agentActivity(summary, now).text}`;
}

/** Compose noninteractive status text inside the existing host-owned space button. */
export function renderSpaceSummary(space, now = Date.now()) {
  const row = element('span', null, 'space-summary');
  if (!space.summary) {
    row.textContent = 'Сводка недоступна';
    return row;
  }
  const summary = space.summary;
  row.append(element('span', `○ Бэклог ${summary.backlog}`, 'space-backlog'));
  row.append(element('span', `● В работе ${summary.inProgress}`, 'space-in-progress'));
  const activity = agentActivity(summary, now);
  const hint = element('span', `● ${activity.text}`, `space-activity space-activity--${activity.state}`);
  if (summary.lastAgentWorkAt) hint.title = `Последнее подтверждение работы: ${summary.lastAgentWorkAt}`;
  row.append(hint);
  return row;
}

/** Build a server graph filter before pagination; the host retains query and status filters. */
export function taskGraphPath(spaceId, activeOnly) {
  const query = new URLSearchParams({spaceId, limit:'200'});
  if (activeOnly) query.set('activeOnly', 'true');
  return `/api/v1/issues/graph?${query}`;
}
