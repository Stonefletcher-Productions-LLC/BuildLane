'use strict';
// Runs one job of a workflow on this machine, the way a self-hosted Actions runner would: run steps in
// the requested shell, JavaScript and composite actions, environment files, outputs, if: conditions,
// timeouts, continue-on-error and post steps. Child processes run at below-normal priority and are
// stopped by process tree (only the processes this runner started).
const fs = require('fs');
const os = require('os');
const path = require('path');
const crypto = require('crypto');
const { spawn, spawnSync } = require('child_process');
const { evaluateIf, interpolate, interpolateDeep, str } = require('./expr.cjs');
const { parseEnvFile, parseCommand } = require('./commands.cjs');
const builtins = require('./builtins.cjs');
const YAML = require('../vendor/yaml.cjs');

const WIN = process.platform === 'win32';

function which(cmd, extraPaths = []) {
  const exts = WIN ? (process.env.PATHEXT || '.EXE;.CMD;.BAT').split(';') : [''];
  for (const dir of [...extraPaths, ...(process.env.PATH || '').split(path.delimiter)]) {
    if (!dir) continue;
    for (const e of exts) { const p = path.join(dir, cmd + e); try { if (fs.statSync(p).isFile()) return p; } catch {} }
  }
  return null;
}

function killTree(child) {
  if (!child || child.exitCode !== null || child.killed) return;
  try {
    if (WIN) spawnSync('taskkill', ['/PID', String(child.pid), '/T', '/F'], { stdio: 'ignore' });
    else process.kill(-child.pid, 'SIGKILL');
  } catch { try { child.kill('SIGKILL'); } catch {} }
}

class JobLog {
  constructor(file, onLine) { this.file = file; this.onLine = onLine; fs.mkdirSync(path.dirname(file), { recursive: true }); this.fd = fs.openSync(file, 'a'); this.masks = []; }
  mask(v) { if (v && String(v).length >= 4) this.masks.push(String(v)); }
  write(line) {
    let l = String(line);
    for (const m of this.masks) l = l.split(m).join('***');
    fs.writeSync(this.fd, `${new Date().toISOString()} ${l}\n`);
    if (this.onLine) this.onLine(l);
  }
  close() { try { fs.closeSync(this.fd); } catch {} }
}

class JobRunner {
  // o: { run, wf, jobKey, jobDef, matrix, needs, store, config, apiUrl, token, secrets, vars, log, signal, job }
  constructor(o) { this.o = o; this.status = 'success'; this.posts = []; this.cancelled = false; this.children = new Set(); }

  cancel(reason = 'cancelled') { this.cancelled = true; this.status = 'cancelled'; this.cancelReason = reason; for (const c of this.children) killTree(c); }

  ctxStatus() { return this.cancelled ? 'cancelled' : this.status; }

  prepare() {
    const { run, wf, jobKey, config, store, job } = this.o;
    const pool = this.o.pool || {};
    const baseRoot = pool.workRoot || path.join(config.root, '_work');
    // Jobs written for Linux runners get LF checkouts (as on Linux) in their own work folder, so a Windows
    // job's CRLF checkout never reaches them (Git for Windows sets core.autocrlf=true system-wide).
    this.linux = WIN && /ubuntu|linux/.test([].concat(this.o.jobDef['runs-on'] || []).map(String).join(' ').toLowerCase());
    const workRoot = `${baseRoot}${this.linux ? '-linux' : ''}${this.o.slot ? `-${this.o.slot + 1}` : ''}`;
    const repoName = run.repository.name;
    this.workspace = pool.workspace || path.join(workRoot, repoName, repoName);
    this.temp = path.join(workRoot, '_temp', String(job.id));
    this.toolCache = path.join(workRoot, '_tool');
    for (const d of [this.workspace, this.temp, this.toolCache]) fs.mkdirSync(d, { recursive: true });
    this.files = {};
    for (const k of ['GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY', 'GITHUB_STATE']) this.files[k] = path.join(this.temp, `_runner_file_commands`, `${k.toLowerCase()}`);
    fs.mkdirSync(path.join(this.temp, '_runner_file_commands'), { recursive: true });
    this.eventPath = path.join(this.temp, '_github_workflow', 'event.json');
    fs.mkdirSync(path.dirname(this.eventPath), { recursive: true });
    fs.writeFileSync(this.eventPath, JSON.stringify(run.payload || {}, null, 2));
    this.summaryFile = path.join(store.runDir(run.id), 'logs', `${job.id}.summary.md`);
    this.log = new JobLog(store.logPath(run.id, job.id), this.o.onLine);
    this.log.mask(this.o.token);
    for (const v of Object.values(this.o.secrets || {})) this.log.mask(v);
    this.shims = this.makeShims();
    this.extraPath = [];
    this.jobEnv = {};
    this.stepsCtx = {};
    this.github = this.githubContext();
  }

  makeShims() {
    // python3 -> python for workflows written for Linux runners; harmless elsewhere.
    const dir = path.join(this.o.config.root, '_shims'); fs.mkdirSync(dir, { recursive: true });
    if (WIN && !which('python3')) {
      fs.writeFileSync(path.join(dir, 'python3.cmd'), '@python %*\r\n');
      fs.writeFileSync(path.join(dir, 'python3'), '#!/bin/sh\nexec python "$@"\n');
    }
    return dir;
  }

  githubContext() {
    const { run, wf, jobKey, config } = this.o;
    const ref = run.ref || (run.head_branch ? `refs/heads/${run.head_branch}` : '');
    const isPr = run.event === 'pull_request' || run.event === 'pull_request_target';
    const pr = run.payload && run.payload.pull_request;
    return {
      token: this.o.token, job: jobKey, ref, ref_name: isPr ? `${pr.number}/merge` : ref.replace(/^refs\/(heads|tags)\//, ''),
      ref_type: ref.startsWith('refs/tags/') ? 'tag' : 'branch', ref_protected: false,
      sha: run.github_sha || run.head_sha, repository: run.repository.full_name, repository_owner: run.repository.owner.login,
      repositoryUrl: `git://github.com/${run.repository.full_name}.git`, event_name: run.event, event: run.payload || {},
      run_id: String(run.id), run_number: String(run.run_number), run_attempt: String(run.run_attempt), retention_days: '90',
      actor: run.actor.login, triggering_actor: run.triggering_actor.login, workflow: wf.name, workflow_ref: `${run.repository.full_name}/${wf.path}@${ref}`,
      workflow_sha: run.workflow_sha || run.head_sha,
      head_ref: isPr ? pr.head.ref : '', base_ref: isPr ? pr.base.ref : '', server_url: 'https://github.com', api_url: this.o.apiUrl,
      graphql_url: `${this.o.apiUrl}/graphql`, workspace: this.workspace, action: '', action_path: '', path: this.files.GITHUB_PATH,
      env: this.files.GITHUB_ENV, output: this.files.GITHUB_OUTPUT, step_summary: this.files.GITHUB_STEP_SUMMARY, state: this.files.GITHUB_STATE,
      secret_source: 'Actions',
    };
  }

  contexts(extra = {}) {
    const { run, matrix, needs, secrets, vars } = this.o;
    return {
      github: { ...this.github, ...(extra.github || {}) },
      env: { ...this.wfEnv, ...this.jobEnvDecl, ...this.jobEnv, ...(extra.env || {}) },
      vars: vars || {}, secrets: { GITHUB_TOKEN: this.o.token, ...(secrets || {}) },
      inputs: extra.inputs || run.inputs || {}, steps: extra.steps || this.stepsCtx, needs: needs || {}, matrix: matrix || {},
      strategy: { 'fail-fast': true, 'job-index': 0, 'job-total': 1, 'max-parallel': 1 },
      runner: { os: WIN ? 'Windows' : (process.platform === 'darwin' ? 'macOS' : 'Linux'), arch: os.arch() === 'x64' ? 'X64' : os.arch().toUpperCase(), name: 'buildlane-local', temp: this.temp, tool_cache: this.toolCache, environment: 'self-hosted', debug: '' },
      job: { status: this.ctxStatus(), container: {}, services: {} },
    };
  }

  exprCtx(extra) { return { contexts: this.contexts(extra), status: () => this.ctxStatus(), hashFiles: pats => this.hashFiles(pats) }; }

  hashFiles(patterns) {
    const { globToRegex } = require('./workflow.cjs');
    const res = patterns.map(p => globToRegex(p.replace(/^\.\//, '')));
    const files = require('./zip.cjs').listFiles(this.workspace).map(f => path.relative(this.workspace, f).replace(/\\/g, '/')).filter(f => !f.startsWith('.git/') && res.some(r => r.test(f))).sort();
    if (!files.length) return '';
    const h = crypto.createHash('sha256');
    for (const f of files) h.update(crypto.createHash('sha256').update(fs.readFileSync(path.join(this.workspace, f))).digest());
    return h.digest('hex');
  }

  baseEnv() {
    const g = this.github; const { run, config } = this.o;
    const pathParts = [this.shims, ...this.extraPath.slice().reverse()];
    const env = { ...process.env, ...(config.env || {}) };
    for (const k of Object.keys(env)) if (/^(INPUT_|STATE_|GITHUB_|RUNNER_|ACTIONS_)/i.test(k)) delete env[k];
    // The local API is on 127.0.0.1: never send it through a proxy.
    if (this.linux) {
      const n = Number(env.GIT_CONFIG_COUNT || 0);
      Object.assign(env, { GIT_CONFIG_COUNT: String(n + 2), [`GIT_CONFIG_KEY_${n}`]: 'core.autocrlf', [`GIT_CONFIG_VALUE_${n}`]: 'false', [`GIT_CONFIG_KEY_${n + 1}`]: 'core.eol', [`GIT_CONFIG_VALUE_${n + 1}`]: 'lf' });
    }
    const noProxy = [env.NO_PROXY || env.no_proxy, '127.0.0.1', 'localhost'].filter(Boolean).join(',');
    env.NO_PROXY = noProxy; env.no_proxy = noProxy;
    const pathKey = Object.keys(env).find(k => k.toLowerCase() === 'path') || 'PATH';
    env[pathKey] = [...pathParts, env[pathKey] || ''].join(path.delimiter);
    Object.assign(env, {
      CI: 'true', GITHUB_ACTIONS: 'true', BUILDLANE_LOCAL_RUNNER: '1', GITHUB_REPOSITORY: g.repository, GITHUB_REPOSITORY_OWNER: g.repository_owner,
      GITHUB_RUN_ID: g.run_id, GITHUB_RUN_NUMBER: g.run_number, GITHUB_RUN_ATTEMPT: g.run_attempt, GITHUB_JOB: g.job, GITHUB_SHA: g.sha,
      GITHUB_REF: g.ref, GITHUB_REF_NAME: g.ref_name, GITHUB_REF_TYPE: g.ref_type, GITHUB_HEAD_REF: g.head_ref, GITHUB_BASE_REF: g.base_ref,
      GITHUB_EVENT_NAME: g.event_name, GITHUB_EVENT_PATH: this.eventPath, GITHUB_ACTOR: g.actor, GITHUB_TRIGGERING_ACTOR: g.triggering_actor,
      GITHUB_WORKFLOW: g.workflow, GITHUB_WORKFLOW_REF: g.workflow_ref, GITHUB_WORKFLOW_SHA: g.workflow_sha, GITHUB_WORKSPACE: this.workspace,
      GITHUB_SERVER_URL: g.server_url, GITHUB_API_URL: this.o.apiUrl, GITHUB_GRAPHQL_URL: g.graphql_url, GITHUB_RETENTION_DAYS: '90',
      GITHUB_OUTPUT: this.files.GITHUB_OUTPUT, GITHUB_ENV: this.files.GITHUB_ENV, GITHUB_PATH: this.files.GITHUB_PATH,
      GITHUB_STEP_SUMMARY: this.files.GITHUB_STEP_SUMMARY, GITHUB_STATE: this.files.GITHUB_STATE,
      RUNNER_OS: this.contexts().runner.os, RUNNER_ARCH: this.contexts().runner.arch, RUNNER_NAME: 'buildlane-local', RUNNER_TEMP: this.temp,
      RUNNER_TOOL_CACHE: this.toolCache, RUNNER_ENVIRONMENT: 'self-hosted', RUNNER_WORKSPACE: path.dirname(this.workspace),
    });
    return env;
  }

  resetFiles() {
    for (const k of ['GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY']) fs.writeFileSync(this.files[k], '');
    if (!fs.existsSync(this.files.GITHUB_STATE)) fs.writeFileSync(this.files.GITHUB_STATE, '');
  }

  collectFiles(stepOutputs, actionState) {
    Object.assign(stepOutputs, parseEnvFile(this.files.GITHUB_OUTPUT));
    Object.assign(this.jobEnv, parseEnvFile(this.files.GITHUB_ENV));
    try { for (const l of fs.readFileSync(this.files.GITHUB_PATH, 'utf8').split(/\r?\n/)) if (l.trim()) this.extraPath.push(l.trim()); } catch {}
    try { const s = fs.readFileSync(this.files.GITHUB_STEP_SUMMARY, 'utf8'); if (s) fs.appendFileSync(this.summaryFile, s + '\n'); } catch {}
    if (actionState) { Object.assign(actionState, parseEnvFile(this.files.GITHUB_STATE)); fs.writeFileSync(this.files.GITHUB_STATE, ''); }
  }

  spawnLogged(file, args, { cwd, env, timeoutMs, stepOutputs }) {
    return new Promise(resolve => {
      if (this.cancelled) return resolve({ code: null, cancelled: true });
      let child;
      try { child = spawn(file, args, { cwd, env, windowsHide: true, detached: !WIN, stdio: ['ignore', 'pipe', 'pipe'], windowsVerbatimArguments: WIN && /cmd(\.exe)?$/i.test(file) }); }
      catch (e) { this.log.write(`##[error]${e.message}`); return resolve({ code: 1 }); }
      this.children.add(child);
      try { os.setPriority(child.pid, os.constants.priority.PRIORITY_BELOW_NORMAL); } catch {}
      let stopCommands = null, timedOut = false;
      const onData = stream => { let buf = ''; stream.on('data', d => { buf += d.toString(); let i; while ((i = buf.indexOf('\n')) >= 0) { handle(buf.slice(0, i).replace(/\r$/, '')); buf = buf.slice(i + 1); } }); stream.on('end', () => { if (buf) handle(buf); }); };
      const handle = line => {
        if (stopCommands) { if (line.trim() === `::${stopCommands}::`) stopCommands = null; this.log.write(line); return; }
        const c = parseCommand(line);
        if (!c) return this.log.write(line);
        switch (c.cmd) {
          case 'add-mask': this.log.mask(c.data); this.log.write('***'); break;
          case 'set-output': if (c.props.name) stepOutputs[c.props.name] = c.data; break;
          case 'save-state': break;
          case 'stop-commands': stopCommands = c.data; break;
          case 'group': this.log.write(`##[group]${c.data}`); break;
          case 'endgroup': this.log.write('##[endgroup]'); break;
          case 'error': case 'warning': case 'notice': case 'debug':
            this.log.write(`##[${c.cmd}]${c.props.title ? c.props.title + ': ' : ''}${c.data}`);
            if (c.cmd !== 'debug') (this.annotations = this.annotations || []).push({ level: c.cmd, message: c.data }); break;
          default: this.log.write(line);
        }
      };
      onData(child.stdout); onData(child.stderr);
      const timer = timeoutMs ? setTimeout(() => { timedOut = true; this.log.write(`##[error]The step has timed out after ${Math.round(timeoutMs / 60000)} minutes.`); killTree(child); }, timeoutMs) : null;
      child.on('error', e => { this.log.write(`##[error]${e.message}`); });
      child.on('close', code => { if (timer) clearTimeout(timer); this.children.delete(child); resolve({ code: code === null ? 1 : code, timedOut, cancelled: this.cancelled }); });
    });
  }

  shellFor(step, jobDef) {
    const d = (jobDef.defaults && jobDef.defaults.run) || {};
    const wd = (this.o.wf.doc.defaults && this.o.wf.doc.defaults.run) || {};
    let shell = step.shell || d.shell || wd.shell;
    if (!shell) {
      const labels = [].concat(jobDef['runs-on'] || []).map(String).join(' ').toLowerCase();
      if (/ubuntu|linux|macos/.test(labels) || !WIN) shell = 'bash';
      else shell = this.findPwsh() ? 'pwsh' : 'powershell';
    }
    return shell;
  }
  findPwsh() { if (this._pwsh === undefined) this._pwsh = this.o.config.pwshPath || which('pwsh'); return this._pwsh; }
  findBash() {
    if (this._bash !== undefined) return this._bash;
    const cfg = this.o.config.bashPath;
    const candidates = WIN ? [cfg, 'C:\\Program Files\\Git\\bin\\bash.exe', 'C:\\Program Files\\Git\\usr\\bin\\bash.exe', 'C:\\Program Files (x86)\\Git\\bin\\bash.exe'] : [cfg, which('bash')];
    this._bash = candidates.find(p => p && fs.existsSync(p)) || null;
    return this._bash;
  }

  buildCommand(shell, script, dir) {
    const id = crypto.randomUUID();
    const sh = shell.trim();
    const write = (ext, text, bom = false) => { const f = path.join(dir, `${id}${ext}`); fs.writeFileSync(f, (bom ? '\uFEFF' : '') + text); return f; };
    const psWrap = s => `$ErrorActionPreference = 'stop'\r\n${s}\r\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\r\n`;
    switch (sh) {
      case 'bash': {
        const bash = this.findBash(); if (!bash) throw new Error('bash was not found; install Git for Windows or set bashPath in the runner config');
        const f = write('.sh', script.replace(/\r\n/g, '\n')); return [bash, ['--noprofile', '--norc', '-eo', 'pipefail', f]];
      }
      case 'sh': { const bash = this.findBash(); const f = write('.sh', script.replace(/\r\n/g, '\n')); return [WIN ? bash : 'sh', WIN ? ['-e', f] : ['-e', f]]; }
      case 'pwsh': { const p = this.findPwsh() || 'powershell'; const f = write('.ps1', psWrap(script), true); return [p, ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', `. '${f.replace(/'/g, "''")}'`]]; }
      case 'powershell': { const f = write('.ps1', psWrap(script), true); return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', `. '${f.replace(/'/g, "''")}'`]]; }
      case 'python': { const f = write('.py', script); return [this.o.config.pythonPath || (WIN ? 'python' : 'python3'), [f]]; }
      case 'cmd': { const f = write('.cmd', script.replace(/\r?\n/g, '\r\n')); return [process.env.ComSpec || 'cmd.exe', ['/D', '/E:ON', '/V:OFF', '/S', '/C', `"CALL "${f}""`]]; }
      default: {
        if (!sh.includes('{0}')) throw new Error(`Unsupported shell '${sh}'`);
        const f = write('.tmp', script);
        const parts = sh.replace('{0}', f).match(/"[^"]*"|\S+/g).map(p => p.replace(/^"|"$/g, ''));
        return [parts[0], parts.slice(1)];
      }
    }
  }

  stepName(step, i) {
    if (step.name) return str(interpolate(String(step.name), this.exprCtx()));
    if (step.uses) return `Run ${step.uses}`;
    if (step.run) return `Run ${String(step.run).trim().split(/\r?\n/)[0]}`;
    return `Step ${i + 1}`;
  }

  async runSteps(steps, jobDef, record) {
    for (let i = 0; i < steps.length; i++) {
      const step = steps[i];
      const rec = { name: this.stepName(step, i), number: record.steps.length + 1, status: 'queued', conclusion: null, started_at: null, completed_at: null };
      record.steps.push(rec); this.o.onUpdate && this.o.onUpdate();
      const stepOutputs = {};
      const key = step.id || `__${i}`;
      let run = false;
      try { run = evaluateIf(step.if, this.exprCtx()); }
      catch (e) { this.log.write(`##[error]if: ${e.message}`); this.status = 'failure'; }
      if (!run) {
        Object.assign(rec, { status: 'completed', conclusion: 'skipped', started_at: new Date().toISOString(), completed_at: new Date().toISOString() });
        this.stepsCtx[key] = { outputs: {}, outcome: 'skipped', conclusion: 'skipped' };
        continue;
      }
      rec.status = 'in_progress'; rec.started_at = new Date().toISOString(); this.o.onUpdate && this.o.onUpdate();
      this.log.write(`##[group]${rec.name}`);
      let outcome = 'success';
      try { outcome = await this.runStep(step, jobDef, stepOutputs); }
      catch (e) { this.log.write(`##[error]${e.message}`); outcome = 'failure'; }
      if (this.cancelled && outcome !== 'success') outcome = 'cancelled';
      this.log.write('##[endgroup]');
      let coe = false;
      try { coe = step['continue-on-error'] !== undefined && !!interpolate(step['continue-on-error'], this.exprCtx(), true); } catch {}
      const conclusion = outcome === 'failure' && coe ? 'success' : outcome;
      this.stepsCtx[key] = { outputs: stepOutputs, outcome, conclusion };
      Object.assign(rec, { status: 'completed', conclusion, completed_at: new Date().toISOString() });
      if (conclusion === 'failure') this.status = 'failure';
      this.o.onUpdate && this.o.onUpdate();
    }
  }

  async runStep(step, jobDef, stepOutputs) {
    const ctx = this.exprCtx();
    const stepEnv = interpolateDeep(step.env || {}, ctx);
    const env = { ...this.baseEnv(), ...this.wfEnv, ...this.jobEnvDecl, ...this.jobEnv, ...Object.fromEntries(Object.entries(stepEnv).map(([k, v]) => [k, str(v)])) };
    const timeoutMs = step['timeout-minutes'] ? Number(interpolate(step['timeout-minutes'], ctx, true)) * 60000 : 0;
    this.resetFiles();
    if (step.run !== undefined) {
      const script = str(interpolate(String(step.run), ctx));
      const shell = this.shellFor(step, jobDef);
      const wdDefault = (jobDef.defaults && jobDef.defaults.run && jobDef.defaults.run['working-directory']) || (this.o.wf.doc.defaults && this.o.wf.doc.defaults.run && this.o.wf.doc.defaults.run['working-directory']);
      const wd = step['working-directory'] || wdDefault;
      const cwd = wd ? path.resolve(this.workspace, str(interpolate(String(wd), ctx))) : this.workspace;
      const [file, args] = this.buildCommand(shell, script, this.temp);
      const r = await this.spawnLogged(file, args, { cwd, env, timeoutMs, stepOutputs });
      this.collectFiles(stepOutputs);
      if (r.cancelled) return 'cancelled';
      if (r.code !== 0) { this.log.write(`##[error]Process completed with exit code ${r.code}.`); return 'failure'; }
      return 'success';
    }
    if (step.uses) {
      const withInputs = interpolateDeep(step.with || {}, ctx);
      return this.runAction(String(step.uses), withInputs, env, timeoutMs, stepOutputs, step);
    }
    throw new Error('A step needs run or uses');
  }

  async runAction(uses, inputs, env, timeoutMs, stepOutputs, step) {
    const builtin = builtins.find(uses);
    if (builtin) {
      const r = await builtin.run({ job: this, inputs, env, stepOutputs, uses });
      this.collectFiles(stepOutputs);
      return r || 'success';
    }
    if (uses.startsWith('docker://')) throw new Error(`Docker actions are not supported by the local runner (${uses})`);
    const local = builtins.localAction(uses);
    let dir, meta;
    if (local) ({ dir, meta } = local);
    else {
      dir = uses.startsWith('./') ? path.resolve(this.workspace, uses) : this.o.actionCache.resolve(uses, this.log);
      const metaFile = ['action.yml', 'action.yaml'].map(f => path.join(dir, f)).find(f => fs.existsSync(f));
      if (!metaFile) throw new Error(`No action.yml in ${uses}`);
      meta = YAML.parse(fs.readFileSync(metaFile, 'utf8'));
    }
    const ctx = this.exprCtx();
    const inputEnv = {};
    const resolvedInputs = {};
    for (const [k, d] of Object.entries(meta.inputs || {})) {
      let v = inputs[k] !== undefined ? inputs[k] : (d && d.default !== undefined && d.default !== null ? interpolate(String(d.default), ctx) : '');
      resolvedInputs[k] = str(v);
      inputEnv[`INPUT_${k.replace(/ /g, '_').toUpperCase()}`] = str(v);
    }
    for (const [k, v] of Object.entries(inputs)) if (!(k in (meta.inputs || {}))) { inputEnv[`INPUT_${k.replace(/ /g, '_').toUpperCase()}`] = str(v); resolvedInputs[k] = str(v); }
    const using = String(meta.runs && meta.runs.using || '');
    if (/^node\d+$/.test(using)) {
      const state = {};
      const nodeEnv = { ...env, ...inputEnv, GITHUB_ACTION_PATH: dir, GITHUB_ACTION_REPOSITORY: uses.split('@')[0], GITHUB_ACTION_REF: uses.split('@')[1] || '' };
      const node = this.o.config.nodePath || process.execPath;
      if (meta.runs.pre) {
        const r = await this.spawnLogged(node, [path.join(dir, meta.runs.pre)], { cwd: this.workspace, env: nodeEnv, timeoutMs, stepOutputs });
        this.collectFiles(stepOutputs, state); if (r.code !== 0) return 'failure';
      }
      const r = await this.spawnLogged(node, [path.join(dir, meta.runs.main)], { cwd: this.workspace, env: nodeEnv, timeoutMs, stepOutputs });
      this.collectFiles(stepOutputs, state);
      if (meta.runs.post) this.posts.push({ name: `Post ${this.stepName(step, 0)}`, node, file: path.join(dir, meta.runs.post), env: nodeEnv, state, cond: meta.runs['post-if'] || 'always()' });
      if (r.cancelled) return 'cancelled';
      return r.code === 0 ? 'success' : 'failure';
    }
    if (using === 'composite') {
      const saveSteps = this.stepsCtx; this.stepsCtx = {}; const prevStatus = this.status;
      const nested = { steps: [] };
      const inner = (meta.runs.steps || []).map(s => ({ ...s, env: { ...(s.env || {}) } }));
      const saveGithub = this.github; this.github = { ...this.github, action_path: dir };
      const savedInputs = this.o.run.inputs; this.o.run = { ...this.o.run, inputs: resolvedInputs };
      try { await this.runSteps(inner, { defaults: {} }, nested); }
      finally { this.github = saveGithub; this.o.run = { ...this.o.run, inputs: savedInputs }; }
      const ctxInner = this.exprCtx({ steps: this.stepsCtx, inputs: resolvedInputs });
      for (const [k, d] of Object.entries(meta.outputs || {})) stepOutputs[k] = str(interpolate(String(d.value || ''), ctxInner));
      const failed = nested.steps.some(s => s.conclusion === 'failure');
      this.stepsCtx = saveSteps;
      this.status = this.cancelled ? 'cancelled' : prevStatus;
      return failed ? 'failure' : 'success';
    }
    throw new Error(`Action runtime '${using}' is not supported by the local runner (${uses})`);
  }

  async runPosts(record) {
    for (const p of this.posts.reverse()) {
      let run = true;
      try { run = evaluateIf(p.cond, this.exprCtx()); } catch {}
      const rec = { name: p.name, number: record.steps.length + 1, status: 'in_progress', conclusion: null, started_at: new Date().toISOString(), completed_at: null };
      record.steps.push(rec);
      if (!run) { Object.assign(rec, { status: 'completed', conclusion: 'skipped', completed_at: new Date().toISOString() }); continue; }
      const env = { ...p.env, ...Object.fromEntries(Object.entries(p.state).map(([k, v]) => [`STATE_${k}`, v])) };
      this.resetFiles();
      const wasCancelled = this.cancelled; this.cancelled = false;
      const r = await this.spawnLogged(p.node, [p.file], { cwd: this.workspace, env, timeoutMs: 300000, stepOutputs: {} });
      this.cancelled = wasCancelled;
      Object.assign(rec, { status: 'completed', conclusion: r.code === 0 ? 'success' : 'failure', completed_at: new Date().toISOString() });
    }
  }

  // Runs the job; fills record (the store job object) with steps and conclusion; returns { conclusion, outputs }.
  async execute(record) {
    const { jobDef, wf } = this.o;
    this.prepare();
    const ctx0 = this.exprCtx();
    this.wfEnv = Object.fromEntries(Object.entries(interpolateDeep(wf.doc.env || {}, ctx0)).map(([k, v]) => [k, str(v)]));
    this.jobEnvDecl = {};
    this.jobEnvDecl = Object.fromEntries(Object.entries(interpolateDeep(jobDef.env || {}, this.exprCtx())).map(([k, v]) => [k, str(v)]));
    const start = { name: 'Set up job', number: 1, status: 'completed', conclusion: 'success', started_at: new Date().toISOString(), completed_at: new Date().toISOString() };
    record.steps.push(start);
    this.log.write(`Buildlane local runner: ${wf.name} / ${record.name} (run ${this.o.run.id}, job ${record.id})`);
    this.log.write(`Workspace: ${this.workspace}`);
    const jobTimeout = Number(jobDef['timeout-minutes'] ? interpolate(jobDef['timeout-minutes'], this.exprCtx(), true) : 360) * 60000;
    const timer = setTimeout(() => { this.log.write(`##[error]The job running on buildlane-local has exceeded the maximum execution time of ${jobTimeout / 60000} minutes.`); this.cancel('timeout'); this.timedOut = true; }, jobTimeout);
    try {
      await this.runSteps(jobDef.steps || [], jobDef, record);
      await this.runPosts(record);
    } finally { clearTimeout(timer); }
    let outputs = {};
    try { outputs = Object.fromEntries(Object.entries(jobDef.outputs || {}).map(([k, v]) => [k, str(interpolate(String(v), this.exprCtx()))])); }
    catch (e) { this.log.write(`##[warning]outputs: ${e.message}`); }
    record.steps.push({ name: 'Complete job', number: record.steps.length + 1, status: 'completed', conclusion: 'success', started_at: new Date().toISOString(), completed_at: new Date().toISOString() });
    const conclusion = this.timedOut ? 'failure' : this.ctxStatus();
    this.log.write(`Job ${conclusion}.`);
    this.log.close();
    try { if (!this.o.config.keepTemp) fs.rmSync(this.temp, { recursive: true, force: true }); } catch {}
    return { conclusion, outputs };
  }
}

module.exports = { JobRunner, killTree, which };
