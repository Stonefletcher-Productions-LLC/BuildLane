'use strict';
// End-to-end check of the local runner against a fake GitHub API: triggers (push, PR, schedule,
// workflow_run, dispatch from a job, command comment), outputs/needs/matrix/if, artifacts through the
// local API, local check runs, commit statuses, concurrency and cancellation, shadow mode.
// Run: node runner/test/TestRunner.cjs   (needs bash; on Windows, Git for Windows)
const assert = require('assert');
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { LocalRunner } = require('../lib/runner.cjs');
const expr = require('../lib/expr.cjs');
const cron = require('../lib/cron.cjs');
const wfLib = require('../lib/workflow.cjs');
const zip = require('../lib/zip.cjs');
const { parseEnvFile } = require('../lib/commands.cjs');

function unit() {
  const ctx = { contexts: { github: { event_name: 'push', event: { pull_request: { labels: [{ name: 'full-tests' }, { name: 'x' }] } }, ref: 'refs/heads/main' }, inputs: { tier: 'package', version: '1.2', empty: '' }, steps: { a: { outputs: { n: '3' } } } } };
  assert.strictEqual(expr.evaluate("inputs.tier == 'PACKAGE'", ctx), true);
  assert.strictEqual(expr.evaluate("contains(github.event.pull_request.labels.*.name, 'full-tests') && 'full' || 'auto'", ctx), 'full');
  assert.strictEqual(expr.evaluate("inputs.empty || 'auto'", ctx), 'auto');
  assert.strictEqual(expr.evaluate("format('Package v{0}', inputs.version)", ctx), 'Package v1.2');
  assert.strictEqual(expr.evaluate("steps.a.outputs.n > 2", ctx), true);
  assert.strictEqual(expr.evaluate("fromJSON('[1,2]')[1]", ctx), 2);
  assert.strictEqual(expr.evaluate("github.event_name != 'schedule' && inputs.tier != 'package'", ctx), false);
  assert.strictEqual(expr.interpolate('a-${{ inputs.tier }}-${{ github.missing }}', ctx), 'a-package-');
  assert.strictEqual(expr.evaluateIf("github.event_name == 'push'", { ...ctx, status: () => 'failure' }), false);
  assert.strictEqual(expr.evaluateIf('always()', { ...ctx, status: () => 'failure' }), true);
  assert.strictEqual(expr.evaluateIf("${{ failure() }}", { ...ctx, status: () => 'failure' }), true);
  assert.strictEqual(expr.evaluate("toJSON(inputs.version)", ctx), '"1.2"');
  assert.strictEqual(expr.evaluate("'it''s'", ctx), "it's");
  assert.strictEqual(expr.evaluate('1 == 1.0', ctx), true);
  assert.strictEqual(expr.evaluate("'' == 0", ctx), true);
  assert.strictEqual(expr.evaluate('null == false', ctx), true); // GitHub coerces both to 0

  assert.deepStrictEqual(cron.firesBetween('*/5 * * * *', new Date('2026-10-31T10:02:00Z'), new Date('2026-10-31T10:07:00Z')).map(d => d.toISOString()), ['2026-10-31T10:05:00.000Z']);
  assert.deepStrictEqual(cron.firesBetween('0 7 * * *', new Date('2026-10-31T06:59:30Z'), new Date('2026-10-31T07:00:10Z')).length, 1);
  assert.deepStrictEqual(cron.firesBetween('30 6 * * *', new Date('2026-10-31T06:31:00Z'), new Date('2026-10-31T07:00:00Z')).length, 0);
  assert.ok(cron.matches(cron.parse('0 0 * * sun'), new Date('2026-11-01T00:00:00Z')));

  assert.ok(wfLib.filterMatch(['.github/workflows/*.yml'], '.github/workflows/ci.yml'));
  assert.ok(!wfLib.filterMatch(['.github/workflows/*.yml'], '.github/workflows/sub/ci.yml'));
  assert.ok(wfLib.filterMatch(['Tools/**', '!Tools/x/**'], 'Tools/CI/a.cjs'));
  assert.ok(!wfLib.filterMatch(['Tools/**', '!Tools/x/**'], 'Tools/x/a.cjs'));
  assert.ok(wfLib.filterMatch(['codex/**'], 'codex/a/b'));
  const wf = wfLib.load('on:\n  pull_request:\n    types: [labeled]\n  push:\n    branches: [main]\n    paths: ["Tools/**"]\njobs: {}\n', 'x.yml');
  assert.ok(wfLib.wants(wf, { name: 'push', ref: 'refs/heads/main', files: ['Tools/a'] }));
  assert.ok(!wfLib.wants(wf, { name: 'push', ref: 'refs/heads/main', files: ['Docs/a'] }));
  assert.ok(!wfLib.wants(wf, { name: 'push', ref: 'refs/heads/dev', files: ['Tools/a'] }));
  assert.ok(!wfLib.wants(wf, { name: 'pull_request', action: 'opened', payload: { pull_request: { base: { ref: 'main' } } } }));
  assert.ok(wfLib.wants(wf, { name: 'pull_request', action: 'labeled', payload: { pull_request: { base: { ref: 'main' } } } }));

  const z = zip.build([{ name: 'a/b.txt', data: Buffer.from('hello '.repeat(50)) }, { name: 'c.bin', data: Buffer.from([1, 2, 3]) }]);
  const back = zip.unzip(z);
  assert.strictEqual(back[0].data.toString(), 'hello '.repeat(50)); assert.deepStrictEqual([...back[1].data], [1, 2, 3]);

  const tmp = path.join(os.tmpdir(), `bl-env-${process.pid}`); fs.writeFileSync(tmp, 'a=1\nmulti<<EOF\nx\ny\nEOF\nb=c=d\n');
  assert.deepStrictEqual(parseEnvFile(tmp), { a: '1', multi: 'x\ny', b: 'c=d' }); fs.unlinkSync(tmp);
  console.log('PASS unit: expressions, cron, filters, triggers, zip, env files');
}

// ---------------------------------------------------------------------------------------------
const OWNER = 'acme', REPO = 'game';
const W = {
  'ci.yml': `
name: CI
run-name: \${{ github.event.pull_request.title || 'CI on push' }}
on:
  push:
    branches: [main]
  pull_request:
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      x: \${{ steps.s1.outputs.x }}
    steps:
      - id: s1
        run: |
          echo "x=1" >> "$GITHUB_OUTPUT"
          echo "FROM_ENV=yes" >> "$GITHUB_ENV"
          mkdir -p out && echo built > out/result.txt
      - run: test "$FROM_ENV" = yes
      - uses: actions/upload-artifact@v4
        with:
          name: result
          path: out/
  test:
    needs: build
    if: needs.build.outputs.x == '1'
    runs-on: ubuntu-latest
    strategy:
      matrix:
        part: [a, b]
    steps:
      - run: echo "part \${{ matrix.part }} event \${{ github.event_name }}"
      - name: Local API
        env:
          TOKEN: \${{ github.token }}
        run: node "$SCRIPTS/api-check.cjs" \${{ matrix.part == 'b' && 'no' || 'yes' }}
  never:
    needs: build
    if: needs.build.outputs.x == '2'
    runs-on: ubuntu-latest
    steps:
      - run: exit 1
`,
  'second.yml': `
name: Second
on:
  workflow_dispatch:
    inputs:
      foo:
        required: true
jobs:
  echo:
    runs-on: ubuntu-latest
    steps:
      - run: test "\${{ inputs.foo }}" = bar
`,
  'after.yml': `
name: After CI
on:
  workflow_run:
    workflows: [CI]
    types: [completed]
jobs:
  report:
    runs-on: ubuntu-latest
    steps:
      - run: test "\${{ github.event.workflow_run.conclusion }}" = success
`,
  'sched.yml': `
name: Nightly
on:
  schedule:
    - cron: "* * * * *"
concurrency:
  group: nightly
  cancel-in-progress: true
jobs:
  slow:
    runs-on: [self-hosted, heavy]
    steps:
      - run: sleep 30
`,
  'fail.yml': `
name: Fails
on:
  push:
    branches: [main]
    paths: ['Docs/**']
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - run: exit 3
      - if: failure()
        run: echo cleanup
      - run: echo never
`,
};

function fakeGitHub() {
  const st = { branches: { main: 'a'.repeat(40) }, prs: [], statuses: [], comments: [], replies: [], writesRefused: 0 };
  const files = () => Object.entries(W).map(([name, text]) => ({ name, type: 'file', sha: require('crypto').createHash('sha1').update(text).digest('hex') }));
  const server = http.createServer(async (req, res) => {
    const url = new URL(req.url, 'http://x'); const p = url.pathname;
    let body = ''; for await (const c of req) body += c;
    const send = (code, data) => { res.writeHead(code, { 'content-type': 'application/json' }); res.end(JSON.stringify(data)); };
    const base = `/repos/${OWNER}/${REPO}`;
    if (p === '/user') return send(200, { login: 'ProjektPayne' });
    if (p === `${base}/branches`) return send(200, url.searchParams.get('page') === '1' ? Object.entries(st.branches).map(([name, sha]) => ({ name, commit: { sha } })) : []);
    let m;
    if ((m = new RegExp(`^${base}/branches/(.+)$`).exec(p))) return send(200, { name: m[1], commit: { sha: st.branches[decodeURIComponent(m[1])] } });
    if (p === `${base}/contents/.github/workflows`) return send(200, files());
    if ((m = new RegExp(`^${base}/contents/\\.github/workflows/(.+)$`).exec(p))) return send(200, { content: Buffer.from(W[m[1]]).toString('base64') });
    if ((m = new RegExp(`^${base}/compare/`).exec(p))) return send(200, { files: [{ filename: 'Docs/readme.md' }], commits: [{ sha: st.branches.main, commit: { message: 'docs', committer: { date: '' }, author: {} }, author: { login: 'ProjektPayne' } }] });
    if (p === `${base}/pulls`) return send(200, url.searchParams.get('page') === '1' ? st.prs : []);
    if ((m = new RegExp(`^${base}/pulls/(\\d+)/files$`).exec(p))) return send(200, url.searchParams.get('page') === '1' ? [{ filename: 'Source/x.cpp' }] : []);
    if ((m = new RegExp(`^${base}/pulls/(\\d+)$`).exec(p))) return send(200, st.prs.find(x => String(x.number) === m[1]));
    if ((m = new RegExp(`^${base}/statuses/([0-9a-f]{40})$`).exec(p)) && req.method === 'POST') { st.statuses.push({ sha: m[1], ...JSON.parse(body) }); return send(201, {}); }
    if ((m = new RegExp(`^${base}/commits/([^/]+)/check-runs$`).exec(p))) return send(200, { total_count: 1, check_runs: [{ id: 1, name: 'remote-check', head_sha: m[1], status: 'completed', conclusion: 'success', app: { id: 999 } }] });
    if ((m = new RegExp(`^${base}/issues/7/comments$`).exec(p))) { if (req.method === 'POST') { st.replies.push(JSON.parse(body).body); return send(201, {}); } return send(200, url.searchParams.get('page') === '1' ? st.comments : []); }
    if (p === `${base}/git/refs` && req.method === 'POST') return send(201, { ref: 'x' });
    send(404, { message: `fake: ${req.method} ${p}` });
  });
  return new Promise(r => server.listen(0, '127.0.0.1', () => r({ server, st, url: `http://127.0.0.1:${server.address().port}` })));
}

async function until(runner, cond, ms = 90000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { await runner.tick(); if (cond()) return; await new Promise(r => setTimeout(r, 200)); }
  throw new Error('timed out waiting for the runner');
}

async function e2e() {
  const gh = await fakeGitHub();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bl-runner-'));
  const scripts = path.join(root, 'scripts'); fs.mkdirSync(scripts);
  // A job script that uses the local API like github-script would.
  fs.writeFileSync(path.join(scripts, 'api-check.cjs'), `
const api = process.env.GITHUB_API_URL, repo = process.env.GITHUB_REPOSITORY, H = { authorization: 'Bearer ' + process.env.TOKEN, 'content-type': 'application/json' };
(async () => {
  const run = await (await fetch(api + '/repos/' + repo + '/actions/runs/' + process.env.GITHUB_RUN_ID, { headers: H })).json();
  if (run.id !== Number(process.env.GITHUB_RUN_ID) || run.path !== '.github/workflows/ci.yml' || run.status !== 'in_progress') throw Error('run ' + JSON.stringify(run));
  const arts = await (await fetch(api + '/repos/' + repo + '/actions/runs/' + run.id + '/artifacts', { headers: H })).json();
  if (arts.total_count !== 1 || arts.artifacts[0].name !== 'result') throw Error('artifacts ' + JSON.stringify(arts));
  const zipRes = await fetch(api + '/repos/' + repo + '/actions/artifacts/' + arts.artifacts[0].id + '/zip', { headers: H });
  const buf = Buffer.from(await zipRes.arrayBuffer());
  if (!buf.includes(Buffer.from('built'))) throw Error('zip content');
  const jobs = await (await fetch(api + '/repos/' + repo + '/actions/runs/' + run.id + '/attempts/1/jobs', { headers: H })).json();
  const build = jobs.jobs.find(j => j.name === 'build');
  if (!build || build.conclusion !== 'success' || !build.steps.some(s => s.name === 'Run actions/upload-artifact@v4' && s.conclusion === 'success')) throw Error('jobs ' + JSON.stringify(jobs));
  const checks = await (await fetch(api + '/repos/' + repo + '/commits/' + run.head_sha + '/check-runs?filter=latest', { headers: H })).json();
  const names = checks.check_runs.map(c => c.name + ':' + c.app.id);
  if (!names.includes('remote-check:999') || !names.includes('build:15368')) throw Error('checks ' + names);
  if (process.env.GITHUB_EVENT_NAME === 'push' && process.argv[2] !== 'no') {
    const d = await fetch(api + '/repos/' + repo + '/actions/workflows/second.yml/dispatches', { method: 'POST', headers: H, body: JSON.stringify({ ref: 'main', inputs: { foo: 'bar' } }) });
    if (d.status !== 204) throw Error('dispatch ' + d.status + ' ' + await d.text());
  }
  const w = await fetch(api + '/repos/' + repo + '/git/refs', { method: 'POST', headers: H, body: '{}' });
  console.log('write status', w.status);
  if (process.env.EXPECT_WRITE && String(w.status) !== process.env.EXPECT_WRITE) throw Error('write ' + w.status);
})().catch(e => { console.error(e.message); process.exit(1); });
`);
  const cfg = { repo: `${OWNER}/${REPO}`, root, token: 'test-token', api: gh.url, mode: 'shadow', quiet: true, commandIssue: 7, dispatchers: ['ProjektPayne'],
    pools: { heavy: { labels: ['self-hosted', 'heavy'], max: 1 } }, env: { SCRIPTS: scripts, EXPECT_WRITE: '403' }, replyInShadow: true };
  const r = new LocalRunner(cfg);
  await r.start();
  await r.poll(); // first poll records the current state only
  assert.strictEqual(r.store.list().length, 0, 'the first poll starts nothing');

  // A push to main + a new PR + a due schedule + a command comment, all in one poll.
  gh.st.branches.main = 'b'.repeat(40);
  gh.st.prs.push({ number: 5, title: 'Add a thing', draft: false, labels: [], head: { sha: 'c'.repeat(40), ref: 'feature' }, base: { ref: 'Test-Staging' }, user: { login: 'stonefletcher' }, merge_commit_sha: 'd'.repeat(40), mergeable: true });
  gh.st.comments.push({ id: 11, created_at: new Date().toISOString(), user: { login: 'ProjektPayne' }, body: '/buildlane run second.yml foo=bar' });
  gh.st.comments.push({ id: 12, created_at: new Date().toISOString(), user: { login: 'mallory' }, body: '/buildlane run second.yml foo=bar' });
  const s = r.loadState(); s.lastSchedule = new Date(Date.now() - 120000).toISOString(); r.saveState(s);
  await r.poll();
  const names = r.store.list().map(x => `${x.name}/${x.event}`).sort();
  assert.deepStrictEqual(names, ['CI/pull_request', 'CI/push', 'Fails/push', 'Nightly/schedule', 'Second/workflow_dispatch'], names.join(','));
  assert.ok(/queued as local run/.test(gh.st.replies.join()) && /mallory may not send/.test(gh.st.replies.join()), gh.st.replies.join('\n'));

  // A second schedule tick cancels the running nightly (cancel-in-progress).
  await until(r, () => r.store.list().some(x => x.name === 'Nightly' && x.status === 'in_progress'));
  const firstNightly = r.store.list().find(x => x.name === 'Nightly');
  const s2 = r.loadState(); s2.lastSchedule = new Date(Date.now() - 61000).toISOString(); r.saveState(s2);
  await r.poll();
  await until(r, () => r.store.get(firstNightly.id).status === 'completed');
  assert.strictEqual(r.store.get(firstNightly.id).conclusion, 'cancelled');
  const second = r.store.list().find(x => x.name === 'Nightly' && x.id !== firstNightly.id);
  r.cancelRun(second.id);

  await until(r, () => r.active.size === 0 && !(r.pendingEvents || []).length && r.store.list().filter(x => x.name === 'After CI').length === 2 && r.store.list().every(x => x.status === 'completed'), 120000);
  const runs = r.store.list();
  const byKey = k => runs.filter(x => `${x.name}/${x.event}` === k);
  const ciPush = byKey('CI/push')[0];
  const dump = run => run.jobs.map(j => `${j.name}=${j.conclusion}`).join(' ') + ' ' + run.jobs.map(j => fs.readFileSync(r.store.logPath(run.id, j.id), 'utf8')).join('\n').slice(-3000);
  assert.strictEqual(ciPush.conclusion, 'success', dump(ciPush));
  assert.deepStrictEqual(ciPush.jobs.map(j => j.name).sort(), ['build', 'test (a)', 'test (b)']);
  assert.strictEqual(ciPush.display_title, 'CI on push');
  const ciPr = byKey('CI/pull_request')[0];
  assert.strictEqual(ciPr.conclusion, 'success', dump(ciPr));
  assert.strictEqual(ciPr.display_title, 'Add a thing');
  assert.strictEqual(ciPr.head_sha, 'c'.repeat(40));
  assert.strictEqual(byKey('Second/workflow_dispatch').length, 2, 'command comment + dispatch from a job');
  assert.ok(byKey('Second/workflow_dispatch').every(x => x.conclusion === 'success'), byKey('Second/workflow_dispatch').map(dump).join('\n'));
  assert.ok(byKey('After CI/workflow_run').every(x => x.conclusion === 'success'));
  const fails = byKey('Fails/push')[0];
  assert.strictEqual(fails.conclusion, 'failure');
  assert.deepStrictEqual(fails.jobs[0].steps.map(x => x.conclusion), ['success', 'failure', 'success', 'skipped', 'success']);
  // statuses: shadow prefix, PR status on the PR head
  assert.ok(gh.st.statuses.some(x => x.sha === 'c'.repeat(40) && x.context === 'buildlane-shadow / build' && x.state === 'success'));
  assert.ok(gh.st.statuses.some(x => x.sha === 'b'.repeat(40) && x.context === 'buildlane-shadow / a' && x.state === 'failure'));
  assert.ok(!gh.st.statuses.some(x => x.context.includes('never')));
  await r.stop(); gh.server.close();
  console.log(`PASS e2e: ${runs.length} local runs (push, PR, schedule + cancel-in-progress, workflow_run, dispatch from a job and a comment, artifacts, check runs, statuses, shadow write refusal)`);
}

// Node action inputs: a null default is empty, defaults are expressions, GITHUB_STATE reaches the post
// step, and two jobs in one pinned workspace run one after the other (found on the build PC).
async function actionInputs() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'bl-act-'));
  fs.mkdirSync(path.join(dir, 'act'));
  fs.writeFileSync(path.join(dir, 'act', 'action.yml'), 'name: t\ninputs:\n  filter:\n    default: null\n  other:\n    default: ${{ github.event_name }}\nruns:\n  using: node20\n  main: main.js\n  post: post.js\n');
  fs.writeFileSync(path.join(dir, 'act', 'main.js'), "const fs=require('fs');if(process.env.INPUT_FILTER!=='')process.exit(1);if(process.env.INPUT_OTHER!=='workflow_dispatch')process.exit(2);if(fs.existsSync('lock'))process.exit(4);fs.writeFileSync('lock','');setTimeout(()=>{fs.unlinkSync('lock');fs.appendFileSync(process.env.GITHUB_STATE,'k=v\\n');},500);");
  fs.writeFileSync(path.join(dir, 'act', 'post.js'), "if(process.env.STATE_k!=='v')process.exit(3);");
  fs.writeFileSync(path.join(dir, 'wf.yml'), 'name: NA\non: workflow_dispatch\njobs:\n  a:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./act\n  b:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: ./act\n');
  const log = console.log; console.log = () => {};
  let done; try { done = await require('../lib/offline.cjs').exec([path.join(dir, 'wf.yml')], { workspace: dir, quiet: true }); } finally { console.log = log; }
  assert.strictEqual(done.conclusion, 'success', JSON.stringify(done.jobs.map(j => [j.name, j.steps.map(s => s.conclusion)])));
  process.exitCode = 0;
  console.log('PASS action inputs, post-step state, one job at a time in a pinned workspace');
}

// Retention: old finished runs go, the newest keepRuns and unfinished runs stay; stale job temp folders
// under the runner's own work folders go, another runner's folders are left alone.
function retention() {
  const { Store } = require('../lib/store.cjs');
  const { LocalRunner } = require('../lib/runner.cjs');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bl-ret-'));
  const st = new Store(root, { owner: 'o', repo: 'r' });
  const old = new Date(Date.now() - 30 * 86400000).toISOString();
  for (let i = 1; i <= 5; i++) { const d = path.join(st.runsDir, String(i)); fs.mkdirSync(d, { recursive: true }); fs.writeFileSync(path.join(d, 'run.json'), JSON.stringify({ id: i, status: i === 1 ? 'in_progress' : 'completed', updated_at: i === 5 ? new Date().toISOString() : old, jobs: [] })); }
  assert.strictEqual(st.prune({ keepDays: 7, keepRuns: 1 }), 3);
  assert.deepStrictEqual(st.list().map(r => r.id), [5, 1]);
  const zipBuf = Buffer.from('PK fake zip');
  const art = st.addArtifact(st.get(5), 'receipt', zipBuf);
  const want = 'sha256:' + require('crypto').createHash('sha256').update(zipBuf).digest('hex');
  assert.strictEqual(st.restArtifact(art).digest, want);
  const legacy = { ...art }; delete legacy.digest;
  assert.strictEqual(st.restArtifact(legacy).digest, want, 'digest computed for artifacts stored without one');
  const mine = path.join(root, '_work-linux', '_temp', '42'), other = path.join(root, '_workbench', '_temp', '43');
  for (const d of [mine, other]) { fs.mkdirSync(d, { recursive: true }); const t = new Date(Date.now() - 2 * 86400000); fs.utimesSync(d, t, t); }
  const r = new LocalRunner({ repo: 'o/r', root, token: 'x', pools: {}, quiet: true });
  r.housekeep();
  assert.ok(!fs.existsSync(mine) && fs.existsSync(other));
  const rp = new LocalRunner({ repo: 'o/r', root, token: 'x', quiet: true, replacePools: true, pools: { lane: { labels: ['windows', 'lifeshare-lane'], disabled: true }, quick: { labels: ['windows'], jobs: ['ci.yml:quick'] } } });
  assert.strictEqual(rp.poolFor(['self-hosted', 'Windows', 'lifeshare-lane'], { file: 'ci.yml', key: 'quick' }).name, 'quick');
  assert.strictEqual(rp.poolFor(['self-hosted', 'Windows', 'lifeshare-lane'], { file: 'lane.yml', key: 'quick' }).name, 'lane');
  assert.strictEqual(rp.poolFor(['self-hosted', 'Windows'], { file: 'ci.yml', key: 'other' }).name, 'lane');
  // Holds: schedule and workflow_run starts are skipped while held; dispatches are not; holds expire.
  const hr = new LocalRunner({ repo: 'o/r', root, token: 'x', pools: {}, quiet: true });
  const wf = require('../lib/workflow.cjs').load('name: C\non:\n  schedule:\n    - cron: "*/5 * * * *"\n  workflow_dispatch:\njobs:\n  c:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo\n', 'ctl.yml');
  fs.mkdirSync(path.join(root, 'holds'), { recursive: true });
  fs.writeFileSync(hr.holdFile('ctl.yml'), JSON.stringify({ until: new Date(Date.now() + 60000).toISOString() }));
  assert.ok(hr.held('ctl.yml'));
  let order;
  return hr.trigger({ name: 'schedule', cron: '*/5 * * * *', ref: 'refs/heads/main', sha: 'a'.repeat(40), branch: 'main', payload: {}, workflows: [wf] }).then(runs => {
    assert.strictEqual(runs.length, 0, 'held schedule skipped');
    fs.writeFileSync(hr.holdFile('ctl.yml'), JSON.stringify({ until: new Date(Date.now() - 1000).toISOString() }));
    assert.ok(!hr.held('ctl.yml') && !fs.existsSync(hr.holdFile('ctl.yml')), 'expired hold removed');
    for (const s of hr.active.values()) hr.active.delete(s.run.id);
    // Statuses for one commit and job post in order even when the first request is slow.
    order = [];
    hr.gh.post = (url, body) => new Promise(res => setTimeout(() => { order.push(body.state); res({}); }, body.state === 'pending' ? 50 : 0));
    const runStub = { id: 1, head_sha: 'b'.repeat(40) }, jobStub = { name: 'Quick checks' };
    hr.postStatus(runStub, jobStub, 'pending');
    return hr.postStatus(runStub, jobStub, 'success');
  }).then(() => {
    assert.deepStrictEqual(order, ['pending', 'success'], 'statuses posted in order');
    // `list` keeps an old unfinished run visible behind newer finished ones.
    const { Store } = require('../lib/store.cjs');
    const ls = new Store(path.join(root, 'list-store'), { owner: 'o', repo: 'r' });
    const wfStub = { name: 'W', path: '.github/workflows/w.yml', file: 'w.yml' };
    const old = ls.create({ workflow: wfStub, event: 'workflow_dispatch', sha: 'c'.repeat(40), branch: 'main', actor: 'a' });
    old.status = 'in_progress'; ls.save(old);
    for (let i = 0; i < 25; i++) { const done = ls.create({ workflow: wfStub, event: 'schedule', sha: 'c'.repeat(40), branch: 'main', actor: 'a' }); done.status = 'completed'; done.conclusion = 'success'; ls.save(done); }
    const shown = ls.listForDisplay();
    assert.strictEqual(shown.length, 21, 'unfinished run plus the 20 most recent');
    assert.strictEqual(shown[0].id, old.id, 'old unfinished run listed first');
    assert.deepStrictEqual(ls.listForDisplay({ active: true }).map(x => x.id), [old.id], '--active shows only unfinished runs');
    console.log('PASS retention, artifact digests, holds, status order and list: old runs pruned, stale temp folders cleared, other folders kept');
  });
}

// Shallow checkouts that gained one pack per depth-1 fetch are tidied back to one pack; HEAD and files stay.
async function tidyShallow() {
  const { execFileSync } = require('child_process');
  const g = (cwd, ...a) => execFileSync('git', a, { cwd, stdio: ['ignore', 'pipe', 'pipe'] }).toString().trim();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bl-tidy-'));
  const src = path.join(root, 'src'); fs.mkdirSync(src);
  g(src, 'init', '-q'); g(src, 'config', 'user.email', 't@t'); g(src, 'config', 'user.name', 't');
  const ws = path.join(root, '_work', 'r', 'r'); fs.mkdirSync(ws, { recursive: true });
  g(ws, 'init', '-q'); g(ws, 'remote', 'add', 'origin', src);
  for (let i = 0; i < 5; i++) {
    fs.writeFileSync(path.join(src, 'f.txt'), `v${i} ` + 'x'.repeat(5000 + i)); g(src, 'add', '.'); g(src, 'commit', '-qm', `c${i}`);
    const sha = g(src, 'rev-parse', 'HEAD');
    g(ws, '-c', 'protocol.file.allow=always', '-c', 'fetch.unpackLimit=1', 'fetch', '-q', '--depth=1', '--no-auto-gc', 'origin', `+${sha}:refs/remotes/origin/c${i}`);
    g(ws, 'checkout', '-q', '--detach', sha);
  }
  const packs = () => fs.readdirSync(path.join(ws, '.git', 'objects', 'pack')).filter(n => n.endsWith('.pack')).length;
  assert.ok(packs() >= 4, `fixture made ${packs()} packs`);
  const head = g(ws, 'rev-parse', 'HEAD');
  const r = new LocalRunner({ repo: 'o/r', root, token: 'x', pools: {}, quiet: true });
  assert.deepStrictEqual(r.gitCheckouts(), [ws]);
  assert.strictEqual(await r.tidyGit(), 1);
  assert.strictEqual(packs(), 1, 'one pack after tidy');
  assert.strictEqual(g(ws, 'rev-parse', 'HEAD'), head);
  assert.strictEqual(g(ws, 'status', '--porcelain'), '');
  assert.strictEqual(g(ws, 'cat-file', '-t', head), 'commit');
  assert.strictEqual(await r.tidyGit(), 0, 'nothing to do the second time');
  fs.rmSync(root, { recursive: true, force: true });
  console.log('PASS shallow checkout tidy: packs merged, HEAD and files kept');
}

// A stalled loop trips the watchdog; a GitHub request with no answer fails after its deadline.
async function stalls() {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'bl-stall-'));
  const r = new LocalRunner({ repo: 'o/r', root, token: 'x', pools: {}, quiet: true });
  const lines = []; r.log = m => lines.push(m);
  const now = Date.now();
  r.loopAt = now - 60000; r.phase = 'tick: run 7';
  assert.strictEqual(r.watchdog(now), false, 'a recent pass is fine');
  r.loopAt = now - 16 * 60000;
  assert.strictEqual(r.watchdog(now), true, 'a 16 min stall trips the default 15 min watchdog');
  assert.ok(/stalled for 16 min in tick: run 7/.test(lines.join('\n')));
  const silent = http.createServer(() => {}); await new Promise(res => silent.listen(0, '127.0.0.1', res));
  const { GitHub } = require('../lib/github.cjs');
  const g = new GitHub({ token: 'x', api: `http://127.0.0.1:${silent.address().port}`, owner: 'o', repo: 'r', timeoutMs: 300 });
  await assert.rejects(g.get('/x'), e => e.timeout === true && /no answer within 0.3 s/.test(e.message));
  silent.closeAllConnections && silent.closeAllConnections(); silent.close();
  fs.rmSync(root, { recursive: true, force: true });
  console.log('PASS stalls: loop watchdog trips after 15 min, GitHub requests time out');
}

(async () => { unit(); await retention(); await tidyShallow(); await stalls(); await actionInputs(); await e2e(); })().catch(e => { console.error(e); process.exit(1); });
