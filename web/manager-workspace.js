import {element,button} from './dom.js';
import {renderWorkAnalytics} from './work-analytics.js';
import {renderManagerControl} from './manager-control.js';

/** Compose independent operational/historical scopes, preserving applied filters and the manager draft. */
export function renderManagerWorkspace(target,{request,openIssue,filters}) {
  const root=element('section',null,'manager-workspace');
  const modes=element('div',null,'manager-modes');
  modes.setAttribute('role','group');modes.setAttribute('aria-label','Раздел контроля задач');
  const host=element('div');
  for (const node of [root,host]) node.dataset.agWidth='fill';
  let mode=filters.workspaceMode || 'management',disposeMode=()=>{};
  const controls={};
  /** Switch through the shared host mount; the inactive view owns no polling or chart listeners. */
  function show(next) {
    disposeMode();host.replaceChildren();mode=next;filters.workspaceMode=next;
    for (const [name,control] of Object.entries(controls)) control.setAttribute('aria-pressed',String(name===mode));
    if (mode==='management') {
      filters.manager ||= {};
      disposeMode=renderManagerControl(host,{request,openIssue,filters:filters.manager});
    } else disposeMode=renderWorkAnalytics(host,{request,openIssue,filters});
  }
  for (const [name,title] of [['management','Контроль выполнения'],['analytics','Аналитика времени']]) {
    controls[name]=button(title,()=>{if(mode!==name)show(name);});modes.append(controls[name]);
  }
  root.append(modes,host);target.append(root);show(mode);
  return () => {disposeMode();root.remove();};
}
