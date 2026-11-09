"""Public AppKit titlebar sidebar accessory aligned with the owned web sidebar."""

import logging
import sys
from threading import Thread, current_thread, main_thread
from types import SimpleNamespace

_delegate_class = None
_message_class = None


def bind_sidebar_presentation(window, own_origin, **options):
    """Expose only zero-argument measurement notifications; return an idempotently disposable binding.

    Trusted options are sidebar_id, right_class, collapsed_class and observe.
    Page content supplies no selectors, URLs, code or application commands.
    """
    from .native_presentation import InactivePresentation, SidebarPresentation
    if sys.platform != 'darwin':
        return InactivePresentation()
    binding = SidebarPresentation(window, own_origin, lambda state: _queue_state(window, state), **options)
    window._standalone_sidebar_presentation = binding
    return binding


def install_native_toolbar(window, on_toggle, *, identity=None) -> None:
    """Install the real framed titlebar control; None removes this page's unavailable capability."""
    if sys.platform != 'darwin':
        return
    from PyObjCTools import AppHelper
    AppHelper.callAfter(_install_on_main, window, on_toggle, identity)


def _active(window):
    """Suppress late native work after a successful close or native window removal."""
    binding = getattr(window, '_standalone_sidebar_presentation', None)
    return binding is None or binding.active()


def _remove_accessory(window):
    """Remove only this adapter's retained accessory, preserving other native window controls."""
    accessory = getattr(window, '_standalone_sidebar_accessory', None)
    if accessory:
        if getattr(accessory, 'window_commands', None):
            accessory.window_commands.close()
        controllers = list(window.native.titlebarAccessoryViewControllers())
        for controller in (accessory.controller, accessory.identity_controller):
            if controller in controllers:
                window.native.removeTitlebarAccessoryViewControllerAtIndex_(controllers.index(controller))
                controllers.remove(controller)
        window._standalone_sidebar_accessory = None


def _install_on_main(window, on_toggle, identity=None) -> None:
    """Create an NSButton/NSBox accessory in the standard NSWindow titlebar on Cocoa's main thread."""
    import AppKit
    if not _active(window):
        return
    if on_toggle is None:
        _remove_accessory(window)
        _mark_capability(window, False)
        _mark_identity(window, False)
        return
    try:
        image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_('sidebar.left', 'Toggle Sidebar')
        if image is None:
            return
        _remove_accessory(window)
        window._standalone_native_capability = None
        window._standalone_native_identity_capability = None
        window._standalone_window_commands_capability = None
        target = _delegate_type().alloc().init()
        target.callback = on_toggle
        controller = AppKit.NSTitlebarAccessoryViewController.alloc().init()
        view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (40, 38)))
        button = AppKit.NSButton.buttonWithImage_target_action_(image, target, 'toggleSidebar:')
        button.setBordered_(False)
        button.setToolTip_('Toggle Sidebar (⌘⇧S)')
        button.setAccessibilityLabel_('Toggle Sidebar')
        view.addSubview_(button)
        line = AppKit.NSBox.alloc().initWithFrame_(((0, 0), (1, 38)))
        line.setBoxType_(AppKit.NSBoxSeparator)
        view.addSubview_(line)
        controller.setView_(view)
        controller.setLayoutAttribute_(AppKit.NSLayoutAttributeLeft)
        toolbar = AppKit.NSToolbar.alloc().initWithIdentifier_('standalone.sidebar')
        window.native.setToolbar_(toolbar)
        window.native.setToolbarStyle_(AppKit.NSWindowToolbarStyleUnifiedCompact)
        window.native.setTitleVisibility_(AppKit.NSWindowTitleHidden)
        window.native.addTitlebarAccessoryViewController_(controller)
        identity_controller, identity_view, identity_label = _install_identity(window, identity)
        # AppKit delegate/targets can be weak; the Python window retains the complete native closure.
        accessory = SimpleNamespace(controller=controller, view=view, button=button, line=line,
                                    target=target, toolbar=toolbar, side='left', generation=0,
                                    identity_controller=identity_controller, identity_view=identity_view,
                                    identity_label=identity_label)
        window._standalone_sidebar_accessory = accessory
        binding = getattr(window, '_standalone_sidebar_presentation', None)
        if binding:
            _install_message_channel(window, binding)
            Thread(target=binding.loaded, name='native-sidebar-loaded', daemon=True).start()
    except Exception:
        logging.getLogger(__name__).exception('Native sidebar accessory unavailable; browser control remains visible')


def _install_identity(window, identity):
    """Place the service mark and trusted application name on the native row, after the sidebar."""
    import AppKit
    label_text = identity if isinstance(identity, str) and 0 < len(identity) <= 80 else str(window.native.title())[:80]
    controller = AppKit.NSTitlebarAccessoryViewController.alloc().init()
    label = AppKit.NSTextField.labelWithString_(label_text)
    label.setFont_(AppKit.NSFont.systemFontOfSize_weight_(12, AppKit.NSFontWeightSemibold))
    label.sizeToFit()
    text_width = float(label.frame().size.width)
    view = AppKit.NSView.alloc().initWithFrame_(((0, 0), (text_width + 46, 38)))
    from desktop_support.brand import icon_path
    mark_path = icon_path('mark.png')
    mark_image = AppKit.NSImage.alloc().initWithContentsOfFile_(str(mark_path)) if mark_path else None
    if mark_image is None:
        mark_image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_('checkmark.rectangle', None)
    if mark_image is not None:
        mark_image.setTemplate_(True)
    if mark_image is None:
        raise RuntimeError('Native application identity symbol is unavailable')
    mark_view = AppKit.NSImageView.alloc().initWithFrame_(((10, 10), (18, 18)))
    mark_view.setImage_(mark_image)
    mark_view.setAccessibilityLabel_(label_text)
    mark_view.setContentTintColor_(AppKit.NSColor.systemBlueColor())
    view.addSubview_(mark_view)
    label.setFrame_(((34, (38 - label.frame().size.height) / 2), (text_width, label.frame().size.height)))
    view.addSubview_(label)
    controller.setView_(view)
    controller.setLayoutAttribute_(AppKit.NSLayoutAttributeLeft)
    window.native.addTitlebarAccessoryViewController_(controller)
    return controller, view, label


def _queue_state(window, state):
    """Queue one validated presentation update without querying or touching AppKit from a worker."""
    if not _active(window):
        return
    from PyObjCTools import AppHelper
    binding = getattr(window, '_standalone_sidebar_presentation', None)
    generation = binding.document_generation if binding else None
    AppHelper.callAfter(_apply_presentation, window, state, generation)


def _apply_presentation(window, state, document_generation=None):
    """Move the owned accessory to its sidebar side; unsupported pages retain their HTML fallback."""
    import AppKit
    from PyObjCTools import AppHelper
    if not _active(window):
        return
    binding = getattr(window, '_standalone_sidebar_presentation', None)
    if binding and document_generation != binding.document_generation:
        return
    accessory = getattr(window, '_standalone_sidebar_accessory', None)
    if accessory is None:
        return
    if state is None or state['width'] > window.native.contentView().bounds().size.width:
        if getattr(accessory, 'window_commands', None):
            accessory.window_commands.close()
            accessory.window_commands = None
        _set_document_capability(window, False, 'native-window-commands', '_standalone_window_commands_capability')
        accessory.view.setHidden_(True)
        accessory.identity_view.setHidden_(True)
        _mark_identity(window, False)
        _mark_capability(window, False)
        return
    accessory.view.setHidden_(False)
    accessory.identity_view.setHidden_(False)
    if state['side'] != accessory.side:
        controllers = list(window.native.titlebarAccessoryViewControllers())
        window.native.removeTitlebarAccessoryViewControllerAtIndex_(controllers.index(accessory.controller))
        accessory.controller.setLayoutAttribute_(AppKit.NSLayoutAttributeLeft if state['side'] == 'left' else AppKit.NSLayoutAttributeRight)
        window.native.addTitlebarAccessoryViewController_(accessory.controller)
        accessory.side = state['side']
    accessory.generation += 1
    # AppKit settles a moved accessory's origin during its layout pass; stale passes are discarded.
    AppHelper.callLater(.05, _layout_accessory, window, state, accessory, accessory.generation, document_generation)


def _layout_accessory(window, state, expected_accessory, generation, document_generation):
    """Align the native separator with the web sidebar boundary, retaining safe collapsed system controls."""
    if not _active(window):
        return
    accessory = getattr(window, '_standalone_sidebar_accessory', None)
    binding = getattr(window, '_standalone_sidebar_presentation', None)
    if accessory is not expected_accessory or accessory.generation != generation:
        return
    if binding and document_generation != binding.document_generation:
        return
    frame = accessory.view.convertRect_toView_(accessory.view.bounds(), None)
    origin, height = float(frame.origin.x), float(frame.size.height)
    left = state['side'] == 'left'
    # A 48/56pt left rail is narrower than the OS traffic lights. Never draw through their cluster.
    controls_width = 100 if state.get('windowContext') else 40
    safe_collapsed = left and state['collapsed'] and state['width'] <= origin + controls_width
    if left and not state['collapsed'] and state['width'] < origin + controls_width:
        if getattr(accessory, 'window_commands', None):
            accessory.window_commands.close()
            accessory.window_commands = None
        _set_document_capability(window, False, 'native-window-commands', '_standalone_window_commands_capability')
        accessory.view.setHidden_(True)
        accessory.identity_view.setHidden_(True)
        _mark_identity(window, False)
        _mark_capability(window, False)
        return
    if safe_collapsed:
        width = controls_width
    elif left:
        width = state['width'] - origin
    else:
        width = state['width']
    accessory.view.setFrameSize_((width, height))
    accessory.button.setFrame_(((max(2, width - 36) if left else 8, (height - 28) / 2), (28, 28)))
    accessory.line.setFrame_(((width - 1 if left else 0, 0), (1, height)))
    accessory.line.setHidden_(safe_collapsed)
    accessory.state = dict(state)
    from .native_window_commands import apply_context
    try:
        commands_ready = apply_context(window, accessory, state)
    except Exception:
        logging.getLogger(__name__).exception('Native commands unavailable; retaining web fallback')
        commands_ready = False
    _set_document_capability(window, commands_ready, 'native-window-commands', '_standalone_window_commands_capability')
    _mark_capability(window, True)
    _mark_identity(window, True)


def _mark_capability(window, available):
    """Hide the browser fallback only after a valid measurement and successful native alignment."""
    _set_document_capability(window, available, 'native-toolbar', '_standalone_native_capability')


def _mark_identity(window, available):
    """Remove the duplicate HTML identity only when the native application identity exists."""
    _set_document_capability(window, available, 'native-identity', '_standalone_native_identity_capability')


def _set_document_capability(window, available, name, cache_name):
    """Update one trusted capability class after native installation, resetting its cache on every document."""
    if not _active(window) or getattr(window, cache_name, None) is available:
        return
    setattr(window, cache_name, available)
    action = 'add' if available else 'remove'
    script = f"document.documentElement.classList.{action}('{name}')"
    def update_document():
        """Never change classes in a document that navigated away from this window's owned origin."""
        binding = getattr(window, '_standalone_sidebar_presentation', None)
        if _active(window) and (binding is None or binding._same_origin()):
            window.run_js(script)
    Thread(target=_invoke, args=(update_document,), name='native-toolbar-capability', daemon=True).start()


def _delegate_type():
    """Register one native accessory action class per process, including page reloads."""
    global _delegate_class
    if _delegate_class is None:
        from Foundation import NSObject

        class StandaloneSidebarAccessoryTarget(NSObject):
            """Keep the titlebar button's fixed application action outside the Cocoa event loop."""

            def toggleSidebar_(self, sender):
                """Run the same trusted action used by the menu shortcut, without exposing it to page content."""
                Thread(target=_invoke, args=(self.callback,), name='native-sidebar-toggle', daemon=True).start()

        _delegate_class = StandaloneSidebarAccessoryTarget
    return _delegate_class


def _invoke(callback):
    """Keep native UI callback failures observable during ordinary operation and teardown."""
    try:
        callback()
    except Exception:
        logging.getLogger(__name__).exception('Native sidebar action failed')


def _install_message_channel(window, binding):
    """Register one null-only public WKScriptMessageHandler, avoiding pywebview's eval-based expose generator."""
    if getattr(binding, 'message_handler', None) is not None:
        return
    controller = window.native.contentView().configuration().userContentController()
    handler = _message_type().alloc().init()
    handler.binding = binding
    controller.addScriptMessageHandler_name_(handler, 'standaloneSidebar')
    binding.message_handler, binding.message_controller = handler, controller

    def remove():
        """Remove only this owned channel on Cocoa's thread; closed bindings reject already queued messages."""
        from PyObjCTools import AppHelper
        if current_thread() is main_thread():
            _remove_message_channel(binding)
        else:
            AppHelper.callAfter(_remove_message_channel, binding)
    binding.native_cleanup = remove


def _remove_message_channel(binding):
    """Release the owned message handler without touching a window or delaying native Quit."""
    controller = getattr(binding, 'message_controller', None)
    if controller is not None:
        controller.removeScriptMessageHandlerForName_('standaloneSidebar')
        binding.message_controller = None
        binding.message_handler = None


def _message_type():
    """Register one Objective-C message adapter with exact frame-origin validation."""
    global _message_class
    if _message_class is None:
        from Foundation import NSObject, NSNull
        from .native_presentation import accepts_notification

        class StandaloneSidebarMessageHandler(NSObject):
            """Accept only harmless null notifications from the owned top-level document."""
            def userContentController_didReceiveScriptMessage_(self, controller, message):
                """Validate real WebKit frame metadata before requesting a fixed native DOM measurement."""
                binding = self.binding
                if not binding.active() or message.name() != 'standaloneSidebar':
                    return
                frame = message.frameInfo()
                origin = frame.securityOrigin()
                source = (str(origin.protocol()), str(origin.host()), int(origin.port()))
                body = message.body()
                if accepts_notification(binding.origin, source, bool(frame.isMainFrame()), body is None or body == NSNull.null()):
                    binding.notify()

        _message_class = StandaloneSidebarMessageHandler
    return _message_class
