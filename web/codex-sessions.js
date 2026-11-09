import {element, statusLabel} from './dom.js';
import {orderSessions, placeSessionRows} from './session-order.js';

const labels = {running:'Выполняется · по журналу', idle:'Ожидает сообщения', unknown:'Состояние выполнения неизвестно'};

/**
 * Show read-only local chat metadata in the host's selected activity order.
 * @param {Element} parent - Host receiving the Codex source section.
 * @param {object} options - Read adapter and optional getSort callback (newest by default).
 * @returns {object} Section element, refresh/reorder operations and a disposer.
 */
export function renderCodexSessions(parent, {request, getSort=()=> 'newest'}) {
  let alive=true, pending=false;
  let sessions=[];
  const rows=new Map(), root=element('section',null,'codex-sessions');
  const heading=element('h2','Чаты Codex');
  const feedback=element('p','Проверка подключённых чатов…','muted');feedback.setAttribute('role','status');
  const list=element('div',null,'codex-session-rows');
  const waiting=element('details'), waitingTitle=element('summary'), waitingRows=element('div',null,'codex-session-rows');
  waiting.append(waitingTitle,waitingRows);waiting.hidden=true;
  root.append(heading,feedback,list,waiting);parent.append(root);

  /** Keep source failures distinct from an empty source, and replace stale success evidence on failure. */
  function unavailable() {
    feedback.textContent='Источник Codex недоступен. Обновите данные после запуска Codex.';
    for(const row of rows.values()) row.status.textContent=labels.unknown;
    list.hidden=true;waiting.hidden=true;
  }

  /**
   * Reorder retained source rows locally without rereading or changing chat state.
   * @returns {void}
   */
  function reorder() {
    const active=[], idle=[];
    for(const session of orderSessions(sessions,getSort())) {
      const row=rows.get(session.id);
      if(row) (session.state==='idle' ? idle : active).push(row.node);
    }
    placeSessionRows(list,active);placeSessionRows(waitingRows,idle);
  }

  /** Refresh fixed text slots without moving focus or replacing the selected task/session form. */
  async function refresh() {
    if(!alive || pending) return;
    pending=true;
    try {
      const data=await request('/api/v1/codex-sessions');
      if(!alive) return;
      if(!data.available || !Array.isArray(data.items)) {unavailable();return;}
      sessions=data.items;
      feedback.textContent=data.items.length
        ? `Открыты у работающего процесса: ${data.items.length}. Выполнение определяется по событиям журнала; устаревшие данные — «неизвестно».`
        : 'У работающих процессов Codex нет открытых локальных чатов.';
      if(data.truncated) feedback.textContent+=' Показана ограниченная часть списка.';
      list.hidden=false;
      const idleCount=data.items.filter(item=>item.state==='idle').length;
      waiting.hidden=idleCount===0;waitingTitle.textContent=`Ожидают сообщения: ${idleCount}`;
      const retained=new Set();
      for(const session of data.items) {
        retained.add(session.id);
        let row=rows.get(session.id);
        if(!row) {
          const node=element('article',null,'codex-session-row'), title=element('h3');
          const status=statusLabel(labels.unknown), time=element('p',null,'muted');
          node.append(title,status,time);row={node,title,status,time};rows.set(session.id,row);
        }
        const target=session.state==='idle' ? waitingRows : list;
        if(row.node.parentElement!==target) target.append(row.node);
        row.title.textContent=session.title;
        row.status.textContent=labels[session.state] || labels.unknown;
        row.time.textContent=session.activityAt ? `Последнее событие: ${session.activityAt}` : 'События выполнения не подтверждены';
      }
      for(const [id,row] of rows) if(!retained.has(id)) {row.node.remove();rows.delete(id);}
      reorder();
    } catch {
      if(alive) unavailable();
    } finally {pending=false;}
  }
  return {element:root,refresh,reorder,dispose:()=>{alive=false;}};
}
