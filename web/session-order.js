/**
 * Return a copy ordered by the latest available activity timestamp; undated sources stay last.
 * @param {Array<object>} sessions - Source metadata with activityAt, updatedAt or createdAt.
 * @param {string} sort - oldest for ascending order; otherwise newest first.
 * @returns {Array<object>} Ordered metadata; equal undated entries keep source order.
 */
export function orderSessions(sessions, sort='newest') {
  const direction=sort==='oldest' ? 1 : -1;
  return [...sessions].sort((left,right)=>{
    const leftTime=activityTime(left), rightTime=activityTime(right);
    if(leftTime===null) return rightTime===null ? 0 : 1;
    if(rightTime===null) return -1;
    for(let part=0;part<leftTime.length;part++) {
      if(leftTime[part]!==rightTime[part]) return (leftTime[part]-rightTime[part])*direction;
    }
    const leftId=String(left.id), rightId=String(right.id);
    return (leftId<rightId ? -1 : leftId>rightId ? 1 : 0)*direction;
  });
}

/**
 * Resolve the first valid timestamp without treating absent/invalid dates as fresh activity.
 * @param {object} session - Metadata from one session source.
 * @returns {Array<number>|null} Epoch milliseconds and remaining microseconds, or null for unknown time.
 */
function activityTime(session) {
  for(const value of [session.activityAt,session.updatedAt,session.createdAt]) {
    const time=typeof value==='string' ? Date.parse(value) : NaN;
    if(Number.isFinite(time)) {
      const fraction=value.match(/\.(\d+)/)?.[1] || '';
      const remaining=Number(fraction.padEnd(6,'0').slice(3,6));
      return [time,remaining];
    }
  }
  return null;
}

/**
 * Reorder existing rows in place, preserving an active control even when its row moves.
 * @param {Element} parent - Container with ordered rows (and optional trailing controls).
 * @param {Array<Element>} nodes - Existing rows in the required display order.
 * @returns {void}
 */
export function placeSessionRows(parent,nodes) {
  const focused=document.activeElement;
  let anchor=parent.firstElementChild;
  for(const node of nodes) {
    if(node!==anchor) parent.insertBefore(node,anchor);
    anchor=node.nextElementSibling;
  }
  if(focused?.isConnected && document.activeElement!==focused) focused.focus({preventScroll:true});
}
