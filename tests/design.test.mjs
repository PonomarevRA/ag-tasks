/** Integration contracts exercise the actual vendored AG components and issue navigation. */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { bindTaskNavigation, selectTaskView } from '../web/navigation.js';
import { emptyState, statusLabel } from '../web/dom.js';

/** A small inert element records native focus and listener cleanup without inventing rendered dimensions. */
class Node {
  constructor(tag = 'button') {
    this.tagName = tag; this.dataset = {}; this.attributes = {}; this.listeners = {}; this.children = []; this.classes = new Set();
    this.classList = {toggle: (name, force) => force ? this.classes.add(name) : this.classes.delete(name)};
  }
  setAttribute(name, value) { this.attributes[name] = value; }
  getAttribute(name) { return this.attributes[name] ?? null; }
  removeAttribute(name) { delete this.attributes[name]; }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  removeEventListener(name) { delete this.listeners[name]; }
  closest() { return null; }
  focus() { this.ownerDocument.activeElement = this; }
  append(...nodes) { this.children.push(...nodes); }
}

test('shared arrow navigation moves focus only; Enter/click retains application selection ownership', () => {
  const owner = { activeElement: null }; const nav = new Node('nav'); nav.ownerDocument = owner;
  const buttons = ['board', 'list', 'repositories'].map(view => { const node = new Node(); node.dataset.view = view; node.ownerDocument = owner; return node; });
  nav.querySelectorAll = () => buttons; owner.activeElement = buttons[0]; const visits = [];
  const dispose = bindTaskNavigation(nav, view => visits.push(view)); selectTaskView(nav, 'board');
  let prevented = false;
  nav.listeners.keydown({key:'ArrowDown',preventDefault:()=>{prevented=true;}});
  assert.ok(prevented); assert.equal(owner.activeElement, buttons[1]); assert.deepEqual(visits, []);
  assert.equal(buttons[0].attributes['aria-current'], 'page'); assert.equal(buttons[1].attributes['aria-current'], undefined);
  buttons[1].listeners.click(); assert.deepEqual(visits, ['list']); selectTaskView(nav, 'list');
  assert.equal(buttons[0].attributes['aria-current'], undefined); assert.equal(buttons[1].attributes['aria-current'], 'page');
  buttons[2].disabled = true; nav.listeners.keydown({key:'End',preventDefault(){}}); assert.equal(owner.activeElement, buttons[1]);
  dispose(); assert.equal(nav.listeners.keydown, undefined); assert.ok(buttons.every(node => node.listeners.click === undefined));
});

test('Tasks empty/status render through the shared primitives as inert labelled content', () => {
  globalThis.document = {createElement:tag=>new Node(tag)};
  const empty = emptyState('<script>title</script>', 'Create a task'); assert.equal(empty.className, 'ag-empty');
  assert.equal(empty.children[0].textContent, '<script>title</script>');
  const status = statusLabel('Заблокировано', 'danger'); assert.equal(status.className, 'ag-status'); assert.equal(status.dataset.tone, 'danger');
});

test('static Tasks landmarks, dialog names, local package assets and palette forwarding are wired', async () => {
  const html = await readFile(new URL('../web/index.html', import.meta.url), 'utf8');
  const adapter = await readFile(new URL('../web/design-adapter.css', import.meta.url), 'utf8');
  assert.match(html, /href="#main"/); assert.match(html, /<main id="main" tabindex="-1"/);
  for (const id of ['editor', 'detail', 'approval']) assert.match(html, new RegExp(`<dialog id="${id}" aria-labelledby="${id}-title"`));
  assert.match(html, /aria-current="page"/); assert.match(html, /id="content" aria-busy="false"/);
  assert.ok(html.indexOf('/vendor/ag-design/tokens.css') < html.indexOf('/vendor/ag-design/styles.css'));
  assert.ok(html.indexOf('/styles.css') < html.indexOf('/design-adapter.css'));
  assert.match(adapter, /--ag-accent:var\(--blue\)/); assert.match(adapter, /--ag-surface:var\(--panel\)/);
});

test('committed vendor manifest matches every real shared asset', async () => {
  const manifest = JSON.parse(await readFile(new URL('../web/vendor/ag-design/manifest.json', import.meta.url), 'utf8'));
  const integration = JSON.parse(await readFile(new URL('../ag-design.integration.json', import.meta.url), 'utf8'));
  assert.equal(manifest.version, integration.packages['@ag-design/components']);
  for (const [name, expected] of Object.entries(manifest.files)) {
    const body = await readFile(new URL('../web/vendor/ag-design/' + name, import.meta.url));
    assert.equal(createHash('sha256').update(body).digest('hex'), expected, name);
  }
});
