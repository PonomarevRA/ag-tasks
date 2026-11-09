import { particleRandom } from "./chart-particle-targets.js";
/** Own positions/velocities in fixed typed arrays; spring motion never mutates target/data coordinates. */
export function createParticleField(targets, previous) {
  const count = targets.length,
    random = particleRandom(9182);
  const positions = new Float32Array(count * 2),
    velocity = new Float32Array(count * 2),
    delay = new Float32Array(count);
  let elapsed = 0;
  /** Start from the displayed previous field, or condense a staggered cloud around each target on replay. */
  function reset(scatter = false) {
    elapsed = 0;
    for (let i = 0; i < count; i++) {
      const p = targets[i],
        old =
          !scatter && previous?.length
            ? Math.floor((i * previous.length) / (count * 2)) * 2
            : -1;
      positions[i * 2] =
        old >= 0 ? previous[old] : p.x + (random() - 0.5) * 180;
      positions[i * 2 + 1] =
        old >= 0 ? previous[old + 1] : p.y + (random() - 0.5) * 150;
      velocity[i * 2] = velocity[i * 2 + 1] = 0;
      delay[i] = random() * 360;
    }
  }
  /** Advance at most 32ms per step; force a literal settled frame after 2s, including on slow devices. */
  function advance(dt) {
    if (elapsed >= 2000) return true;
    elapsed += dt;
    const step = Math.min(32, dt) / 16.667,
      damping = Math.pow(0.76, step);
    for (let i = 0; i < count; i++) {
      if (elapsed < delay[i]) continue;
      const j = i * 2,
        p = targets[i];
      velocity[j] =
        (velocity[j] + (p.x - positions[j]) * 0.075 * step) * damping;
      velocity[j + 1] =
        (velocity[j + 1] + (p.y - positions[j + 1]) * 0.075 * step) * damping;
      positions[j] += velocity[j] * step;
      positions[j + 1] += velocity[j + 1] * step;
    }
    if (elapsed >= 2000) settle();
    return elapsed >= 2000;
  }
  /** Reduced motion, pause and offscreen suspension expose exact final targets without a latent flight. */
  function settle() {
    elapsed = 2000;
    for (let i = 0; i < count; i++) {
      positions[i * 2] = targets[i].x;
      positions[i * 2 + 1] = targets[i].y;
    }
  }
  reset();
  return {
    positions,
    delay,
    advance,
    reset,
    settle,
    get elapsed() {
      return elapsed;
    },
    snapshot: () => positions.slice(),
  };
}
