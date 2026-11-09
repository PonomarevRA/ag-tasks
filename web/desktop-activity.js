/** Read only the local bounded snapshot; absence, unknown identity and capture time stay visible. */
export function renderSnapshot(document, value) {
  if (value?.schemaVersion !== 'ag-desktop/widget-v1' || value.appId !== 'ag-tasks' || !Array.isArray(value.requests)) throw new Error('Unavailable snapshot');
  const coverage = document.getElementById('coverage');
  coverage.textContent = `${value.status} · Снимок ${value.capturedAt} · ${String(value.source).slice(0, 512)}`;
  const rows = [];
  for (const request of value.requests.slice(0, 8)) {
    const row = document.createElement('li');
    for (const key of ['timestamp', 'operation', 'actorLabel', 'status', 'requestSummary', 'responseSummary']) {
      const part = document.createElement(key === 'timestamp' ? 'time' : 'p');
      part.textContent = String(request[key] ?? 'Неизвестно').slice(0, 512);
      row.append(part);
    }
    rows.push(row);
  }
  document.getElementById('requests').replaceChildren(...rows);
  if (!rows.length) coverage.textContent += ' · После запуска операций ещё не зафиксировано.';
}

/** Poll while visible only; each request and the page lifetime have bounded cancellation. */
export function startActivity(document, window, fetch = globalThis.fetch) {
  let timer, controller, disposed = false;
  const refresh = async () => {
    if (disposed) return;
    if (document.hidden) { timer = window.setTimeout(refresh, 2000); return; }
    controller = new AbortController();
    const timeout = window.setTimeout(() => controller.abort(), 3000);
    try {
      const response = await fetch('/api/desktop/v1/widget', { signal: controller.signal, cache: 'no-store', redirect: 'error' });
      if (!response.ok) throw new Error('Unavailable snapshot');
      const raw = await response.text();
      if (new TextEncoder().encode(raw).length > 65536) throw new Error('Oversized snapshot');
      if (!disposed) renderSnapshot(document, JSON.parse(raw));
    } catch {
      if (!disposed) document.getElementById('coverage').textContent = 'История недоступна; предыдущие строки могут быть устаревшими.';
    } finally {
      window.clearTimeout(timeout);
      controller = null;
      if (!disposed) timer = window.setTimeout(refresh, 2000);
    }
  };
  const dispose = () => { disposed = true; window.clearTimeout(timer); controller?.abort(); window.removeEventListener('pagehide', dispose); };
  window.addEventListener('pagehide', dispose);
  void refresh();
  return dispose;
}
if (typeof document !== 'undefined') startActivity(document, window);
