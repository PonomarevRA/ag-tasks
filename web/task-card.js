import { element, statusLabel } from "./dom.js";
import { renderLifecycle } from "./task-workflow.js";
import { renderTaskLabels } from "./task-labels.js";
import { formatElapsed, roles, statuses } from "./task-format.js";

/**
 * Compose one task summary in graph, board and list without replacing its native action.
 * @param {HTMLElement} card - Existing host control; metadata is grouped without nesting interactive controls.
 * @param {object} issue - Literal server status, lifecycle, role, labels and accounted work values.
 */
export function renderCardSummary(card, issue) {
  const heading = element("span", null, "task-card-heading");
  heading.append(
    element("span", issue.key, "key"),
    statusLabel(
      statuses[issue.status] || issue.status || "Статус неизвестен",
      issue.status === "blocked" ? "danger" : "neutral",
    ),
  );
  card.replaceChildren(
    heading,
    element("strong", issue.title, "task-card-title"),
  );
  const flow = element('div', null, 'task-card-state');
  renderLifecycle(flow, issue);
  card.append(flow);
  card.dataset.needsAttention = String(Boolean(issue.lifecycle?.needsAttention));
  // Card buttons have an explicit short name; expose the same rendered lifecycle as context.
  const lifecycle = card.querySelector('[data-lifecycle-description]');
  const context = [statuses[issue.status] || issue.status || "Статус неизвестен", lifecycle?.textContent].filter(Boolean);
  card.setAttribute("aria-label", `${issue.key}: ${issue.title}. ${context.join(". ")}`);
  const contextPanel = element('div', null, 'task-card-context');
  contextPanel.append(
    element(
      "span",
      roles[issue.intendedRole] || "Роль не назначена",
      "task-card-role",
    ),
  );
  renderTaskLabels(contextPanel, issue, { compact: true });
  const facts = [];
  if (issue.kind === "rnd") facts.push("RnD · План");
  if (issue.kind === "review") facts.push("Проверка критика");
  if (issue.storage?.tier === "zip") facts.push("ZIP · откроется по запросу");
  if (issue.estimatedComplexity)
    facts.push(
      `Оценка ${issue.estimatedComplexity.points} · факт ${issue.actualComplexity?.points ?? "—"}`,
    );
  if (issue.work?.elapsedMs > 0 || issue.work?.state === 'running')
    facts.push(`В работе ${formatElapsed(issue.work?.elapsedMs)}`);
  if (facts.length) contextPanel.append(element("span", facts.join(" · "), "task-card-facts muted"));
  card.append(contextPanel);
}
