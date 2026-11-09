import {createCommandClient, issueCommand} from './service-commands.js';
let uiToken, commandClient;

/** Initialize the same-origin UI action guard; never share this token with MCP tools. */
export async function connect() {
  uiToken = (await request("/api/v1/ui-session")).token;
}

/** Send local JSON with bounded server contracts and surface domain failures as text. */
export async function request(path, method = "GET", body) {
  const mapped = issueCommand(path, method, body);
  if (mapped) return command(...mapped);
  return send(path, method, body);
}

/** Use the same domain command contract as agents, retaining its key if delivery is uncertain. */
export async function command(operation, payload) {
  commandClient ||= createCommandClient(send);
  return commandClient(operation, payload);
}

/** Transport only: domain semantics and command outcomes live in the service. */
async function send(path, method = 'GET', body) {
  const headers = {};
  if (method !== "GET") headers["X-Ag-Tasks-UI"] = uiToken || "";
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const controller = new AbortController();
  // Git operations have a 30-second command limit; allow the server's bounded workflow time too.
  const timeout = setTimeout(() => controller.abort(), method === "GET" ? 45000 : 120000);
  try {
    const response = await fetch(path, {method, headers, body: body === undefined ? undefined : JSON.stringify(body), cache: "no-store", signal:controller.signal});
    const value = await response.json();
    if (!response.ok) {
      const failures = value.error?.details?.criterionFailures || [];
      const explanation = failures.slice(0, 3).map(item => `${item.text}: ${item.reason}`).join(" · ");
      const error = new Error((value.error?.message || `HTTP ${response.status}`) + (explanation ? ` · ${explanation}` : ""));
      error.code = value.error?.code;
      error.details = value.error?.details;
      error.status = response.status;
      throw error;
    }
    return value;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("Сервис не ответил вовремя. Обновите данные перед повтором операции.");
    throw error;
  } finally { clearTimeout(timeout); }
}
