import { defaultHoverDelayMs, validateHintDelay } from "./hint-timing.js";

/**
 * Share cancellable hover/focus timing and layer ownership between a tooltip and an i-disclosure.
 * Only disclosures pin on click; short action hints dismiss on activation. No polling is installed.
 */
export function bindHintBehavior({
  trigger,
  panel,
  position,
  disclosure = false,
  hoverDelayMs = defaultHoverDelayMs,
  focusDelayMs = 0,
}) {
  validateHintDelay(hoverDelayMs);
  validateHintDelay(focusDelayMs, "focusDelayMs");
  const document = trigger.ownerDocument;
  let open = false,
    disposed = false,
    pinned = false;
  let hovered = false,
    panelHovered = false,
    focused = false;
  let dismissed = false,
    restoring = false,
    showTimer,
    hideTimer;

  /** Keep a host's other accessible descriptions when this hint appears or retires. */
  function describe(enabled) {
    const references = (trigger.getAttribute("aria-describedby") ?? "")
      .split(/\s+/)
      .filter(Boolean);
    const next = references.filter((id) => id !== panel.id);
    if (enabled) next.push(panel.id);
    if (next.length) trigger.setAttribute("aria-describedby", next.join(" "));
    else trigger.removeAttribute("aria-describedby");
  }

  /** Invalidated pending work cannot reopen a hidden, detached or disabled trigger. */
  function available() {
    return (
      !disposed &&
      !trigger.matches(":disabled,[aria-disabled=true]") &&
      !trigger.closest("[hidden],[inert]")
    );
  }

  /** Retire both directions before an explicit state transition or lifecycle change. */
  function cancelTimers() {
    clearTimeout(showTimer);
    clearTimeout(hideTimer);
    showTimer = hideTimer = undefined;
  }

  /** Own document listeners and top-layer/portal resources only while actually open. */
  function publish(value) {
    const next = Boolean(value) && available();
    if (!next) panelHovered = false;
    if (next === open) return;
    open = next;
    panel.hidden = !open;
    if (disclosure) trigger.setAttribute("aria-expanded", String(open));
    describe(open);
    if (open) {
      position.start();
      document.addEventListener("keydown", escape);
      document.addEventListener("pointerdown", outside);
      document.addEventListener("focusin", outside);
    } else {
      position.stop();
      document.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", outside);
      document.removeEventListener("focusin", outside);
    }
  }

  /** Explicit disclosure works immediately; focus restoration must not schedule a reopen after Escape. */
  function setOpen(value, { restoreFocus = false } = {}) {
    if (disposed) return;
    cancelTimers();
    pinned = Boolean(value) && disclosure;
    dismissed = !value;
    publish(value);
    if (restoreFocus) {
      restoring = true;
      try {
        trigger.focus();
      } finally {
        restoring = false;
      }
    }
  }

  /** Keyboard focus is immediate by default; pointer hover waits for its configured dwell. */
  function scheduleShow(delay) {
    clearTimeout(hideTimer);
    if (open || dismissed || !available()) return;
    clearTimeout(showTimer);
    const show = () => {
      showTimer = undefined;
      if (!dismissed && (hovered || focused) && trigger.isConnected)
        publish(true);
    };
    if (delay === 0) show();
    else showTimer = setTimeout(show, delay);
  }

  /** A short fixed exit grace lets the pointer cross the10px gap into the hoverable note. */
  function scheduleHide() {
    clearTimeout(showTimer);
    if (!open || hovered || panelHovered || focused || pinned) return;
    clearTimeout(hideTimer);
    hideTimer = setTimeout(() => {
      hideTimer = undefined;
      if (!hovered && !panelHovered && !focused && !pinned) publish(false);
    }, 100);
  }

  /** Ignore touch hover emulation; only a new pointer visit clears Escape dismissal. */
  function enter(event) {
    if (event.pointerType === "touch") return;
    hovered = true;
    dismissed = false;
    scheduleShow(hoverDelayMs);
  }
  /** Leaving cancels pending dwell before considering a visible panel exit. */
  function leave() {
    hovered = false;
    scheduleHide();
  }
  /** The visible note stays reachable while crossing the trigger/panel gap. */
  function enterPanel() {
    panelHovered = true;
    clearTimeout(hideTimer);
  }
  /** A pinned or keyboard-focused explanation survives pointer departure. */
  function leavePanel() {
    panelHovered = false;
    scheduleHide();
  }
  /** A real keyboard focus visit reveals the description without depending on hover. */
  function focus() {
    focused = true;
    if (restoring) return;
    dismissed = false;
    scheduleShow(focusDelayMs);
  }
  /** Retire pending focus work before an outside focusin or exit grace runs. */
  function blur() {
    focused = false;
    scheduleHide();
  }

  /** First activation pins an automatically shown i-note; another closes it. Ordinary action hints never pin. */
  function activate() {
    if (disclosure) setOpen(!(open && pinned));
    else setOpen(false);
  }

  function inside(target) {
    return trigger.contains(target) || panel.contains(target);
  }

  /** Dismiss a focused or pointer-hovered hint without consuming Escape for an unrelated pinned disclosure. */
  function escape(event) {
    if (event.key !== "Escape" || event.defaultPrevented) return;
    const targeted = inside(event.target);
    if (!targeted && !(hovered && !pinned)) return;
    event.preventDefault();
    event.stopPropagation();
    setOpen(false, { restoreFocus: targeted && disclosure });
  }

  /** Outside navigation closes without moving the new pointer/keyboard target. */
  function outside(event) {
    if (!inside(event.target)) setOpen(false);
  }

  trigger.addEventListener("pointerenter", enter);
  trigger.addEventListener("pointerleave", leave);
  trigger.addEventListener("focus", focus);
  trigger.addEventListener("blur", blur);
  trigger.addEventListener("click", activate);
  panel.addEventListener("pointerenter", enterPanel);
  panel.addEventListener("pointerleave", leavePanel);
  const observer = document.defaultView?.MutationObserver
    ? new document.defaultView.MutationObserver(() => {
        if (!available()) setOpen(false);
      })
    : null;
  observer?.observe(trigger, {
    attributes: true,
    attributeFilter: ["disabled", "aria-disabled", "hidden"],
  });

  return {
    setOpen,
    get isOpen() {
      return open;
    },
    /** Change timing atomically; an older pending timer cannot win after a configuration update. */
    setDelays({
      hoverDelayMs: hover = hoverDelayMs,
      focusDelayMs: focus = focusDelayMs,
    } = {}) {
      validateHintDelay(hover);
      validateHintDelay(focus, "focusDelayMs");
      if (hover === hoverDelayMs && focus === focusDelayMs) return;
      cancelTimers();
      hoverDelayMs = hover;
      focusDelayMs = focus;
      if (focused || hovered)
        scheduleShow(focused ? focusDelayMs : hoverDelayMs);
    },
    /** Content/locale changes can reposition an already visible panel without reopening it. */
    refresh() {
      if (open) position.refresh?.();
    },
    /** Idempotent cleanup includes pending dwell/exit timers, observers and external layer listeners. */
    dispose() {
      if (disposed) return;
      setOpen(false);
      disposed = true;
      observer?.disconnect();
      trigger.removeEventListener("pointerenter", enter);
      trigger.removeEventListener("pointerleave", leave);
      trigger.removeEventListener("focus", focus);
      trigger.removeEventListener("blur", blur);
      trigger.removeEventListener("click", activate);
      panel.removeEventListener("pointerenter", enterPanel);
      panel.removeEventListener("pointerleave", leavePanel);
      position.stop();
    },
  };
}
