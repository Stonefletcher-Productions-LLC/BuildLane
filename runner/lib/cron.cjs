'use strict';
// POSIX cron (5 fields, UTC, as GitHub schedules). Supports * , - / and month/day names.
const NAMES = { jan: 1, feb: 2, mar: 3, apr: 4, may: 5, jun: 6, jul: 7, aug: 8, sep: 9, oct: 10, nov: 11, dec: 12, sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };
const RANGES = [[0, 59], [0, 23], [1, 31], [1, 12], [0, 6]];

function field(text, [lo, hi]) {
  const set = new Set();
  for (const part of text.toLowerCase().split(',')) {
    const [range, stepText] = part.split('/');
    const step = stepText ? Number(stepText) : 1;
    let a = lo, b = hi;
    if (range !== '*') {
      const [x, y] = range.split('-').map(v => (v in NAMES ? NAMES[v] : Number(v)));
      a = x; b = y === undefined ? (stepText ? hi : x) : y;
    }
    if (![a, b, step].every(Number.isInteger) || step < 1) throw new Error(`Bad cron field '${text}'`);
    for (let v = a; v <= b; v += step) set.add(v === 7 && hi === 6 ? 0 : v);
  }
  return set;
}

function parse(expr) {
  const parts = String(expr).trim().split(/\s+/);
  if (parts.length !== 5) throw new Error(`Cron needs 5 fields: '${expr}'`);
  const f = parts.map((p, i) => field(p, RANGES[i]));
  return { min: f[0], hour: f[1], dom: f[2], mon: f[3], dow: f[4], domStar: parts[2] === '*', dowStar: parts[4] === '*' };
}

function matches(c, d) {
  if (!c.min.has(d.getUTCMinutes()) || !c.hour.has(d.getUTCHours()) || !c.mon.has(d.getUTCMonth() + 1)) return false;
  const dom = c.dom.has(d.getUTCDate()), dow = c.dow.has(d.getUTCDay());
  if (c.domStar && c.dowStar) return true;
  if (c.domStar) return dow; if (c.dowStar) return dom;
  return dom || dow;
}

// Every matching minute in (from, to], at most `limit` (a long sleep fires a schedule once, not a backlog).
function firesBetween(expr, from, to, limit = 1) {
  const c = parse(expr); const out = [];
  if (to - from > 86400000) from = new Date(to.getTime() - 86400000);
  const t = new Date(Math.floor(from.getTime() / 60000) * 60000 + 60000);
  while (t <= to && out.length < limit) {
    if (matches(c, t)) out.push(new Date(t));
    t.setTime(t.getTime() + 60000);
  }
  return out;
}

module.exports = { parse, matches, firesBetween };
