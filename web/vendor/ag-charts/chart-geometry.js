import { asciiChartTargets } from "./chart-ascii-targets.js";
import { sampleParticleTargets } from "./chart-particle-targets.js";
import { makeChartScene } from "./chart-scene.js";
import { pieScene, radarScene } from "./chart-radial.js";
import { cartesianScene } from "./chart-cartesian.js";
export { validateChart, chartTypes } from "./chart-validation.js";
export { chartDomain } from "./chart-scale.js";
/** Compute one chart from validated supplied data; geometry is independent of browser layout and external services. */
export function chartScene(state) {
  const scene = makeChartScene();
  if (state.type === "pie" || state.type === "donut") pieScene(state, scene);
  else if (state.type === "radar") radarScene(state, scene);
  else cartesianScene(state, scene);
  if (state.renderer === "particles")
    scene.particles = sampleParticleTargets(scene.regions, state);
  if (state.renderer === "ascii")
    scene.particles = asciiChartTargets(scene.regions, state);
  return scene;
}
