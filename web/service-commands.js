/** Keep domain command identities across ambiguous UI retries; the server owns effects and deduplication. */
export function createCommandClient(send, storage = globalThis.sessionStorage, uuid = () => crypto.randomUUID()) {
  if (!storage) throw new Error('Хранилище ключей команд недоступно. Сверьте результаты с сервисом перед новым действием.');
  const storageKey = 'ag-tasks:pending-domain-commands:v1';
  let blocked = false;
  let saved = {clientId:uuid(), pending:{}};
  if (storage) {
    try {
      const raw = storage.getItem(storageKey);
      if (raw !== null) {
        const old = JSON.parse(raw);
        if (typeof old?.clientId !== 'string' || !old?.pending || typeof old.pending !== 'object' || Array.isArray(old.pending)) throw new Error('Invalid command cache');
        saved = old;
      }
    } catch { throw new Error('Не удалось прочитать неподтверждённые команды. Сверьте результаты с сервисом перед новым действием.'); }
  }
  /** Retain only key digests and command IDs, never task bodies or credentials in browser storage. */
  function persist() {
    if (storage) storage.setItem(storageKey, JSON.stringify(saved));
  }
  /** Retire a definitive outcome without letting an older concurrent reply erase a newer intent. */
  function retire(digest,commandId) {
    if(saved.pending[digest]!==commandId)return;
    delete saved.pending[digest];
    try {persist();} catch(error) {saved.pending[digest]=commandId;blocked=true;throw error;}
  }
  persist();
  /** Sort object keys while preserving exact text and array order for a stable retry digest. */
  function canonical(value) {
    if (Array.isArray(value)) return value.map(canonical);
    if (value && typeof value === 'object') {
      const result = Object.create(null);
      for (const key of Object.keys(value).sort()) result[key] = canonical(value[key]);
      return result;
    }
    return value;
  }
  return async (operation, payload) => {
    if (blocked) throw new Error('Не удалось сохранить состояние ключа команды. Сверьте подтверждённый результат и перезагрузите страницу.');
    const bytes = new TextEncoder().encode(JSON.stringify(canonical({operation,payload})));
    const digest = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(byte => byte.toString(16).padStart(2,'0')).join('');
    if (!saved.pending[digest]) {
      if (Object.keys(saved.pending).length >= 100) throw new Error('Слишком много неподтверждённых команд. Сначала сверьте их результаты с сервисом.');
      saved.pending[digest] = uuid();
      try {persist();} catch(error) {delete saved.pending[digest];throw error;}
    }
    const commandId = saved.pending[digest];
    let response;
    try {
      response = await send('/api/v1/commands', 'POST', {
        clientId:saved.clientId, commandId, operation, payload,
      });
    } catch(error) {
      // A confirmed domain refusal ends this intent. Transport/server timeouts remain ambiguous.
      if(Number.isInteger(error.status) && error.status>=400 && error.status<500 &&
          ![408,425,429].includes(error.status) && typeof error.code==='string')retire(digest,commandId);
      throw error;
    }
    retire(digest,commandId);
    return response;
  };
}

/** Map legacy UI affordances to shared domain operations; workflow/Git commands retain their guarded routes. */
export function issueCommand(path, method, body) {
  if (path === '/api/v1/issues' && method === 'POST') return ['issue.create', body];
  const match = path.match(/^\/api\/v1\/issues\/([^/]+)(?:\/(comments|links))?$/);
  if (!match) return null;
  const issueId = decodeURIComponent(match[1]);
  if (method === 'PATCH' && !match[2]) return ['issue.update', {issueId, changes:body}];
  if (method === 'POST' && match[2] === 'comments') return ['issue.comment', {issueId, text:body.text}];
  if (method === 'POST' && match[2] === 'links') return ['issue.link', {issueId, relatedId:body.relatedId, kind:body.kind || 'related'}];
  return null;
}
