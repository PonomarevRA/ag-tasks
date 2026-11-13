import { button, element, statusLabel } from "./dom.js";
import { renderLifecycle } from "./task-workflow.js";
import { roles, statuses } from "./task-format.js";
import { renderReviewProgress, reviewTime } from "./review-progress.js";

/** Draw the detail structure with decorative AG skeleton shapes during a pending read. */
export function createTaskDetailSkeleton() {
  const root = element("div", null, "task-detail-skeleton");
  root.setAttribute("aria-hidden", "true");
  const summary = element("div", null, "task-detail-summary");
  const columns = element("div", null, "task-detail-columns");
  const main = element("div", null, "task-detail-main");
  const actions = element("div", null, "task-detail-actions");
  summary.append(
    element("span", null, "task-detail-skeleton-line skeleton-status"),
    element("span", null, "task-detail-skeleton-line skeleton-heading"),
    element("span", null, "task-detail-skeleton-line skeleton-caption"),
    element("span", null, "task-detail-skeleton-line skeleton-action"),
  );
  main.append(
    element("span", null, "task-detail-skeleton-line skeleton-heading"),
    element("span", null, "task-detail-skeleton-line"),
    element("span", null, "task-detail-skeleton-line skeleton-caption"),
    element("span", null, "task-detail-skeleton-line skeleton-section"),
    element("span", null, "task-detail-skeleton-line skeleton-section"),
  );
  actions.append(
    element("span", null, "task-detail-skeleton-line skeleton-caption"),
    element("span", null, "task-detail-skeleton-line skeleton-section"),
    element("span", null, "task-detail-skeleton-line skeleton-section"),
  );
  columns.append(main, actions);
  root.append(summary, columns);
  return root;
}

/**
 * Compose retained native disclosures around actual task data without owning its model or commands.
 * @param {object} issue - Current server task, including its independently derived lifecycle and result.
 * @param {object} options - One phase action and retained section state.
 * @returns {object} Root and explicit rendering slots; existing features retain their controls and handlers.
 */
export function createTaskDetailLayout(issue, {nextAction, expanded = new Map()} = {}) {
  const root = element("div", null, "task-detail-layout");
  root.dataset.agWidth = "fill";
  const summary = element("section", null, "task-detail-summary");
  summary.dataset.agWidth = "fill";
  summary.setAttribute("aria-label", "Текущее состояние задачи");
  const identity = element("div", null, "task-detail-identity");
  identity.append(
    statusLabel(statuses[issue.status] || issue.status || "Статус неизвестен"),
    element(
      "span",
      roles[issue.intendedRole] || "Роль не назначена",
      "task-detail-role",
    ),
  );
  summary.append(identity);
  if (issue.reviewProgress) renderReviewProgress(summary, issue.reviewProgress);
  else renderLifecycle(summary, issue);
  if (issue.kind !== "review") {
    const owner = issue.work?.activeSession?.agentId || issue.assignee;
    summary.append(element("p", owner ? `Ответственный: ${owner}` : "Ответственный не назначен", "muted"));
  }
  if (issue.lastActivityAt && Number.isFinite(Date.parse(issue.lastActivityAt))) {
    const activity = element("p", "Последняя значимая активность · ", "muted");
    activity.append(reviewTime(issue.lastActivityAt));
    summary.append(activity);
  }
  const readiness = (issue.dor || []).filter(item => item.passed).length;
  const completion = (issue.dod || []).filter(item => item.passed).length;
  if (issue.kind !== "review") {
    summary.append(element("p", `Готовность ${readiness}/${issue.dor?.length || 0} · Завершение ${completion}/${issue.dod?.length || 0}`, "muted"));
  }
  if (nextAction) summary.append(button(nextAction.label, nextAction.run, "task-detail-next primary"));
  const columns = element("div", null, "task-detail-columns");
  columns.dataset.agWidth = "fill";
  const actions = element("section", null, "task-detail-actions");
  actions.dataset.agWidth = "content";
  actions.dataset.agWidthMinEm = "10";
  actions.setAttribute("aria-label", "Действия с задачей");
  actions.append(element("h3", "Управление задачей"));
  const main = element("div", null, "task-detail-main");
  main.dataset.agWidth = "content";
  main.dataset.agWidthMinEm = "12";
  const description = element("section", null, "task-detail-description");
  description.append(element("h3", "Задача и ожидаемый результат"));
  main.append(description);

  /**
   * Append one native disclosure while preserving its feature's DOM and natural content height.
   * @param {Element} owner - Same-document content owner.
   * @param {string} key - Persistent identity within the current task.
   * @param {string} title - Visible, keyboard accessible summary.
   * @param {boolean} open - Initial visibility chosen from actual task evidence.
   * @returns {Element} Feature slot inside the disclosure.
   */
  function disclosure(owner, key, title, open = false) {
    const group = element("details", null, "task-detail-disclosure");
    group.dataset.detailSection = key;
    group.open = expanded.get(key) ?? open;
    group.append(element("summary", title));
    const slot = element("div", null, "task-detail-slot");
    group.append(slot);
    owner.append(group);
    return slot;
  }

  const documents = disclosure(
    main,
    "documents",
    "Файлы и документы",
    Boolean(issue.documents?.length),
  );
  const criteria = disclosure(main, "criteria", "Готовность и критерии завершения", !issue.reviewProgress);
  const results = disclosure(
    main,
    "results",
    "Результат работы",
    Boolean(issue.currentResult && !issue.reviewProgress),
  );
  const critic = disclosure(
    main,
    "critic",
    issue.codeTask ? "Независимое ревью кода" : "Проверка критика",
    Boolean(issue.criticReview),
  );
  const links = disclosure(
    main,
    "links",
    "Связи и подзадачи",
    Boolean(issue.parentId || issue.childProgress?.total),
  );
  const evidence = disclosure(
    main,
    "evidence",
    "Инциденты и доказательства",
    Boolean(issue.externalRefs?.length),
  );
  const history = disclosure(main, "history", "Обсуждение и история");
  const conversations = disclosure(main, "conversations", "Связанные чаты", Boolean(issue.conversations?.items?.length));
  const planning = disclosure(
    main,
    "planning",
    "Оценка сложности и проблемы",
    Boolean(issue.problemsCount),
  );
  const overview = disclosure(actions, "overview", "Роль, рабочие сессии и время");
  columns.append(main, actions);
  root.append(summary, columns);
  return {
    root,
    actions,
    description,
    documents,
    criteria,
    results,
    critic,
    links,
    evidence,
    history,
    conversations,
    planning,
    overview,
  };
}
