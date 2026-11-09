"""Bounded, event-driven sidebar measurements from an owned local WebView.

The sole exposed function accepts no arguments and grants no I/O or actions. It
only asks Python to re-read a fixed DOM measurement at the configured origin.
"""

import json
import math
from threading import Event, Lock, Thread
from urllib.parse import urlsplit


class SidebarPresentation:
    """Coalesce notifications into one active native pull; closed windows reject late work."""

    def __init__(self, window, own_origin, on_state, *, sidebar_id='section-nav',
                 right_class='grafana-mode', collapsed_class='nav-collapsed', observe=False):
        """Retain application-owned selectors and exact origin, never page-supplied commands or URLs."""
        self.window, self.origin, self.on_state = window, origin_key(own_origin), on_state
        self.lock, self.closed = Lock(), Event()
        self.pending = self.running = False
        self.sidebar_id, self.right_class, self.collapsed_class = sidebar_id, right_class, collapsed_class
        self.observe = observe
        self.script = measurement_script(sidebar_id, right_class, collapsed_class)

        self.document_generation = 0
        self.native_cleanup = None

    def active(self):
        """Reject queued callbacks after either orderly shutdown or native window removal."""
        return not self.closed.is_set() and not self.window.events.closed.is_set()

    def notify(self):
        """Keep at most one worker and one pending refresh, including resize storms."""
        with self.lock:
            if not self.active():
                return
            self.pending = True
            if self.running:
                return
            self.running = True
        Thread(target=self._drain, name='native-sidebar-presentation', daemon=True).start()

    def loaded(self):
        """Reinstall optional fixed watchers after navigation; application-owned notifiers need no injection."""
        with self.lock:
            self.document_generation += 1
        if not self.active():
            return
        if not self._same_origin():
            self.on_state(None)
            return
        # pywebview.expose uses new Function under 6.2.1; this fixed ordinary function respects CSP.
        self.window.run_js(notification_script())
        if self.observe:
            self.window.run_js(watcher_script(self.sidebar_id))
        self.notify()

    def _same_origin(self):
        """Compare the current top-level origin before and after a native pull."""
        try:
            return origin_key(self.window.get_current_url()) == self.origin
        except Exception:
            return False

    def _drain(self):
        """Debounce native pulls independently of web notifications and drop invalid or stale measurements."""
        while not self.closed.wait(.15):
            with self.lock:
                if not self.pending or not self.active():
                    self.running = False
                    return
                self.pending = False
                generation = self.document_generation
            if self._same_origin():
                try:
                    value = self.window.run_js(self.script)
                    if isinstance(value, str):
                        value = json.loads(value)
                    state = validate_state(value)
                    if self.active() and self._same_origin() and generation == self.document_generation:
                        self.on_state(state)
                except Exception:
                    # A document may disappear during navigation; leave the fallback available.
                    pass
        with self.lock:
            self.running = False

    def close(self):
        """Dispose pending pulls before native teardown; the exposed no-argument function becomes inert."""
        if self.closed.is_set():
            return
        self.closed.set()
        with self.lock:
            self.pending = False
        if self.native_cleanup:
            self.native_cleanup()


def origin_key(value):
    """Accept only an exact numeric loopback HTTP origin without credentials."""
    parsed = urlsplit(value)
    if parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or parsed.username or parsed.password or not parsed.port:
        raise ValueError('Expected owned numeric loopback origin')
    return parsed.scheme, parsed.hostname, parsed.port


def validate_state(value):
    """Bound presentation fields only; the native adapter additionally checks the actual window width."""
    if not isinstance(value, dict) or set(value) not in ({'side', 'collapsed', 'width'}, {'side', 'collapsed', 'width', 'windowContext'}):
        return None
    if value['side'] not in ('left', 'right') or type(value['collapsed']) is not bool:
        return None
    width = value['width']
    if type(width) not in (int, float) or not math.isfinite(width) or not 48 <= width <= 1600:
        return None
    result = {'side': value['side'], 'collapsed': value['collapsed'], 'width': float(width)}
    if 'windowContext' in value:
        from .native_window_commands import validate_context
        context = validate_context(value['windowContext'])
        if context is None:
            return None
        result['windowContext'] = context
    return result


def measurement_script(sidebar_id, right_class, collapsed_class):
    """Build one fixed read-only DOM query from trusted Python configuration."""
    script = """JSON.stringify((() => {
      const e=document.getElementById(SIDEBAR_ID);if(!e)return null;
      const has=n=>!!n&&(document.documentElement.classList.contains(n)||document.body.classList.contains(n));
      const side=has(RIGHT_CLASS)?'right':'left',collapsed=has(COLLAPSED_CLASS)||e.hidden;
      const r=e.getBoundingClientRect();if(side==='left'&&!collapsed&&r.width>=document.documentElement.clientWidth-1)return null;let width=Math.round(side==='left'?r.right:innerWidth-r.left);
      if(collapsed&&width===0)width=48;const result={side,collapsed,width};const windowContext=WINDOW_CONTEXT;if(windowContext)result.windowContext=windowContext;return result;
    })())"""
    for placeholder, value in [('SIDEBAR_ID', sidebar_id), ('RIGHT_CLASS', right_class), ('COLLAPSED_CLASS', collapsed_class)]:
        script = script.replace(placeholder, json.dumps(value))
    from .native_window_commands import context_script
    return script.replace('WINDOW_CONTEXT', context_script())


def watcher_script(sidebar_id):
    """Observe only shell layout for pages without their own notifier; reload/pagehide clean up idempotently."""
    return """(() => {
      window.__standaloneSidebarWatcher?.();
      const panel=document.getElementById(SIDEBAR_ID); if(!panel)return;
      let timer; let disposed=false;
      const notify=()=>{clearTimeout(timer);timer=setTimeout(()=>{if(disposed)return;
        try{Promise.resolve(window.pywebview?.api?.standalone_sidebar_changed?.()).catch(()=>{});}catch{}
      },150);};
      const resize=new ResizeObserver(notify);resize.observe(panel);
      const mutation=new MutationObserver(notify);mutation.observe(panel,{attributes:true,attributeFilter:['hidden','class','style']});
      const dispose=()=>{if(disposed)return;disposed=true;clearTimeout(timer);resize.disconnect();mutation.disconnect();window.removeEventListener('pagehide',dispose);};
      window.__standaloneSidebarWatcher=dispose;window.addEventListener('pagehide',dispose,{once:true});notify();
    })()""".replace('SIDEBAR_ID', json.dumps(sidebar_id))


def notification_script():
    """Add only one ordinary zero-argument function; its fixed native channel accepts null and no commands."""
    return """(() => {const api=window.pywebview?.api;if(!api)return;
      api.standalone_sidebar_changed=function(){window.webkit.messageHandlers.standaloneSidebar.postMessage(null);};
    })()"""


def accepts_notification(expected_origin, source_origin, main_frame, null_body):
    """Keep iframe, foreign-origin and non-null messages out of the native presentation queue."""
    return main_frame is True and null_body is True and source_origin == expected_origin


class InactivePresentation:
    """Keep the browser fallback on unsupported native renderers without registering a bridge."""
    def active(self):
        """Report the absence of a supported native sidebar capability."""
        return False

    def notify(self):
        """Ignore optional presentation notifications on unsupported platforms."""
        return None

    def close(self):
        """Require no native cleanup when no native resources were allocated."""
        return None

    def loaded(self):
        """Keep ordinary browser layout unchanged on document load."""
        return None
