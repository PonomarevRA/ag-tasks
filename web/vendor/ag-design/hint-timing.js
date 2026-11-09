export const defaultHoverDelayMs = 250;

/** Validate bounded millisecond delays; zero explicitly opts into immediate disclosure. */
export function validateHintDelay(value, name = "hoverDelayMs") {
  if (!Number.isInteger(value) || value < 0 || value > 5000)
    throw new RangeError(`${name} must be an integer from0 through5000ms`);
  return value;
}
