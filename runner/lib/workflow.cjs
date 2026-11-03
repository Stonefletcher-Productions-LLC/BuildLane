'use strict';
const YAML = require('../vendor/yaml.cjs');

// GitHub filter pattern: * (not /), ** (anything), ? + [] and leading ! for negation.
function globToRegex(pattern) {
  let re = '';
  for (let i = 0; i < pattern.length; i++) {
    const c = pattern[i];
    if (c === '*') { if (pattern[i + 1] === '*') { re += '.*'; i++; if (pattern[i + 1] === '/') { re += '(?:/)?'; i++; } } else re += '[^/]*'; }
    else if (c === '?') re += '?';
    else if (c === '+') re += '+';
    else if (c === '[') { const j = pattern.indexOf(']', i); re += pattern.slice(i, j + 1); i = j; }
    else re += c.replace(/[.^$|(){}\\]/g, '\\$&');
  }
  return new RegExp(`^${re}$`);
}
function filterMatch(patterns, value) {
  let ok = false;
  for (const p of [].concat(patterns)) {
    const s = String(p);
    if (s.startsWith('!')) { if (globToRegex(s.slice(1)).test(value)) ok = false; }
    else if (globToRegex(s).test(value)) ok = true;
  }
  return ok;
}
function allowed(include, ignore, value) {
  if (include !== undefined) return filterMatch(include, value);
  if (ignore !== undefined) return !filterMatch(ignore, value);
  return true;
}
function pathsAllowed(cfg, files) {
  if (!cfg || (cfg.paths === undefined && cfg['paths-ignore'] === undefined)) return true;
  if (!Array.isArray(files)) return true; // unknown file list (e.g. too large): run, as GitHub does over 3000 files
  if (cfg.paths !== undefined) return files.some(f => filterMatch(cfg.paths, f));
  return files.some(f => !filterMatch(cfg['paths-ignore'], f));
}

function normalizeOn(on) {
  if (typeof on === 'string') return { [on]: {} };
  if (Array.isArray(on)) return Object.fromEntries(on.map(e => [e, {}]));
  return Object.fromEntries(Object.entries(on || {}).map(([k, v]) => [k, v || {}]));
}

function load(text, file) {
  const doc = YAML.parse(text) || {};
  const on = normalizeOn(doc.on ?? doc[true]);
  return { file, path: `.github/workflows/${file}`, name: doc.name || `.github/workflows/${file}`, on, doc, jobs: doc.jobs || {} };
}

const PR_DEFAULT_TYPES = ['opened', 'synchronize', 'reopened'];

// Does this workflow want this event? Returns false or the trigger config.
function wants(wf, event) {
  const cfg = wf.on[event.name];
  if (!cfg) return false;
  switch (event.name) {
    case 'push': {
      const ref = event.ref || '';
      if (ref.startsWith('refs/tags/')) {
        if (cfg.tags === undefined && cfg['tags-ignore'] === undefined && (cfg.branches !== undefined || cfg['branches-ignore'] !== undefined)) return false;
        if (!allowed(cfg.tags, cfg['tags-ignore'], ref.slice(10))) return false;
      } else {
        if (cfg.branches === undefined && cfg['branches-ignore'] === undefined && (cfg.tags !== undefined || cfg['tags-ignore'] !== undefined)) return false;
        if (!allowed(cfg.branches, cfg['branches-ignore'], ref.replace(/^refs\/heads\//, ''))) return false;
      }
      return pathsAllowed(cfg, event.files) && cfg;
    }
    case 'pull_request':
    case 'pull_request_target': {
      const types = cfg.types ? [].concat(cfg.types) : PR_DEFAULT_TYPES;
      if (!types.includes(event.action)) return false;
      if (!allowed(cfg.branches, cfg['branches-ignore'], event.payload.pull_request.base.ref)) return false;
      return pathsAllowed(cfg, event.files) && cfg;
    }
    case 'workflow_run': {
      const names = [].concat(cfg.workflows || []);
      if (!names.includes(event.payload.workflow_run.name)) return false;
      const types = cfg.types ? [].concat(cfg.types) : ['completed', 'requested'];
      if (!types.includes(event.action)) return false;
      return allowed(cfg.branches, cfg['branches-ignore'], event.payload.workflow_run.head_branch || '') && cfg;
    }
    case 'schedule': return [].concat(cfg).some(s => s && s.cron === event.cron) && cfg;
    case 'workflow_dispatch':
    case 'repository_dispatch':
    case 'issue_comment':
    case 'issues':
    case 'release':
      return cfg;
    default: return false;
  }
}

// workflow_dispatch inputs: apply defaults and coerce booleans/numbers like GitHub.
function dispatchInputs(wf, given = {}) {
  const decl = (wf.on.workflow_dispatch && wf.on.workflow_dispatch.inputs) || {};
  const out = {};
  for (const [k, d] of Object.entries(decl)) {
    let v = given[k] !== undefined ? given[k] : d.default;
    if (v === undefined) v = d.type === 'boolean' ? false : '';
    if (d.required && (v === '' || v === undefined) && d.type !== 'boolean') throw new Error(`Input '${k}' is required`);
    if (d.type === 'boolean') v = v === true || String(v).toLowerCase() === 'true';
    else if (d.type === 'number') v = Number(v);
    else if (d.type === 'choice' && Array.isArray(d.options) && v !== '' && !d.options.map(String).includes(String(v))) throw new Error(`Input '${k}' must be one of ${d.options.join(', ')}`);
    else v = String(v);
    out[k] = v;
  }
  for (const k of Object.keys(given)) if (!(k in decl)) throw new Error(`Unexpected input '${k}'`);
  return out;
}

module.exports = { load, wants, dispatchInputs, globToRegex, filterMatch };
