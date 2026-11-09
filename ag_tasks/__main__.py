"""One independent executable provides desktop, HTTP and agent MCP modes."""

import argparse
from pathlib import Path


def main():
    """Select the standalone runtime without sharing Observer state or launching model calls."""
    parser = argparse.ArgumentParser(prog="ag-tasks")
    parser.add_argument("--data-dir", type=Path)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--desktop", action="store_true")
    parser.add_argument("--enable-desktop-automation", action="store_true", help="Opt in to local desktop MCP on this process")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mcp", action="store_true")
    modes.add_argument("--agent-console", action="store_true", help="Fresh standard MCP JSONL client; owns only explicitly started work")
    modes.add_argument("--agent-tools", action="store_true", help="Discover current MCP tools and input schemas")
    parser.add_argument("--agent-idle-seconds", type=int, default=300)
    options = parser.parse_args()
    if options.enable_desktop_automation and not (options.desktop or options.mcp or options.agent_console or options.agent_tools):
        parser.error("Desktop automation requires desktop or MCP mode")
    if options.agent_console or options.agent_tools:
        import asyncio
        import sys
        from .agent_console import run_console
        if not 30 <= options.agent_idle_seconds <= 3600:
            parser.error("Agent idle pause must be 30–3600 seconds")
        command = sys.executable
        arguments = ["--mcp"] if getattr(sys, "frozen", False) else ["-m", "ag_tasks", "--mcp"]
        if options.enable_desktop_automation:
            arguments += ["--enable-desktop-automation"]
        if options.data_dir:
            arguments += ["--data-dir", str(options.data_dir)]
        asyncio.run(run_console(command, arguments, list_only=options.agent_tools, idle_seconds=options.agent_idle_seconds))
        return
    if options.mcp:
        from .mcp_server import build_mcp
        build_mcp(directory=options.data_dir, desktop_automation=options.enable_desktop_automation).run()
        return
    from .app import create_app, default_directory
    from desktop_support import read_installation
    directory = options.data_dir if options.data_dir is not None else default_directory()
    installation = read_installation('ag-tasks', directory)
    port = options.port if options.port is not None else installation.preferred_port or 8895
    if options.desktop:
        from .desktop import run_desktop
        run_desktop(create_app, directory, port, enable_automation=options.enable_desktop_automation)
        return
    import uvicorn
    uvicorn.run(create_app(directory, origin=f"http://127.0.0.1:{port}"), host="127.0.0.1", port=port, access_log=False)


if __name__ == "__main__":
    main()
