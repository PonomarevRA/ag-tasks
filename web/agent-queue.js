import {element, button, emptyState, statusLabel} from './dom.js';

const itemStates={queued:'Ожидает',active:'В работе',cancel_requested:'Отмена запрошена',completed:'Завершена',cancelled:'Отменена',unknown:'Исход неизвестен'};
const attemptStates={claimed:'Зарезервирована',dispatched:'Отправка разрешена',running:'Выполняется',finished:'Ответ завершён',unknown:'Нужно восстановление',released:'Исполнитель освобождён'};

/** Compose host controls over the common domain API; this view never launches an agent. */
export function renderAgentQueue(parent,{request,command,selection,openIssue,onError}) {
  let alive=true,generation=0,listGeneration=0,selected=selection.laneId || null;
  const root=element('section',null,'agent-queue');
  const list=element('div',null,'queue-list'),detail=element('section',null,'queue-detail');
  detail.setAttribute('aria-label','Выбранная очередь');
  detail.tabIndex=-1;
  const notice=element('p','','queue-feedback');notice.setAttribute('role','status');
  root.append(list,detail,notice);parent.replaceChildren(root);

  /** Keep error recovery local and preserve form values after a rejected or ambiguous command. */
  function fail(error) {if(alive){notice.textContent=error.message || 'Не удалось обновить очередь. Обновите данные и повторите действие.';onError(error);}}

  /** Label each editable value persistently; long prompts use a textarea and bounded input. */
  function field(form,title,name,{value='',maximum=128,multiline=false,required=true}={}) {
    const label=element('label',title,'ag-field');
    const control=element(multiline?'textarea':'input');control.name=name;control.value=value;
    control.maxLength=maximum;control.required=required;
    if(multiline)control.rows=4;
    label.append(control);form.append(label);return control;
  }

  /** Serialize a form submission without hiding a failed intent or stealing focus from another view. */
  function submit(form,label,action) {
    const control=element('button',label,'ag-button ag-button--secondary');control.type='submit';form.append(control);
    form.addEventListener('submit',async event=>{
      event.preventDefault();if(control.disabled)return;control.disabled=true;
      try{await action();}catch(error){fail(error);}finally{control.disabled=false;}
    });
  }

  /** Execute one revision-checked action, then reload the selected lane from its first pending page. */
  async function mutate(operation,payload,focusLabel) {
    const current=generation,laneId=selected;
    await command(operation,payload);
    if(!alive || current!==generation || selected!==laneId)return;
    notice.textContent='Изменения сохранены.';
    const refreshed=await select(laneId);
    if(!alive || refreshed!==generation || selected!==laneId)return;
    const candidate=[...detail.querySelectorAll('button')].find(node=>node.textContent===focusLabel);
    (candidate || detail).focus();
  }

  /** Refresh only bounded lane pages; selection is retained by the caller across application refreshes. */
  async function loadList(after=0) {
    const current=++listGeneration,viewGeneration=generation;
    const data=await request(`/api/v1/queues?limit=20&after=${after}`);
    if(!alive || current!==listGeneration)return;
    list.querySelector('[data-queue-more]')?.remove();
    if(!after)list.replaceChildren(element('h2','Очереди агентов'),createForm());
    if(!after && !data.items.length)list.append(emptyState('Очередей пока нет','Создайте очередь и добавьте задания. Внешний исполнитель подключается отдельно.'));
    for(const lane of data.items) {
      const control=button(`${lane.title} · ${lane.queuedCount} ожидают`,()=>select(lane.laneId),'session-select');
      control.dataset.laneId=lane.laneId;control.setAttribute('aria-pressed',String(selected===lane.laneId));list.append(control);
    }
    if(data.hasMore){const more=button('Следующие очереди',()=>loadList(data.nextCursor));more.dataset.queueMore='true';list.append(more);}
    if(!after && viewGeneration===generation) {
      if(selected)await select(selected);
      else if(data.items.length)await select(data.items[0].laneId);
    }
  }

  /** Keep provider identity explicit; profile metadata never becomes an executable command. */
  function createForm() {
    const group=element('details');group.append(element('summary','Создать очередь'));
    const form=element('form',null,'queue-form');
    const lane=field(form,'Идентификатор очереди','laneId');lane.pattern='[A-Za-z0-9][A-Za-z0-9._:-]{0,127}';
    const title=field(form,'Название','title',{maximum:240});
    const profile=field(form,'Профиль исполнителя','profileId');
    const version=field(form,'Версия профиля','profileVersion');
    form.append(element('p','Латинские буквы, цифры, . _ : - в идентификаторе. Профиль должен совпасть с подключённым исполнителем ACP v1.','muted'));
    submit(form,'Создать',async()=>{
      const current=generation;
      const result=await command('queue.create',{laneId:lane.value.trim(),title:title.value.trim(),profile:{id:profile.value.trim(),version:version.value.trim(),protocol:'acp',protocolVersion:1}});
      if(!alive || current!==generation)return;selected=result.laneId;selection.laneId=selected;await loadList();
    });
    group.append(form);return group;
  }

  /** Read current runtime authority and pending order together; older replies cannot replace a newer lane. */
  async function select(laneId) {
    const current=++generation;selected=laneId;selection.laneId=laneId;
    for(const control of list.querySelectorAll('[data-lane-id]'))control.setAttribute('aria-pressed',String(control.dataset.laneId===laneId));
    detail.setAttribute('aria-busy','true');
    try{
      const lane=await request(`/api/v1/queues/${encodeURIComponent(laneId)}`);
      if(!alive || current!==generation)return;
      detail.replaceChildren(element('h2',lane.title));
      detail.append(statusLabel(lane.state==='quarantined'?'Нужно восстановление':lane.state==='busy'?'Исполнитель занят':'Свободна'),
        element('p',`${lane.profile.id} · ${lane.profile.version} · ${lane.queuedCount} ожидают`,'muted'));
      const actions=element('div',null,'queue-actions');
      actions.append(button(lane.draining?'Продолжить приём исполнителем':'Приостановить новые запуски',()=>mutate('queue.configure',{laneId,expectedRevision:lane.revision,draining:!lane.draining})));
      actions.append(button('Обновить очередь',()=>select(laneId)));
      detail.append(actions,element('p',lane.draining?'Новые запуски приостановлены. Текущая попытка продолжает работать.':'Задания берёт внешний исполнитель. «В начало» меняет порядок ожидания и не прерывает текущую работу.','muted'));
      detail.append(enqueueForm(laneId),profileForm(lane));
      if(lane.activeAttempt)await activeAttempt(lane.activeAttempt,current);
      if(!alive || current!==generation)return;
      const pending=element('section',null,'queue-pending');pending.append(element('h3','Ожидают исполнения'));detail.append(pending);
      await loadPending(laneId,pending,0,current);
      if(!alive || current!==generation)return;
      const history=element('details');history.append(element('summary','История заданий'));
      const rows=element('div');history.append(rows);detail.append(history);
      let loaded=false;
      history.addEventListener('toggle',()=>{if(history.open && !loaded){loaded=true;loadHistory(laneId,rows,0,current).catch(error=>{loaded=false;fail(error);});}});
      return current;
    }catch(error){if(alive && current===generation)fail(error);}
    finally{if(alive && current===generation)detail.removeAttribute('aria-busy');}
  }

  /** Enqueue durable text with an optional issue link; a failed request preserves its editable content. */
  function enqueueForm(laneId) {
    const form=element('form',null,'queue-form');form.append(element('h3','Добавить задание'));
    const text=field(form,'Задание агенту','text',{multiline:true,maximum:8192});
    const issue=field(form,'Связанная задача · номер или ID, необязательно','issueId',{required:false});
    submit(form,'В очередь',async()=>{
      const current=generation;
      const payload={laneId,text:text.value};if(issue.value.trim())payload.issueId=issue.value.trim();
      await command('queue.enqueue',payload);
      if(!alive || current!==generation)return;
      const refreshed=await select(laneId);
      if(alive && refreshed===generation && selected===laneId)detail.querySelector('textarea')?.focus();
    });return form;
  }

  /** A profile update affects future attempts; the active attempt retains the profile shown below. */
  function profileForm(lane) {
    const group=element('details');group.append(element('summary','Профиль следующих запусков'));
    const form=element('form',null,'queue-form');
    const id=field(form,'Профиль исполнителя','profileId',{value:lane.profile.id});
    const version=field(form,'Версия профиля','profileVersion',{value:lane.profile.version});
    form.append(element('p','Изменение применяется к следующим заданиям. Профиль активной попытки сохраняется.','muted'));
    submit(form,'Сохранить профиль',()=>mutate('queue.configure',{laneId:lane.laneId,expectedRevision:lane.revision,profile:{...lane.profile,id:id.value.trim(),version:version.value.trim()}}));
    group.append(form);return group;
  }

  /** Show cancellation as desired state; only explicit runtime evidence can release uncertain ownership. */
  async function activeAttempt(attempt,current) {
    const item=await request(`/api/v1/queue-items/${encodeURIComponent(attempt.itemId)}`);
    if(!alive || current!==generation)return;
    const section=element('section',null,'queue-active');
    section.append(element('h3','Текущая попытка'),statusLabel(attemptStates[attempt.state] || attempt.state),element('p',item.text),
      element('p',`Профиль: ${attempt.profile.id} · ${attempt.profile.version}. ${attempt.outcome || ''}`,'muted'));
    if(!['released','cancelled','completed'].includes(item.state)) {
      const cancel=button(attempt.cancelRequested?'Отмена уже запрошена':'Запросить отмену',()=>mutate('queue.cancel',{itemId:item.id,expectedRevision:item.revision}));
      cancel.disabled=Boolean(attempt.cancelRequested);section.append(cancel);
    }
    if(['unknown','finished'].includes(attempt.state)) {
      const form=element('form',null,'queue-form');
      form.append(element('p','Освободите очередь после проверки остановки всех процессов этой попытки. Завершённый ответ и выход основного процесса этого не подтверждают.','muted'));
      const evidence=field(form,'Подтверждение остановки исполнителя','evidence',{maximum:2048,multiline:true});
      submit(form,'Подтвердить остановку и освободить',()=>mutate('attempt.reconcile',{attemptId:attempt.id,expectedRevision:attempt.revision,resolution:'owned_runtime_stopped',evidence:evidence.value.trim()}));
      section.append(form);
    }
    detail.append(section);
  }

  /** Waiting pages come from domain dispatch order, never from sorting a partial history page. */
  async function loadPending(laneId,target,after,current) {
    const data=await request(`/api/v1/queues/${encodeURIComponent(laneId)}/pending?limit=20&after=${after}`);
    if(!alive || current!==generation)return;
    target.querySelector('[data-pending-more]')?.remove();
    if(!after && !data.items.length)target.append(emptyState('Нет ожидающих заданий','Добавьте новое задание выше.'));
    for(const item of data.items) {
      const row=itemRow(item);const actions=element('div',null,'queue-actions');
      // First-page head is authoritative; after reorder all pending pages are discarded.
      const first=target.querySelector('[data-item-id]')?.dataset.itemId;
      if(first)actions.append(button('В начало',()=>mutate('queue.move',{itemId:item.id,expectedRevision:item.revision,beforeItemId:first})));
      actions.append(button('В конец',()=>mutate('queue.move',{itemId:item.id,expectedRevision:item.revision,beforeItemId:null})),
        button('Отменить',()=>mutate('queue.cancel',{itemId:item.id,expectedRevision:item.revision})));
      row.append(actions);target.append(row);
    }
    if(data.hasMore){const more=button('Следующие ожидающие',()=>loadPending(laneId,target,data.nextCursor,current));more.dataset.pendingMore='true';target.append(more);}
  }

  /** History remains accessible independently of the active or pending lists and is always bounded. */
  async function loadHistory(laneId,target,after,current) {
    const data=await request(`/api/v1/queues/${encodeURIComponent(laneId)}/items?limit=20&after=${after}`);
    if(!alive || current!==generation)return;
    target.querySelector('[data-history-more]')?.remove();
    for(const item of data.items)target.append(itemRow(item));
    if(!after && !data.items.length)target.append(element('p','Заданий пока нет.','muted'));
    if(data.hasMore){const more=button('Следующие задания',()=>loadHistory(laneId,target,data.nextCursor,current));more.dataset.historyMore='true';target.append(more);}
  }

  /** External prompts and evidence stay text; a linked issue remains a separate workflow entity. */
  function itemRow(item) {
    const row=element('article',null,'queue-item');row.dataset.itemId=item.id;
    row.append(statusLabel(itemStates[item.state] || item.state),element('p',item.text));
    if(item.issueId)row.append(button('Открыть связанную задачу',()=>openIssue(item.issueId)));
    return row;
  }

  loadList().catch(fail);
  return ()=>{alive=false;generation++;listGeneration++;};
}
