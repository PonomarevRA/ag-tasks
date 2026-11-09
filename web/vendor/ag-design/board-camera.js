/** Bound persisted camera values; malformed storage never reaches CSS transforms. */
export function normalizeBoardState(value = {}) {
  const finite = (n, fallback, low, high) => Number.isFinite(n) ? Math.max(low, Math.min(high, n)) : fallback;
  return {
    x: finite(value?.x, 24, -1000000, 1000000),
    y: finite(value?.y, 24, -1000000, 1000000),
    scale: finite(value?.scale, 1, .2, 2.5),
    color: /^#[0-9a-f]{6}$/i.test(value?.color) ? value.color : '',
    pattern: ['none', 'dots', 'grid'].includes(value?.pattern) ? value.pattern : 'dots',
  };
}

/** Keep the world point under the cursor stationary while changing scale. Coordinates are viewport CSS pixels. */
export function zoomBoardAt(state, factor, point) {
  const scale = normalizeBoardState({...state, scale: state.scale * factor}).scale;
  return normalizeBoardState({...state, scale,
    x: point.x - (point.x - state.x) * scale / state.scale,
    y: point.y - (point.y - state.y) * scale / state.scale});
}

/** Fit finite scene bounds inside the viewport; padding reserves room for fixed controls. */
export function fitBoardState(state, viewport, scene) {
  if (!viewport.width || !viewport.height || !scene.width || !scene.height) return state;
  const scale = Math.max(.2, Math.min(1, (viewport.width - 48) / scene.width, (viewport.height - 88) / scene.height));
  return normalizeBoardState({...state, scale, x: Math.max(24, (viewport.width - scene.width * scale) / 2), y:24});
}
