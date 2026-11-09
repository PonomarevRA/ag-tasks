import { element, statusLabel } from "./dom.js";
import { renderLifecycle } from "./task-workflow.js";
import { roles, statuses } from "./task-format.js";

/**
 * Compose retained native disclosures around actual task data without owning its model or commands.
 * @param {object} issue - Current server task, including its independently derived lifecycle and result.
 * @returns {object} Root and explicit rendering slots; existing features retain their controls and handlers.
 */
export function createTaskDetailLayout(issue) {
  const root = element("div", null, "task-detail-layout");
  const summary = element("section", null, "task-detail-summary");
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
  renderLifecycle(summary, issue);
  const columns = element("div", null, "task-detail-columns");
  const actions = element("section", null, "task-detail-actions");
  actions.setAttribute("aria-label", "Действия с задачей");
  actions.append(element("h3", "Управление задачей"));
  const main = element("div", null, "task-detail-main");
  const description = element("section", null, "task-detail-description");
  description.append(element("h3", "Задача и ожидаемый результат"));
  main.append(description);

  /**
   * Append one native disclosure while preserving its feature's DOM and natural content height.
   * @param {Element} owner - Same-document content owner.
   * @param {string} title - Visible, keyboard accessible summary.
   * @param {boolean} open - Initial visibility chosen from actual task evidence.
   * @returns {Element} Feature slot inside the disclosure.
   */
  function disclosure(owner, title, open = false) {
    const group = element("details", null, "task-detail-disclosure");
    group.open = open;
    group.append(element("summary", title));
    const slot = element("div", null, "task-detail-slot");
    group.append(slot);
    owner.append(group);
    return slot;
  }

  const documents = disclosure(
    main,
    "Файлы и документы",
    Boolean(issue.documents?.length),
  );
  const criteria = disclosure(main, "Готовность и критерии завершения", true);
  const results = disclosure(
    main,
    "Результат работы",
    Boolean(issue.currentResult),
  );
  const critic = disclosure(
    main,
    issue.codeTask ? "Независимое ревью кода" : "Проверка критика",
    Boolean(issue.criticReviewId || issue.criticReview),
  );
  const links = disclosure(
    main,
    "Связи и подзадачи",
    Boolean(issue.parentId || issue.childProgress?.total),
  );
  const evidence = disclosure(
    main,
    "Инциденты и доказательства",
    Boolean(issue.externalRefs?.length),
  );
  const history = disclosure(main, "Обсуждение и история");
  const conversations = disclosure(main, "Связанные чаты", Boolean(issue.conversations?.items?.length));
  const planning = disclosure(
    main,
    "Оценка сложности и проблемы",
    Boolean(issue.problemsCount),
  );
  const overview = disclosure(actions, "Роль, рабочие сессии и время");
  columns.append(actions, main);
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
