'use strict';
// The local runner: watches GitHub, decides which workflows an event starts (from the repository's own
// .github/workflows files), schedules their jobs onto this PC's runner pools, and reports results back
// to GitHub as commit statuses. It replaces GitHub-hosted Actions; GitHub stays the source of code,
// PRs, reviews and merges.
const fs = require('fs');
const path = require('path');
const { Store, writeAtomic } = require('./store.cjs');
const { GitHub } = require('./github.cjs');
const { ApiProxy } = require('./proxy.cjs');
const { ActionCache } = require('./actioncache.cjs');
const { JobRunner } = require('./executor.cjs');
const workflow = require('./workflow.cjs');
const { interpolate, evaluateIf, str } = require('./expr.cjs');
const cron = require('./cron.cjs');

const ZERO = '0000000000000000000000000000000000000000';
const DEFAULT_POOLS = {
  hosted: { labels: ['ubuntu-latest', 'ubuntu-24.04', 'ubuntu-22.04', 'windows-latest', 'windows-2022', 'windows-2025'], max: 2 },
};

function matrixCombos(strategy, ctx) {
  if (!strategy || !strategy.matrix) return [null];
  let m = strategy.matrix;
  if (typeof m === 'string') m = interpolate(m, ctx, true);
  m = Object.fromEntries(Object.entries(m).map(([k, v]) => [k, typeof v === 'string' ? interpolate(v, ctx, true) : v]));
  const { include = [], exclude = [], ...axes } = m;
  let combos = [{}];
  for (const [k, vals] of Object.entries(axes)) { const list = [].concat(vals); combos = combos.flatMap(c => list.map(v => ({ ...c, [k]: v }))); }
  if (!Object.keys(axes).length) combos = [];
  const sub = (a, b) => Object.entries(b).every(([k, v]) => JSON.stringify(a[k]) === JSON.stringify(v));
  combos = combos.filter(c => !exclude.some(e => sub(c, e)));
  for (const inc of [].concat(include)) {
    let merged = false;
    for (const c of combos) { const shared = Object.keys(axes).filter(k => k in inc); if (shared.length && shared.every(k => JSON.stringify(c[k]) === JSON.stringify(inc[k]))) { Object.assign(c, inc); merged = true; } }
    if (!merged) combos.push({ ...inc });
  }
  return combos.length ? combos : [];
}

class LocalRunner {
  constructor(config) {
    this.c = config;
    const [owner, repo] = config.repo.split('/');
    this.owner = owner; this.repo = repo;
    fs.mkdirSync(config.root, { recursive: true });
    this.store = new Store(config.root, { owner, repo });
    this.gh = new GitHub({ token: config.token, api: config.api || 'https://api.github.com', owner, repo });
    this.actionCache = new ActionCache(config.root, { token: config.token });
    this.pools = config.replacePools ? config.pools : { ...DEFAULT_POOLS, ...(config.pools || {}) };
    this.active = new Map(); // runId -> { run, wf, jobs: Map(key -> state), concurrency }
    this.running = new Map(); // jobId -> JobRunner
    this.stateFile = path.join(config.root, 'poll-state.json');
    this.requestsDir = path.join(config.root, 'requests');
    fs.mkdirSync(this.requestsDir, { recursive: true });
    this.wfCache = new Map();
    this.logFile = path.join(config.root, 'runner.log');
    this.mode = config.mode || 'shadow';
  }

  log(msg) {
    const line = `${new Date().toISOString()} ${msg}`;
    try { fs.appendFileSync(this.logFile, line + '\n'); } catch {}
    if (!this.c.quiet) console.log(line);
  }

  loadState() { try { return JSON.parse(fs.readFileSync(this.stateFile, 'utf8')); } catch { return { initialized: false, branches: {}, prs: {}, lastSchedule: null, lastCommentId: 0 }; } }
  saveState(s) { writeAtomic(this.stateFile, JSON.stringify(s, null, 1)); }

  async start() {
    if (!this.c.token) throw new Error('No GitHub token. Connect GitHub in Buildlane or set BUILDLANE_TOKEN.');
    const me = await this.gh.get('/user');
    this.login = me.login;
    this.proxy = new ApiProxy({
      store: this.store, upstream: this.gh.api, repos: [this.c.repo, ...(this.c.repoAliases || [])], mode: this.mode, appId: this.c.checkAppId || 15368,
      log: m => this.log(m), workflows: () => this.workflowList(),
      onDispatch: (wf, ref, inputs) => this.dispatch(wf, ref, inputs, this.login, 0),
      onCancel: id => this.cancelRun(id), onRerun: (id, failed) => this.rerun(id, failed),
    });
    await this.proxy.listen(this.c.apiPort || 0);
    this.recoverInterrupted();
    this.housekeep();
    this.log(`Buildlane local runner for ${this.c.repo} as ${this.login}, mode ${this.mode}, local API ${this.proxy.url}`);
    this.writeHeartbeat();
  }

  async stop() { for (const j of this.running.values()) j.cancel('runner stopped'); if (this.proxy) await this.proxy.close(); }

  writeHeartbeat(extra = {}) {
    const runs = this.store.list().slice(0, 30).map(r => ({ id: r.id, name: r.name, title: r.display_title, event: r.event, status: r.status, conclusion: r.conclusion, sha: r.head_sha, branch: r.head_branch, created: r.created_at, updated: r.updated_at,
      jobs: r.jobs.map(j => ({ id: j.id, name: j.name, status: j.status, conclusion: j.conclusion, step: (j.steps.find(s => s.status === 'in_progress') || {}).name || '' })) }));
    writeAtomic(path.join(this.c.root, 'runner-status.json'), JSON.stringify({ at: new Date().toISOString(), pid: process.pid, mode: this.mode, repo: this.c.repo, login: this.login, api: this.proxy && this.proxy.url, paused: this.paused(), runs, ...extra }, null, 1));
  }

  // Keeps the data folder from growing without bound: old runs, answered requests, stale job temp
  // folders and a large runner.log. Runs at start and then hourly.
  housekeep(now = Date.now()) {
    this.lastHousekeep = now;
    const keep = this.c.retention || {};
    try { const n = this.store.prune({ keepDays: keep.days ?? 7, keepRuns: keep.runs ?? 100, now }); if (n) this.log(`housekeeping: removed ${n} old run(s)`); } catch (e) { this.log(`housekeeping: prune failed: ${e.message}`); }
    const oldDay = now - 86400000;
    const stale = p => { try { return fs.statSync(p).mtimeMs < oldDay; } catch { return false; } };
    try { for (const n of fs.readdirSync(this.requestsDir)) if (n.endsWith('.done') && stale(path.join(this.requestsDir, n))) fs.rmSync(path.join(this.requestsDir, n), { force: true }); } catch {}
    const running = new Set([...this.running.keys()].map(String));
    // Only the work folders this runner names (<base>, <base>-linux, <base>-2 ...), never another runner's.
    const bases = [path.join(this.c.root, '_work'), ...Object.values(this.c.pools || {}).map(p => p.workRoot).filter(Boolean)];
    for (const base of new Set(bases)) {
      const dir = path.dirname(base), stem = path.basename(base);
      let names = []; try { names = fs.readdirSync(dir); } catch {}
      for (const n of names.filter(n => n.startsWith(stem) && /^(-linux)?(-\d+)?$/.test(n.slice(stem.length)))) {
        const temp = path.join(dir, n, '_temp');
        let jobs = []; try { jobs = fs.readdirSync(temp); } catch {}
        for (const j of jobs) if (/^\d+$/.test(j) && !running.has(j) && stale(path.join(temp, j))) try { fs.rmSync(path.join(temp, j), { recursive: true, force: true }); } catch {}
      }
    }
    try { if (fs.statSync(this.logFile).size > (keep.logMB ?? 20) * 1048576) { fs.rmSync(this.logFile + '.1', { force: true }); fs.renameSync(this.logFile, this.logFile + '.1'); } } catch {}
  }

  paused() { return !!(this.c.pauseFile && fs.existsSync(this.c.pauseFile)); }

  recoverInterrupted() {
    for (const r of this.store.list()) {
      if (r.status === 'completed') continue;
      for (const j of r.jobs) if (j.status !== 'completed') Object.assign(j, { status: 'completed', conclusion: 'cancelled', completed_at: new Date().toISOString() });
      Object.assign(r, { status: 'completed', conclusion: 'cancelled' });
      this.store.save(r);
      this.log(`run ${r.id} (${r.name}) was interrupted by a runner restart; marked cancelled`);
    }
  }

  // Workflows -----------------------------------------------------------------
  allowed(file) {
    const allow = this.c.workflows; const deny = this.c.skipWorkflows || [];
    if (deny.includes(file)) return false;
    if (Array.isArray(allow) && allow.length) return allow.includes(file);
    return true;
  }

  async workflowsAt(sha) {
    const entries = (await this.gh.listDir('.github/workflows', sha)).filter(e => e.type === 'file' && /\.ya?ml$/.test(e.name));
    const out = [];
    for (const e of entries) {
      let wf = this.wfCache.get(e.sha);
      if (!wf) {
        const text = await this.gh.fileAt(`.github/workflows/${e.name}`, sha);
        try { wf = workflow.load(text, e.name); } catch (err) { this.log(`workflow ${e.name}@${sha.slice(0, 8)} does not parse: ${err.message}`); continue; }
        this.wfCache.set(e.sha, wf);
      }
      out.push(wf);
    }
    return out;
  }

  workflowList() {
    const wfs = this.lastDefaultWorkflows || [];
    return { total_count: wfs.length, workflows: wfs.map(w => ({ id: this.store.workflowId(w.file), node_id: '', name: w.name, path: w.path, state: 'active', created_at: '', updated_at: '', url: '', html_url: '', badge_url: '' })) };
  }

  async defaultHead(branch = this.c.defaultBranch || 'main') {
    return (await this.gh.get(`${this.gh.base}/branches/${encodeURIComponent(branch)}`)).commit.sha;
  }

  // Events ----------------------------------------------------------------------
  // event: { name, action, ref, sha (commit the workflow files come from), headSha (commit statuses go to), branch, payload, files, actor, inputs, depth, githubSha }
  async trigger(event) {
    const wfs = event.workflows || await this.workflowsAt(event.sha);
    const runs = [];
    for (const wf of wfs) {
      if (!this.allowed(wf.file)) continue;
      if (!workflow.wants(wf, event)) continue;
      runs.push(this.createRun(wf, event));
    }
    return runs;
  }

  createRun(wf, event, inputs = {}) {
    const run = this.store.create({ workflow: wf, event: event.name, sha: event.headSha || event.sha, branch: event.branch, actor: event.actor || this.login,
      triggeringActor: event.actor || this.login, inputs, payload: event.payload, ref: event.ref, prNumbers: event.prNumbers });
    run.github_sha = event.githubSha || event.sha; run.workflow_sha = event.sha; run.depth = event.depth || 0;
    if (event.name === 'schedule') run.payload = { ...(run.payload || {}), schedule: event.cron };
    const ctx = this.runContext(run, wf);
    try { run.display_title = str(interpolate(String(wf.doc['run-name'] || ''), ctx)) || this.defaultTitle(run, wf); } catch (e) { run.display_title = this.defaultTitle(run, wf); this.log(`run-name: ${e.message}`); }
    this.store.save(run);
    const state = { run, wf, jobs: new Map(), concurrency: null };
    if (wf.doc.concurrency) state.concurrency = this.concurrencyOf(wf.doc.concurrency, ctx);
    this.active.set(run.id, state);
    this.log(`queued run ${run.id}: ${wf.name} (${event.name}${event.action ? '/' + event.action : ''}) "${run.display_title}" at ${String(run.head_sha).slice(0, 8)}`);
    if (state.concurrency) this.applyRunConcurrency(state);
    this.emitWorkflowRun(state, 'requested');
    return run;
  }

  defaultTitle(run, wf) {
    const p = run.payload || {};
    return (p.pull_request && p.pull_request.title) || (p.head_commit && p.head_commit.message && p.head_commit.message.split('\n')[0]) || wf.name;
  }

  runContext(run, wf) {
    const isPr = run.event === 'pull_request' || run.event === 'pull_request_target';
    const pr = run.payload && run.payload.pull_request;
    const ref = run.ref || '';
    return { contexts: {
      github: { event_name: run.event, event: run.payload || {}, ref, ref_name: isPr ? `${pr.number}/merge` : ref.replace(/^refs\/(heads|tags)\//, ''), head_ref: isPr ? pr.head.ref : '', base_ref: isPr ? pr.base.ref : '',
        sha: run.github_sha || run.head_sha, run_id: String(run.id), run_number: String(run.run_number), run_attempt: String(run.run_attempt), actor: run.actor.login, triggering_actor: run.triggering_actor.login,
        repository: run.repository.full_name, repository_owner: run.repository.owner.login, workflow: wf.name, server_url: 'https://github.com', api_url: this.proxy ? this.proxy.url : '' },
      inputs: run.inputs || {}, vars: this.c.vars || {}, env: {}, secrets: {},
    } };
  }

  concurrencyOf(spec, ctx) {
    if (typeof spec === 'string') return { group: str(interpolate(spec, ctx)), cancel: false, queueAll: false };
    const group = str(interpolate(String(spec.group || ''), ctx));
    let cancel = spec['cancel-in-progress'];
    if (typeof cancel === 'string') cancel = !!interpolate(cancel, ctx, true) && interpolate(cancel, ctx, true) !== 'false';
    return { group, cancel: !!cancel, queueAll: spec.queue === 'max' };
  }

  applyRunConcurrency(state) {
    const { group, cancel, queueAll } = state.concurrency;
    for (const other of this.active.values()) {
      if (other === state || !other.concurrency || other.concurrency.group !== group || other.run.status === 'completed') continue;
      const started = [...other.jobs.values()].some(j => j.started);
      if (started && cancel) { this.log(`run ${other.run.id} cancelled: newer run ${state.run.id} in group ${group}`); this.cancelRun(other.run.id); }
      else if (!started && !queueAll) { this.log(`run ${other.run.id} cancelled: replaced by run ${state.run.id} waiting in group ${group}`); this.cancelRun(other.run.id); }
    }
  }

  groupBusy(state) {
    if (!state.concurrency) return false;
    for (const other of this.active.values()) {
      if (other === state || !other.concurrency || other.concurrency.group !== state.concurrency.group || other.run.status === 'completed') continue;
      if ([...other.jobs.values()].some(j => j.started && !j.done)) return true;
      if (other.run.id < state.run.id && !this.runDone(other)) return true; // first come, first served
    }
    return false;
  }

  async dispatch(file, ref, inputs, actor, depth = 0) {
    const branch = String(ref || this.c.defaultBranch || 'main').replace(/^refs\/heads\//, '');
    const sha = /^[0-9a-f]{40}$/i.test(branch) ? branch : (await this.gh.get(`${this.gh.base}/branches/${encodeURIComponent(branch)}`)).commit.sha;
    const wfs = await this.workflowsAt(sha);
    const wf = wfs.find(w => w.file === file || w.path === file || String(this.store.workflowId(w.file)) === String(file));
    if (!wf) throw new Error(`Workflow ${file} was not found at ${branch}`);
    if (!wf.on.workflow_dispatch) throw new Error(`Workflow ${file} does not have a workflow_dispatch trigger`);
    if (!this.allowed(wf.file)) throw new Error(`Workflow ${file} is not enabled on this local runner`);
    const resolved = workflow.dispatchInputs(wf, Object.fromEntries(Object.entries(inputs || {}).map(([k, v]) => [k, typeof v === 'string' ? v : String(v)])));
    const payload = { inputs: resolved, ref: `refs/heads/${branch}`, repository: { full_name: this.c.repo }, sender: { login: actor }, workflow: wf.path };
    return this.createRun(wf, { name: 'workflow_dispatch', ref: `refs/heads/${branch}`, sha, branch, payload, actor, depth }, resolved);
  }

  emitWorkflowRun(state, action) {
    const { run } = state;
    if ((run.depth || 0) >= 3) return;
    const payload = { action, workflow_run: { ...this.store.restRun(run), status: action === 'completed' ? 'completed' : run.status }, workflow: { name: state.wf.name, path: state.wf.path }, repository: { full_name: this.c.repo }, sender: { login: run.actor.login } };
    const event = { name: 'workflow_run', action, ref: `refs/heads/${this.c.defaultBranch || 'main'}`, branch: this.c.defaultBranch || 'main', payload, actor: run.actor.login, depth: (run.depth || 0) + 1 };
    // workflow_run workflows come from the default branch.
    this.pendingEvents = this.pendingEvents || [];
    this.pendingEvents.push(event);
  }

  async flushEvents() {
    const events = this.pendingEvents || []; this.pendingEvents = [];
    if (!events.length) return;
    const sha = await this.defaultHead();
    const wfs = await this.workflowsAt(sha);
    for (const e of events) await this.trigger({ ...e, sha, workflows: wfs });
  }

  cancelRun(id) {
    const state = this.active.get(Number(id));
    const r = state ? state.run : this.store.get(id);
    if (!r || r.status === 'completed') return false;
    if (state) {
      state.cancelled = true;
      for (const j of state.jobs.values()) if (j.runner) j.runner.cancel();
    }
    return true;
  }

  async rerun(id, failedOnly) {
    const old = this.store.get(id);
    if (!old || old.status !== 'completed') throw new Error('Only a completed run can be re-run');
    const wfs = await this.workflowsAt(old.workflow_sha || old.head_sha);
    const wf = wfs.find(w => w.path === old.path);
    if (!wf) throw new Error('The workflow file is gone');
    const snapshot = { ...old }; delete snapshot.previousAttempts;
    old.previousAttempts = [...(old.previousAttempts || []), snapshot];
    old.run_attempt += 1; old.status = 'queued'; old.conclusion = null;
    const keep = failedOnly ? old.jobs.filter(j => j.conclusion === 'success') : [];
    old.jobs = [];
    this.store.save(old);
    const state = { run: old, wf, jobs: new Map(), concurrency: wf.doc.concurrency ? this.concurrencyOf(wf.doc.concurrency, this.runContext(old, wf)) : null, reuse: new Map(keep.map(j => [j.key, j])) };
    this.active.set(old.id, state);
    this.log(`re-running run ${old.id} attempt ${old.run_attempt}${failedOnly ? ' (failed jobs)' : ''}`);
  }

  // Scheduling ------------------------------------------------------------------
  // A pool with a `jobs` list ("ci.yml:quick" or "quick") takes only those jobs, and takes them ahead of
  // other pools, so one light job can run while the rest of its runner's pool stays off.
  poolFor(runsOn, job) {
    if (job) for (const [name, p] of Object.entries(this.pools)) if (Array.isArray(p.jobs) && p.jobs.some(j => j === `${job.file}:${job.key}` || j === job.key)) return { name, pool: p, have: p.labels || [] };
    let labels = runsOn;
    if (labels && typeof labels === 'object' && !Array.isArray(labels)) labels = labels.labels || [];
    labels = [].concat(labels || []).map(l => String(l).toLowerCase());
    const set = labels.filter(l => l !== 'self-hosted');
    let best = null;
    for (const [name, p] of Object.entries(this.pools)) {
      const have = (p.labels || []).map(l => l.toLowerCase());
      if (Array.isArray(p.jobs)) continue;
      if (set.every(l => have.includes(l)) && (!best || have.length < best.have.length)) best = { name, pool: p, have };
    }
    return best;
  }

  busyParts(name) { const out = []; for (const s of this.active.values()) for (const e of s.jobs.values()) for (const p of e.parts || []) if (p.started && !p.done && p.pool && p.pool.name === name) out.push(p); return out; }
  poolLoad(name) { return this.busyParts(name).length; }
  // A pinned workspace holds one job at a time; otherwise each parallel slot gets its own workspace.
  poolMax(pool) { return pool.workspace ? 1 : (pool.max || 1); }
  freeSlot(name) { const used = new Set(this.busyParts(name).map(p => p.slot)); let i = 0; while (used.has(i)) i++; return i; }

  runDone(state) {
    const keys = Object.keys(state.wf.jobs);
    return keys.every(k => { const j = state.jobs.get(k); return j && j.done; });
  }

  async tick() {
    await this.flushEvents().catch(e => this.log(`workflow_run events: ${e.message}`));
    for (const state of [...this.active.values()]) {
      if (state.run.status === 'completed') { this.active.delete(state.run.id); continue; }
      await this.advance(state);
    }
  }

  async advance(state) {
    const { run, wf } = state;
    if (this.groupBusy(state) && ![...state.jobs.values()].some(j => j.started)) {
      if (run.status !== 'pending') { run.status = 'pending'; this.store.save(run); }
      return;
    }
    for (const [key, def] of Object.entries(wf.jobs)) {
      if (state.jobs.has(key)) continue;
      const needs = [].concat(def.needs || []);
      const deps = needs.map(n => state.jobs.get(n));
      if (deps.some(d => !d || !d.done)) continue;
      const needsCtx = Object.fromEntries(needs.map(n => [n, { result: state.jobs.get(n).result, outputs: state.jobs.get(n).outputs || {} }]));
      const anyFailed = deps.some(d => d.result === 'failure' || d.result === 'cancelled' || d.result === 'skipped' && d.failedBranch);
      const ctx = this.runContext(run, wf);
      ctx.contexts.needs = needsCtx;
      const statusOf = () => state.cancelled ? 'cancelled' : (anyFailed ? 'failure' : 'success');
      ctx.status = statusOf;
      let go;
      try { go = evaluateIf(def.if, ctx); } catch (e) { this.log(`run ${run.id} job ${key} if: ${e.message}`); go = false; }
      if (!go) { state.jobs.set(key, { done: true, result: 'skipped', failedBranch: anyFailed, outputs: {} }); continue; }
      if (state.reuse && state.reuse.has(key)) {
        const j = state.reuse.get(key); run.jobs.push(j); this.store.save(run);
        state.jobs.set(key, { done: true, result: 'success', outputs: j.outputs || {} }); continue;
      }
      let combos;
      try { combos = matrixCombos(def.strategy, ctx); } catch (e) { this.log(`run ${run.id} job ${key} matrix: ${e.message}`); combos = [null]; }
      const entry = { key, started: false, done: false, parts: [], outputs: {}, result: null };
      state.jobs.set(key, entry);
      if (!combos.length) { Object.assign(entry, { done: true, result: 'skipped' }); continue; }
      for (const matrix of combos) {
        const mctx = { ...ctx, contexts: { ...ctx.contexts, matrix: matrix || {} } };
        let name = def.name ? str(interpolate(String(def.name), mctx)) : key;
        if (matrix && !(def.name && String(def.name).includes('matrix.'))) name = `${name} (${Object.values(matrix).map(v => typeof v === 'object' ? JSON.stringify(v) : String(v)).join(', ')})`;
        const pool = this.poolFor(typeof def['runs-on'] === 'string' ? str(interpolate(def['runs-on'], mctx)) : def['runs-on'], { file: wf.file, key });
        entry.parts.push({ name, matrix, pool, def, needsCtx, job: null, runner: null, done: false });
      }
    }
    // start parts whose pools have room
    for (const entry of state.jobs.values()) {
      if (!entry.parts) continue;
      for (const part of entry.parts) {
        if (part.job) continue;
        if (state.cancelled) { part.done = true; part.result = 'cancelled'; continue; }
        if (!part.pool) {
          part.job = this.store.addJob(run, { key: entry.key, name: part.name, labels: [].concat(part.def['runs-on'] || []) });
          Object.assign(part.job, { status: 'completed', conclusion: 'failure', started_at: new Date().toISOString(), completed_at: new Date().toISOString() });
          this.store.save(run); part.done = true; part.result = 'failure';
          this.log(`run ${run.id} job ${part.name}: no local pool has labels ${JSON.stringify(part.def['runs-on'])}`);
          continue;
        }
        const pool = part.pool.pool;
        if (pool.pausable === true && this.paused()) continue; // the lane and CI workflows read the pause file themselves
        if (this.poolLoad(part.pool.name) >= this.poolMax(pool)) continue;
        if (pool.disabled || (this.mode === 'shadow' && pool.shadow === false)) {
          part.job = this.store.addJob(run, { key: entry.key, name: part.name, labels: [].concat(part.def['runs-on'] || []) });
          Object.assign(part.job, { status: 'completed', conclusion: 'skipped', started_at: new Date().toISOString(), completed_at: new Date().toISOString() });
          this.store.save(run); part.done = true; part.result = 'skipped';
          this.log(`run ${run.id} job ${part.name}: pool ${part.pool.name} does not run in ${this.mode} mode; skipped`);
          continue;
        }
        this.startPart(state, entry, part);
      }
      if (entry.parts.length && entry.parts.every(p => p.done) && !entry.done) {
        entry.done = true;
        const results = entry.parts.map(p => p.result);
        entry.result = results.includes('failure') ? 'failure' : results.includes('cancelled') ? 'cancelled' : results.every(r => r === 'skipped') ? 'skipped' : 'success';
        for (const p of entry.parts) Object.assign(entry.outputs, p.outputs || {});
      }
    }
    if (run.status !== 'completed' && this.runDone(state)) await this.finishRun(state);
  }

  startPart(state, entry, part) {
    const { run, wf } = state;
    entry.started = true;
    if (run.status !== 'in_progress') { run.status = 'in_progress'; run.run_started_at = run.run_started_at || new Date().toISOString(); this.emitWorkflowRun(state, 'in_progress'); }
    const job = this.store.addJob(run, { key: entry.key, name: part.name, labels: [].concat(part.def['runs-on'] || []) });
    part.job = job; part.slot = this.freeSlot(part.pool.name); part.started = true;
    job.status = 'in_progress'; job.started_at = new Date().toISOString();
    this.store.save(run);
    this.postStatus(run, job, 'pending');
    const runner = new JobRunner({ run, wf, jobKey: entry.key, jobDef: part.def, matrix: part.matrix, needs: part.needsCtx, store: this.store, config: this.c, pool: part.pool.pool, slot: part.slot,
      apiUrl: this.proxy.url, token: this.c.token, secrets: this.c.secrets || {}, vars: this.c.vars || {}, job, actionCache: this.actionCache,
      onUpdate: () => this.store.save(run) });
    part.runner = runner;
    this.log(`run ${run.id} job ${part.name} started on pool ${part.pool.name}`);
    runner.execute(job).then(({ conclusion, outputs }) => {
      Object.assign(job, { status: 'completed', conclusion, completed_at: new Date().toISOString(), outputs });
      part.done = true; part.result = conclusion; part.outputs = outputs;
      this.store.save(run);
      this.postStatus(run, job, conclusion);
      this.log(`run ${run.id} job ${part.name}: ${conclusion}`);
    }).catch(err => {
      this.log(`run ${run.id} job ${part.name} crashed: ${err.stack || err}`);
      Object.assign(job, { status: 'completed', conclusion: 'failure', completed_at: new Date().toISOString() });
      part.done = true; part.result = 'failure'; this.store.save(run); this.postStatus(run, job, 'failure');
    });
  }

  async finishRun(state) {
    const { run } = state;
    const results = [...state.jobs.values()].map(j => j.result);
    run.conclusion = state.cancelled ? 'cancelled' : results.includes('failure') ? 'failure' : results.includes('cancelled') ? 'cancelled' : results.length && results.every(r => r === 'skipped') ? 'skipped' : 'success';
    run.status = 'completed';
    this.store.save(run);
    this.active.delete(run.id);
    this.log(`run ${run.id} ${run.name}: ${run.conclusion}`);
    this.emitWorkflowRun(state, 'completed');
    this.writeHeartbeat();
  }

  async postStatus(run, job, conclusion) {
    if (this.c.postStatuses === false) return;
    const sha = run.head_sha;
    if (!/^[0-9a-f]{40}$/i.test(sha || '')) return;
    const state = { pending: 'pending', success: 'success', failure: 'failure', cancelled: 'error', skipped: 'success' }[conclusion] || 'error';
    const prefix = this.mode === 'shadow' ? (this.c.shadowStatusPrefix || 'buildlane-shadow / ') : (this.c.statusPrefix || '');
    const words = { pending: 'Running on the build PC', success: 'Passed on the build PC', failure: 'Failed on the build PC', cancelled: 'Cancelled', skipped: 'Skipped' }[conclusion] || conclusion;
    try {
      await this.gh.post(`${this.gh.base}/statuses/${sha}`, { state, context: `${prefix}${job.name}`, description: `${words} (run ${run.id})`.slice(0, 140), ...(this.c.statusUrl ? { target_url: this.c.statusUrl } : {}) });
    } catch (e) { this.log(`status for ${job.name} on ${sha.slice(0, 8)}: ${e.message}`); }
  }

  // Polling -----------------------------------------------------------------------
  async poll() {
    const s = this.loadState();
    const first = !s.initialized;
    const defaultBranch = this.c.defaultBranch || 'main';
    // pushes
    const branches = await this.gh.paginate(`${this.gh.base}/branches`, null, 20);
    const watched = this.c.watchBranches;
    for (const b of branches) {
      if (Array.isArray(watched) && watched.length && !workflow.filterMatch(watched, b.name)) continue;
      const before = s.branches[b.name];
      const after = b.commit.sha;
      s.branches[b.name] = after;
      if (first || before === after) continue;
      let files, commits = [];
      if (before) {
        try { const cmp = await this.gh.get(`${this.gh.base}/compare/${before}...${after}`); if (cmp.files && cmp.files.length < 300) files = cmp.files.flatMap(f => [f.filename, f.previous_filename].filter(Boolean)); commits = (cmp.commits || []).slice(-20); } catch (e) { this.log(`compare ${b.name}: ${e.message}`); }
      }
      const head = commits[commits.length - 1];
      const payload = { ref: `refs/heads/${b.name}`, before: before || ZERO, after, created: !before, forced: false, repository: { full_name: this.c.repo, default_branch: defaultBranch }, sender: { login: (head && head.author && head.author.login) || this.login },
        head_commit: head ? { id: head.sha, message: head.commit.message, timestamp: head.commit.committer.date, author: head.commit.author } : { id: after, message: '' }, commits: commits.map(c => ({ id: c.sha, message: c.commit.message })) };
      await this.trigger({ name: 'push', ref: `refs/heads/${b.name}`, sha: after, branch: b.name, payload, files, actor: payload.sender.login });
    }
    // pull requests
    const prs = await this.gh.paginate(`${this.gh.base}/pulls?state=open`, null, 10);
    const seen = new Set();
    for (const pr of prs) {
      seen.add(String(pr.number));
      const prev = s.prs[pr.number];
      const labels = (pr.labels || []).map(l => l.name);
      s.prs[pr.number] = { head: pr.head.sha, base: pr.base.ref, draft: pr.draft, labels };
      if (first) continue;
      const actions = [];
      if (!prev) actions.push('opened');
      else {
        if (prev.head !== pr.head.sha) actions.push('synchronize');
        if (prev.draft && !pr.draft) actions.push('ready_for_review');
        for (const l of labels) if (!(prev.labels || []).includes(l)) actions.push(`labeled:${l}`);
      }
      if (!actions.length) continue;
      const full = await this.gh.get(`${this.gh.base}/pulls/${pr.number}`);
      let files;
      try { const list = await this.gh.paginate(`${this.gh.base}/pulls/${pr.number}/files`, null, 30); files = list.flatMap(f => [f.filename, f.previous_filename].filter(Boolean)); } catch {}
      for (const a of actions) {
        const [action, label] = a.split(':');
        const payload = { action, number: pr.number, pull_request: full, repository: { full_name: this.c.repo, default_branch: defaultBranch }, sender: { login: full.user.login }, ...(label ? { label: { name: label } } : {}) };
        // pull_request: the PR's own workflow files at its merge commit (falls back to the head);
        // pull_request_target: the base branch's files, on the base branch.
        const mergeSha = full.merge_commit_sha && full.mergeable !== false ? full.merge_commit_sha : null;
        await this.trigger({ name: 'pull_request', action, ref: mergeSha ? `refs/pull/${pr.number}/merge` : `refs/pull/${pr.number}/head`, sha: mergeSha || full.head.sha, githubSha: mergeSha || full.head.sha, headSha: full.head.sha,
          branch: full.head.ref, payload, files, actor: full.user.login, prNumbers: [pr.number] });
        const baseSha = (await this.gh.get(`${this.gh.base}/branches/${encodeURIComponent(full.base.ref)}`)).commit.sha;
        await this.trigger({ name: 'pull_request_target', action, ref: `refs/heads/${full.base.ref}`, sha: baseSha, headSha: full.head.sha, branch: full.base.ref, payload, files, actor: full.user.login, prNumbers: [pr.number] });
      }
    }
    for (const n of Object.keys(s.prs)) if (!seen.has(n)) delete s.prs[n];
    // schedules
    const now = new Date();
    const head = s.branches[defaultBranch] || await this.defaultHead();
    const wfs = await this.workflowsAt(head);
    this.lastDefaultWorkflows = wfs;
    if (!first && s.lastSchedule) {
      const from = new Date(s.lastSchedule);
      for (const wf of wfs) {
        for (const sched of [].concat(wf.on.schedule || [])) {
          if (!sched || !sched.cron) continue;
          let fires = [];
          try { fires = cron.firesBetween(sched.cron, from, now, 1); } catch (e) { this.log(`${wf.file} cron '${sched.cron}': ${e.message}`); }
          if (fires.length) await this.trigger({ name: 'schedule', cron: sched.cron, ref: `refs/heads/${defaultBranch}`, sha: head, branch: defaultBranch, payload: { schedule: sched.cron, repository: { full_name: this.c.repo } }, actor: this.login, workflows: [wf] });
        }
      }
    }
    s.lastSchedule = now.toISOString();
    // command comments
    if (this.c.commandIssue) await this.pollCommands(s, first);
    s.initialized = true;
    this.saveState(s);
    await this.consumeRequests();
  }

  async pollCommands(s, first) {
    const comments = await this.gh.paginate(`${this.gh.base}/issues/${this.c.commandIssue}/comments${s.lastCommentAt ? `?since=${encodeURIComponent(s.lastCommentAt)}` : ''}`, null, 5);
    for (const c of comments) {
      if (c.id <= (s.lastCommentId || 0)) continue;
      s.lastCommentId = c.id; s.lastCommentAt = c.created_at;
      if (first) continue;
      const lines = String(c.body || '').split(/\r?\n/).map(l => l.trim()).filter(l => /^\/buildlane\s/i.test(l));
      if (!lines.length) continue;
      const author = c.user.login;
      const allowedBy = (this.c.dispatchers || [this.login]).map(x => x.toLowerCase());
      const replies = [];
      for (const line of lines) {
        if (!allowedBy.includes(author.toLowerCase())) { replies.push(`\`${line}\`: ${author} may not send Buildlane commands (dispatchers in the runner config).`); continue; }
        try { replies.push(await this.command(line, author)); }
        catch (e) { replies.push(`\`${line}\`: ${e.message}`); }
      }
      if (this.mode !== 'shadow' || this.c.replyInShadow) {
        try { await this.gh.post(`${this.gh.base}/issues/${this.c.commandIssue}/comments`, { body: `${replies.map(r => `- ${r}`).join('\n')}\n\n<sub>Buildlane local runner (${this.mode})</sub>` }); } catch (e) { this.log(`command reply: ${e.message}`); }
      } else this.log(`commands from ${author}: ${replies.join(' | ')}`);
    }
  }

  // /buildlane run <workflow.yml> [ref=<branch>] [name=value ...] | cancel <run> | rerun <run> [failed] | status
  async command(line, actor) {
    const tokens = (line.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || []).slice(1).map(t => t.replace(/^(\w[\w-]*)=(["'])([\s\S]*)\2$/, '$1=$3').replace(/^(["'])([\s\S]*)\1$/, '$2'));
    const verb = (tokens.shift() || '').toLowerCase();
    if (verb === 'run' || verb === 'dispatch') {
      const file = tokens.shift(); if (!file) throw new Error('name a workflow file');
      const inputs = {}; let ref;
      for (const t of tokens) { const i = t.indexOf('='); if (i < 1) throw new Error(`'${t}' is not name=value`); const k = t.slice(0, i), v = t.slice(i + 1); if (k === 'ref') ref = v; else inputs[k] = v; }
      const run = await this.dispatch(file, ref, inputs, actor, 0);
      return `\`${file}\` queued as local run ${run.id} by ${actor}.`;
    }
    if (verb === 'cancel') { const id = tokens[0]; return this.cancelRun(id) ? `run ${id} is being cancelled.` : `run ${id} is not running.`; }
    if (verb === 'rerun') { await this.rerun(tokens[0], tokens[1] === 'failed'); return `run ${tokens[0]} queued again.`; }
    if (verb === 'status') {
      const active = [...this.active.values()].map(s => `${s.run.id} ${s.wf.name} (${s.run.status})`);
      return active.length ? `active: ${active.join('; ')}` : 'idle.';
    }
    throw new Error(`unknown command '${verb}'`);
  }

  // Requests dropped into <root>/requests by the Buildlane app or the CLI.
  async consumeRequests() {
    for (const f of fs.readdirSync(this.requestsDir).filter(n => n.endsWith('.json')).sort()) {
      const p = path.join(this.requestsDir, f);
      let req; try { req = JSON.parse(fs.readFileSync(p, 'utf8')); } catch (e) { this.log(`request ${f}: ${e.message}`); fs.renameSync(p, p + '.bad'); continue; }
      const result = { at: new Date().toISOString() };
      try {
        if (req.kind === 'dispatch') result.runId = (await this.dispatch(req.workflow, req.ref, req.inputs || {}, this.login, 0)).id;
        else if (req.kind === 'cancel') result.ok = this.cancelRun(req.runId);
        else if (req.kind === 'rerun') { await this.rerun(req.runId, !!req.failedOnly); result.ok = true; }
        else throw new Error(`unknown request kind ${req.kind}`);
      } catch (e) { result.error = e.message; this.log(`request ${f}: ${e.message}`); }
      fs.writeFileSync(p.replace(/\.json$/, '.done'), JSON.stringify({ ...req, result }, null, 1));
      fs.unlinkSync(p);
    }
  }

  async loop() {
    let lastPoll = 0;
    const pollMs = (this.c.pollSeconds || 60) * 1000;
    this.stopping = false;
    while (!this.stopping) {
      if (Date.now() - lastPoll >= pollMs || fs.readdirSync(this.requestsDir).some(n => n.endsWith('.json'))) {
        lastPoll = Date.now();
        try { await this.poll(); } catch (e) { this.log(`poll failed: ${e.message}`); }
      }
      try { await this.tick(); } catch (e) { this.log(`tick failed: ${e.stack || e}`); }
      try { this.writeHeartbeat(); } catch {}
      if (Date.now() - (this.lastHousekeep || 0) > 3600000) try { this.housekeep(); } catch (e) { this.log(`housekeeping failed: ${e.message}`); }
      await new Promise(r => setTimeout(r, 2000));
    }
  }
}

module.exports = { LocalRunner, matrixCombos };
