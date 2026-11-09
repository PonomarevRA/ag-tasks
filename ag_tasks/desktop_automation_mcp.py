"""Optional client-neutral MCP access to the opted-in desktop owner."""

from mcp.server.fastmcp import Image
from mcp.types import ToolAnnotations

from .desktop_automation import ACTIONS, ROUTE, decode_png


def register(server, request):
    """Expose three independent operations in the opted-in MCP catalog only."""

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def tasks_desktop_inspect() -> dict:
        """Read the owned desktop window, revision, view, sidebar, loading, focus and geometry."""
        return request("GET", ROUTE + "/inspect")

    @server.tool(annotations=ToolAnnotations(destructiveHint=False, openWorldHint=False))
    def tasks_desktop_execute(window_id: str, revision: str, action: ACTIONS) -> dict:
        """Navigate or open/close the spaces dialog in the last inspected window."""
        return request("POST", ROUTE + "/execute", {"windowId": window_id, "revision": revision, "action": action})

    @server.tool(annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False))
    def tasks_desktop_capture(window_id: str, revision: str):
        """Return an image of the desktop WebView content; excludes native titlebar and menus."""
        result = request("POST", ROUTE + "/capture", {"windowId": window_id, "revision": revision})
        if result.get("status") != "ok":
            return result
        return Image(data=decode_png(result.get("pngBase64")), format="png")
