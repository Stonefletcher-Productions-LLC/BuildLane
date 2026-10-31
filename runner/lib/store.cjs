'use strict';
// Local run store: one folder per run under <root>/runs/<id>/ with run.json, logs/ and artifacts/.
// Run, job and artifact IDs start far above GitHub's (about 4e10 in 2026) so they never collide with
// real run IDs still recorded in project state. Objects are shaped like the GitHub REST responses.
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const BASE_ID = 900000000000;

class Store {
  constructor(root, { owner, repo }) {
    this.root = root; this.owner = owner; this.repo = repo;
    this.runsDir = path.join(root, 'runs');
    fs.mkdirSync(this.runsDir, { recursive: true });
    this.counterFile = path.join(root, 'counters.json');
  }

  nextId(kind) {
    let c = {};
    try { c = JSON.parse(fs.readFileSync(this.counterFile, 'utf8')); } catch {}
    c[kind] = (c[kind] || 0) + 1;
    writeAtomic(this.counterFile, JSON.stringify(c));
    return BASE_ID + c[kind];
  }

  workflowId(file) { return BASE_ID + (parseInt(crypto.createHash('sha1').update(file).digest('hex').slice(0, 7), 16)); }

  runDir(id) { return path.join(this.runsDir, String(id)); }
  logPath(runId, jobId) { return path.join(this.runDir(runId), 'logs', `${jobId}.log`); }
  artifactDir(runId) { return path.join(this.runDir(runId), 'artifacts'); }

  create({ workflow, event, sha, branch, actor, triggeringActor, displayTitle, inputs, payload, ref, prNumbers }) {
    const id = this.nextId('run');
    const now = new Date().toISOString();
    const runNumber = this.list().filter(r => r.path === workflow.path).length + 1;
    const run = {
      id, run_number: runNumber, run_attempt: 1, name: workflow.name, display_title: displayTitle || workflow.name,
      path: workflow.path, workflow_file: workflow.file, workflow_id: this.workflowId(workflow.file), event, head_sha: sha, head_branch: branch, ref,
      status: 'queued', conclusion: null, created_at: now, updated_at: now, run_started_at: null,
      actor: { login: actor }, triggering_actor: { login: triggeringActor || actor },
      repository: { full_name: `${this.owner}/${this.repo}`, name: this.repo, owner: { login: this.owner } },
      html_url: '', jobs: [], artifacts: [], inputs: inputs || {}, payload: payload || {}, pull_requests: (prNumbers || []).map(number => ({ number })),
      local: true,
    };
    fs.mkdirSync(path.join(this.runDir(id), 'logs'), { recursive: true });
    this.save(run);
    return run;
  }

  save(run) { run.updated_at = new Date().toISOString(); writeAtomic(path.join(this.runDir(run.id), 'run.json'), JSON.stringify(run, null, 1)); }

  get(id) {
    try { return JSON.parse(fs.readFileSync(path.join(this.runDir(id), 'run.json'), 'utf8')); } catch { return null; }
  }

  list() {
    const out = [];
    for (const name of fs.readdirSync(this.runsDir)) {
      if (!/^\d+$/.test(name)) continue;
      const r = this.get(name); if (r) out.push(r);
    }
    return out.sort((a, b) => b.id - a.id);
  }

  addJob(run, { key, name, labels }) {
    const job = { id: this.nextId('job'), run_id: run.id, run_attempt: run.run_attempt, key, name, workflow_name: run.name, head_sha: run.head_sha, head_branch: run.head_branch,
      status: 'queued', conclusion: null, created_at: new Date().toISOString(), started_at: null, completed_at: null,
      labels: labels || [], runner_name: 'buildlane-local', steps: [] };
    run.jobs.push(job); this.save(run); return job;
  }

  addArtifact(run, name, zipBuffer) {
    const id = this.nextId('artifact');
    const dir = this.artifactDir(run.id); fs.mkdirSync(dir, { recursive: true });
    const file = path.join(dir, `${id}.zip`);
    fs.writeFileSync(file, zipBuffer);
    const fresh = this.get(run.id) || run;
    fresh.artifacts = (fresh.artifacts || []).filter(a => a.name !== name);
    const art = { id, name, size_in_bytes: zipBuffer.length, expired: false, created_at: new Date().toISOString(), file: path.basename(file),
      workflow_run: { id: run.id, head_sha: run.head_sha, head_branch: run.head_branch } };
    fresh.artifacts.push(art); this.save(fresh); run.artifacts = fresh.artifacts;
    return art;
  }

  findArtifact(id) {
    for (const r of this.list()) for (const a of r.artifacts || []) if (a.id === Number(id)) return { run: r, artifact: a, file: path.join(this.artifactDir(r.id), a.file) };
    return null;
  }

  // REST shapes ------------------------------------------------------------
  restRun(r) {
    const { jobs, artifacts, inputs, payload, local, workflow_file, ...rest } = r;
    return { ...rest, node_id: `LOCAL_${r.id}`, check_suite_id: r.id, jobs_url: '', logs_url: '', artifacts_url: '' };
  }
  restJob(r, j) {
    const { key, ...rest } = j;
    return { ...rest, run_url: '', html_url: '', check_run_url: '', steps: j.steps.map(s => ({ ...s })) };
  }
  restArtifact(a) { const { file, ...rest } = a; return { ...rest, archive_download_url: `/repos/${this.owner}/${this.repo}/actions/artifacts/${a.id}/zip` }; }

  // Local job results presented as check runs for the given commit.
  checkRunsFor(sha, appId) {
    const out = [];
    for (const r of this.list()) {
      if (r.head_sha !== sha && r.payload?.pull_request?.head?.sha !== sha) continue;
      for (const j of r.jobs) {
        out.push({ id: j.id, name: j.name, head_sha: sha, status: j.status === 'completed' ? 'completed' : (j.status === 'queued' ? 'queued' : 'in_progress'),
          conclusion: j.conclusion, started_at: j.started_at, completed_at: j.completed_at, external_id: `buildlane:${r.id}:${j.id}`,
          details_url: '', html_url: '', output: { title: null, summary: null, text: null, annotations_count: 0 },
          check_suite: { id: r.id }, app: { id: appId, slug: 'github-actions', name: 'GitHub Actions (Buildlane local)' }, pull_requests: [] });
      }
    }
    return out;
  }
}

function writeAtomic(file, text) {
  const tmp = `${file}.${process.pid}.tmp`;
  fs.writeFileSync(tmp, text);
  fs.renameSync(tmp, file);
}

module.exports = { Store, BASE_ID, writeAtomic };
