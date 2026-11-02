'use strict';
// Local GitHub API stand-in for jobs (GITHUB_API_URL). Everything is forwarded to the real API with the
// caller's own Authorization header, except the Actions endpoints, which this runner owns now: workflow
// dispatch, runs, attempts, jobs, logs and artifacts are answered from the local run store, and local job
// results are added to commits/{ref}/check-runs so policy code that reads checks keeps working.
// In shadow mode, writes outside /actions are refused so a trial run cannot change GitHub.
const http = require('http');
const fs = require('fs');

function json(res, code, body, headers = {}) {
  const text = body === undefined ? '' : JSON.stringify(body);
  res.writeHead(code, { 'content-type': 'application/json; charset=utf-8', ...headers });
  res.end(text);
}
function readBody(req) {
  return new Promise((resolve, reject) => { const c = []; req.on('data', d => c.push(d)); req.on('end', () => resolve(Buffer.concat(c))); req.on('error', reject); });
}
function page(items, q) {
  const per = Math.min(Number(q.get('per_page')) || 30, 100), p = Math.max(Number(q.get('page')) || 1, 1);
  return items.slice((p - 1) * per, p * per);
}

class ApiProxy {
  // opts: { store, upstream, repos: ['owner/repo', aliases...], mode, appId, onDispatch(wfFile, ref, inputs, auth), onCancel(id), onRerun(id, failedOnly), log }
  constructor(opts) {
    this.o = opts; this.upstream = (opts.upstream || 'https://api.github.com').replace(/\/$/, '');
    this.repos = new Set(opts.repos.map(r => r.toLowerCase()));
    this.log = opts.log || (() => {});
  }

  listen(port = 0) {
    this.server = http.createServer((req, res) => this.handle(req, res).catch(err => {
      this.log(`api error ${req.method} ${req.url}: ${err.stack || err}`);
      if (!res.headersSent) json(res, 500, { message: String(err.message || err) }); else res.end();
    }));
    return new Promise(resolve => this.server.listen(port, '127.0.0.1', () => { this.port = this.server.address().port; this.url = `http://127.0.0.1:${this.port}`; resolve(this.url); }));
  }
  close() { return new Promise(r => this.server ? this.server.close(() => r()) : r()); }

  async handle(req, res) {
    const url = new URL(req.url, 'http://local');
    const m = /^\/repos\/([^/]+)\/([^/]+)(\/.*)?$/.exec(url.pathname);
    const ours = m && this.repos.has(`${m[1]}/${m[2]}`.toLowerCase());
    const rest = m ? (m[3] || '') : '';
    if (ours && rest.startsWith('/actions/')) {
      if (await this.actions(req, res, rest, url.searchParams, m[1], m[2])) return;
    }
    if (ours && req.method === 'GET' && /^\/commits\/[^/]+\/check-runs$/.test(rest)) return this.checkRuns(req, res, decodeURIComponent(rest.split('/')[2]), url, m[1], m[2]);
    if (this.o.mode === 'shadow' && !['GET', 'HEAD'].includes(req.method) && !(url.pathname === '/graphql' && !(await this.isMutation(req)))) {
      this.log(`shadow: refused ${req.method} ${url.pathname}`);
      return json(res, 403, { message: `Buildlane shadow mode: ${req.method} ${url.pathname} was not sent to GitHub` });
    }
    return this.forward(req, res, url);
  }

  async isMutation(req) {
    try { return /^\s*mutation\b/.test(JSON.parse((await this.peek(req)).toString()).query || ''); } catch { return true; }
  }
  async peek(req) { if (!req._body) req._body = await readBody(req); return req._body; }

  async forward(req, res, url) {
    const body = ['GET', 'HEAD'].includes(req.method) ? undefined : await this.peek(req);
    const headers = {};
    for (const [k, v] of Object.entries(req.headers)) if (!['host', 'connection', 'content-length', 'accept-encoding'].includes(k)) headers[k] = v;
    const r = await fetch(this.upstream + url.pathname + url.search, { method: req.method, headers, body, redirect: 'follow', signal: AbortSignal.timeout(300000) }); // a stalled upstream must not hang a job forever
    const out = {};
    r.headers.forEach((v, k) => { if (!['content-encoding', 'content-length', 'transfer-encoding', 'connection'].includes(k)) out[k] = k === 'link' ? v.split(this.upstream).join(this.selfUrl()) : v; });
    const buf = Buffer.from(await r.arrayBuffer());
    res.writeHead(r.status, out); res.end(req.method === 'HEAD' ? undefined : buf);
  }
  selfUrl() { return this.url || ''; }

  async fetchUpstreamJson(path, req) {
    const headers = { accept: 'application/vnd.github+json', 'user-agent': 'Buildlane-Runner' };
    if (req.headers.authorization) headers.authorization = req.headers.authorization;
    const r = await fetch(this.upstream + path, { headers, signal: AbortSignal.timeout(60000) });
    if (!r.ok) { const e = new Error(`${r.status} ${path}`); e.status = r.status; throw e; }
    return r.json();
  }

  async checkRuns(req, res, ref, url, owner, repo) {
    let sha = ref;
    if (!/^[0-9a-f]{40}$/i.test(ref)) {
      try { sha = (await this.fetchUpstreamJson(`/repos/${owner}/${repo}/commits/${encodeURIComponent(ref)}`, req)).sha; } catch (e) { return json(res, e.status || 502, { message: e.message }); }
    }
    let remote = [];
    try {
      for (let p = 1; p <= 10; p++) {
        const q = new URLSearchParams(url.searchParams); q.set('per_page', '100'); q.set('page', String(p));
        const data = await this.fetchUpstreamJson(`/repos/${owner}/${repo}/commits/${sha}/check-runs?${q}`, req);
        remote.push(...data.check_runs);
        if (data.check_runs.length < 100) break;
      }
    } catch (e) { if (e.status !== 404) this.log(`check-runs upstream: ${e.message}`); remote = []; }
    let local = this.o.store.checkRunsFor(sha, this.o.appId || 15368);
    const name = url.searchParams.get('check_name');
    if (name) local = local.filter(c => c.name === name);
    if (url.searchParams.get('filter') !== 'all') {
      const latest = new Map();
      for (const c of local.sort((a, b) => a.id - b.id)) latest.set(c.name, c);
      local = [...latest.values()];
    }
    const all = [...remote, ...local];
    json(res, 200, { total_count: all.length, check_runs: all });
  }

  async actions(req, res, rest, q, owner, repo) {
    const s = this.o.store;
    const parts = rest.split('/').filter(Boolean).slice(1); // after 'actions'
    const method = req.method;
    const runList = filterFn => {
      let runs = s.list().filter(filterFn);
      for (const [k, f] of [['status', r => v => (['completed', 'queued', 'in_progress', 'waiting', 'pending', 'requested'].includes(v) ? r.status === v : r.conclusion === v)], ['event', r => v => r.event === v], ['branch', r => v => r.head_branch === v], ['head_sha', r => v => r.head_sha === v], ['actor', r => v => r.actor.login.toLowerCase() === v.toLowerCase()]]) {
        const v = q.get(k); if (v) runs = runs.filter(r => f(r)(v));
      }
      return { total_count: runs.length, workflow_runs: page(runs, q).map(r => s.restRun(r)) };
    };
    const wfMatch = (r, wf) => r.workflow_file === wf || String(r.workflow_id) === String(wf) || r.path === wf;
    const withRun = (id, fn) => { const r = s.get(id); return r ? fn(r) : false; };

    if (method === 'GET' && parts[0] === 'runs' && parts.length === 1) { json(res, 200, runList(() => true)); return true; }
    if (parts[0] === 'workflows') {
      if (method === 'GET' && parts.length === 1) { json(res, 200, this.o.workflows ? this.o.workflows() : { total_count: 0, workflows: [] }); return true; }
      const wf = decodeURIComponent(parts[1] || '');
      if (method === 'GET' && parts.length === 2) { const all = this.o.workflows ? this.o.workflows().workflows : []; const w = all.find(x => x.path.endsWith(`/${wf}`) || String(x.id) === wf); w ? json(res, 200, w) : json(res, 404, { message: 'Not Found' }); return true; }
      if (method === 'GET' && parts[2] === 'runs') { json(res, 200, runList(r => wfMatch(r, wf))); return true; }
      if (method === 'POST' && parts[2] === 'dispatches') {
        const body = JSON.parse((await this.peek(req)).toString() || '{}');
        try { await this.o.onDispatch(wf, body.ref, body.inputs || {}, req.headers.authorization || ''); res.writeHead(204); res.end(); }
        catch (e) { json(res, 422, { message: String(e.message || e) }); }
        return true;
      }
      if (method === 'PUT' && ['enable', 'disable'].includes(parts[2])) { res.writeHead(204); res.end(); return true; }
    }
    if (parts[0] === 'runs' && /^\d+$/.test(parts[1] || '')) {
      const id = parts[1];
      return withRun(id, r => {
        if (r.local !== true) return false;
        if (method === 'GET' && parts.length === 2) { json(res, 200, s.restRun(r)); return true; }
        if (method === 'GET' && parts[2] === 'attempts' && parts.length === 4) {
          const a = Number(parts[3]); const snap = a === r.run_attempt ? r : (r.previousAttempts || []).find(x => x.run_attempt === a);
          snap ? json(res, 200, s.restRun(snap)) : json(res, 404, { message: 'Not Found' }); return true;
        }
        if (method === 'GET' && (parts[2] === 'jobs' || (parts[2] === 'attempts' && parts[4] === 'jobs'))) {
          const a = parts[2] === 'attempts' ? Number(parts[3]) : r.run_attempt;
          const snap = a === r.run_attempt ? r : (r.previousAttempts || []).find(x => x.run_attempt === a);
          if (!snap) { json(res, 404, { message: 'Not Found' }); return true; }
          let jobs = snap.jobs; if (parts[2] === 'jobs' && q.get('filter') === 'all') jobs = [...(r.previousAttempts || []).flatMap(x => x.jobs), ...r.jobs];
          json(res, 200, { total_count: jobs.length, jobs: page(jobs, q).map(j => s.restJob(snap, j)) }); return true;
        }
        if (method === 'GET' && parts[2] === 'artifacts') {
          let arts = r.artifacts || []; const n = q.get('name'); if (n) arts = arts.filter(a => a.name === n);
          json(res, 200, { total_count: arts.length, artifacts: page(arts, q).map(a => s.restArtifact(a)) }); return true;
        }
        if (method === 'POST' && parts[2] === 'cancel') { this.o.onCancel(r.id); json(res, 202, {}); return true; }
        if (method === 'POST' && parts[2] === 'force-cancel') { this.o.onCancel(r.id); json(res, 202, {}); return true; }
        if (method === 'POST' && (parts[2] === 'rerun' || parts[2] === 'rerun-failed-jobs')) { this.o.onRerun(r.id, parts[2] === 'rerun-failed-jobs'); json(res, 201, {}); return true; }
        if (method === 'GET' && parts[2] === 'logs') { json(res, 404, { message: 'Logs are on the build PC; see the Buildlane Jobs view' }); return true; }
        return false;
      });
    }
    if (parts[0] === 'jobs' && /^\d+$/.test(parts[1] || '')) {
      const found = s.list().flatMap(r => r.jobs.map(j => ({ r, j }))).find(x => x.j.id === Number(parts[1]));
      if (!found) return false;
      if (method === 'GET' && parts.length === 2) { json(res, 200, s.restJob(found.r, found.j)); return true; }
      if (method === 'GET' && parts[2] === 'logs') {
        const p = s.logPath(found.r.id, found.j.id);
        res.writeHead(200, { 'content-type': 'text/plain; charset=utf-8' }); res.end(fs.existsSync(p) ? fs.readFileSync(p) : ''); return true;
      }
    }
    if (parts[0] === 'artifacts') {
      if (method === 'GET' && parts.length === 1) {
        let arts = s.list().flatMap(r => r.artifacts || []); const n = q.get('name'); if (n) arts = arts.filter(a => a.name === n);
        json(res, 200, { total_count: arts.length, artifacts: page(arts, q).map(a => s.restArtifact(a)) }); return true;
      }
      const found = /^\d+$/.test(parts[1] || '') && s.findArtifact(parts[1]);
      if (!found) return false;
      if (method === 'GET' && parts.length === 2) { json(res, 200, s.restArtifact(found.artifact)); return true; }
      if (method === 'GET' && parts[2] === 'zip') { res.writeHead(200, { 'content-type': 'application/zip' }); res.end(fs.readFileSync(found.file)); return true; }
      if (method === 'DELETE' && parts.length === 2) {
        const r = s.get(found.run.id); r.artifacts = r.artifacts.filter(a => a.id !== found.artifact.id); s.save(r);
        try { fs.unlinkSync(found.file); } catch {}
        res.writeHead(204); res.end(); return true;
      }
    }
    return false;
  }
}

module.exports = { ApiProxy };
