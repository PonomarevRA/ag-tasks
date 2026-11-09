import {createBoardViewport} from './vendor/ag-design/index.js';
import {renderCardSummary} from './task-card.js';
import {applyIdentity} from './task-colors.js';
import {element, button} from './dom.js';

let graphSequence = 0;

/** Group only loaded task ancestry; missing parents and cycles stay explicit and never create tasks. */
export function groupTaskGraph(issues, maximum = 200) {
  const tasks = [];
  const byId = new Map();
  for (const issue of issues || []) {
    if (!issue?.id || byId.has(issue.id)) continue;
    if (tasks.length >= maximum) break;
    tasks.push(issue);
    byId.set(issue.id, issue);
  }
  const groups = new Map();
  const edges = [];
  let missingParents = 0;
  let cycles = 0;
  for (const issue of tasks) {
    if (issue.parentId && byId.has(issue.parentId)) {
      edges.push({source:issue.parentId, target:issue.id, kind:'parent'});
    } else if (issue.parentId) {
      missingParents++;
    }
    let ancestor = issue;
    let depth = 0;
    const seen = new Set([issue.id]);
    while (ancestor.parentId && byId.has(ancestor.parentId)) {
      if (seen.has(ancestor.parentId)) {
        cycles++;
        // A corrupt cycle has no real root. Keep its member visible as an independent group.
        ancestor = issue;
        depth = 0;
        break;
      }
      ancestor = byId.get(ancestor.parentId);
      seen.add(ancestor.id);
      depth++;
    }
    if (!groups.has(ancestor.id)) groups.set(ancestor.id, {root:ancestor, tasks:[]});
    groups.get(ancestor.id).tasks.push({issue, depth});
  }
  return {
    groups:[...groups.values()], edges, tasks,
    omitted:Math.max(0, (issues?.length || 0) - tasks.length), missingParents, cycles,
  };
}

/** Render real task cards in a shared camera; geometry stays in unscaled scene coordinates, not screen pixels. */
export function renderTaskGraph(container, graph, options) {
  const model = groupTaskGraph(graph.nodes, options.maximum || 200);
  const markerId = `task-direction-${++graphSequence}`;
  const knownIds = new Set(model.tasks.map(issue => issue.id));
  const edges = (graph.edges || []).filter(edge => knownIds.has(edge.from) && knownIds.has(edge.to));
  const toolbar = element('div', null, 'graph-toolbar');
  toolbar.append(element('span', `${model.tasks.length} задач · группы по родительской задаче`, 'muted'));
  const viewport = element('div', null, 'task-graph-viewport');
  viewport.setAttribute('aria-label', 'Граф задач и подзадач');
  const scene = element('div', null, 'task-graph-scene');
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('task-graph-edges');
  svg.setAttribute('aria-hidden', 'true');
  const groups = element('div', null, 'task-graph-groups');
  const cards = new Map();
  for (const group of model.groups) {
    const frame = element('section', null, 'task-graph-group');
    frame.append(element('h2', `${group.root.key} · ${group.root.title}`, 'task-group-heading'));
    const levels = element('div', null, 'task-graph-levels');
    const columns = new Map();
    for (const entry of group.tasks) {
      if (!columns.has(entry.depth)) {
        const column = element('div', null, 'task-graph-level');
        columns.set(entry.depth, column);
      }
      const issue = entry.issue;
      const card = button(issue.title, () => options.onOpen(issue.id), 'task-graph-card');
      card.replaceChildren(element('span', issue.key, 'key'), element('strong', issue.title));
      card.setAttribute('aria-label', `${issue.key}: ${issue.title}`);
      card.dataset.issueId = issue.id;
      applyIdentity(card, issue.title, issue.spaceName || issue.spaceId);
      renderCardSummary(card, issue);
      columns.get(entry.depth).append(card);
      cards.set(issue.id, card);
    }
    for (const [, column] of [...columns.entries()].sort((a, b) => a[0] - b[0])) levels.append(column);
    frame.append(levels);
    groups.append(frame);
  }
  scene.append(svg, groups);
  viewport.append(scene);
  container.append(toolbar, viewport);
  const notices = [];
  if (model.omitted) notices.push(`Лимит отображения: ещё ${model.omitted} задач не помещены в граф.`);
  if (model.missingParents) notices.push(`Родители ${model.missingParents} задач вне отображаемой выборки; узлы не дорисованы.`);
  if (model.cycles) notices.push('Обнаружены циклы в родительских связях; показаны фактические задачи без выдуманной иерархии.');
  const missingEdges = (graph.edges?.length || 0) - edges.length;
  if (missingEdges) notices.push(`У ${missingEdges} связей один из концов вне отображаемой выборки; линии не дорисованы.`);
  if (notices.length) container.append(element('p', notices.join(' '), 'coverage-notice'));
  if (graph.stubs?.length) {
    const outside = element('details', null, 'graph-stubs');
    outside.append(element('summary', `Связанные задачи вне страницы · ${graph.stubs.length}`));
    for (const stub of graph.stubs.slice(0, 50)) {
      outside.append(button(`${stub.key || stub.id} · ${stub.title}`, () => options.onOpen(stub.id)));
    }
    if (graph.stubs.length > 50) outside.append(element('p', 'Показаны первые 50 ссылок вне страницы.', 'muted'));
    container.append(outside);
  }
  toolbar.append(element('span', 'Сплошная стрелка: родитель → подзадача · пунктирная стрелка: блокирует · пунктир без стрелки: связь/дубликат', 'muted'));

  /** Reserve the actual remaining window area; long filters must not push board controls below the fold. */
  function sizeViewport() {
    const documentTop = viewport.getBoundingClientRect().top + window.scrollY;
    const available = Math.max(280, Math.min(900, window.innerHeight - documentTop - 24));
    const height = `${available}px`;
    if (viewport.style.height !== height) viewport.style.height = height;
  }
  sizeViewport();
  const board = createBoardViewport({viewport, scene, storageKey:options.storageKey});
  let disposed = false;
  let scheduled;
  /** Recalculate connectors after actual layout; skip unavailable rectangles instead of guessing positions. */
  function draw() {
    scheduled = null;
    if (disposed || !scene.isConnected) return;
    sizeViewport();
    const bounds = scene.getBoundingClientRect();
    const width = scene.clientWidth;
    const height = scene.clientHeight;
    // Use the rendered scale: a newer queued camera state may not have painted yet.
    const scale = scene.offsetWidth ? bounds.width / scene.offsetWidth : board.state.scale;
    const rectangles = new Map();
    for (const [id, card] of cards) rectangles.set(id, card.getBoundingClientRect());
    // Read all card geometry before any SVG mutations to avoid repeated forced layouts.
    const definitions = document.createElementNS('http://www.w3.org/2000/svg', 'defs');
    const marker = document.createElementNS('http://www.w3.org/2000/svg', 'marker');
    marker.id = markerId;
    for (const [key, value] of Object.entries({viewBox:'0 0 8 8',refX:'8',refY:'4',markerWidth:'8',markerHeight:'8',orient:'auto'})) marker.setAttribute(key, value);
    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    arrow.setAttribute('d', 'M0,0 L8,4 L0,8 Z');
    arrow.setAttribute('class', 'graph-arrowhead');
    marker.append(arrow);
    definitions.append(marker);
    svg.replaceChildren(definitions);
    svg.setAttribute('width', String(width));
    svg.setAttribute('height', String(height));
    for (const edge of edges) {
      const from = rectangles.get(edge.from);
      const to = rectangles.get(edge.to);
      if (!from?.width || !to?.width) continue;
      const x1 = (from.right - bounds.left) / scale;
      const y1 = (from.top + from.height / 2 - bounds.top) / scale;
      const x2 = (to.left - bounds.left) / scale;
      const y2 = (to.top + to.height / 2 - bounds.top) / scale;
      const middle = (x1 + x2) / 2;
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', `M${x1},${y1} C${middle},${y1} ${middle},${y2} ${x2},${y2}`);
      path.setAttribute('data-kind', edge.kind);
      if (edge.kind === 'parent' || edge.kind === 'blocks') path.setAttribute('marker-end', `url(#${markerId})`);
      svg.append(path);
    }
  }
  /** Coalesce geometry work so resizing never creates an unbounded animation loop. */
  function schedule() {
    if (!disposed && scheduled == null) scheduled = requestAnimationFrame(draw);
  }
  const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(schedule) : null;
  observer?.observe(scene);
  for (const card of cards.values()) observer?.observe(card);
  /** Resize the host viewport, then measure its cards in the next frame. */
  function resize() {sizeViewport();schedule();}
  window.addEventListener('resize', resize);
  schedule();
  return () => {
    disposed = true;
    board.dispose();
    if (scheduled != null) cancelAnimationFrame(scheduled);
    observer?.disconnect();
    window.removeEventListener('resize', resize);
  };
}
