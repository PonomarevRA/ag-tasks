"""Run the fixed automation bridge on the macOS Cocoa thread."""

import base64
import json
import logging
import sys
from threading import Event
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .desktop_automation import MAX_PNG_BYTES

LOG = logging.getLogger(__name__)
TIMEOUT = 12


def failure(code):
    """Return renderer failures separately from API and scope errors."""
    return {"status": "error", "error": code}


def capture_webview(view, done):
    """Capture the viewport content only, bounded before and after encoding."""
    import AppKit
    import WebKit

    size = view.bounds().size
    if size.width <= 0 or size.height <= 0:
        done(failure("desktop_capture_unavailable"))
        return
    width = min(1000, size.width)
    scale = view.window().backingScaleFactor() if view.window() else 1
    if width * width * size.height / size.width * scale ** 2 > 5_000_000:
        done(failure("desktop_capture_too_large"))
        return
    config = WebKit.WKSnapshotConfiguration.alloc().init()
    config.setSnapshotWidth_(width)

    def complete(image, error):
        """Convert the owned image without writing to the user's filesystem."""
        try:
            if error or image is None:
                done(failure("desktop_capture_failed"))
                return
            bitmap = AppKit.NSBitmapImageRep.imageRepWithData_(image.TIFFRepresentation())
            data = bytes(bitmap.representationUsingType_properties_(AppKit.NSBitmapImageFileTypePNG, {}))
            if len(data) > MAX_PNG_BYTES:
                done(failure("desktop_capture_too_large"))
                return
            done({"status": "ok", "adapter": "wkwebview", "surface": "webview",
                  "pngBase64": base64.b64encode(data).decode("ascii")})
        except Exception:
            LOG.debug("WKWebView image encoding failed", exc_info=True)
            done(failure("desktop_capture_failed"))

    view.takeSnapshotWithConfiguration_completionHandler_(config, complete)


class NativeWindow:
    """Schedule bounded operations on the current owned native document."""

    def __init__(self, window, origin, capture=capture_webview):
        self.window, self.origin, self.capture = window, origin, capture

    def _request(self, deadline, dispatch, operation):
        """Enforce the deadline and check the actual top-level URL on the UI thread."""
        from PyObjCTools import AppHelper
        from webview.platforms.cocoa import BrowserView

        ready = Event()
        result = None

        def done(value):
            """Deliver one native result to the waiting local request."""
            nonlocal result
            result = value
            ready.set()

        def invoke():
            """Skip expired work and refuse other documents, even on the same renderer."""
            if time.monotonic() >= deadline:
                return
            try:
                view = BrowserView.instances[self.window.uid].webview
                url = view.URL()
                route = urlsplit(str(url.absoluteString()) if url else "")
                if f"{route.scheme}://{route.netloc}" != self.origin or route.path != "/" or route.query != "desktop=1":
                    done(failure("desktop_document_changed"))
                    return
                if not dispatch(lambda: operation(view, done)):
                    done(failure("desktop_window_changed"))
            except Exception:
                LOG.debug("Desktop native operation failed", exc_info=True)
                done(failure("desktop_window_unavailable"))

        AppHelper.callAfter(invoke)
        if not ready.wait(max(0, deadline - time.monotonic())):
            return failure("desktop_timeout")
        return result

    def _script(self, deadline, dispatch, expression):
        """Evaluate fixed source with JSON arguments and an in-document origin guard."""
        expected = json.dumps(self.origin)
        script = f"""JSON.stringify((() => {{
          if (location.origin !== {expected} || location.pathname !== '/' || location.search !== '?desktop=1')
            return {{status:'error',error:'desktop_document_changed'}};
          const bridge = window.agTasksDesktopAutomation;
          if (!bridge) return {{status:'error',error:'desktop_bridge_unavailable'}};
          {expression}
        }})())"""

        def evaluate(view, done):
            """Use raw WebKit evaluation so the production CSP remains in force."""
            view.evaluateJavaScript_completionHandler_(script, lambda value, error: done(value if not error else None))

        result = self._request(deadline, dispatch, evaluate)
        if isinstance(result, dict):
            return result
        if isinstance(result, str) and len(result.encode()) <= 16_384:
            try:
                value = json.loads(result)
                if isinstance(value, dict):
                    return value
            except ValueError:
                pass
        return failure("desktop_script_failed")

    def _execute(self, deadline, dispatch, payload):
        """Start one async product action and observe its bounded completion."""
        data = json.dumps(payload, ensure_ascii=True)
        token = json.dumps(uuid4().hex)
        started = self._script(deadline, dispatch, f"""
          const slot = {{id:{token}, result:null}};
          window.__agTasksAutomationResult = slot;
          Promise.resolve(bridge.execute({data})).then(result => {{slot.result=result;}},
            () => {{slot.result={{status:'error',error:'desktop_command_failed'}};}});
          setTimeout(() => {{
            if (window.__agTasksAutomationResult === slot) delete window.__agTasksAutomationResult;
          }}, 15000);
          return {{status:'pending'}};
        """)
        while started.get("status") == "pending" and time.monotonic() < deadline:
            time.sleep(.04)
            started = self._script(deadline, dispatch, f"""
              const slot = window.__agTasksAutomationResult;
              if (slot?.id !== {token}) return {{status:'error',error:'desktop_document_changed'}};
              if (slot.result === null) return {{status:'pending'}};
              delete window.__agTasksAutomationResult;
              return slot.result;
            """)
        return failure("desktop_timeout") if started.get("status") == "pending" else started

    def _capture(self, deadline, dispatch, revision):
        """Fence pixels before and after the asynchronous renderer operation."""
        before = self._script(deadline, dispatch, "return bridge.inspect();")
        if before.get("status") != "ok":
            return before
        if before.get("revision") != revision:
            return failure("desktop_revision_changed")
        image = self._request(deadline, dispatch, self.capture)
        if image.get("status") != "ok":
            return image
        after = self._script(deadline, dispatch, "return bridge.inspect();")
        if after.get("revision") != revision:
            return failure("desktop_revision_changed")
        return image

    def __call__(self, operation, payload, dispatch):
        """Keep unsupported platforms explicit; inspection never starts a capture."""
        if sys.platform != "darwin":
            return failure("desktop_renderer_unsupported")
        deadline = time.monotonic() + TIMEOUT
        if operation == "inspect":
            return self._script(deadline, dispatch, "return bridge.inspect();")
        if operation == "execute":
            return self._execute(deadline, dispatch, payload)
        return self._capture(deadline, dispatch, payload["revision"])
