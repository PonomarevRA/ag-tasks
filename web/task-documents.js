import {createPopup} from './vendor/ag-design/index.js';
import {element, button, report} from './dom.js';
import {request} from './api.js';

let documentDialog, documentGeneration = 0;

/** Build a Markdown display from server-generated HTML (raw HTML/images are disabled by the owned parser). */
export function markdownBody(html) {
  const body = element('article', null, 'markdown-body');
  const template = document.createElement('template');
  template.innerHTML = html;
  body.append(template.content);
  for (const table of body.querySelectorAll('table')) {
    const scroll = element('div', null, 'markdown-table');
    scroll.tabIndex = 0;
    scroll.setAttribute('aria-label', 'Таблица Markdown; горизонтальная прокрутка');
    table.before(scroll);
    scroll.append(table);
  }
  for (const code of body.querySelectorAll('pre > code')) {
    const copy = button('Копировать код', async () => {
      await navigator.clipboard.writeText(code.textContent);
      copy.textContent = 'Скопировано';
    }, 'copy-code');
    code.parentElement.append(copy);
  }
  return body;
}

/** Open one bounded file in a reusable expandable modal, preserving focus and rejecting stale fetch results. */
export async function openDocument(issue, documentId, reload) {
  const opener = document.activeElement;
  const parentDialog = opener?.closest("dialog");
  const generation = ++documentGeneration;
  const data = await request(`/api/v1/issues/${issue.id}/documents/${documentId}`);
  if (generation !== documentGeneration || !opener?.isConnected) return;
  if (parentDialog && !parentDialog.open) return;
  if (documentDialog?.open) documentDialog.close();
  documentDialog?.remove();
  const content = element('div', null, 'document-content');
  const toolbar = element('div', null, 'document-toolbar');
  const rendered = markdownBody(data.html);
  const source = element('pre', data.content, 'markdown-source');
  source.hidden = true;
  const mode = button('Исходник', () => {
    rendered.hidden = !rendered.hidden;
    source.hidden = !source.hidden;
    mode.textContent = source.hidden ? 'Исходник' : 'Просмотр';
    mode.setAttribute('aria-pressed', String(!source.hidden));
  });
  mode.setAttribute('aria-pressed', 'false');
  const expand = button('Развернуть', () => {
    const on = documentDialog.classList.toggle('document-expanded');
    expand.textContent = on ? 'Компактно' : 'Развернуть';
    expand.setAttribute('aria-pressed', String(on));
  });
  expand.setAttribute('aria-pressed', 'false');
  const download = element('a', 'Скачать .md', 'ag-button ag-button--secondary');
  download.href = `/api/v1/issues/${issue.id}/documents/${documentId}/download`;
  download.download = data.name;
  toolbar.append(mode, button('Копировать', () => navigator.clipboard.writeText(data.content)), download,
    button('Открыть папку', () => request(`/api/v1/issues/${issue.id}/open-folder`, 'POST', {})), expand);
  content.append(toolbar, element('p', `Версия ${data.revision} · ${issue.key}`, 'muted'), rendered, source);
  // Relative Markdown references stay inside task attachments; never navigate the application to a local path.
  content.addEventListener('click', event => {
    const link = event.target.closest('a');
    const href = link?.getAttribute('href');
    if (!href || /^(https?:|\/)/i.test(href)) return;
    if (href.startsWith('#')) { event.preventDefault(); return; }
    event.preventDefault();
    let name;
    try { name = decodeURIComponent(href).toLowerCase(); } catch { return; }
    const sibling = issue.documents.find(item => item.name.toLowerCase() === name);
    if (sibling) openDocument(issue, sibling.id, reload).catch(report);
  });
  documentDialog = createPopup({id:'task-document', title:data.name, content});
  documentDialog.classList.add('document-dialog');
  const viewer = documentDialog;
  viewer.addEventListener('close', () => {
    if (documentDialog === viewer) { documentGeneration++; documentDialog = null; }
    // Remove the closed viewer so expanded layout cannot leave stale file content in the page.
    viewer.remove();
    if (opener?.isConnected && !opener.disabled) opener.focus();
  }, {once:true});
  document.body.append(documentDialog);
  documentDialog.showModal();
}

/** Show Markdown chips and an explicit editor/upload surface; file reads never grant arbitrary service paths. */
export function renderDocuments(parent, issue, reload) {
  const section = element('section', null, 'section task-documents');
  section.append(element('h3', issue.kind === 'rnd' ? 'План и файлы · Markdown' : 'Файлы · Markdown'));
  const chips = element('div', null, 'document-chips');
  for (const file of issue.documents || []) {
    const chip = button(`▤ ${file.name}`, () => openDocument(issue, file.id, reload), 'document-chip');
    chip.title = `${file.purpose === 'plan' ? 'План' : 'Markdown'} · версия ${file.revision}`;
    chips.append(chip);
  }
  if (!chips.children.length) chips.append(element('p', 'Файлы пока не добавлены.', 'muted'));
  section.append(chips, button('Открыть папку с файлами', () => request(`/api/v1/issues/${issue.id}/open-folder`, 'POST', {})));
  if (!issue.archivedAt && issue.kind !== 'review') {
    const disclosure = element('details');
    disclosure.append(element('summary', 'Добавить или изменить Markdown'));
    const editor = element('div', null, 'document-editor');
    const name = element('input');
    name.value = issue.kind === 'rnd' ? 'plan.md' : 'notes.md';
    name.maxLength = 128;
    name.setAttribute('aria-label', 'Имя Markdown файла');
    const purpose = element('select');
    purpose.setAttribute('aria-label', 'Назначение файла');
    purpose.append(new Option('План', 'plan'), new Option('Вложение', 'attachment'), new Option('Результат', 'result'));
    purpose.value = issue.kind === 'rnd' ? 'plan' : 'attachment';
    const text = element('textarea');
    text.rows = 10; text.maxLength = 49152;
    text.setAttribute('aria-label', 'Текст Markdown');
    const upload = element('input');
    upload.type = 'file'; upload.accept = '.md,text/markdown,text/plain';
    upload.setAttribute('aria-label', 'Загрузить Markdown файл до 48 КиБ');
    upload.addEventListener('change', async () => {
      const file = upload.files[0];
      if (!file) return;
      if (file.size > 49152) { upload.setCustomValidity('Файл больше48 КиБ'); upload.reportValidity(); return; }
      upload.setCustomValidity('');
      try { text.value = new TextDecoder('utf-8', {fatal:true}).decode(await file.arrayBuffer()); name.value = file.name; }
      catch { upload.setCustomValidity('Нужен текстовый файл UTF-8'); upload.reportValidity(); }
    });
    let revision;
    name.addEventListener('input', () => { revision = undefined; });
    editor.append(name, purpose, upload, text, button('Загрузить текущую версию', async () => {
      const file = issue.documents.find(item => item.name.toLowerCase() === name.value.trim().toLowerCase());
      if (!file) throw new Error('Файл ещё не создан');
      const current = await request(`/api/v1/issues/${issue.id}/documents/${file.id}`);
      text.value = current.content; purpose.value = current.purpose; revision = current.revision;
    }), button('Сохранить файл', async () => {
      const value = {name:name.value.trim(), content:text.value, purpose:purpose.value};
      if (revision !== undefined) value.expectedRevision = revision;
      await request(`/api/v1/issues/${issue.id}/documents`, 'POST', value);
      await reload();
    }, 'primary'));
    editor.append(element('p', 'До 48 КиБ, UTF-8. Для замены сначала загрузите текущую версию. Изменение плана требует новой проверки результата.', 'muted'));
    disclosure.append(editor);
    section.append(disclosure);
  }
  parent.append(section);
}
