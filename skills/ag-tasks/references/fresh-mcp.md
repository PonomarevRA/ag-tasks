# Fresh MCP connection and work runner

Use this when an already running Codex/Cursor conversation exposes an older tool set. It is a second standard MCP connection, not an HTTP/database workaround.

Resolve the configured local ag-tasks executable from the existing MCP entry. Inspect only that entry, preserving unrelated settings and credentials. The standalone executable accepts:

```text
AgTasks --agent-tools
AgTasks --agent-console
```

For a source installation, use the installed virtual environment's absolute Python executable with `-m ag_tasks --agent-tools` or `-m ag_tasks --agent-console`. If the service was started with `--data-dir`, pass the same absolute directory in both modes. The owner must already be running. The client launches that executable with `--mcp`, initializes the standard protocol, checks service identity and calls tools/list. `--agent-tools` prints current schemas. `--agent-console` prints a ready event and accepts one JSON object per line:

```json
{"id":"list","tool":"tasks_list","arguments":{"limit":20}}
```

Each response has the same request ID and result. Use only advertised schemas. No generic HTTP, approval or shell execution is exposed. Domain failures remain errors, not saved task data. Writes are never automatically retried.

In Codex, open the console with a persistent exec session (`tty: true`), retain the returned session ID and send JSONL with `write_stdin`. Cursor may use its persistent terminal/task runner. Do not pipe a lone `tasks_work_start` into a process that immediately receives EOF: EOF intentionally pauses its work.

The console owns at most one session acquired through `tasks_work_start`, including a server-accepted exact-key replay after verified reconnect. It renews that exact lease every 25 seconds, pauses on clean EOF and pauses after 300 seconds without a command. `--agent-idle-seconds` supports 30–3600 seconds; choose the shortest interval that covers real work and explicitly stop before handing off. Do not send synthetic commands simply to avoid idle pause. For a long operation, send a genuine task update before starting it and choose a bounded interval appropriate to that operation. A lost transport cannot prove stop; inspect the task and respect the server's 90 second lease. Do not retry start with a new key to force ownership.

`tasks_submit_result` closes its producer lease. `tasks_work_stop` pauses without submitting. A later work period requires a new session key after confirming the prior session is closed. On task switch stop the old session first. Send `{"control":"close"}` after the last result/stop so the runner and its owned MCP child exit; EOF works for pipes. POSIX PTY input is noncanonical to preserve JSONL records over 4 KiB, so use the close record rather than Ctrl-D there. Never leave a runner renewing after a turn ends.

A fresh tool set does not grant execution, delegation, path or merge authority. Existing task/space/role/DoR/DoD and distinct-critic gates still apply.
