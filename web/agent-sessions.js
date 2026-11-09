import {element, button, emptyState, statusLabel, labelledField} from './dom.js';
import {renderCodexSessions} from './codex-sessions.js';
import {renderAgentQueue} from './agent-queue.js';
import {orderSessions, placeSessionRows} from './session-order.js';

const connectionLabels={connected:'MCP подключён',disconnected:'MCP отключён',stale:'Связь MCP не подтверждена',unknown:'Состояние связи MCP неизвестно'};

const states = {unknown:'Неизвестно',running:'Работает',requires_action:'Ждёт решения',idle:'Основной поток завершён'};

/**
 * Render activity-ordered session sources and retain the selected task-link draft during refresh.
 * @param {Element} parent - Host container replaced with this view.
 * @param {object} options - Read/command adapters, retained selection, navigation and error callbacks.
 * @returns {Function} Disposer cancelling polling and invalidating outstanding reads.
 */
export function renderAgentSessions(parent, {request, command, selection, openIssue, onError}) {
  let alive = true, generation = 0, listGeneration = 0, view = selection.view || 'sessions', disposeQueue = null;
  let sortOrder=selection.sort==='oldest' ? 'oldest' : 'newest';
  const root = element('section', null, 'agent-sessions');
  const list = element('div', null, 'agent-session-list');
  const detail = element('section', null, 'agent-session-detail');
  detail.setAttribute('aria-label','События выбранной сессии');
  const codex=renderCodexSessions(root,{request,getSort:()=>sortOrder});
  const sessionControls=new Map();
  const sessionMetadata=new Map();
  const orderControls=element('div',null,'session-order-controls');
  const orderSelect=element('select');orderSelect.dataset.sessionSort='true';
  for(const [value,title] of [['newest','От свежих к старым'],['oldest','От старых к свежим']]) {
    const option=element('option',title);option.value=value;orderSelect.append(option);
  }
  orderSelect.value=sortOrder;
  orderControls.append(labelledField('Сортировка по последней активности',orderSelect),
    element('p','Неактивные записи удаляются через 3 дня. Активные подключения сохраняются.','muted'));
  orderSelect.addEventListener('change',()=>{
    sortOrder=orderSelect.value;selection.sort=sortOrder;
    sessionControls.clear();sessionMetadata.clear();
    list.replaceChildren(element('h2','MCP и наблюдаемые сессии'),historyLabel);emptySessions=null;
    codex.reorder();void loadList(0,true);
  });
  let emptySessions=null, listError=null, activeOnly=selection.history!==true;
  const historyLabel=element('label','Показать историю наблюдаемых сессий','muted');
  const history=element('input');history.type='checkbox';history.checked=!activeOnly;historyLabel.prepend(history);
  history.addEventListener('change',()=>{
    activeOnly=!history.checked;selection.history=history.checked;sessionControls.clear();sessionMetadata.clear();
    list.replaceChildren(element('h2','MCP и наблюдаемые сессии'),historyLabel);emptySessions=null;void loadList();
  });
  const sections = element('div',null,'session-sections');
  const facts = element('section',null,'service-facts'); facts.hidden=true;
  const queue = element('section',null,'agent-queue-container');queue.hidden=true;
  sections.setAttribute('role','group');
  sections.setAttribute('aria-label','Представление активности');
  const sectionButtons = new Map();
  for (const [key, label, action] of [['sessions','Сессии агентов',showSessions],['queue','Очереди заданий',showQueue],['facts','События сервиса',showFacts]]) {
    const control = button(label,action);
    control.classList.replace("ag-button--secondary", "ag-button--quiet");
    control.dataset.sessionView=key;
    markSection(control,view===key);
    sectionButtons.set(key,control);sections.append(control);
  }
  sections.append(element('p','Список обновляется каждые 10 секунд.','muted'));
  root.replaceChildren(sections,orderControls,codex.element,list,detail,facts,queue); parent.replaceChildren(root);

  /** The pressed state controls styling and is also available to assistive technology. */
  function markSection(control,selected) {
    control.setAttribute('aria-pressed',String(selected));
  }

  /** Invalidate reads when the visible section changes and expose its selected state. */
  function setView(next) {
    view=next;selection.view=next;++generation;++listGeneration;
    for (const [key, control] of sectionButtons) markSection(control,key===next);
    codex.element.hidden=next!=='sessions';list.hidden=next!=='sessions';detail.hidden=next!=='sessions';facts.hidden=next!=='facts';queue.hidden=next!=='queue';
    orderControls.hidden=next!=='sessions';
    if(next!=='facts') facts.setAttribute('aria-busy','false');
  }

  /** Keep queue intents separate from captured source events while retaining one service command API. */
  function showQueue() {
    setView('queue');
    disposeQueue?.();
    disposeQueue=renderAgentQueue(queue,{request,command,selection,openIssue,onError});
  }

  /** Keep the universal command journal available even when no ACP/MCP source has registered. */
  async function showFacts(after=0) {
    if(typeof after!=='number') after=0;
    setView('facts');const current=generation;
    disposeQueue?.();disposeQueue=null;queue.hidden=true;
    if(!after) facts.replaceChildren(element('h2','События сервиса'));
    facts.querySelector('[data-facts-feedback]')?.remove();
    const feedback=element('div');feedback.dataset.factsFeedback='true';
    const loading=element('p','Загрузка событий…','muted');loading.setAttribute('role','status');
    feedback.append(loading);facts.append(feedback);facts.setAttribute('aria-busy','true');
    try {
      const data=await request(`/api/v1/events?limit=20&after=${after}`);
      if(!alive || current!==generation) return;
      feedback.remove();
      facts.querySelector('[data-facts-more]')?.remove();
      if(!after && !data.items.length) facts.append(emptyState('Событий пока нет','Общие команды UI и агентов появятся здесь после обработки сервисом.'));
      const titles={'issue.create':'Создание задачи','issue.update':'Изменение задачи','issue.comment':'Комментарий','issue.link':'Связь задач',
        'session.register':'Регистрация сессии','session.link':'Связь сессии с задачей','session.rebuild':'Обновление представления','events.append':'Приём событий'};
      for(const fact of data.items) {
        const row=element('article',null,'session-event');
        row.append(element('h3',titles[fact.operation] || fact.operation),statusLabel(fact.type==='command.committed'?'Применено':'Отклонено'),element('p',fact.createdAt,'muted'));
        const metadata=element('details');metadata.append(element('summary','Данные команды'),element('pre',JSON.stringify(fact,null,2)));row.append(metadata);facts.append(row);
      }
      if(data.hasMore){const more=button('Следующие события сервиса',()=>showFacts(data.nextCursor));more.dataset.factsMore='true';facts.append(more);}
    } catch(error) {
      if(!alive || current!==generation) return;
      feedback.replaceChildren(emptyState('События не загружены','Повторите загрузку. Уже показанные события сохранены.'),button('Повторить загрузку',()=>showFacts(after)));
      onError(error);
    } finally {
      if(alive && current===generation) facts.setAttribute('aria-busy','false');
    }
  }

  /** Return to source observations without creating or resuming an agent session. */
  async function showSessions() {
    setView('sessions');
    disposeQueue?.();disposeQueue=null;queue.hidden=true;
    list.replaceChildren(element('h2','MCP и наблюдаемые сессии'),historyLabel);sessionControls.clear();sessionMetadata.clear();await Promise.all([loadList(),codex.refresh()]);
  }

  /** Fetch a bounded page; disposed or superseded selections cannot replace the current session. */
  async function select(sessionId) {
    const current = ++generation;
    selection.id = sessionId;
    for (const control of list.querySelectorAll('[data-session-id]')) control.setAttribute('aria-pressed',String(control.dataset.sessionId===sessionId));
    detail.replaceChildren(element('p','Загрузка сессии…','muted'));
    try {
      const session = await request(`/api/v1/sessions/${encodeURIComponent(sessionId)}`);
      if (!alive || current !== generation) return;
      detail.replaceChildren(element('h2',session.title));
      const protocol=session.connection ? 'MCP' : `${session.protocol.toUpperCase()} ${session.protocolVersion}`;
      detail.append(statusLabel('Наблюдение'), element('p',`${session.agent} · ${protocol}`),
        element('p',`${states[session.projection.foregroundState] || session.projection.foregroundState} · Фоновая активность: не подтверждена`,'muted'));
      if(session.connection) {
        const presence=statusLabel(connectionLabels[session.connection.state] || connectionLabels.unknown);presence.dataset.connectionStatus='true';
        detail.append(presence,element('p','Подключение MCP не определяет, выполняет ли агент запрос. Переписка и параметры инструментов не сохраняются.','muted'));
      }
      if (session.issueId) detail.append(button(`Открыть задачу ${session.issueKey || ''}`.trim(),()=>openIssue(session.issueId)));
      const form = element('form',null,'session-link-form');
      const label = element('label','Связать с задачей · номер или ID','ag-field');
      const input = element('input'); input.value=session.issueKey || session.issueId || ''; input.maxLength=128; label.append(input);
      const submit = element('button','Сохранить связь','ag-button ag-button--secondary'); submit.type='submit';
      form.append(label,submit); detail.append(form);
      form.addEventListener('submit', async event => {
        event.preventDefault();
        if(submit.disabled) return;
        submit.disabled=true;
        // A completed command owns only the form/selection that submitted it, never a newer view.
        const ownsForm=()=>alive && view==='sessions' && current===generation && selection.id===sessionId && detail.contains(form);
        try {
          await command('session.link',{sessionId,issueId:input.value.trim() || null,expectedRevision:session.revision});
          if (ownsForm()) {
            const restoreFocus=form.contains(document.activeElement);
            const reload=select(sessionId), reloadGeneration=generation;
            await reload;
            if(alive && view==='sessions' && generation===reloadGeneration && selection.id===sessionId && restoreFocus) detail.querySelector('input')?.focus();
          }
        } catch(error) { if(ownsForm()) onError(error); } finally {submit.disabled=false;}
      });
      const continuity = element('p','Полнота внешнего потока и остановка процессов не подтверждены. События не запускают задачи.','muted');
      detail.append(continuity);
      const events = element('div',null,'session-events'); detail.append(events);
      await loadEvents(sessionId,events,0,current);
    } catch(error) { if(alive && current===generation) {detail.replaceChildren(emptyState('Сессия не загружена','Обновите данные и повторите выбор.'));onError(error);} }
  }

  /** Append original event observations, including unknown payloads, with explicit cursor pagination. */
  async function loadEvents(sessionId, target, after, current) {
    if (target.getAttribute('aria-busy') === 'true') return;
    target.querySelector('[data-events-error]')?.remove();
    target.setAttribute('aria-busy','true');
    try {
      const data = await request(`/api/v1/sessions/${encodeURIComponent(sessionId)}/events?limit=20&after=${after}`);
      if (!alive || current !== generation) return;
      target.querySelector('[data-events-more]')?.remove();
      if (!after && !data.items.length) target.append(emptyState('Событий пока нет','Подключённый агент может передать данные через общий API сервиса.'));
      for (const event of data.items) target.append(eventRow(event));
      if (data.hasMore) {
        const more = button('Следующие события',()=>loadEvents(sessionId,target,data.nextCursor,current));
        more.dataset.eventsMore='true'; target.append(more);
      }
    } catch(error) {
      if (!alive || current !== generation) return;
      // A failed event read must not replace the already-loaded session or its unsaved link form.
      const feedback = element('div'); feedback.dataset.eventsError='true';
      feedback.append(emptyState('События не загружены','Связь с задачей и введённый текст сохранены.'),
        button('Повторить загрузку событий',()=>loadEvents(sessionId,target,after,current)));
      target.append(feedback);
    } finally {
      if (alive && current === generation) target.setAttribute('aria-busy','false');
    }
  }

  /** Show source metadata and untrusted JSON literally; identical text events remain separate observations. */
  function eventRow(event) {
    const row = element('article',null,'session-event');
    const message = event.payload;
    const update = message?.params?.update;
    const title = message?.method || (Object.hasOwn(message,'error') ? 'Ошибка запроса' : 'Ответ запроса');
    row.append(element('h3',title),element('p',`#${event.sequence} · ${event.direction} · ${event.origin} · ${event.receivedAt}`,'muted'));
    if (update?.content?.type==='text') row.append(element('p',update.content.text));
    if (event.redactions.length) row.append(element('p','Поля учётных данных скрыты в архиве.','muted'));
    const raw = element('details'); raw.append(element('summary','Исходное событие'),element('pre',JSON.stringify(message,null,2)));
    row.append(raw); return row;
  }

  /**
   * Read one activity-ordered page and reorder existing controls without replacing detail inputs.
   * @param {string|number} after - Zero or the service's unchanged continuation token.
   * @param {boolean} refresh - Preserve selection/details and reconcile a complete first page.
   * @returns {Promise<void>} Resolves after the current page and any initial selection load.
   */
  async function loadList(after=0, refresh=false) {
    const current=++listGeneration, selectedId=selection.id;
    let data;
    list.setAttribute("aria-busy", "true");
    try {
      data = await request(`/api/v1/sessions?limit=20&after=${encodeURIComponent(after)}&activeOnly=${activeOnly}&sort=${sortOrder}`);
    } catch(error) {
      if(!alive || view!=='sessions' || current!==listGeneration) return;
      listError?.remove();listError=element('div');
      list.dataset.loadFailed = "true";
      listError.append(emptyState('Сессии не загружены','Повторите загрузку списка.'),button('Повторить загрузку сессий',showSessions));list.append(listError);
      for(const control of sessionControls.values()) control.textContent=`${control.dataset.sessionTitle} · ${connectionLabels.unknown}`;
      onError(error);
      return;
    } finally {
      if (current === listGeneration) list.removeAttribute("aria-busy");
    }
    if (!alive || view!=='sessions' || current!==listGeneration) return;
    listError?.remove();listError=null;
    delete list.dataset.loadFailed;
    list.querySelector('[data-sessions-more]')?.remove();
    if(data.items.length) {emptySessions?.remove();emptySessions=null;}
    if (!after && !data.items.length && !emptySessions) {
      emptySessions=emptyState('Нет активных MCP-подключений','Новые подключения появятся автоматически. Для подключений прежней версии переподключите Ag Tasks в клиенте.');list.append(emptySessions);
    }
    for (const session of data.items) {
      sessionMetadata.set(session.id,session);
      const text=session.connection ? `${session.title} · ${connectionLabels[session.connection.state] || connectionLabels.unknown}` : `${session.title} · ${session.agent} · ${session.eventCount} событий`;
      let control=sessionControls.get(session.id);
      if(!control) {control=button(text,()=>select(session.id),'session-select');sessionControls.set(session.id,control);}
      else control.textContent=text;
      if(refresh && selection.id===session.id && session.connection) {
        const presence=detail.querySelector('[data-connection-status]');
        if(presence) presence.textContent=connectionLabels[session.connection.state] || connectionLabels.unknown;
      }
      control.dataset.sessionId=session.id;control.dataset.sessionTitle=session.title;
      control.setAttribute('aria-pressed',String(selection.id===session.id)); if(!control.isConnected) list.append(control);
    }
    if(refresh && !data.hasMore) {
      const currentIds=new Set(data.items.map(session=>session.id));
      for(const [id,control] of sessionControls) if(!currentIds.has(id)) {
        if(document.activeElement===control) control.textContent=`${control.dataset.sessionTitle} · ${connectionLabels.unknown}`;
        else {control.remove();sessionControls.delete(id);sessionMetadata.delete(id);}
      }
    }
    // The dedicated row container keeps headings/filters in place while activity moves existing buttons.
    const rowNodes=[];
    for(const session of orderSessions([...sessionMetadata.values()],sortOrder)) {
      const control=sessionControls.get(session.id);
      if(control) rowNodes.push(control);
    }
    let target=list.querySelector('.observed-session-rows');
    if(!target) {target=element('div',null,'observed-session-rows');list.append(target);}
    placeSessionRows(target,rowNodes);
    if(data.hasMore) {const more=button('Следующие сессии',()=>loadList(data.nextCursor));more.dataset.sessionsMore='true';list.append(more);}
    // A page requested before an explicit selection must not replace that selection when it arrives.
    if(!refresh && !after && selection.id===selectedId) {
      if(selection.id) await select(selection.id);
      else if(data.items.length) await select(data.items[0].id);
    }
  }
  list.append(element('h2','MCP и наблюдаемые сессии'),historyLabel);
  const ready = view==='facts' ? showFacts() : view==='queue' ? Promise.resolve(showQueue()) : Promise.all([loadList(),codex.refresh()]);
  ready.catch(error=>{if(alive){list.append(emptyState('Источники недоступны','Обновите данные для повторной загрузки.'));onError(error);}});
  /** Update only the connection badge; a background read cannot replace an unsaved task-link form. */
  async function refreshSelectedConnection() {
    const id=selection.id, current=generation;
    const badge=detail.querySelector('[data-connection-status]');
    if(!id || !badge) return;
    let label=connectionLabels.unknown;
    try {
      const session=await request(`/api/v1/sessions/${encodeURIComponent(id)}`);
      label=connectionLabels[session.connection?.state] || label;
    } catch {}
    if(alive && view==='sessions' && generation===current && selection.id===id && detail.contains(badge)) badge.textContent=label;
  }
  let refreshPending=false;
  /** Sample only the visible list, serialize slow reads, and preserve unsaved detail controls. */
  async function refreshVisible() {
    if(!alive || refreshPending || view!=='sessions' || document.visibilityState==='hidden') return;
    refreshPending=true;
    try {await Promise.all([loadList(0,true),codex.refresh(),refreshSelectedConnection()]);}
    finally {refreshPending=false;}
  }
  const poll=setInterval(refreshVisible,10000);
  poll.unref?.();
  codex.element.hidden=view!=='sessions';
  orderControls.hidden=view!=='sessions';
  return () => {alive=false;generation++;listGeneration++;clearInterval(poll);codex.dispose();disposeQueue?.();};
}
