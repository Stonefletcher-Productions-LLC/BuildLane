'use strict';
// GitHub Actions expression language: ${{ ... }} evaluation with the documented coercion rules.
// Supports literals, property and index access, the .* object filter, ! == != < <= > >= && ||,
// and the functions contains, startsWith, endsWith, format, join, toJSON, fromJSON, hashFiles,
// success, failure, always, cancelled.

function tokenize(src) {
  const out = []; let i = 0;
  while (i < src.length) {
    const c = src[i];
    if (/\s/.test(c)) { i++; continue; }
    if (c === "'") {
      let s = ''; i++;
      for (;;) {
        if (i >= src.length) throw new Error(`Unterminated string in expression: ${src}`);
        if (src[i] === "'") { if (src[i + 1] === "'") { s += "'"; i += 2; continue; } i++; break; }
        s += src[i++];
      }
      out.push({ t: 'str', v: s }); continue;
    }
    const two = src.slice(i, i + 2);
    if (['==', '!=', '<=', '>=', '&&', '||'].includes(two)) { out.push({ t: 'op', v: two }); i += 2; continue; }
    if ('!<>()[],.*'.includes(c)) { out.push({ t: 'op', v: c }); i++; continue; }
    const num = /^(0x[0-9a-fA-F]+|-?\d+(\.\d+)?([eE][+-]?\d+)?)/.exec(src.slice(i));
    if (num && !(c === '-' && out.length && !['op'].includes(out[out.length - 1].t))) { out.push({ t: 'num', v: Number(num[0]) }); i += num[0].length; continue; }
    const id = /^[A-Za-z_][A-Za-z0-9_-]*/.exec(src.slice(i));
    if (id) { out.push({ t: 'id', v: id[0] }); i += id[0].length; continue; }
    throw new Error(`Unexpected '${c}' in expression: ${src}`);
  }
  return out;
}

class Parser {
  constructor(tokens, src) { this.k = tokens; this.i = 0; this.src = src; }
  peek(v) { const t = this.k[this.i]; return t && t.t === 'op' && t.v === v; }
  eat(v) { if (!this.peek(v)) throw new Error(`Expected '${v}' in expression: ${this.src}`); this.i++; }
  parse() { const n = this.or(); if (this.i < this.k.length) throw new Error(`Unexpected token in expression: ${this.src}`); return n; }
  or() { let l = this.and(); while (this.peek('||')) { this.i++; l = { k: 'or', l, r: this.and() }; } return l; }
  and() { let l = this.cmp(); while (this.peek('&&')) { this.i++; l = { k: 'and', l, r: this.cmp() }; } return l; }
  cmp() {
    let l = this.unary();
    for (;;) {
      const t = this.k[this.i];
      if (t && t.t === 'op' && ['==', '!=', '<', '<=', '>', '>='].includes(t.v)) { this.i++; l = { k: 'cmp', op: t.v, l, r: this.unary() }; } else return l;
    }
  }
  unary() { if (this.peek('!')) { this.i++; return { k: 'not', e: this.unary() }; } return this.postfix(); }
  postfix() {
    let n = this.primary();
    for (;;) {
      if (this.peek('.')) {
        this.i++;
        if (this.peek('*')) { this.i++; n = { k: 'star', e: n }; continue; }
        const t = this.k[this.i++];
        if (!t || t.t !== 'id') throw new Error(`Expected property name in expression: ${this.src}`);
        n = { k: 'prop', e: n, name: t.v };
      } else if (this.peek('[')) {
        this.i++;
        if (this.peek('*')) { this.i++; this.eat(']'); n = { k: 'star', e: n }; continue; }
        const idx = this.or(); this.eat(']'); n = { k: 'index', e: n, idx };
      } else return n;
    }
  }
  primary() {
    const t = this.k[this.i++];
    if (!t) throw new Error(`Unexpected end of expression: ${this.src}`);
    if (t.t === 'str') return { k: 'lit', v: t.v };
    if (t.t === 'num') return { k: 'lit', v: t.v };
    if (t.t === 'op' && t.v === '(') { const e = this.or(); this.eat(')'); return e; }
    if (t.t === 'id') {
      if (t.v === 'true') return { k: 'lit', v: true };
      if (t.v === 'false') return { k: 'lit', v: false };
      if (t.v === 'null') return { k: 'lit', v: null };
      if (this.peek('(')) {
        this.i++; const args = [];
        if (!this.peek(')')) { args.push(this.or()); while (this.peek(',')) { this.i++; args.push(this.or()); } }
        this.eat(')');
        return { k: 'call', name: t.v.toLowerCase(), args };
      }
      return { k: 'ctx', name: t.v };
    }
    throw new Error(`Unexpected token in expression: ${this.src}`);
  }
}

function toNum(v) {
  if (v === null || v === undefined) return 0;
  if (typeof v === 'boolean') return v ? 1 : 0;
  if (typeof v === 'number') return v;
  if (typeof v === 'string') { if (v.trim() === '') return 0; const n = Number(v); return Number.isNaN(n) ? NaN : n; }
  return NaN;
}
function truthy(v) { return !(v === null || v === undefined || v === false || v === 0 || v === '' || Number.isNaN(v)); }
function str(v) {
  if (v === null || v === undefined) return '';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  if (typeof v === 'number') return String(v);
  if (typeof v === 'string') return v;
  return JSON.stringify(v, null, 2);
}
function looseEq(a, b) {
  if (a === undefined) a = null; if (b === undefined) b = null;
  const ta = typeof a, tb = typeof b;
  if (a !== null && b !== null && ta === 'object' && tb === 'object') return a === b;
  if (ta === tb) return ta === 'string' ? a.toLowerCase() === b.toLowerCase() : a === b;
  if (ta === 'object' || tb === 'object') { if (a === null && b === null) return true; if ((a !== null && ta === 'object') || (b !== null && tb === 'object')) return false; }
  return toNum(a) === toNum(b);
}
function compare(op, a, b) {
  if (op === '==') return looseEq(a, b);
  if (op === '!=') return !looseEq(a, b);
  let x = a, y = b;
  if (!(typeof a === 'string' && typeof b === 'string')) { x = toNum(a); y = toNum(b); } else { x = a.toLowerCase(); y = b.toLowerCase(); }
  if (op === '<') return x < y; if (op === '<=') return x <= y; if (op === '>') return x > y; return x >= y;
}
function getProp(obj, name) {
  if (obj === null || obj === undefined) return null;
  if (Array.isArray(obj) && obj.__filtered) { const r = obj.map(o => getProp(o, name)).filter(v => v !== null && v !== undefined); r.__filtered = true; return r; }
  if (typeof obj !== 'object') return null;
  if (Object.prototype.hasOwnProperty.call(obj, name)) return obj[name];
  const key = Object.keys(obj).find(k => k.toLowerCase() === String(name).toLowerCase());
  return key === undefined ? null : obj[key];
}

function evalNode(n, ctx) {
  switch (n.k) {
    case 'lit': return n.v;
    case 'ctx': return getProp(ctx.contexts, n.name);
    case 'prop': return getProp(evalNode(n.e, ctx), n.name);
    case 'index': {
      const o = evalNode(n.e, ctx), i = evalNode(n.idx, ctx);
      if (Array.isArray(o)) { const idx = toNum(i); return Number.isInteger(idx) ? (o[idx] ?? null) : null; }
      return getProp(o, str(i));
    }
    case 'star': {
      const o = evalNode(n.e, ctx); let r = [];
      if (Array.isArray(o)) r = o.slice(); else if (o && typeof o === 'object') r = Object.values(o);
      r.__filtered = true; return r;
    }
    case 'not': return !truthy(evalNode(n.e, ctx));
    case 'and': { const l = evalNode(n.l, ctx); return truthy(l) ? evalNode(n.r, ctx) : l; }
    case 'or': { const l = evalNode(n.l, ctx); return truthy(l) ? l : evalNode(n.r, ctx); }
    case 'cmp': return compare(n.op, evalNode(n.l, ctx), evalNode(n.r, ctx));
    case 'call': return call(n.name, n.args.map(a => evalNode(a, ctx)), ctx);
  }
  throw new Error(`Unknown node ${n.k}`);
}

function call(name, a, ctx) {
  switch (name) {
    case 'contains': {
      const [hay, needle] = a;
      if (Array.isArray(hay)) return hay.some(x => looseEq(x, needle));
      return str(hay).toLowerCase().includes(str(needle).toLowerCase());
    }
    case 'startswith': return str(a[0]).toLowerCase().startsWith(str(a[1]).toLowerCase());
    case 'endswith': return str(a[0]).toLowerCase().endsWith(str(a[1]).toLowerCase());
    case 'format': { const f = str(a[0]); return f.replace(/\{\{|\}\}|\{(\d+)\}/g, (m, d) => m === '{{' ? '{' : m === '}}' ? '}' : str(a[Number(d) + 1])); }
    case 'join': { const sep = a.length > 1 ? str(a[1]) : ','; return Array.isArray(a[0]) ? a[0].map(str).join(sep) : str(a[0]); }
    case 'tojson': return JSON.stringify(a[0] === undefined ? null : a[0], null, 2);
    case 'fromjson': return JSON.parse(str(a[0]));
    case 'hashfiles': return ctx.hashFiles ? ctx.hashFiles(a.map(str)) : '';
    case 'success': return ctx.status ? ctx.status() === 'success' : true;
    case 'failure': return ctx.status ? ctx.status() === 'failure' : false;
    case 'cancelled': return ctx.status ? ctx.status() === 'cancelled' : false;
    case 'always': return true;
  }
  throw new Error(`Unsupported expression function ${name}()`);
}

const cache = new Map();
function compile(src) {
  if (!cache.has(src)) cache.set(src, new Parser(tokenize(src), src).parse());
  return cache.get(src);
}
function evaluate(src, ctx) { return evalNode(compile(src), ctx); }
function usesStatusFunction(src) { return /\b(success|failure|always|cancelled)\s*\(/i.test(src); }

// if: conditions are expressions even without ${{ }}; an implicit success() is added unless a status function is used.
function evaluateIf(cond, ctx) {
  if (cond === undefined || cond === null || cond === '') return ctx.status ? ctx.status() === 'success' : true;
  if (typeof cond === 'boolean') return cond && (ctx.status ? ctx.status() === 'success' : true);
  let src = String(cond).trim();
  const m = /^\$\{\{([\s\S]*)\}\}$/.exec(src);
  if (m && !m[1].includes('}}')) src = m[1].trim();
  else if (src.includes('${{')) src = `'${interpolate(src, ctx).replace(/'/g, "''")}'`;
  const v = truthy(evaluate(src, ctx));
  return usesStatusFunction(src) ? v : v && (ctx.status ? ctx.status() === 'success' : true);
}

// Replace every ${{ expr }} in a string. A value that is exactly one expression keeps its type.
function interpolate(value, ctx, keepType = false) {
  if (typeof value !== 'string' || !value.includes('${{')) return value;
  const whole = /^\$\{\{((?:(?!\}\})[\s\S])*)\}\}$/.exec(value);
  if (whole && keepType) return evaluate(whole[1].trim(), ctx);
  return value.replace(/\$\{\{((?:(?!\}\})[\s\S])*)\}\}/g, (_, e) => str(evaluate(e.trim(), ctx)));
}
function interpolateDeep(value, ctx) {
  if (typeof value === 'string') return interpolate(value, ctx, true);
  if (Array.isArray(value)) return value.map(v => interpolateDeep(v, ctx));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, interpolateDeep(v, ctx)]));
  return value;
}

module.exports = { evaluate, evaluateIf, interpolate, interpolateDeep, truthy, str };
