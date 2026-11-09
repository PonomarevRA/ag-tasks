/** Own one finite SVG entrance and selection pulse; reduced motion and disposal cancel only these animations. */
export function chartMotion({ element, group, selection, animated = true }) {
  const view = element.ownerDocument.defaultView,
    preference = view?.matchMedia?.("(prefers-reduced-motion: reduce)");
  let entrance = null,
    pulse = null,
    disposed = false;
  /** Cancel owned effects without retaining fill styles or hiding the final supplied data. */
  function stop() {
    entrance?.cancel();
    pulse?.cancel();
    entrance = pulse = null;
    element.removeAttribute("data-animating");
  }
  /** Replay a visible 700ms reveal; animation support is optional and final content is always already in the DOM. */
  function play() {
    if (disposed) return false;
    stop();
    if (!animated || preference?.matches || typeof group.animate !== "function")
      return false;
    element.dataset.animating = "true";
    entrance = group.animate(
      [
        { opacity: 0.12, transform: "translateY(14px) scaleY(.78)" },
        { opacity: 1, transform: "translateY(0) scaleY(1)" },
      ],
      { duration: 700, easing: "cubic-bezier(.2,.7,.2,1)", fill: "none" },
    );
    const owned = entrance;
    owned.finished.then(
      () => {
        if (entrance === owned) {
          entrance = null;
          element.removeAttribute("data-animating");
        }
      },
      () => {},
    );
    return true;
  }
  /** A short selection pulse makes the changed mark visible without another chart entrance. */
  function selected() {
    if (
      disposed ||
      !animated ||
      preference?.matches ||
      typeof selection.animate !== "function"
    )
      return;
    pulse?.cancel();
    pulse = selection.animate([{ opacity: 0.3 }, { opacity: 1 }], {
      duration: 180,
      fill: "none",
    });
    pulse.finished.catch(() => {});
  }
  const changed = () => {
    if (preference.matches) stop();
  };
  preference?.addEventListener?.("change", changed);
  return {
    play,
    selected,
    dispose() {
      if (disposed) return;
      stop();
      disposed = true;
      preference?.removeEventListener?.("change", changed);
    },
  };
}
