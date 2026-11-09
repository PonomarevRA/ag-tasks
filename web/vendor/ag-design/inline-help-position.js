const clamp = (value, minimum, maximum) =>
  Math.max(minimum, Math.min(value, maximum));

/** Choose a viewport-bounded side before applying any DOM writes; narrow screens prefer vertical placement. */
export function helpPlacement(
  trigger,
  { width, height },
  viewport,
  preferred = "auto",
) {
  const margin = 12,
    gap = 10;
  const panelWidth = Math.max(1, Math.min(width, viewport.width - margin * 2));
  const panelHeight = Math.max(
    1,
    Math.min(height, viewport.height - margin * 2),
  );
  const right = viewport.width - trigger.right - gap - margin,
    left = trigger.left - gap - margin;
  let side = "bottom";
  if (preferred !== "bottom" && right >= panelWidth) side = "right";
  else if (preferred !== "bottom" && left >= panelWidth) side = "left";
  else if (
    viewport.height - trigger.bottom - gap < panelHeight &&
    trigger.top > viewport.height - trigger.bottom
  )
    side = "top";
  const availableHeight =
    side === "bottom"
      ? Math.max(1, viewport.height - trigger.bottom - gap - margin)
      : side === "top"
        ? Math.max(1, trigger.top - gap - margin)
        : Math.max(1, viewport.height - margin * 2);
  const visibleHeight = Math.min(panelHeight, availableHeight);
  let x = clamp(trigger.left, margin, viewport.width - margin - panelWidth),
    y = trigger.bottom + gap;
  if (side === "right") {
    x = trigger.right + gap;
    y = trigger.top + trigger.height / 2 - visibleHeight / 2;
  }
  if (side === "left") {
    x = trigger.left - gap - panelWidth;
    y = trigger.top + trigger.height / 2 - visibleHeight / 2;
  }
  if (side === "top") y = trigger.top - gap - visibleHeight;
  y = clamp(y, margin, viewport.height - margin - visibleHeight);
  return {
    side,
    x,
    y,
    width: panelWidth,
    maxHeight: Math.max(1, availableHeight - 2),
    height: visibleHeight,
    arrow: clamp(
      trigger.top + trigger.height / 2 - y,
      10,
      Math.max(10, visibleHeight - 10),
    ),
  };
}

/** Position a help note in the top layer, with a theme-preserving body portal fallback for older renderers. */
export function helpPositioner({
  document,
  element,
  trigger,
  panel,
  preferred,
  restoreParent = element,
  maxWidth = 360,
  fitContent = false,
}) {
  const view = document.defaultView;
  let active = false,
    ported = false,
    observer;
  /** Copy the trigger’s current semantic tokens even when its top-layer panel belongs to a separate container. */
  function place() {
    if (!active) return;
    if (view?.getComputedStyle) {
      const style = view.getComputedStyle(element);
      for (const key of [
        "accent",
        "on-accent",
        "surface",
        "surface-alt",
        "text",
        "muted",
        "border",
        "font",
        "layer-popup",
      ]) {
        const value = style.getPropertyValue("--ag-" + key);
        if (value) panel.style.setProperty("--ag-" + key, value);
        else panel.style.removeProperty("--ag-" + key);
      }
    }
    const viewport = {
      width: view?.innerWidth || 1024,
      height: view?.innerHeight || 768,
    };
    const widthLimit = Math.min(maxWidth, Math.max(1, viewport.width - 24));
    panel.style.maxWidth = widthLimit + "px";
    panel.style.width = fitContent ? "max-content" : widthLimit + "px";
    panel.style.removeProperty("--ag-help-max-height");
    const measuredWidth = fitContent
      ? panel.getBoundingClientRect().width
      : widthLimit;
    const result = helpPlacement(
      trigger.getBoundingClientRect(),
      {
        width: Math.min(widthLimit, measuredWidth || widthLimit),
        height: panel.getBoundingClientRect().height || panel.scrollHeight || 1,
      },
      viewport,
      preferred,
    );
    panel.dataset.side = result.side;
    panel.style.left = result.x + "px";
    panel.style.top = result.y + "px";
    panel.style.width = result.width + "px";
    panel.style.setProperty("--ag-help-max-height", result.maxHeight + "px");
    panel.style.setProperty("--ag-help-arrow", result.arrow + "px");
  }
  /** Open one layer and listen only while it is visible, including ancestor theme/profile changes. */
  function start() {
    active = true;
    if (typeof panel.showPopover === "function" && panel.isConnected) {
      panel.setAttribute("popover", "manual");
      panel.showPopover();
    } else {
      document.body.append(panel);
      ported = true;
    }
    place();
    view?.addEventListener("resize", place);
    document.addEventListener("scroll", place, true);
    if (view?.MutationObserver) {
      observer = new view.MutationObserver(place);
      for (let parent = element; parent; parent = parent.parentElement)
        observer.observe(parent, {
          attributes: true,
          attributeFilter: ["style", "class", "data-theme", "data-palette"],
        });
    }
  }
  /** Close the layer and restore DOM ownership before removing listeners; repeated cleanup is safe. */
  function stop() {
    active = false;
    view?.removeEventListener("resize", place);
    document.removeEventListener("scroll", place, true);
    observer?.disconnect();
    observer = undefined;
    if (
      typeof panel.hidePopover === "function" &&
      panel.hasAttribute("popover")
    ) {
      try {
        panel.hidePopover();
      } catch {
        /* The host may already have removed the note's top layer. */
      }
    }
    if (ported) {
      restoreParent.append(panel);
      ported = false;
    }
  }
  return { start, stop, refresh: place };
}
