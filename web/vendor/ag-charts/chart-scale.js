/** Expand constant domains without dividing by zero; bar zero remains visible even for all-negative data. */
export function chartDomain(values, min, max) {
  let low = min ?? Math.min(0, ...values),
    high = max ?? Math.max(0, ...values);
  if (low === high) {
    low -= 1;
    high += 1;
  }
  return [low, high];
}
