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

(async () => { unit(); await e2e(); })().catch(e => { console.error(e); process.exit(1); });
