"""Public native titlebar adapter for the shared window-shell contract; no caller-supplied commands cross the bridge."""

import json
from threading import Thread

COMMANDS = ('back','forward','primary','refresh','settings','theme','search')
SYMBOLS = {'back':'chevron.left','forward':'chevron.right','primary':'plus','refresh':'arrow.clockwise','settings':'gearshape','theme':'circle.lefthalf.filled','search':'magnifyingglass'}
_target_type = None


def validate_context(value):
    """Accept only bounded presentation text and Boolean states for a fixed command vocabulary."""
    if not isinstance(value, dict) or set(value) != {'title','commands'}:
        return None
    title, commands = value['title'], value['commands']
    if not isinstance(title, str) or not 1 <= len(title) <= 256 or any(ord(char) < 32 for char in title) or not isinstance(commands, dict) or set(commands) - set(COMMANDS):
        return None
    for command in commands.values():
        if not isinstance(command, dict) or set(command) != {'enabled','hidden','label'} or type(command['enabled']) is not bool or type(command['hidden']) is not bool or not isinstance(command['label'], str) or len(command['label']) > 80:
            return None
    return {'title': title, 'commands': {key:dict(item) for key,item in commands.items()}}


def context_script():
    """Pull fixed DOM presentation; JSON strings are values, never evaluated as application code."""
    return """(() => {
      const title=document.documentElement.dataset.agWindowTitle;if(!title)return null;
      const commands={};for(const key of ['back','forward','primary','refresh','settings','theme','search']){
        const button=document.querySelector('[data-ag-window-command="'+key+'"]');if(!button)continue;
        commands[key]={enabled:button.disabled!==true,hidden:button.hidden===true||!!button.closest('[hidden]'),label:(button.getAttribute('aria-label')||button.title||button.textContent||key).trim().slice(0,80)};
      }return {title,commands};
    })()"""


def invoke(window, key):
    """Activate only one existing local UI command after the exact owned origin/lifecycle fence passes."""
    binding = getattr(window, '_standalone_sidebar_presentation', None)
    if key not in COMMANDS or binding is None or not binding.active() or not binding._same_origin():
        return
    selector = json.dumps('[data-ag-window-command="' + key + '"]')
    window.run_js('(() => {const button=document.querySelector(' + selector + ');if(button&&!button.disabled&&!button.hidden)button.click();})()')


def target_type():
    """Retain a single AppKit target class whose selectors only dispatch the fixed command set."""
    global _target_type
    if _target_type is None:
        from Foundation import NSObject
        class AGWindowCommandTarget(NSObject):
            def runCommand_(self, sender):
                """Keep WebView evaluation outside the Cocoa event loop."""
                key = str(sender.identifier())
                Thread(target=invoke, args=(self.window, key), name='window-command', daemon=True).start()
        _target_type = AGWindowCommandTarget
    return _target_type


class NativeCommands:
    """Retain actual titlebar accessories while preserving existing traffic lights and Atlas navigation."""

    def __init__(self, window, accessory):
        """Create one trailing command group; controls stay hidden until valid context is pulled."""
        import AppKit
        self.window, self.accessory = window, accessory
        self.target = target_type().alloc().init()
        self.target.window = window
        self.controller = AppKit.NSTitlebarAccessoryViewController.alloc().init()
        self.view = AppKit.NSView.alloc().initWithFrame_(((0,0),(1,38)))
        self.controller.setView_(self.view)
        self.controller.setLayoutAttribute_(AppKit.NSLayoutAttributeRight)
        window.native.addTitlebarAccessoryViewController_(self.controller)
        self.buttons = {}
        for key in COMMANDS:
            if key in ('back','forward') and getattr(accessory, 'navigation', None):
                continue
            image = AppKit.NSImage.imageWithSystemSymbolName_accessibilityDescription_(SYMBOLS[key], key)
            if image is None:
                continue
            button = AppKit.NSButton.buttonWithImage_target_action_(image, self.target, 'runCommand:')
            button.setIdentifier_(key)
            button.setBordered_(False)
            (accessory.view if key in ('back','forward') else self.view).addSubview_(button)
            self.buttons[key] = button

    def update(self, context, state):
        """Publish contextual title/actions only when every advertised command has a native representation."""
        import AppKit
        advertised = {key for key,item in context['commands'].items() if not item['hidden']}
        represented = set(self.buttons) | ({'back','forward'} if getattr(self.accessory,'navigation',None) else set())
        if advertised - represented:
            return False
        left = state['side'] == 'left'
        parent = self.accessory.view if left else self.accessory.identity_view
        for index,key in enumerate(('back','forward')):
            if key not in self.buttons:
                continue
            button = self.buttons[key]
            parent.addSubview_(button)
            button.setFrame_(((index*32+2,5),(28,28)))
        right_index = 0
        for key, button in self.buttons.items():
            value = context['commands'].get(key)
            hidden = value is None or value['hidden']
            button.setHidden_(hidden)
            button.setEnabled_(bool(value and value['enabled']))
            if value:
                button.setToolTip_(value['label'])
                button.setAccessibilityLabel_(value['label'])
            if key not in ('back','forward') and not hidden:
                button.setFrame_(((right_index*32,5),(28,28)))
                right_index += 1
        self.view.setFrameSize_((max(1,right_index*32),38))
        self.view.setHidden_(right_index == 0)
        extra = 64 if not left and not getattr(self.accessory,'navigation',None) else 0
        label = self.accessory.identity_label
        label.setStringValue_(context['title'])
        label.setAccessibilityLabel_(context['title'])
        label.sizeToFit()
        width = max(70,min(float(label.frame().size.width),float(self.window.native.contentView().bounds().size.width)-state['width']-right_index*32-170,360))
        label.setLineBreakMode_(AppKit.NSLineBreakByTruncatingTail)
        label.setFrame_(((34+extra,(38-label.frame().size.height)/2),(width,label.frame().size.height)))
        self.accessory.identity_view.setFrameSize_((width+46+extra,38))
        for child in self.accessory.identity_view.subviews():
            if child.isKindOfClass_(AppKit.NSImageView):
                child.setFrameOrigin_((10+extra,10))
        self.window.native.setTitle_(context['title'])
        return True

    def close(self):
        """Remove only this retained accessory and its target; app lifecycle owns all other resources."""
        controllers = list(self.window.native.titlebarAccessoryViewControllers())
        if self.controller in controllers:
            self.window.native.removeTitlebarAccessoryViewControllerAtIndex_(controllers.index(self.controller))
        for button in self.buttons.values():
            button.removeFromSuperview()
        self.buttons.clear()


def apply_context(window, accessory, state):
    """Install lazily after a valid same-origin measurement; return false to retain browser fallback on failure."""
    context = state.get('windowContext')
    if not context:
        return False
    if not getattr(accessory, 'window_commands', None):
        accessory.window_commands = NativeCommands(window, accessory)
    return accessory.window_commands.update(context, state)
