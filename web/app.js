import {createTaskDetailLayout, createTaskDetailSkeleton} from './task-detail-layout.js';
import {renderTaskConversations} from './task-conversations.js';
import {bindWorkAreaFit} from './work-area-fit.js';
import {renderManagerWorkspace} from './manager-workspace.js';
import {changedIssueFields,readIssueEditorFields} from './task-editor-patch.js';
import {renderReviewInbox} from './review-inbox.js';
import {revealIssueEditorGroups, bindIssueEditorValidation} from './issue-editor.js';
import {renderCardSummary} from './task-card.js';
import {bindWindowShell} from './vendor/ag-design/index.js';
import {connect, request, command} from "./api.js";
import {renderAgentSessions} from './agent-sessions.js';
import {bindTaskNavigation, selectTaskView} from "./navigation.js";
import {bindSidebar} from "./sidebar.js";
import {bindDesktopAutomation} from './desktop-automation.js';
import {element, button, report, evidenceLink, emptyState, labelledField} from "./dom.js";

import {statuses, priorities, roles, issuePagePath, visibleIssues} from "./task-format.js";
import {applyIdentity} from './task-colors.js';
import {markdownBody, renderDocuments} from './task-documents.js';
import {renderPlanning} from './task-planning.js';
import {renderTaskGraph} from "./task-graph.js";
import {renderSpaceSummary, taskGraphPath} from './space-activity.js';
import {criteriaEditor, renderTaskWorkflow} from "./task-workflow.js";

const state = {view:"sessions", spaceId:"default", spaces:[], issues:[], graph:null, repos:[], more:false, selected:null, review:null, loading:false, nextOffset:0};
const sessionSelection = {id:null};
const analyticsFilters = {};
const serviceScopes = new Map([["sessions", "Все источники сервиса"], ["analytics", "Все проекты"], ["repositories", "Общие репозитории"]]);
let renderedScope = null, failedAppend = false;
let refreshGeneration = 0, graphDisposer = null, readDor = () => [], readDod = () => [], deletingIssue = null;
const content = document.querySelector("#content");
const editor = document.querySelector("#editor");
const detail = document.querySelector("#detail");
const approval = document.querySelector("#approval");
let detailDraft = null, detailOrigin = null, pendingDetailReturn = null;
let detailGeneration = 0, reviewGeneration = 0, editorGeneration = 0, editingIssue = null, editorBaseline = null;
// Editor identity advances on open; dialog.open fences closed drafts without a queued-close race.
bindIssueEditorValidation(document.querySelector("#issue-form"));
/** Restore the source row by identity, or the view heading if it has left the current result set. */
function restoreDetailFocus(origin) {
  if (!origin || document.querySelector("dialog[open]")) return;
  const visible = node => node?.isConnected && !node.disabled && !node.closest("[hidden],[inert],dialog:not([open])");
  const original = visible(origin.opener) ? origin.opener : null;
  const replacement = [...content.querySelectorAll("[data-issue-id]")].find(node => node.dataset.issueId === origin.id && visible(node));
  (original || replacement || document.querySelector("#heading")).focus();
}

/** Let a replaced manager table finish before resolving its source button. */
function managerLoading() {
  return Boolean(content.querySelector('.manager-report[aria-busy="true"]'));
}

/** Restore late-arriving focus only while the fallback heading still owns it. */
function finishDetailFocus() {
  if (state.loading || managerLoading()) return;
  if (pendingDetailReturn && document.activeElement === document.querySelector("#heading")) {
    restoreDetailFocus(pendingDetailReturn);
  }
  pendingDetailReturn = null;
}

// Closing invalidates in-flight reads while retaining the source of the original dialog entry.
detail.addEventListener("close", () => {
  if (detail.open) return;
  detailGeneration++;
  reviewGeneration++;
  state.selected = null;
  detailDraft = null;
  if (!document.querySelector("dialog[open]")) {
    if (detailOrigin && (state.loading || managerLoading())) {
      pendingDetailReturn = detailOrigin;
      document.querySelector("#heading").focus();
    } else restoreDetailFocus(detailOrigin);
  }
  detailOrigin = null;
});
approval.addEventListener("close", () => { if (!approval.open) { reviewGeneration++; state.review = null; } });

/** Refresh actual scoped records; newer navigation always wins over an older in-flight response. */
async function refresh(append = false) {
  const generation = ++refreshGeneration;
  const spaceId = state.spaceId, view = state.view, scope = viewScope();
  const retained = renderedScope === scope;
  if (!retained) {
    graphDisposer?.(); graphDisposer = null;
    content.replaceChildren();
    renderedScope = null;
    document.querySelector("#coverage").textContent = "";
  }
  showLoadFeedback(retained ? "Обновляем данные… Показана предыдущая загрузка." : "Загружаем данные…");
  document.querySelector("#notice").textContent = "";
  state.loading = true;
  document.querySelector("#more").hidden = true;
  content.setAttribute("aria-busy", "true");
  content.inert = true;
  renderViewChrome();
  const offset = append ? state.nextOffset : 0;
  try {
    if (view === 'analytics') {
      state.more = false;
      renderedScope = scope;
      render();
      showLoadFeedback("");
      return;
    }
    if (view === 'sessions') {
      const spaces = await request('/api/v1/spaces');
      if (generation !== refreshGeneration) return;
      state.spaces = spaces.items; state.more = false; renderedScope = scope; renderSpaces(); render();
      showLoadFeedback("");
      return;
    }
    let pagePath = issuePagePath(spaceId, view === "archive", offset);
    if (view === "board") pagePath += "&board=true";
    if (view === "reviews") pagePath = `/api/v1/issues/reviews?spaceId=${encodeURIComponent(spaceId)}&limit=200&offset=${offset}`;
    const [issues, repos, spaces, graph] = await Promise.all([
      request(pagePath),
      request("/api/v1/repositories"), request("/api/v1/spaces"),
      view === "graph" ? request(taskGraphPath(spaceId, document.querySelector('#active-work-only').checked)) : Promise.resolve(null),
    ]);
    if (generation !== refreshGeneration) return;
    const unique = new Map((append ? state.issues : []).map(issue => [issue.id, issue]));
    for (const issue of issues.items) unique.set(issue.id, issue);
    const nextOffset = issues.nextOffset ?? offset + issues.items.length;
    if (issues.hasMore && nextOffset <= offset) throw new Error("Сервис вернул страницу без продвижения. Обновите список.");
    state.issues = [...unique.values()];
    state.repos = repos.items;
    state.spaces = spaces.items;
    state.graph = graph;
    state.more = Boolean(issues.hasMore);
    state.nextOffset = nextOffset;
    renderedScope = scope;
    renderSpaces();
    render();
    showLoadFeedback("");
  } catch (error) {
    if (generation === refreshGeneration) {
      failedAppend = append;
      const explanation = retained
        ? "Не удалось обновить данные. Показана предыдущая загрузка; данные могут быть устаревшими."
        : "Не удалось загрузить выбранное представление.";
      if (!retained) content.replaceChildren(emptyState("Данные не загружены", "Повторите загрузку, когда сервис станет доступен."));
      showLoadFeedback(`${explanation} ${error?.message || "Сервис недоступен."}`, true);
    }
    // This read boundary owns recovery; retry repeats only the failed GET/page, never a command.
  } finally {
    if (generation === refreshGeneration) {
      state.loading = false;
      content.setAttribute("aria-busy", "false");
      content.inert = false;
      finishDetailFocus();
    }
  }
}

/** Identify the data context, including the server-side graph filter, to prevent cross-space stale content. */
function viewScope() {
  return JSON.stringify([state.view, serviceScopes.has(state.view) ? null : state.spaceId,
    state.view === 'graph' && document.querySelector('#active-work-only').checked]);
}

/** Announce loading/recovery outside the busy result region without moving keyboard focus. */
function showLoadFeedback(text, retry = false) {
  document.querySelector('#view-feedback').hidden = !text;
  document.querySelector('#view-load-status').textContent = text;
  document.querySelector('#retry-view').hidden = !retry;
}

/** Keep navigation, filters and contextual actions aligned even while the next view is loading. */
function renderViewChrome() {
  renderHeading();
  document.body.dataset.taskView = state.view;
  selectTaskView(document.querySelector("nav"), state.view);
  document.querySelector("#view-filters").hidden = ['repositories','sessions','reviews','analytics'].includes(state.view);
  document.querySelector('.history-toggle').hidden = ['sessions','reviews','repositories','analytics'].includes(state.view);
  document.querySelector('.active-work-toggle').hidden = state.view !== 'graph';
  document.querySelector('.board-empty-toggle').hidden = state.view !== 'board';
  const filtered = document.querySelector('#issue-query').value.trim() || document.querySelector('#status-filter').value;
  document.querySelector('#reset-filters').hidden = ['repositories','sessions','reviews','analytics'].includes(state.view) || !filtered;
}

/** Reapply local filters only to records that belong to the currently selected context. */
function renderLoadedView() {
  renderViewChrome();
  if (renderedScope === viewScope()) render();
}

/** Distinguish filtered results, hidden completed work and a genuinely empty workspace. */
function taskEmptyState(filters) {
  const filtered = filters.query.trim() || filters.status;
  const history = !filters.archived && !filters.includeHistory && state.issues.some(issue => !issue.archivedAt && !issue.deletedAt && ['done','cancelled'].includes(issue.status));
  let title = 'В пространстве пока нет задач';
  let text = 'Задачи появятся, когда агент создаст план и начнёт работу. При необходимости задачу можно создать кнопкой «＋ Задача».';
  if (filtered) {
    title = 'Задачи не найдены';
    text = 'В загруженной выборке нет совпадений. Измените запрос или сбросьте фильтры.';
  } else if (filters.archived) {
    title = 'Архив пуст';
    text = 'Здесь появятся задачи, перенесённые в архив.';
  } else if (history) {
    title = 'Нет текущих задач';
    text = 'Завершённая работа скрыта. Включите «История и завершённые проверки», чтобы увидеть результаты.';
  }
  return emptyState(title, `${text}${state.more ? ' Есть следующие страницы — нажмите «Показать ещё».' : ''}`);
}

/** Render the selected view; graph disposal flushes its per-space camera before replacing the DOM. */
function render() {
  graphDisposer?.();
  graphDisposer = null;
  content.replaceChildren();
  renderViewChrome();
  document.querySelector("#more").hidden = !state.more || ["repositories", "graph"].includes(state.view);
  const coverage = document.querySelector("#coverage");
  coverage.textContent = state.more && !["repositories", "graph"].includes(state.view) ? `Загружено ${state.issues.length} задач. Счётчики относятся к этой выборке. «Показать ещё» продолжит загрузку.` : "";
  if (state.view === 'sessions') {
    document.querySelector('#more').hidden = true; coverage.textContent = '';
    graphDisposer = renderAgentSessions(content,{request,command,selection:sessionSelection,openIssue,onError:report});
    return;
  }
  if (state.view === 'analytics') {
    graphDisposer = renderManagerWorkspace(content, {request, openIssue, filters:analyticsFilters, onSettled:finishDetailFocus});
    return;
  }
  if (state.view === "repositories") { renderRepositories(); return; }
  if (state.view === 'reviews') {
    graphDisposer = renderReviewInbox(content, state.issues, {request, onOpen:openIssue, onRefresh:refresh, spaceId:state.spaceId});
    coverage.textContent = `Ожидают проверки: ${state.issues.length}${state.more ? ' · есть следующие страницы' : ''}. Сначала самые старые.`;
    return;
  }
  const filters = {archived:state.view === "archive",query:document.querySelector("#issue-query").value,status:document.querySelector("#status-filter").value,includeHistory:document.querySelector("#include-history").checked};
  if (state.view === "graph") {
    const graph = state.graph || {nodes:[],edges:[],stubs:[]};
    const visible = visibleIssues(graph.nodes, filters);
    if (!visible.length) {
      const activeOnly = document.querySelector('#active-work-only').checked;
      const title = activeOnly ? 'Нет текущей работы в этой выборке' : 'Нет задач в этой выборке';
      const explanation = activeOnly
        ? 'Нет задач с действующей рабочей сессией, подходящих под фильтры. Снимите галочку или измените фильтры.'
        : 'Создайте задачу или измените фильтры пространства.';
      content.append(emptyState(title, explanation));
      coverage.textContent = graph.truncated ? 'Ответ сокращён сервером; измените фильтры или обновите граф.' : '';
      return;
    }
    graphDisposer = renderTaskGraph(content, {...graph, nodes:visible}, {statuses,onOpen:openIssue,storageKey:`ag-tasks:graph-board:v1:${state.spaceId}`});
    coverage.textContent = `Показано ${visible.length} из ${graph.nodeCount ?? graph.nodes.length} задач; связей в выборке: ${graph.edges.length} из ${graph.edgeCount ?? graph.edges.length}.${graph.truncated ? " Ответ сокращён сервером; граф не является полным." : ""}${filters.query || filters.status ? " Фильтр применяется к загруженным узлам." : ""}`;
    return;
  }
  const issues = visibleIssues(state.issues, filters);
  if (!issues.length) { content.append(taskEmptyState(filters)); return; }
  coverage.textContent = `Показано ${issues.length} из ${state.issues.length} загруженных задач.${state.more ? ' Есть следующие страницы — «Показать ещё».' : ''}${!filters.archived && !filters.includeHistory && !filters.status ? ' Завершённые задачи скрыты.' : ''}`;
  const list = element("div", null, state.view === "board" ? "board" : "issue-list");
  if (state.view === "board") {
    for (const [status, title] of Object.entries(statuses)) {
      const column = element("section", null, "column");
      const items = issues.filter(issue => issue.status === status);
      if (!items.length && !document.querySelector("#show-empty-columns").checked) continue;
      column.classList.toggle("column--empty", items.length === 0);
      column.setAttribute("aria-label", `${title}: ${items.length}`);
      column.append(element("h2", `${title} · ${items.length}`, "column-heading"));
      for (const issue of items) column.append(issueCard(issue));
      list.append(column);
    }
  } else for (const issue of issues) list.append(issueCard(issue, true));
  content.append(list);
}

/** Keep actual selected-space identity visible even when navigation labels are collapsed. */
function renderHeading() {
  const titles = {analytics:'Контроль задач',reviews:'Ожидают ревью',sessions:'Агенты и сессии',graph:"Граф задач",board:"Канбан",list:"Все задачи",archive:"Архив",repositories:"Репозитории"};
  document.querySelector("#heading").textContent = titles[state.view];
  const scope = document.querySelector("#space-breadcrumb");
  const serviceScope = serviceScopes.get(state.view);
  if (serviceScope) {
    scope.classList.remove("named-identity");
    scope.style.removeProperty("--identity-color");
    scope.textContent = serviceScope;
    return;
  }
  const space = state.spaces.find(item => item.id === state.spaceId);
  applyIdentity(scope, space?.name || state.spaceId);
  scope.textContent = space ? `${space.prefix} · ${space.name}` : state.spaceId;
}

/** Keep the selected space reachable both in expanded navigation and through its icon-rail dialog. */
function renderSpaces() {

  const list = document.querySelector("#space-list");
  list.replaceChildren();
  for (const space of state.spaces) {
    const control = button(`${space.prefix} · ${space.name}`, async () => {
      document.querySelector("#spaces-dialog").close();
      state.spaceId = space.id;
      await refresh();
    });
    control.setAttribute("aria-current", space.id === state.spaceId ? "true" : "false");
    control.replaceChildren(element('span', `${space.prefix} · ${space.name}`, 'space-name'), renderSpaceSummary(space));
    control.classList.add('space-choice');
    applyIdentity(control, space.name);
    const row = element('div', null, 'space-management-row');
    const editSpace = button('Изменить', () => {
      const form = document.querySelector('#space-form');
      form.dataset.spaceId = space.id;
      form.elements.name.value = space.name;
      form.elements.prefix.value = space.prefix || '';
      form.querySelector('[type=submit]').textContent = 'Сохранить';
      form.elements.prefix.focus();
    });
    editSpace.setAttribute('aria-label', `Изменить пространство ${space.name}`);
    row.append(control, editSpace);
    row.dataset.spaceId = space.id;
    list.append(row);
  }
  const chosen = state.spaces.find(item => item.id === state.spaceId);
  const picker = document.querySelector("#space-manage");
  const name = chosen ? [chosen.prefix, chosen.name].filter(Boolean).join(" · ") : "Пространство";
  document.querySelector("#space-picker-label").textContent = name;
  picker.title = `Выбрать пространство · ${name}`;
  picker.setAttribute("aria-label", picker.title);
}

/** Show hierarchy, status and assignee on a compact clickable issue card. */
function issueCard(issue, row = false) {
  const card = button(issue.title, () => { void openIssue(issue.id, card); }, row ? "issue-row" : "card");
  applyIdentity(card, issue.title, issue.spaceName || issue.spaceId);
  card.replaceChildren(); card.dataset.issueId = issue.id; card.setAttribute("aria-label", `${issue.key}: ${issue.title}`);
  renderCardSummary(card, issue);
  const contextPanel = card.querySelector(".task-card-context");
  if (issue.parentId) contextPanel.append(element('span', `↳ ${state.issues.find(parent => parent.id === issue.parentId)?.key || 'Подзадача'}`, 'muted'));
  if (issue.assignee) contextPanel.append(element('span', issue.assignee, 'muted'));
  if (issue.priority !== 'normal' && priorities[issue.priority]) card.append(element('span', priorities[issue.priority], 'priority'));
  return card;
}

/** Populate parent/repository selectors using explicit local IDs. */
function populateSelect(select, items, none) {
  select.replaceChildren(new Option(none, ""));
  for (const item of items) select.append(new Option(item.key ? `${item.key} · ${item.title}` : item.name, item.id));
}

/** Open a fresh create form, optionally attached to an existing parent issue. */
function openEditor(parent = null, existing = null) {
  const form = document.querySelector("#issue-form");
  form.reset();
  editorGeneration++;
  form.querySelector("[type=submit]").disabled = false;
  editingIssue = existing;
  form.elements.codeTask.checked = Boolean(existing?.codeTask);
  form.elements.languages.value = (existing?.languages || []).join(", ");
  form.elements.systems.value = (existing?.systems || []).join(", ");
  form.elements.kind.value = existing?.kind || "task";
  form.elements.kind.disabled = Boolean(existing);
  form.elements.estimatedPoints.value = existing?.estimatedComplexity?.points || "";
  form.elements.estimatedReason.value = existing?.estimatedComplexity?.reason || "";
  form.elements.dueDate.value = existing?.dueDate || '';
  document.querySelector("#editor-title").textContent = existing ? "Редактировать задачу" : "Новая задача";
  form.querySelector("[type=submit]").textContent = existing ? "Сохранить" : "Создать";
  editor.querySelectorAll(".error").forEach(node => node.remove());
  populateSelect(form.elements.parentId, state.issues.filter(item => item.id !== existing?.id && !item.archivedAt && !["done","cancelled","stale"].includes(item.status) && item.spaceId === (existing?.spaceId || parent?.spaceId || state.spaceId)), "Без родительской задачи");
  populateSelect(form.elements.repoId, state.repos, "Не выбран");
  populateSelect(form.elements.spaceId, state.spaces, "Выберите пространство");
  form.elements.spaceId.value = existing?.spaceId || parent?.spaceId || state.spaceId;
  form.elements.spaceId.disabled = Boolean(parent || existing?.parentId);
  form.elements.intendedRole.replaceChildren(new Option("Не назначена", ""));
  for (const [value, title] of Object.entries(roles)) form.elements.intendedRole.append(new Option(title, value));
  readDor = criteriaEditor(document.querySelector("#dor-editor"), existing?.dor || []);
  readDod = criteriaEditor(document.querySelector("#dod-editor"), existing?.dod || []);
  if (parent) {
    if (![...form.elements.parentId.options].some(option => option.value === parent.id)) {
      form.elements.parentId.append(new Option(`${parent.key} · ${parent.title}`, parent.id));
    }
    form.elements.parentId.value = parent.id;
    form.elements.repoId.value = parent.repoId || "";
  }
  if (existing) {
    for (const key of ["title", "description", "priority", "assignee", "parentId", "repoId", "intendedRole"]) {
      const value = existing[key] || "";
      if (["parentId", "repoId"].includes(key) && value && ![...form.elements[key].options].some(option => option.value === value)) form.elements[key].append(new Option(value, value));
      form.elements[key].value = value;
    }
    form.elements.repoId.disabled = Boolean(existing.branch);
  } else form.elements.repoId.disabled = false;
  editorBaseline = existing ? readIssueEditorFields(form,{issue:existing,readDor,readDod}) : null;
  revealIssueEditorGroups(form);
  editor.querySelector(".dialog-body").scrollTop = 0;
  editor.showModal();
  form.elements.title.focus();
}

/**
 * Load an exact task session while retaining the surrounding board and same-task comment draft.
 * @param {string} identifier - Server ID or public task key; used only in fixed bounded read routes.
 * @param {Element} opener - Explicit button for pointer activation in native WebKit.
 * Closed or superseded sessions ignore late responses; current failures retain an explicit safe retry.
 */
async function openIssue(identifier, opener = document.activeElement) {
  if (!detail.open) {
    detailOrigin = {id:identifier, opener:opener !== document.body ? opener : null};
    pendingDetailReturn = null;
  }
  const generation = ++detailGeneration;
  const body = document.querySelector("#detail-body");
  const comment = body.querySelector('[data-detail-draft="comment"]');
  if (state.selected?.id === identifier && comment)
    detailDraft = {id:identifier, comment:comment.value.slice(0, 8192), expanded:new Map(
      [...body.querySelectorAll("[data-detail-section]")].map(section => [section.dataset.detailSection, section.open])
    )};
  else if (detailDraft?.id !== identifier) detailDraft = null;
  state.selected = null;
  document.querySelector("#detail-title").textContent = "Загрузка задачи…";
  body.replaceChildren(createTaskDetailSkeleton());
  body.setAttribute("aria-busy", "true");
  document.querySelector("#detail-feedback").textContent = "Загружаем задачу…";
  // Show immediately: a pending B already owns its dialog when close(A) is delivered.
  if (!detail.open) detail.showModal();
  const ownsDetail = () => generation === detailGeneration && detail.open;
  try {
    const issue = await request(`/api/v1/issues/${encodeURIComponent(identifier)}`);
    if (!ownsDetail()) return;
    const children = await request(`/api/v1/issues?spaceId=${encodeURIComponent(issue.spaceId)}&parentId=${encodeURIComponent(issue.id)}&includeArchived=true&limit=200`);
    if (!ownsDetail()) return;
    state.selected = issue;
    document.querySelector("#detail-title").textContent = `${issue.key} · ${issue.title}`;
    const nextAction = taskDetailAction(issue);
    const layout = createTaskDetailLayout(issue, {nextAction, expanded:detailDraft?.expanded});
    layout.description.append(issue.descriptionHtml ? markdownBody(issue.descriptionHtml) : element("p", issue.description || "Описание пока не добавлено", "description"));
    layout.description.append(element('p',`Приоритет: ${priorities[issue.priority] || issue.priority} · Срок (UTC): ${issue.dueDate || 'Не задан'}`,'muted'));
    const reload = async () => { await refresh(); if (detail.open && state.selected?.id === issue.id) await openIssue(issue.id); };
    renderIssueControls(layout.actions, issue, nextAction?.kind);
    layout.actions.append(layout.overview.parentElement);
    renderDocuments(layout.documents, issue, reload);
    renderPlanning(layout.planning, issue, reload, openIssue);
    renderTaskWorkflow(layout.root, issue, openIssue, {overviewHost:layout.overview, criteriaHost:layout.criteria, resultsHost:layout.results, criticHost:layout.critic});
    renderIssueRelations(layout.links, issue, children.items, children.hasMore);
    renderIssueEvidence(layout.evidence, issue);
    renderIssueHistory(layout.history, issue);
    renderTaskConversations(layout.conversations, issue, {request, isCurrent: ownsDetail});
    body.replaceChildren(layout.root);
    if (detailDraft?.id === issue.id) {
      const retained = body.querySelector('[data-detail-draft="comment"]');
      retained.value = detailDraft.comment;
    }
    const activeDialog = document.activeElement?.closest("dialog[open]");
    if ((!activeDialog || document.activeElement === detail) && detail.open) detail.querySelector("[data-close=detail]").focus();
  } catch (error) {
    if (!ownsDetail()) return;
    document.querySelector("#detail-title").textContent = "Задача не загружена";
    const notice = element("p", error.message || "Не удалось загрузить задачу.", "error");
    notice.setAttribute("role", "status");
    body.replaceChildren(notice, button("Повторить загрузку задачи", () => openIssue(identifier)));
  } finally {
    if (ownsDetail()) {
      body.setAttribute("aria-busy", "false");
      document.querySelector("#detail-feedback").textContent = "";
    }
  }
}

/** Resolve one available phase action using the detail's authoritative relationship targets. */
function taskDetailAction(issue) {
  const target = issue.reviewProgress?.nextAction;
  if (target?.kind === "open_critic" && target.issueId !== issue.id) {
    return {kind:target.kind, label:`Открыть проверку ${target.key}`, run:() => openIssue(target.issueId)};
  }
  if (target?.kind === "open_critic" && issue.reviewProgress.reason !== "active") {
    return {kind:"review_inbox", label:"Открыть очередь ревью", run:async () => {
      detailOrigin = null;
      detail.close();
      state.spaceId = issue.spaceId;
      if (await navigate("reviews")) document.querySelector("#heading").focus();
    }};
  }
  if (issue.lifecycle?.phase === "awaiting_merge" && issue.branch) {
    return {kind:"diff", label:"Просмотреть изменения", run:() => openReview(issue.id)};
  }
  const child = issue.attentionChild;
  if (child) return {kind:"child", label:`Открыть подзадачу ${child.key}`, run:() => openIssue(child.id)};
  if (issue.status === "stale" && !issue.archivedAt) {
    return {kind:"revalidate", label:"Перейти к переактуализации", run:() => {
      const planning = detail.querySelector('[data-detail-section="planning"]');
      planning.open = true;
      (planning.querySelector("textarea,input") || planning.querySelector("summary")).focus();
    }};
  }
  return null;
}

/** Provide explicit status changes and isolated branch/review commands. */
function renderIssueControls(body, issue, primaryKind) {
  const bar = element("div", null, "status-bar");
  const select = element("select");
  select.setAttribute("aria-label", "Статус задачи");
  for (const [value, text] of Object.entries(statuses)) {
    const option = new Option(text, value);
    if (value === "in_progress" || issue.status === "stale" && value !== "stale") option.disabled = true;
    if (issue.repoId && value === "done" && issue.status !== "done") option.disabled = true;
    select.append(option);
  }
  select.value = issue.status;
  bar.append(labelledField("Статус задачи", select), button("Сохранить статус", async () => {
    try { await request(`/api/v1/issues/${issue.id}`, "PATCH", {status:select.value}); }
    catch (error) { await openIssue(issue.id); throw error; }
    await refresh(); if (detail.open && state.selected?.id === issue.id) await openIssue(issue.id);
  }), button("Редактировать", () => openEditor(null, issue)), button("＋ Подзадача", () => openEditor(issue)));
  if (issue.repoId && !issue.branch && !issue.archivedAt && !["done", "cancelled", "stale"].includes(issue.status)) bar.append(button("Создать рабочую ветку", async () => {
    await request(`/api/v1/issues/${issue.id}/prepare`, "POST", {});
    await refresh(); if (detail.open && state.selected?.id === issue.id) await openIssue(issue.id);
  }));
  if (issue.branch && primaryKind !== "diff") {
    bar.append(button("Просмотреть изменения", () => openReview(issue.id)));
  }
  bar.append(button("Обновить карточку", () => openIssue(issue.id)));
  if (issue.archivedAt) {
    bar.append(button("Восстановить из архива", async () => {
      await request(`/api/v1/issues/${issue.id}/restore`, "POST", {});
      await refresh(); await openIssue(issue.id);
    }));
    bar.append(button("Удалить…", () => {
      deletingIssue = issue;
      document.querySelector("#delete-description").textContent = `${issue.key} · ${issue.title}`;
      document.querySelector("#delete-consent").checked = false;
      document.querySelector("#delete-dialog").showModal();
    }, "danger-action"));
  } else if (["done", "cancelled", "stale"].includes(issue.status) && !issue.work?.activeSession) {
    bar.append(button("В архив", async () => {
      await request(`/api/v1/issues/${issue.id}/archive`, "POST", {});
      await refresh(); await openIssue(issue.id);
    }));
  }
  for (const control of bar.querySelectorAll("button")) {
    if (control.textContent === "＋ Подзадача" && ["done", "cancelled", "stale"].includes(issue.status)) control.disabled = true;
  }
  if (issue.kind === "review" || issue.archivedAt) {
    select.disabled = true;
    for (const control of bar.querySelectorAll("button")) {
      if (["Сохранить статус", "Редактировать", "＋ Подзадача"].includes(control.textContent)) control.disabled = true;
    }
  }
  body.append(bar);
  const metadata = element("div", null, "meta");
  metadata.append(element("span", `Приоритет: ${priorities[issue.priority]}`), element("span", `Исполнитель: ${issue.assignee || "Не назначен"}`));
  body.append(metadata);
  if (issue.branch) {
    body.append(element("p", `Ветка: ${issue.branch}`, "muted"), element("code", issue.worktree));
    body.append(element("p", "Агент работает и коммитит в этой директории. Слияние доступно после явного одобрения проверенной версии.", "muted"));
  }
}

/** Show parent/children/links and allow new relationships without guessing dependencies. */
function renderIssueRelations(body, issue, children, childrenTruncated) {
  const section = element("section", null, "section");
  section.append(element("h3", "Связи и подзадачи"));
  if (issue.parentId) section.append(button(`↳ Родительская задача`, () => openIssue(issue.parentId)));
  for (const child of children) section.append(button(`${child.key} · ${child.title}`, () => openIssue(child.id)));
  if (childrenTruncated) section.append(element("p", "Показана первая страница подзадач. Остальные доступны через фильтр parentId в HTTP API/MCP.", "muted"));
  if (issue.linksTruncated) section.append(element("p", "Показана часть связей; полные связи сохранены в базе сервиса.", "muted"));
  for (const link of issue.links || []) {
    const relatedId = link.relatedId || link.issueId || link.targetId;
    if (relatedId) section.append(button(`${({related:"Связана",blocks:"Блокирует",blocked_by:"Блокируется",duplicates:"Дубликат"})[link.kind] || link.kind} · ${issue.linkedIssues?.[relatedId]?.key || state.issues.find(item => item.id === relatedId)?.key || relatedId}`, () => openIssue(relatedId)));
  }
  const form = element("div", null, "inline-form");
  const target = element("select"); target.setAttribute("aria-label", "Связанная задача");
  populateSelect(target, state.issues.filter(item => item.id !== issue.id && !item.archivedAt), "Выберите задачу");
  const kind = element("select"); kind.setAttribute("aria-label", "Тип связи");
  kind.append(new Option("Связана", "related"), new Option("Блокирует", "blocks"));
  const crossSpace = element('input'); crossSpace.placeholder = 'Ключ из другого пространства: HP-12';
  crossSpace.className = 'cross-space-key'; crossSpace.maxLength = 64;
  crossSpace.setAttribute('aria-label', 'Ключ связанной задачи из любого пространства');
  form.append(labelledField("Связанная задача", target), labelledField("Ключ задачи из другого пространства", crossSpace), labelledField("Тип связи", kind), button("Связать", async () => {
    const chosen = crossSpace.value.trim() ? (await request(`/api/v1/issues/${encodeURIComponent(crossSpace.value.trim())}`)).id : target.value;
    if (!chosen) return;
    await request(`/api/v1/issues/${issue.id}/links`, "POST", {relatedId:chosen,kind:kind.value});
    if (detail.open && state.selected?.id === issue.id) await openIssue(issue.id); await refresh();
  }));
  if (!issue.archivedAt) section.append(form);
  if (!issue.archivedAt && issue.kind !== "review" && !["done", "cancelled", "stale"].includes(issue.status)) {
    const draft = element("details");
    draft.append(element("summary", "Создать несколько подзадач"));
    const titles = element("textarea");
    titles.rows = 4; titles.maxLength = 12050;
    titles.setAttribute("aria-label", "Названия подзадач, по одному в строке");
    draft.append(labelledField("Названия подзадач, по одному в строке", titles), button("Создать черновики", async () => {
      const lines = titles.value.split("\n").map(value => value.trim()).filter(Boolean);
      const children = lines.map(line => {
        const separator = line.indexOf('|');
        if (separator < 0) return {title:line};
        const role = line.slice(0, separator).trim();
        if (!roles[role]) throw new Error('Неизвестная роль: ' + role);
        return {title:line.slice(separator + 1).trim(), intendedRole:role};
      });
      const names = children.map(child => child.title);
      if (!names.length || names.length > 50 || names.some(name => name.length > 240)) throw new Error("Укажите от 1 до 50 названий, каждое до 240 символов");
      await request(`/api/v1/issues/${issue.id}/decompose`, "POST", {children});
      await refresh(); await openIssue(issue.id);
    }));
    draft.append(element("p", "Формат: developer | Реализовать; qa | Проверить; critic | Ревью (каждая задача на новой строке). Для RnD роль обязательна. Создаются только черновики; до работы подтвердите DoR.", "muted"));
    section.append(draft);
  }
  body.append(section);
}

/** Keep Observer evidence as externally owned URLs, accessible even when collection is stopped. */
function renderIssueEvidence(body, issue) {
  const section = element("section", null, "section");
  section.append(element("h3", "Инциденты и доказательства"));
  for (const ref of issue.externalRefs || []) { const link = evidenceLink(ref); if (link) section.append(link); }
  if (!issue.externalRefs?.length) section.append(element("p", "Ссылок пока нет", "muted"));
  body.append(section);
}

/** Display reported findings and chronological audit separately from verified Git changes. */
function renderIssueHistory(body, issue) {
  const section = element("section", null, "section");
  section.append(element("h3", "Обсуждение и результаты проверок"));
  for (const comment of issue.comments || []) {
    const entry = element("div", null, "history");
    entry.append(element("span", `${comment.actor} · ${comment.createdAt || comment.at || ""}`, "muted"), element("p", comment.text, "description"));
    section.append(entry);
  }
  const text = element("textarea"); text.dataset.detailDraft = "comment"; text.rows = 3; text.maxLength = 8192; text.setAttribute("aria-label", "Комментарий");
  text.placeholder = "Результаты анализа, выполненные проверки, блокеры…";
  section.append(labelledField("Комментарий", text), button("Добавить комментарий", async () => {
    const submitted = text.value;
    await request(`/api/v1/issues/${issue.id}/comments`, "POST", {text:submitted});
    if (text.value === submitted) text.value = ""; // Retire the acknowledged draft before refreshing this same task.
    if (detail.open && state.selected?.id === issue.id) await openIssue(issue.id);
  }));
  section.append(element("p", "Сообщения агента о проверках сохраняются как его отчёт, а не как подтверждение выполнения тестов сервисом.", "muted"));
  if (issue.commentsTruncated || issue.historyTruncated) section.append(element("p", `Показана последняя часть истории. Всего комментариев: ${issue.commentsCount}, изменений: ${issue.historyCount}. Полная история сохранена в базе сервиса.`, "muted"));
  const history = element("details"); history.append(element("summary", "История изменений"));
  for (const event of issue.history || []) history.append(element("p", `${event.createdAt || event.at || ""} · ${event.actor} · ${event.action || event.kind || "изменение"}`, "muted"));
  section.append(history); body.append(section);
}

/**
 * Read one revision-bound Git review; pending and denied responses never enable merge.
 * @param {string} identifier - ID of the current detail task; no approval or merge occurs during this read.
 * Current read failures expose retry; closed or superseded sessions reject late UI effects.
 */
async function openReview(identifier) {
  const generation = ++reviewGeneration, selectedGeneration = detailGeneration;
  const body = document.querySelector("#review-body");
  state.review = null;
  document.querySelector("#merge-consent").checked = false;
  document.querySelector("#merge").disabled = true;
  body.replaceChildren(element("p", "Загружаем текущие изменения…", "detail-loading"));
  if (!approval.open) approval.showModal();
  const ownsReview = () => generation === reviewGeneration && selectedGeneration === detailGeneration && detail.open && approval.open;
  try {
    const review = await request(`/api/v1/issues/${identifier}/review`);
    if (!ownsReview()) return;
    if (!["in_review","done"].includes(state.selected?.status)) {
      review.canMerge = false;
      review.reasons = [...(review.reasons || []), "Сначала передайте задачу на проверку"];
    }
    state.review = review;
    body.replaceChildren(element("p", `${review.baseBranch}: ${review.base}`, "muted"), element("p", `Ветка задачи: ${review.head}`, "muted"));
    if (review.reasons?.length) body.append(element("p", review.reasons.join(" · ")));
    const files = (review.changedFiles || []).map(item => typeof item === "string" ? item : JSON.stringify(item));
    body.append(element("pre", files.join("\n") || "Нет изменённых файлов"));
    body.append(element("pre", review.diff || "Нет доступного diff"));
    if (review.diffTruncated) body.append(element("p", "Diff сокращён. Просмотрите полный diff в рабочей директории перед одобрением.", "muted"));
    document.querySelector("#merge-consent").checked = false;
    document.querySelector("#merge").disabled = true;
    approval.querySelectorAll(".error").forEach(node => node.remove());
  } catch (error) {
    if (!ownsReview()) return;
    body.replaceChildren(element("p", error.message || "Изменения не загружены.", "error"), button("Повторить загрузку изменений", () => openReview(identifier)));
  }
}

/** Register allowlisted local repository paths only through an explicit user form. */
function renderRepositories() {
  const form = element("form", null, "repo-form");
  for (const [name, caption, placeholder] of [["path","Локальная директория","/путь/к/репозиторию"],["name","Название","Мой сервис"],["baseBranch","Основная ветка","main"]]) {
    const label = element("label", caption, "ag-field"), input = element("input");
    input.name = name; input.placeholder = placeholder; input.required = name === "path"; input.maxLength = 2048;
    label.append(input); form.append(label);
  }
  const submit = element("button", "Добавить репозиторий", "primary"); submit.type = "submit"; form.append(submit);
  form.addEventListener("submit", async event => {
    event.preventDefault(); submit.disabled = true;
    try {
      await request("/api/v1/repositories", "POST", {path:form.elements.path.value,name:form.elements.name.value || null,baseBranch:form.elements.baseBranch.value || null});
      await refresh();
    } catch (error) { report(error); } finally { submit.disabled = false; }
  });
  content.append(form, element("p", "Рабочие ветки создаются из коммитов основной ветки. Незакоммиченные изменения не копируются. Слияние проверяет чистоту директории и точные SHA; push не выполняется.", "muted"));
  for (const repo of state.repos) {
    const row = element("div", null, "repo"); row.append(element("h3", repo.name), element("code", repo.path), element("p", `Основная ветка: ${repo.baseBranch}`, "muted")); content.append(row);
  }
}

for (const control of document.querySelectorAll("[data-close]")) control.addEventListener("click", () => document.getElementById(control.dataset.close).close());
/** Navigate through the same refresh boundary for menu and desktop commands. */
async function navigate(view) {
  const generation = refreshGeneration + 1;
  state.view = view;
  await refresh();
  return generation === refreshGeneration;
}

const disposeNavigation = bindTaskNavigation(document.querySelector("nav"), view => navigate(view).catch(report));
window.addEventListener("pagehide", disposeNavigation, {once:true});
document.querySelector("#create").addEventListener("click", () => openEditor());
document.querySelector("#refresh").addEventListener("click", () => refresh().catch(report));
document.querySelector("#more").addEventListener("click", () => refresh(true).catch(report));
document.querySelector("#issue-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget, submit = form.querySelector("[type=submit]");
  const session = editorGeneration;
  const ownsEditor = () => session === editorGeneration && editor.open;
  submit.disabled = true;
  try {
    const fields = readIssueEditorFields(form,{issue:editingIssue,readDor,readDod});
    const isPlan = !editingIssue && fields.kind === 'rnd';
    if (isPlan) { fields.markdown = fields.description; delete fields.description; delete fields.kind; }
    const path = editingIssue ? `/api/v1/issues/${editingIssue.id}` : isPlan ? '/api/v1/plans' : '/api/v1/issues';
    const payload = editingIssue ? changedIssueFields(fields,editingIssue,editorBaseline) : fields;
    const created = editingIssue && !Object.keys(payload).length ? editingIssue : await request(path, editingIssue ? "PATCH" : "POST", payload);
    if (!ownsEditor()) return;
    document.querySelector("#notice").textContent = ""; // The acknowledged retry resolves this form error.
    editor.close();
    const closedSession = editorGeneration;
    await refresh();
    if (editorGeneration === closedSession && !editor.open) await openIssue(created.id);
  } catch (error) {
    if (ownsEditor()) report(error);
    else document.querySelector("#notice").textContent = error.message || "Предыдущая операция не выполнена";
  } finally {
    if (session === editorGeneration) submit.disabled = false;
  }
});
document.querySelector("#merge-consent").addEventListener("change", event => { document.querySelector("#merge").disabled = !event.target.checked || !state.review?.canMerge; });
document.querySelector("#merge").addEventListener("click", async event => {
  if (!state.review?.canMerge || !document.querySelector("#merge-consent").checked) return;
  event.target.disabled = true;
  try {
    const {issueId,head,base} = state.review;
    await request(`/api/v1/issues/${issueId}/approve-merge`, "POST", {expectedHead:head,expectedBase:base});
    approval.close(); await refresh(); if (detail.open && state.selected?.id === issueId) await openIssue(issueId);
  } catch (error) { report(error); }
});

for (const [value, title] of Object.entries(statuses)) document.querySelector("#status-filter").append(new Option(title, value));
document.querySelector("#issue-query").addEventListener("input", renderLoadedView);
document.querySelector("#status-filter").addEventListener("change", renderLoadedView);
document.querySelector("#include-history").addEventListener("change", renderLoadedView);
document.querySelector('#show-empty-columns').addEventListener('change', renderLoadedView);
document.querySelector('#reset-filters').addEventListener('click', () => {
  document.querySelector('#issue-query').value = '';
  document.querySelector('#status-filter').value = '';
  renderLoadedView();
  document.querySelector('#issue-query').focus();
});
document.querySelector('#retry-view').addEventListener('click', async event => {
  const retry = event.currentTarget;
  await refresh(failedAppend);
  // Hiding the consumed recovery action must not strand keyboard focus on the document body.
  if (retry.hidden && [document.body, retry].includes(document.activeElement)) document.querySelector('#refresh').focus();
});
document.querySelector('#active-work-only').addEventListener('change', () => refresh().catch(report));
document.querySelector("#space-manage").addEventListener("click", () => {
  const form = document.querySelector('#space-form'); form.reset(); delete form.dataset.spaceId;
  form.querySelector('[type=submit]').textContent = 'Создать';
  document.querySelector('#spaces-dialog').showModal();
});
document.querySelector("#space-form").addEventListener("submit", async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const submit = form.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    const identifier = form.dataset.spaceId;
    const path = identifier ? `/api/v1/spaces/${encodeURIComponent(identifier)}` : '/api/v1/spaces';
    const space = await request(path, identifier ? 'PATCH' : 'POST', {name:form.elements.name.value.trim(), ...(form.elements.prefix.value.trim() ? {prefix:form.elements.prefix.value.trim()} : {})});
    state.spaceId = space.id;
    form.reset(); delete form.dataset.spaceId;
    form.querySelector("[type=submit]").textContent = "Создать";
    document.querySelector("#spaces-dialog").close();
    await refresh();
  } catch (error) { report(error); } finally { submit.disabled = false; }
});
document.querySelector("#delete-form").addEventListener("submit", async event => {
  event.preventDefault();
  if (!deletingIssue || !document.querySelector("#delete-consent").checked) return;
  const submit = event.currentTarget.querySelector("[type=submit]");
  submit.disabled = true;
  try {
    await request(`/api/v1/issues/${deletingIssue.id}`, "DELETE");
    deletingIssue = null;
    document.querySelector("#delete-dialog").close(); detail.close();
    await refresh();
    document.querySelector("#heading").focus();
  } catch (error) { report(error); } finally { submit.disabled = false; }
});
document.querySelector("#delete-dialog").addEventListener("close", () => { deletingIssue = null; });
document.querySelector("#issue-form [name=spaceId]").addEventListener("change", event => {
  populateSelect(document.querySelector("#issue-form").elements.parentId, state.issues.filter(issue => issue.spaceId === event.target.value && issue.id !== editingIssue?.id && !issue.archivedAt && !["done","cancelled","stale"].includes(issue.status)), "Без родительской задачи");
});
bindSidebar();
const disposeWorkArea = bindWorkAreaFit({content, chrome:[...document.querySelectorAll('.toolbar,.task-filter-bar,#coverage,#view-feedback,.app-window>header')]});
window.addEventListener('pagehide', disposeWorkArea, {once:true});
let windowShell;
connect().then(async () => {
  await refresh();
  bindDesktopAutomation({getView:() => state.view, getSpace:() => state.spaceId,
    isLoading:() => state.loading, navigate});
  windowShell = bindWindowShell({identity:'ag-tasks',header:'.content-header',titleSource:'#heading',detailTitleSource:'#detail-title',detailSelector:'#detail[open]',navigation:'#sidebar [aria-current="page"]',actions:[{selector:'#create',id:'primary'},{selector:'#refresh',id:'refresh'}],panels:[{selector:'#sidebar',edge:'right',variable:'--nav-width',layout:':root'}]});
  const key = new URLSearchParams(window.location.search).get('issue');
  if (key) await openIssue(key);
}).catch(report);
/** Passive refresh must not discard a session reader, an appended page, or a scrolled overview. */
function canRefreshOverview() {
  return document.visibilityState !== "hidden" && !content.contains(document.activeElement)
    && !document.querySelector("dialog[open]") && !state.loading
    && !["sessions", "repositories", "analytics"].includes(state.view)
    && ![...content.querySelectorAll(".board,.column")].some(node => node.scrollTop > 0 || node.scrollLeft > 0)
    && state.issues.length <= 200 && document.querySelector("#main").scrollTop === 0
    && (document.scrollingElement?.scrollTop || window.scrollY || 0) === 0;
}
const refreshTimer = setInterval(() => { if (canRefreshOverview()) refresh().catch(report); }, 10000);

window.addEventListener("pagehide", () => { clearInterval(refreshTimer); windowShell?.dispose(); graphDisposer?.(); refreshGeneration++; detailGeneration++; reviewGeneration++; editorGeneration++; }, {once:true});
