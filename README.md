# ag-tasks

[Русский](README.ru.md) · [Install with an agent](INSTALL_AGENT.md)

ag-tasks is a local task tracker for people and coding agents. It includes a board, task graph, cross-project dashboard, work sessions and review. Agents use the same MCP tools from Cursor, Codex or any client that supports local stdio MCP. Git merges require a person's approval.

## Install a desktop release

Choose the installer for your OS from [GitHub Releases](https://github.com/PonomarevRA/ag-tasks/releases). See [what changed](CHANGELOG.md) and the [desktop release guide](packaging/RELEASE.md).

## Install from source

Use Git and Python 3.11 or newer. Run these commands in a terminal on macOS or Linux:

```sh
git clone https://github.com/PonomarevRA/ag-tasks.git
cd ag-tasks
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m ag_tasks --port 8895
```

On Windows, use `py -3 -m venv .venv` and replace `.venv/bin/python` with `.venv\Scripts\python.exe`. Keep the checkout in place. Open [http://127.0.0.1:8895](http://127.0.0.1:8895) in a browser. If the port is in use, choose a free one and open that address instead. The service keeps its data in a separate user directory. For a clean test, start it with `--data-dir` pointing to a new directory.

For a native window, install the desktop dependencies with `python -m pip install -e ".[desktop]"` using the same environment and start with `python -m ag_tasks --desktop` instead of the browser server. On Linux, the desktop backend also needs system Qt WebEngine libraries. See [desktop distribution](docs/engineering/desktop-distribution.md) for platform requirements and how to build installers.

## Connect an agent

1. Keep ag-tasks running. In your client's MCP settings, register a stdio server named `ag-tasks` with the **absolute path** to the virtual environment's Python executable and arguments `["-m", "ag_tasks", "--mcp"]`. If the service uses `--data-dir`, append the same option and absolute directory to these arguments.
2. Reconnect the client and ask it to call `tasks_capabilities({})`. The response should identify `ag-tasks` and the expected instance. Running `python -m ag_tasks --agent-tools` from the same environment lists the current schemas.
3. Copy the complete [`skills/ag-tasks/`](skills/ag-tasks/) folder to the skill directory your client uses. Common user locations are `~/.codex/skills/`, `~/.cursor/skills/` and `~/.claude/skills/`. Preserve any custom copy and reload skill discovery. The [agent installation prompt](INSTALL_AGENT.md) includes the setup and verification steps.

See the [task workflow](docs/engineering/task-workflow-v2.md) and [MIT license](LICENSE). The public web interface uses [AG Design components and charts](web/vendor/ag-design/manifest.json) from the pinned MIT community source. License notices accompany the vendored files.
