import {element,button,labelledField} from './dom.js';
import {statuses,priorities} from './task-format.js';
import {dashboardBlock} from './dashboard-block.js';
import {mountManagerPanels,kinds,observedAt} from './manager-panels.js';

/** Replace options while preserving the exact selected value, including an unmatched saved owner/project. */
function options(select, rows, label) {
  const selected = select.value;
  select.replaceChildren(new Option(label,''));
  for (const row of rows) select.add(new Option(row.label,row.id));
  if (selected && !rows.some(row => row.id === selected)) select.add(new Option(selected,selected));
  select.value = selected;
}

/** Keep document/ancestor scroll stable when an automatic snapshot replaces the table and charts. */
function scrollState(node) {
  const captured = [];
  for (let current=node;current;current=current.parentElement) captured.push([current,current.scrollTop,current.scrollLeft]);
  return () => { for (const [element,top,left] of captured) {element.scrollTop=top;element.scrollLeft=left;} };
}

/** Mount operational filters and read-only polling; server owns queue/count/deadline semantics. */
export function renderManagerControl(target,{request,openIssue,filters}) {
  const root = element('section',null,'manager-control');
  const form = element('form',null,'manager-filters ag-form-tracks');
  form.dataset.agSelectIndicators = '';
  form.dataset.agFormRow='auto';
  const names = {spaceId:'Проект',kind:'Тип карточки',status:'Статус',priority:'Приоритет',assignee:'Ответственный',q:'Ключ или название'};
  const controls = {};
  for (const [name,label] of Object.entries(names)) {
    const control = element(name === 'q' ? 'input' : 'select');
    control.name = name;
    if (name === 'q') {control.type='search';control.maxLength=200;}
    controls[name] = control;
    const field=labelledField(label,control,{sharedTracks:true});
    field.dataset.agWidth='content';field.dataset.agWidthMinEm='6';
    form.append(field);
  }
  options(controls.spaceId,[],'Все проекты');
  options(controls.assignee,[],'Любой');
  options(controls.kind,Object.entries(kinds).map(([id,label])=>({id,label})),'Все карточки');
  options(controls.status,Object.entries(statuses).map(([id,label])=>({id,label})),'Все статусы');
  options(controls.priority,Object.entries(priorities).map(([id,label])=>({id,label})),'Все приоритеты');
  let active = {spaceId:'',kind:'task',status:'',priority:'',assignee:'',q:'',queue:'open',...filters.applied};
  for (const [name,control] of Object.entries(controls)) {
    const value = filters.draft?.[name] ?? active[name] ?? '';
    if (control.tagName === 'SELECT' && value && ![...control.options].some(option => option.value===value)) control.add(new Option(value,value));
    control.value=value;
  }
  const action = element('div',null,'manager-filter-actions');
  action.dataset.agFormField = '';
  const caption = element('span','','ag-form-track__label');
  caption.setAttribute('aria-hidden','true');
  const submit = element('button','Применить','ag-button ag-button--primary ag-form-track__control');
  submit.type='submit';
  action.append(caption,submit);
  form.append(action);
  const filterBlock = dashboardBlock('manager-filters','Фильтры оперативного контроля');
  filterBlock.body.append(element('p','Эти фильтры управляют текущим контролем задач. У аналитики времени свой период и собственная выборка.','execution-scope'),form);
  const status = element('p',null,'manager-load-status');
  status.setAttribute('role','status');
  status.tabIndex=-1;
  const refreshState = element('span',null,'manager-refresh-state');
  const auto = element('input');
  auto.type='checkbox';auto.checked=filters.autoRefresh !== false;
  const autoLabel = element('label',null,'work-auto-refresh');
  autoLabel.append(auto,document.createTextNode('Обновлять каждые 30 с'));
  const tools = element('div',null,'work-refresh-bar');
  const refresh = button('Обновить контроль',()=>draftChanged()?apply():load(offset));
  const reset = button('Сбросить фильтры',()=>{
    for (const [name,control] of Object.entries(controls)) control.value=name==='kind'?'task':'';
    active.queue='open';apply();
  });
  tools.append(autoLabel,refresh,reset,refreshState);
  const result = element('div',null,'manager-report');
  for (const node of [root,form,result]) node.dataset.agWidth='fill';
  root.append(filterBlock.element,tools,status,result);
  target.append(root);
  let generation=0,disposed=false,pending=false,timer=null,offset=0,disposePanels=()=>{},lastRefresh='',renderedKey=null;

  /** Compare visible fields with the applied scope without applying the retained draft. */
  function draftChanged() {
    return Object.entries(controls).some(([name,control])=>control.value!==active[name]);
  }

  /** Retain unapplied form text independently from the snapshot's applied filter identity. */
  function saveDraft() {
    filters.draft=Object.fromEntries(Object.entries(controls).map(([name,control])=>[name,control.value]));
    refreshState.textContent=draftChanged()?'Фильтры изменены — нажмите «Применить»':lastRefresh;
  }
  form.addEventListener('input',saveDraft);
  form.addEventListener('change',saveDraft);

  /** Unapplied edits and active interactions protect the current DOM before and after network awaits. */
  function pauseReason() {
    if (!auto.checked) return 'Автообновление выключено';
    if (document.hidden) return 'Обновление приостановлено в скрытом окне';
    if (draftChanged()) return 'Изменены фильтры — нажмите «Применить»';
    if (form.contains(document.activeElement) || result.contains(document.activeElement)
        || document.querySelector('dialog[open]') || result.querySelector('.ag-inline-help__trigger[aria-expanded="true"]')) return 'Обновление приостановлено на время взаимодействия';
    return '';
  }

  /** Own one timer after requests settle; leaving this workspace releases it. */
  function schedule() {
    document.defaultView.clearTimeout(timer);timer=null;
    if (disposed || pending || !auto.checked) return;
    timer=document.defaultView.setTimeout(()=>{
      const reason=pauseReason();
      if (reason) {refreshState.textContent=reason;schedule();return;}
      load(offset,true);
    },30_000);
  }

  /** Apply visible fields explicitly and restart table pagination without altering task data. */
  function apply() {
    active={...active,...Object.fromEntries(Object.entries(controls).map(([name,control])=>[name,control.value]))};
    filters.applied={...active};
    saveDraft();
    return load(0);
  }
  form.addEventListener('submit',event=>{event.preventDefault();apply();});
  auto.addEventListener('change',()=>{filters.autoRefresh=auto.checked;refreshState.textContent=auto.checked?'Автообновление включено':'Автообновление выключено';schedule();});

  /** Fetch one generation and retain the previous valid snapshot on errors or interrupted background updates. */
  async function load(page,background=false,focusPage=false) {
    if (disposed) return;
    document.defaultView.clearTimeout(timer);timer=null;
    const version=++generation;
    const previousStatus=status.textContent;
    pending=true;result.setAttribute('aria-busy','true');
    status.textContent=background?'Обновляем контроль…':'Загружаем контроль задач…';
    try {
      const query=new URLSearchParams({...active,offset:page,limit:50});
      const data=await request(`/api/v1/analytics/management?${query}`);
      if (disposed || version!==generation) return;
      const reason=background&&pauseReason();
      if (reason) {status.textContent=previousStatus;refreshState.textContent=reason;return;}
      const restoreScroll=scrollState(root);
      const previousTable=result.querySelector('.manager-table-scroll');
      const horizontal=previousTable?.scrollLeft || 0;
      const vertical=renderedKey===query.toString() ? previousTable?.scrollTop || 0 : 0;
      const panels=mountManagerPanels(data,{openIssue,queue:active.queue,offset:page,
        onQueue:queue=>{active={...active,queue};filters.applied={...active};load(0);},onPage:page=>load(page,false,true)});
      disposePanels();disposePanels=panels.dispose;
      result.replaceChildren(panels.element);
      options(controls.spaceId,data.facets.projects,'Все проекты');
      options(controls.assignee,data.facets.assignees,'Любой');
      offset=page;status.textContent='';
      lastRefresh=`Обновлено ${observedAt(data.asOf)}`;
      saveDraft();
      const tableViewport=result.querySelector('.manager-table-scroll');
      tableViewport.scrollLeft=horizontal;
      tableViewport.scrollTop=vertical;
      renderedKey=query.toString();
      restoreScroll();
      if (focusPage) {
        const focusTarget=tableViewport.getClientRects().length ? tableViewport : result.querySelector('[data-dashboard-block="manager-table"]>summary');
        focusTarget.focus({preventScroll:true});
      }
    } catch (error) {
      if (!disposed && version===generation) status.textContent=`${result.childNodes.length?'Показан предыдущий снимок. ':''}Обновление не удалось: ${error.message}. Нажмите «Обновить контроль».`;
    } finally {
      if (!disposed && version===generation) {pending=false;result.removeAttribute('aria-busy');schedule();}
    }
  }
  load(0);
  return () => {saveDraft();disposed=true;generation++;document.defaultView.clearTimeout(timer);disposePanels();filterBlock.dispose();root.remove();};
}
