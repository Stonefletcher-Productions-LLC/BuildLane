'use strict';
// `exec`: run one workflow file from disk right now, without watching GitHub. The workspace is the
// current folder (or --workspace), so a checkout step is usually skipped with --skip-checkout.
// Handy for checking that a workflow works on this PC before switching it over.
const fs = require('fs');
const os = require('os');
const path = require('path');
const workflow = require('./workflow.cjs');
const { LocalRunner } = require('./runner.cjs');

async function exec(args, flags) {
  const file = args.shift();
  if (!file) throw new Error('exec needs a workflow file');
  const text = fs.readFileSync(file, 'utf8');
  const wf = workflow.load(text, path.basename(file));
  if (flags.job) { const keep = String(flags.job).split(','); wf.jobs = Object.fromEntries(Object.entries(wf.jobs).filter(([k]) => keep.includes(k))); for (const j of Object.values(wf.jobs)) if (j.needs) j.needs = [].concat(j.needs).filter(n => keep.includes(n)); }
  if (flags['skip-checkout']) for (const j of Object.values(wf.jobs)) j.steps = (j.steps || []).filter(s => !String(s.uses || '').startsWith('actions/checkout'));
  const root = flags.root ? path.resolve(flags.root) : fs.mkdtempSync(path.join(os.tmpdir(), 'buildlane-exec-'));
  const workspace = path.resolve(flags.workspace || process.cwd());
  const pools = { all: { labels: ['self-hosted', 'windows', 'linux', 'x64', 'ubuntu-latest', 'windows-latest', ...String(flags.labels || '').split(',').filter(Boolean)], max: 4, workspace } };
  for (const j of Object.values(wf.jobs)) for (const l of [].concat(j['runs-on'] || [])) if (typeof l === 'string' && !l.includes('${{')) pools.all.labels.push(l);
  const cfg = { repo: flags.repo || 'local/repo', root, token: process.env.BUILDLANE_TOKEN || process.env.GH_TOKEN || '', mode: flags.live ? 'live' : 'shadow', postStatuses: false, pools, replacePools: true, api: flags.api, quiet: !!flags.quiet, bashPath: flags.bash, keepTemp: !!flags['keep-temp'] };
  const r = new LocalRunner(cfg);
  r.offline = true; r.login = flags.actor || os.userInfo().username;
  const { ApiProxy } = require('./proxy.cjs');
  r.proxy = new ApiProxy({ store: r.store, upstream: r.gh.api, repos: [cfg.repo], mode: cfg.mode, log: m => r.log(m), onDispatch: async (w) => { throw new Error(`exec does not start other workflows (${w})`); }, onCancel: id => r.cancelRun(id), onRerun: () => { throw new Error('not in exec'); } });
  await r.proxy.listen(0);
  const inputs = Object.fromEntries(args.map(a => { const i = a.indexOf('='); return [a.slice(0, i), a.slice(i + 1)]; }));
  const name = flags.event || 'workflow_dispatch';
  const payload = flags.payload ? JSON.parse(fs.readFileSync(flags.payload, 'utf8')) : (name === 'workflow_dispatch' ? { inputs } : {});
  const sha = flags.sha || '0'.repeat(40);
  const branch = flags.branch || 'main';
  const resolved = name === 'workflow_dispatch' ? workflow.dispatchInputs(wf, inputs) : {};
  r.flushEvents = async () => { r.pendingEvents = []; };
  const run = r.createRun(wf, { name, action: payload.action, ref: flags.ref || `refs/heads/${branch}`, sha, branch, payload, actor: r.login }, resolved);
  while (r.active.size) { await r.tick(); await new Promise(res => setTimeout(res, 300)); }
  const done = r.store.get(run.id);
  for (const j of done.jobs) {
    console.log(`job ${j.name}: ${j.conclusion}`);
    for (const s of j.steps) console.log(`  ${(s.conclusion || s.status).padEnd(9)} ${s.name}`);
    if (j.conclusion !== 'success' || flags.verbose) console.log(`  log: ${r.store.logPath(run.id, j.id)}`);
  }
  console.log(`run ${run.id}: ${done.conclusion}`);
  await r.proxy.close();
  process.exitCode = done.conclusion === 'success' ? 0 : 1;
  return done;
}

module.exports = { exec };
