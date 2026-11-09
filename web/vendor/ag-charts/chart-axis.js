const day = 86400000;
const dateLimit = 8640000000000000;

/** Validate explicit epoch milliseconds; ISO-only auto detection never guesses localized category text. */
export function axisDateValues(labels, explicit) {
  if (explicit === false) return null;
  if (explicit !== undefined) {
    if (!Array.isArray(explicit) || explicit.length !== labels.length)
      throw new TypeError("One axis date per label required");
    for (const value of explicit)
      if (!Number.isFinite(value) || Math.abs(value) > dateLimit)
        throw new RangeError(
          "Axis dates must be epoch milliseconds in the Date range",
        );
    return [...explicit];
  }
  if (!labels.length) return null;
  const values = [];
  for (const label of labels) {
    if (
      !/^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?(?:Z|[+-]\d{2}:\d{2}))?$/.test(
        label,
      )
    )
      return null;
    const value = Date.parse(label);
    // Date.parse normalizes impossible days. Check the calendar portion separately.
    const calendar = Date.parse(label.slice(0, 10));
    if (
      !Number.isFinite(value) ||
      !Number.isFinite(calendar) ||
      new Date(calendar).toISOString().slice(0, 10) !== label.slice(0, 10)
    )
      return null;
    values.push(value);
  }
  return values;
}

/** Choose the smallest useful time unit from the visible ticks, independently of their data coordinates. */
export function timeAxisUnit(values) {
  let step = Infinity;
  for (let i = 1; i < values.length; i++) {
    const delta = Math.abs(values[i] - values[i - 1]);
    if (delta > 0) step = Math.min(step, delta);
  }
  if (!Number.isFinite(step)) return "day";
  if (step >= 330 * day) return "year";
  if (step >= 28 * day) return "month";
  if (step >= day) return "day";
  if (step >= 60000) return "minute";
  if (step >= 1000) return "second";
  return "millisecond";
}

/** Make one local formatter; context is compared with the last retained tick, never a hidden neighbor. */
function tickFormatter(
  values,
  { locale = "ru-RU", timeZone = "UTC", unit = "auto" } = {},
) {
  const granularity = unit === "auto" ? timeAxisUnit(values) : unit;
  if (
    !["year", "month", "day", "minute", "second", "millisecond"].includes(
      granularity,
    )
  )
    throw new TypeError("Unknown time-axis unit");
  const date = new Intl.DateTimeFormat(locale, {
    timeZone,
    calendar: "gregory",
    year: "numeric",
    month: "short",
    day: "2-digit",
  });
  const clock = new Intl.DateTimeFormat(locale, {
    timeZone,
    hourCycle: "h23",
    hour: "2-digit",
    minute: "2-digit",
    ...(granularity === "second" || granularity === "millisecond"
      ? { second: "2-digit" }
      : {}),
    ...(granularity === "millisecond" ? { fractionalSecondDigits: 3 } : {}),
  });
  const offset = new Intl.DateTimeFormat("en", {
    timeZone,
    timeZoneName: "shortOffset",
  });
  function parts(value) {
    const result = {};
    for (const part of date.formatToParts(value))
      result[part.type] = part.value;
    return result;
  }
  return (value, previous) => {
    if (!Number.isFinite(value) || Math.abs(value) > dateLimit)
      throw new RangeError("Valid epoch milliseconds required");
    const current = parts(value),
      before = previous === undefined ? null : parts(previous);
    const yearChanged = !before || current.year !== before.year;
    const monthChanged = yearChanged || current.month !== before.month;
    const dayChanged = monthChanged || current.day !== before.day;
    let label,
      context = "";
    if (granularity === "year") label = current.year;
    else if (granularity === "month") {
      label = current.month;
      if (yearChanged) context = current.year;
    } else if (granularity === "day") {
      label = current.day;
      context = [
        monthChanged ? current.month : "",
        yearChanged ? current.year : "",
      ]
        .filter(Boolean)
        .join(" ");
    } else {
      label = clock.format(value);
      if (dayChanged)
        context = [
          current.day,
          monthChanged ? current.month : "",
          yearChanged ? current.year : "",
        ]
          .filter(Boolean)
          .join(" ");
      // Repeated wall-clock times across a DST fold must remain distinguishable.
      if (before && label === clock.format(previous) && value !== previous) {
        const zone = offset
          .formatToParts(value)
          .find((part) => part.type === "timeZoneName").value;
        label += " " + zone;
      }
    }
    return {
      value,
      label,
      context,
      lines: context ? [label, context] : [label],
      fullLabel: new Date(value).toISOString(),
    };
  };
}

/** Format an ordered visible list with one initial calendar context and new context at period boundaries. */
export function formatTimeAxisTicks(values, options = {}) {
  const format = tickFormatter(values, options),
    result = [];
  let previous;
  for (const value of values) {
    result.push(format(value, previous));
    previous = value;
  }
  return result;
}

/** Fit each text line independently, keeping the short date nearest its actual tick coordinate. */
function placeTick(tick, position, left, right, measure) {
  const placed = [];
  for (const line of tick.lines) {
    const width = measure(line);
    if (!Number.isFinite(width) || width > right - left) return null;
    const start = Math.max(left, Math.min(position - width / 2, right - width));
    placed.push({ x: start + width / 2, start, end: start + width });
  }
  const main = placed[0],
    context = placed[1] || main;
  return {
    ...tick,
    x: main.x,
    contextX: context.x,
    position,
    anchor: "middle",
    start: Math.min(main.start, context.start),
    end: Math.max(main.end, context.end),
  };
}

/** Fit horizontal labels, preferring both visible endpoints when they fit; observations are untouched. */
export function layoutAxisTicks({
  values,
  labels = [],
  positions,
  left,
  right,
  measure = (text) => Array.from(text).length * 8,
  gap = 12,
  singleLine = false,
  ...options
}) {
  if (
    !Array.isArray(positions) ||
    positions.length > 4096 ||
    !Number.isFinite(left) ||
    !Number.isFinite(right) ||
    right < left ||
    !Number.isFinite(gap) ||
    gap < 0
  )
    throw new RangeError(
      "Bounded positions, finite bounds and a nonnegative gap required",
    );
  if (
    (values && values.length !== positions.length) ||
    (!values && labels.length !== positions.length)
  )
    throw new RangeError("One date or label per position required");
  for (let i = 0; i < positions.length; i++)
    if (
      !Number.isFinite(positions[i]) ||
      (i > 0 && positions[i] < positions[i - 1])
    )
      throw new RangeError("Finite ascending tick positions required");
  const format = values ? tickFormatter(values, options) : null;
  const result = [];
  for (let index = 0; index < positions.length; index++) {
    // A last tick may replace nearby minor labels. Reformat after each removal,
    // otherwise a removed year boundary could leave the endpoint without its year.
    while (true) {
      const previous = result.at(-1);
      const tick = format
        ? format(values[index], previous ? values[previous.index] : undefined)
        : {
            label: labels[index],
            context: "",
            lines: [labels[index]],
            fullLabel: labels[index],
          };
      if (singleLine && tick.context)
        tick.lines = [tick.label + " " + tick.context];
      const placed = placeTick(tick, positions[index], left, right, measure);
      if (!placed) break;
      if (previous && placed.start < previous.end + gap) {
        if (index === positions.length - 1 && result.length > 1) {
          result.pop();
          continue;
        }
        break;
      }
      result.push({ ...placed, index });
      break;
    }
  }
  return result;
}

/** Build bounded UTC calendar-aligned candidates, retaining the visible start as the context anchor. */
export function timeAxisValues(range, width) {
  const [start, end] = range;
  const target = Math.max(2, Math.min(20, Math.floor(width / 65)));
  const desired = (end - start) / target;
  const result = [start];
  if (desired >= 28 * day) {
    const months =
      desired < 85 * day
        ? 1
        : desired < 180 * day
          ? 3
          : desired < 330 * day
            ? 6
            : 12 * Math.max(1, Math.ceil(desired / (365.2425 * day)));
    const date = new Date(start);
    date.setUTCDate(1);
    date.setUTCHours(0, 0, 0, 0);
    date.setUTCMonth(Math.floor(date.getUTCMonth() / months) * months);
    for (let i = 0; i < 64; i++) {
      date.setUTCMonth(date.getUTCMonth() + months);
      const value = date.getTime();
      if (!Number.isFinite(value) || value > end) break;
      if (value > start) result.push(value);
    }
  } else {
    const steps = [
      1,
      2,
      5,
      10,
      20,
      50,
      100,
      200,
      500,
      1000,
      2000,
      5000,
      10000,
      15000,
      30000,
      60000,
      120000,
      300000,
      600000,
      900000,
      1800000,
      3600000,
      7200000,
      10800000,
      21600000,
      43200000,
      day,
      2 * day,
      7 * day,
      14 * day,
      28 * day,
    ];
    const step = steps.find((value) => value >= desired) ?? 28 * day;
    let next = (Math.floor(start / step) + 1) * step;
    for (let i = 0; i < 64 && next <= end; i++, next += step)
      if (next > start) result.push(next);
  }
  return result;
}

/** Retain a caller's category label while exposing an explicitly supplied instant in exact readouts. */
export function axisPointLabel(labels, index, explicit) {
  const label = labels[index];
  return Array.isArray(explicit)
    ? label + " · " + new Date(explicit[index]).toISOString()
    : label;
}
