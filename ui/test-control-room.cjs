// Browser checks against a local fixture; never writes to a real Fletchery backend.
// Run with Playwright installed: node ui/test-control-room.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const os = require('node:os');
const path = require('node:path');
const { chromium } = require(process.env.FLETCHERY_PLAYWRIGHT || 'playwright');

const now = new Date().toISOString();
const data = {
  project: 'Harbor', repo: 'example/harbor', repo_url: 'https://github.com/example/harbor',
  label: 'fletchery', max_parallel: 8, workers_enabled: true, spent_today: 27.08, daily_budget: 400,
  auth: 'subscription', held: false, poll_minutes: 10, last_pass: now, next_pass_at: now,
  agents: {multi: true, list: [], keys: [], routing: {}, escalate: true}, lane: null,
  live: [
    {task: 891, title: 'Shower interactions', agent: 'sonnet', stage: 'Reviewing', since: now, actions: 12, last: 'Reviewing the fix', last_at: now},
    {task: 899, title: 'Preview reactions', agent: 'codex-sol', stage: 'Writing', since: now, actions: 4, last: 'Updating reactions', last_at: now},
  ],
  tasks: [
    {number: 891, title: 'Shower interactions', status: 'running', branch: 'task/891'},
    {number: 899, title: 'Preview reactions', status: 'running'},
    {number: 840, title: 'Bikes and parcels', status: 'queued', why: 'ready', url: '#840'},
    {number: 900, title: 'Missing <asset> access', status: 'blocked', url: '#900'},
  ],
  merge: [{pr: 1041, task: 1036, title: 'Save purchased bike details', head: 'abc123', unblocks: [840], flags: [], ready: true, pr_url: '#1041'}],
  merge_view: {round: 16, state: 'running', activity: {updated: now, source: 'Auto-Game.log', health: 'active'}, headline: 'Game tests running · 47 min elapsed', prs: [1041], steps: [
    {name: 'Compile', state: 'done', duration_seconds: 372}, {name: 'Imports', state: 'done'},
    {name: 'Game tests', state: 'running', started: now, message: 'Verify the rebuilt game before landing.',
      checks: [{name: 'M1 Commands', state: 'done', duration_seconds: 56}, {name: 'M4b Reconnect', state: 'running'}],
      activity: [{at: now, line: 'Both clients connected'}, {line: '<script>unsafe()</script>'}],
      files: [{name: 'Auto-Game.log', url: '/api/merge/log?round=16&name=Auto-Game.log'}]}, {name: 'Land', state: 'todo'},
  ]},
  merge_auto: {on: true, held: false, running: {round: 16, agent: 'codex-sol', started: now, prs: [1041]}, last: {}},
  history: [{task: 840, result: 'waiting_dependencies', at: now, agent: 'codex-sol', cost_usd: 0}],
  log: [{at: now, line: 'Merge round 16 started'}], usage_week: [],
};
const requests = [];
const pageSource = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8').replace('__FLETCHERY_TOKEN__', 'fixture-token').replace('__FLETCHERY_PAGE__', 'page-a');
data.page_id = 'page-a';
const server = http.createServer(async (req, res) => {
  if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
  if (req.method === 'POST') {
    let rawBody = '';
    for await (const chunk of req) rawBody += chunk;
    const body = JSON.parse(rawBody || '{}');
    requests.push({url: req.url, token: req.headers['x-fletchery-token'], body});
    if (req.url === '/api/settings/save' && body.changes && body.changes.emergency_workers != null) {
      data.emergency_workers.max_parallel = body.changes.emergency_workers;
    }
    if (req.url === '/api/queue/add') {
      const row = {...body, id: 'request-' + requests.length, state: 'queued', heads: body.kind === 'merge' ? {'1041': 'a'.repeat(40)} : undefined};
      data.integration_queue.requests.push(row); data.integration_queue.planned = true;
    }
    if (req.url === '/api/queue/cancel') data.integration_queue.requests.find(r => r.id === body.id).state = 'cancelled';
    if (req.url === '/api/queue/mode') data.integration_queue.planned = body.planned;
    if (req.url === '/api/workers/handoff') { data.held = true; data.worker_handoffs = {'891': {task: 891, state: 'requested'}}; }
    if (req.url === '/api/workers/ranks') { data.worker_ranks.limits = body.limits; data.worker_ranks.mode = 'manual'; data.max_parallel = Object.values(body.limits).reduce((a,b) => a+b,0); }
    if (req.url === '/api/workers') { data.max_parallel = body.max_parallel; if (data.worker_ranks) data.worker_ranks.mode = 'automatic'; }
    if (req.url === '/api/pause') data.held = true;
    if (req.url === '/api/resume') data.held = false;
    if (req.url === '/api/storage/cleanup') data.storage.requested = true;
    if (req.url === '/api/machines/add') { res.setHeader('Content-Type', 'application/json'); return res.end(JSON.stringify({name: body.name, key: 'fnk_test-key'})); }
    if (req.url === '/api/jobs/add') data.machines.queued += 1;
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(req.url === '/api/issue/get' ? {body: 'Missing access', comments: []} : {ok: true}));
  }
  if (req.url !== '/api/status') data.page_id = 'page-a';  // a freshly served page is current
  res.setHeader('Content-Type', req.url === '/api/status' ? 'application/json' : 'text/html');
  res.end(req.url === '/api/status' ? JSON.stringify(data) : pageSource);
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${server.address().port}`;
  const executablePath = process.env.FLETCHERY_BROWSER;
  const browser = await chromium.launch(executablePath ? {executablePath} : {channel: 'msedge'});
  const context = await browser.newContext({viewport: {width: 1400, height: 1000}, colorScheme: 'light'});
  await context.route('https://**', route => route.abort());
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const visible = selector => page.locator(selector).isVisible();
  const out = process.env.FLETCHERY_SCREENSHOTS || path.join(os.tmpdir(), 'fletchery-control-room-preview');
  fs.mkdirSync(out, {recursive: true});
  try {
    await page.goto(url);
    await page.waitForFunction(() => document.querySelector('#projName').textContent === 'Harbor');
    assert.equal(await visible('#workers'), true);
    // Overview lanes: coding, merging and building read at a glance.
    assert.equal(await page.locator('#lanes .lane-card').count(), 3);
    assert.match(await page.locator('#laneCoding').innerText(), /2 of 8 workers[\s\S]*#891[\s\S]*#899/);
    assert.match(await page.locator('#laneMerging').innerText(), /round 16/i);
    assert.match(await page.locator('#laneBuilding').innerText(), /Gaming PC/);
    // Which machine does what: a Runs on line per lane and one row per machine.
    assert.match(await page.locator('#laneCoding .lane-where').innerText(), /Gaming PC/);
    assert.match(await page.locator('#laneMerging .lane-where').innerText(), /Gaming PC[\s\S]*codex-sol/);
    assert.match(await page.locator('#machinesGlance').innerText(), /Gaming PC[\s\S]*Coding ×2[\s\S]*Merge round 16/);
    assert.equal(await visible('#needsStrip'), true, 'a blocked task shows the Needs you strip');
    assert.equal(await visible('#queue'), false);
    assert.equal(await visible('#agents'), false);
    assert.equal(await visible('#lowerBox'), false);
    assert.deepEqual(await page.locator('[data-view]').evaluateAll(links => links.map(link => link.dataset.view)),
      ['overview', 'tasks', 'merge', 'build', 'agents', 'activity']);
    assert.equal(await page.locator('#slots details').count(), 2);
    assert.equal(await page.locator('#slots .free').count(), 1);
    assert.equal(await visible('#emergencyWorkers'), false, 'older payloads hide unavailable emergency controls');
    assert.match(await page.locator('.round-status').innerText(), /Game tests/);
    const workerWidth = (await page.locator('#workers').boundingBox()).width;
    const mergeWidth = (await page.locator('#mergeCard').boundingBox()).width;
    assert.ok(Math.abs(workerWidth - mergeWidth) <= 1, 'overview gives workers and merging equal column widths');
    assert.equal(await visible('#mergeNow .msteps[aria-label="Current round steps"]'), false);
    await page.locator('.round-detail summary').click();
    await page.evaluate(() => render(last));
    assert.equal(await page.locator('.round-detail').getAttribute('open'), '');
    assert.match(await page.locator('.round-detail').innerText(), /47 min/);
    await page.locator('.round-detail summary').click();
    await page.locator('#slots summary').first().click();
    await page.evaluate(() => render(last));
    assert.equal(await page.locator('#slots details[open]').count(), 1, 'polling preserves worker details');
    await page.locator('#slots summary').first().click();
    await page.screenshot({path: path.join(out, 'overview-light.png'), fullPage: true});
    await page.selectOption('#themeChoice', 'dark');
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.screenshot({path: path.join(out, 'overview-dark.png'), fullPage: true});
    // Text size scales the whole page and the menu folds to icons; both survive a reload.
    await page.selectOption('#textSize', '115');
    await page.locator('#navToggle').click();
    assert.equal(await page.locator('html').getAttribute('data-nav'), 'rail');
    assert.equal(await page.evaluate(() => document.documentElement.style.zoom), '115%');
    await page.screenshot({path: path.join(out, 'overview-rail-large.png')});
    await page.reload(); await page.waitForSelector('#navToggle');
    assert.equal(await page.locator('html').getAttribute('data-nav'), 'rail');
    assert.equal(await page.locator('#textSize').inputValue(), '115');
    await page.locator('#navToggle').click();
    await page.selectOption('#textSize', '100');
    assert.equal(await page.locator('html').getAttribute('data-nav'), null);
    assert.equal(await page.evaluate(() => document.documentElement.style.zoom), '');
    // Each step expands independently; polling preserves explicit choices and keyboard focus.
    await page.getByRole('link', {name: 'Round details →'}).click();
    assert.equal(await visible('#view-merge'), true);
    assert.equal(await visible('#mergeCard .merge-aside'), true);
    assert.match(await page.locator('#mergeNow .merge-health').innerText(), /Active/);
    assert.match(await page.locator('#mergeNow .merge-subline').innerText(), /Last activity/);
    assert.match(await page.locator('#mergeNow .merge-progress-label').innerText(), /2 of 4 steps complete[\s\S]*1 running · 1 queued/);
    const roundMeter = page.getByRole('progressbar', {name: 'Required round steps complete'});
    assert.equal(await roundMeter.getAttribute('value'), '2');
    assert.equal(await roundMeter.getAttribute('max'), '4');
    const activeStep=page.locator('.merge-step').nth(2);
    assert.equal(await activeStep.locator('.merge-step-badge').innerText(), 'Running');
    assert.equal(await activeStep.locator('summary').first().evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(25, 60, 50)');
    assert.equal(await activeStep.getAttribute('open'), '', 'current step expands automatically');
    assert.match(await activeStep.innerText(), /M4b Reconnect/);
    assert.equal(await page.locator('#mergeNow script').count(), 0, 'log lines are text, never markup');
    assert.equal(await page.locator('#mergeNow .merge-current .merge-spinner').count(), 1);
    assert.equal(await activeStep.locator('summary .merge-spinner').count(), 1);
    assert.equal(await activeStep.locator('.check-running .merge-spinner').count(), 1);
    assert.equal(await activeStep.locator('.check-done .merge-spinner').count(), 0);
    const testMergeView = data.merge_view;
    data.merge_view = {...testMergeView, prs: [1030, 1031, 1032, 1034],
      activity: {...testMergeView.activity, source: 'Saved/MergeRound16/merge-1031.log'}, steps: [{
      name: 'Merge the PRs into the round branch', state: 'running', check_kind: 'merge_prs',
      checks: [{name: 'PR #1030', state: 'done'}, {name: 'PR #1031', state: 'running'},
        {name: 'PR #1032', state: 'waiting', message: 'Git reported a conflict.'}, {name: 'PR #1034', state: 'todo'}],
    }, {name: 'Build', state: 'todo'}]};
    await page.evaluate(next => {last = next; render(next);}, data);
    const merging = page.locator('.merge-step').first();
    assert.match(await merging.innerText(), /1 of 4 PRs integrated/);
    assert.match(await merging.innerText(), /Git reported a conflict/);
    assert.equal(await merging.locator('progress').getAttribute('value'), '1');
    assert.equal(await merging.locator('.check-waiting .merge-spinner').count(), 0);
    await page.emulateMedia({reducedMotion: 'reduce'});
    assert.equal(await merging.locator('summary .merge-spinner').evaluate(el => getComputedStyle(el).animationName), 'none');
    await page.emulateMedia({reducedMotion: 'no-preference'});
    await page.locator('#mergeNow').screenshot({path: path.join(out, 'merge-pr-progress-dark.png')});
    data.merge_view = testMergeView;
    await page.evaluate(next => {last = next; render(next);}, data);
    await activeStep.locator('[data-step-tab="activity"]').click();
    await page.evaluate(() => render(last));
    assert.equal(await activeStep.locator('[data-step-tab="activity"]').getAttribute('aria-pressed'), 'true');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.mergeFocus), 'step-2-activity');
    assert.match(await activeStep.locator('[data-step-panel="activity"]').innerText(), /Both clients connected/);
    await activeStep.locator('[data-step-tab="files"]').click();
    assert.equal(await activeStep.locator('[data-step-panel="files"] a').getAttribute('href'), '/api/merge/log?round=16&name=Auto-Game.log');
    await activeStep.locator('summary').click();
    await page.evaluate(() => render(last));
    assert.equal(await activeStep.getAttribute('open'), null, 'polling respects a manually collapsed active step');
    await activeStep.locator('summary').click();
    await activeStep.locator('[data-step-tab="progress"]').click();
    data.merge_view.steps[2].activity[1].line='Checking reconnect state';
    await page.evaluate(next=>{last=next;render(next);},data);
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-expanded-dark.png')});
    await page.selectOption('#themeChoice', 'light');
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-expanded-light.png')});
    await page.setViewportSize({width:420,height:900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth),true,'expanded merge fits narrow windows');
    await page.screenshot({path:path.join(out,'merge-expanded-mobile.png'),fullPage:true});
    // Another view opens at its top, not at this view's scroll position.
    await page.evaluate(() => window.scrollTo(0, 200));
    assert.ok(await page.evaluate(() => scrollY > 0), 'the merge view is tall enough to scroll in a narrow window');
    // Click in place: Playwright's own click would scroll the sidebar link into view first.
    // The tasks view now shrinks to its content, so give it room to scroll before checking it opens at the top.
    await page.evaluate(() => { document.body.style.minHeight = '3000px'; document.querySelector('[data-view="tasks"]').click(); });
    assert.ok(await page.evaluate(() => document.documentElement.scrollHeight - innerHeight >= 200), 'the tasks view can scroll as far');
    assert.equal(await page.evaluate(() => scrollY), 0, 'switching views starts at the top');
    await page.evaluate(() => { document.body.style.minHeight = ''; });
    await page.locator('[data-view="merge"]').click();
    await page.setViewportSize({width:1400,height:1000});
    await page.locator('[data-view="overview"]').click();
    await page.selectOption('#themeChoice', 'dark');
    data.merge_view.activity={updated:now,source:'Progress checkpoint',health:'active'};
    data.update_pending=true;
    await page.evaluate(next => {last=next;render(next);},data);
    assert.match(await page.locator('.merge-health').innerText(),/Recent update/);
    assert.match(await page.locator('.merge-subline').innerText(),/Status updated/);
    await page.locator('[data-view="merge"]').click();
    assert.match(await page.locator('.merge-aside').innerText(),/App update ready/);
    data.merge_view.stale=true;
    data.merge_view.activity.health='possibly_stuck';
    await page.evaluate(next => {last=next;render(next);},data);
    assert.match(await page.locator('.merge-health').innerText(),/May be stuck/);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'),'merge-now running','quiet output is not a confirmed failure');
    delete data.merge_view.stale;
    data.update_pending=false;
    data.merge_view.activity={updated:now,source:'Auto-Game.log',health:'active'};
    await page.locator('[data-view="overview"]').click();
    // Terminal rounds belong in a collapsed history section, even when an older
    // backend still sends their failed steps as the current merge_view.
    const originalMergeView = JSON.parse(JSON.stringify(data.merge_view));
    const originalMergeAuto = JSON.parse(JSON.stringify(data.merge_auto));
    const started = '2026-11-05T00:03:45Z';
    const ended = '2026-11-05T03:27:56Z';
    const previous = {round: 19, result: 'failed', started, ended, duration_seconds: 12251,
      note: 'Agent stopped; the candidate is preserved.', steps: [{name: 'Build', state: 'failed'}]};
    data.merge_view = {active: false, round: '', state: 'idle', prs: [], steps: [], previous,
      headline: 'No merge round running. The PC is busy with hair validation.',
      pc: {what: '#992 official MCP house hair validation'}};
    data.merge_auto = {on: true, held: false, running: {}, priority: true,
      why: 'The PC heavy lease is held; merge checkout preparation waits', last: {round: 19, result: 'failed', at: started,
      summary: 'Agent stopped; the candidate is preserved.'}};
    data.merge[0].round = '19';
    data.merge[0].in_round_branch = true;
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'), 'merge-now idle');
    assert.equal(await page.locator('#mergeNow .round-status').count(), 0);
    assert.match(await page.locator('#mergeNow .merge-current').innerText(), /Waiting for #992 to release the PC/);
    assert.match(await page.locator('#mergeNow .merge-priority').innerText(), /New workers wait/);
    assert.match(await page.locator('.round-history summary').innerText(), /Previous round 19 didn't land.*3 h 24 min total/);
    assert.equal(await page.locator('.round-history').getAttribute('open'), null);
    assert.equal(await visible('[aria-label="Previous round steps"]'), false);
    assert.doesNotMatch(await page.locator('#c-pr').innerText(), /In round 19|merged into the round branch/);
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-waiting-history-dark.png')});
    await page.selectOption('#themeChoice', 'light');
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-waiting-history-light.png')});
    data.merge_auto.held = true;
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow .merge-priority').count(), 0, 'paused merging must not claim worker priority');
    data.merge_auto.held = false;
    await page.evaluate(next => { last = next; render(next); }, data);
    const frozen = await page.locator('.round-history summary').innerText();
    await page.evaluate(() => { window.fixtureNow = Date.now; Date.now = () => fixtureNow() + 3600000; render(last); });
    assert.equal(await page.locator('.round-history summary').innerText(), frozen, 'finished duration must not keep ticking');
    await page.locator('.round-history summary').click();
    assert.match(await page.locator('[aria-label="Previous round steps"]').innerText(), /Build[\s\S]*failed/);
    await page.evaluate(() => render(last));
    assert.equal(await page.locator('.round-history').getAttribute('open'), '', 'polling preserves history expansion');
    await page.locator('.round-history summary').click();
    await page.evaluate(() => { Date.now = fixtureNow; delete window.fixtureNow; });
    data.merge_view = {round: 19, state: 'failed', stale: true, started, updated: ended,
      headline: 'Merging round 19: FAILED at Build', prs: [1041], steps: previous.steps};
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'), 'merge-now idle', 'legacy terminal failures are historical too');
    assert.equal(await page.locator('#mergeNow .round-status').count(), 0);
    assert.match(await page.locator('.round-history summary').innerText(), /3 h 24 min total/);
    assert.doesNotMatch(await page.locator('#mergeNow').innerText(), /elapsed in this round/);
    // Current progress/history remains visible before GitHub has supplied queue rows.
    const cachedQueue=data.merge;
    delete data.merge;
    await page.evaluate(next=>{last=next;render(next);},data);
    assert.match(await page.locator('.round-history summary').innerText(),/Previous round 19/);
    assert.match(await page.locator('#mergeNow').innerText(),/PC heavy lease is held/);
    assert.match(await page.locator('#c-pr').innerText(),/GitHub PR queue is not available yet/);
    data.merge=cachedQueue;
    // A newly started round must not inherit old failed steps or old ancestry.
    data.merge_auto.running = {round: 20, agent: 'codex-sol', started: now, prs: [1041]};
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow .round-status').count(), 0);
    assert.doesNotMatch(await page.locator('#c-pr').innerText(), /merged into the round branch|In round 19/);
    assert.match(await page.locator('#c-pr').innerText(), /In round 20/);
    data.merge_view = {active: true, round: 20, state: 'failed', started: now, prs: [1041],
      headline: 'Current compile failed', steps: [{name: 'Build', state: 'failed'}]};
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'), 'merge-now failed', 'a failure in a live round stays visible');
    assert.match(await page.locator('.round-status').innerText(), /Build[\s\S]*failed/);
    assert.equal(await page.locator('[aria-label="Current round steps"]').count(), 1);
    assert.equal(await page.locator('.merge-step-badge').innerText(), 'Failed');
    assert.equal(await page.locator('.merge-step > summary').evaluate(el => getComputedStyle(el).backgroundColor), 'rgb(252, 231, 235)');
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-active-failure-light.png')});
    data.merge_auto.running = {};
    assert.match(await page.locator('.round-recovery').innerText(), /Landing blocked[\s\S]*repair or exclude[\s\S]*rerun affected checks/);
    // A later failure must stay visible even with an older backend.
    data.merge_auto.running = {round: 20, agent: 'codex-sol', started: now, prs: [1041]};
    data.merge_view.state = 'running';
    data.merge_view.steps = [{name: '<Package>', state: 'running'}, {name: 'Game specs', state: 'failed', checks: [{name: 'RunSpecs', state: 'running'}]}];
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'), 'merge-now failed');
    assert.match(await page.locator('.round-status').innerText(), /Game specs[\s\S]*failed/);
    assert.match(await page.locator('.round-recovery').innerText(), /Reported activity: <Package>[\s\S]*does not clear/);
    assert.equal(await page.locator('.round-recovery package').count(), 0, 'activity names are escaped');
    const failedStep = page.locator('.msteps > li.failed .merge-step');
    assert.match(await failedStep.innerText(), /RunSpecs[\s\S]*Final result not reported/);
    assert.equal(await failedStep.locator('.merge-spinner').count(), 0, 'stale child reports do not spin in a failed step');
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-recovery-light.png')});
    data.merge_view.steps = [{name: 'Old repair', state: 'waiting', required: false}, {name: 'Studio', state: 'waiting'},
      {name: 'Game specs', state: 'running', checks: [
      {name: 'ValidateData', state: 'done'}, {name: 'RunSpecs', state: 'running'}, {name: 'RunNavCheck', state: 'todo'}
    ], activity: [{line: 'Running LifeShare.Pets'}]}, {name: 'Old low-VRAM check', state: 'failed', required: false}, {name: 'Landing', state: 'todo'}];
    data.merge_auto.running.prs = [1041, 1042];
    data.merge_view.prs = [1041];
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.match(await page.locator('.merge-current').innerText(), /1 PR · started/);
    assert.equal(await page.locator('.round-recovery').count(), 0);
    assert.match(await page.locator('[aria-label="Current round steps"]').innerText(), /Old low-VRAM check \(previous candidate\)/);
    await page.locator('[data-view="merge"]').click();
    assert.match(await page.locator('.merge-check-summary').innerText(), /1 of 3 passed[\s\S]*1 running/);
    assert.match(await page.locator('.merge-checks li').first().innerText(), /RunSpecs/);
    assert.match(await page.locator('[data-step-panel="progress"]:visible .merge-activity').innerText(), /Running LifeShare.Pets/);
    data.update_pending=true;
    data.merge_auto.running = {};
    await page.evaluate(next=>{last=next;render(next);},data);
    assert.match(await page.locator('#holdBanner').innerText(),/Running sessions finish/);
    // Every reason shows at once: an update must not hide "Paused" or the merge-backlog pause (2026-11-06).
    data.held = true; data.merge_auto.backlog = {count: 32, paused: true, high: 12, low: 6};
    await page.evaluate(next=>{last=next;render(next);},data);
    const reasons = await page.locator('#holdBanner').innerText();
    assert.match(reasons, /Paused\. Nothing new starts until you press Resume/);
    assert.match(reasons, /Update ready/);
    assert.match(reasons, /32 ready PRs are waiting to merge; new tasks start again when that drops to 6/);
    data.held = false; delete data.merge_auto.backlog;
    assert.doesNotMatch(await page.locator('.merge-aside').innerText(),/current merge continues/);
    data.update_pending=false;
    data.merge_auto.running = {};
    data.merge_auto.last = {round: 20, result: 'interrupted', summary: 'Machine checks passed', stop_reason: 'Physical Escape stopped acceptance'};
    data.merge_auto.resume = {round: 20, authorized: false};
    data.merge_view = {active: false, state: 'idle', previous: {round: 20, result: 'interrupted', steps: [{name: 'Build', state: 'done'}, {name: 'Acceptance', state: 'waiting'}]}};
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.match(await page.locator('[data-merge-now]').innerText(), /Resume saved round/);
    await page.locator('.round-history summary').click();
    assert.match(await page.locator('.round-history').innerText(), /Physical Escape stopped acceptance/);
    assert.doesNotMatch(await page.locator('.round-history summary').innerText(), /failed/);
    delete data.merge_auto.resume;
    data.merge_auto.running = {round: 20, agent: 'codex-sol', started: now, prs: [1041]};
    await page.locator('[data-view="overview"]').click();
    // Results read as words on Overview too, and a PR fixer is not counted as writing a task.
    const savedHistory = data.history, savedLive = data.live;
    data.history = [{task: 901, result: 'gave_up', at: now, agent: 'opus'}, {task: 902, result: 'some_new_code', at: now}];
    data.live = [{task: 903, title: 'Fix a PR', kind: 'fix', stage: 'Fixing PR #1041', since: now, agent: 'opus'},
      {task: 904, title: 'Write a task', kind: 'task', stage: 'Writing', since: now}];
    await page.evaluate(d => render(d), data);
    const recent = await page.locator('#recentPreview').innerText();
    assert.match(recent, /Gave up/);
    assert.match(recent, /Some new code/);
    assert.doesNotMatch(recent, /_/);
    assert.match(await page.locator('#state').innerText(), /2 of \d+ workers busy · 1 on tasks, 1 fixing a PR/);
    assert.match(await page.locator('#slots').innerText(), /Choosing an agent/);
    data.history = savedHistory; data.live = savedLive;
    // Worker availability explains merge scheduling, without presenting it as a manual pause.
    data.live = [];
    data.merge_auto.priority = true;
    data.merge_view = {active: true, round: 20, steps: [{name: 'Build', state: 'running'}]};
    await page.evaluate(d => render(d), data);
    assert.match(await page.locator('#slots').innerText(), /New coding waits for merge round 20/);
    assert.match(await page.locator('#slots').innerText(), /Current merge step: Build/);
    assert.match(await page.locator('#holdBanner').innerText(), /resumes automatically/);
    assert.doesNotMatch(await page.locator('#state').innerText(), /Idle/);
    assert.equal(await page.locator('#slots .free').count(), 0);
    await page.locator('#workers').screenshot({path: path.join(out, 'workers-merge-wait-light.png')});
    // A round that shares its account only caps that account: other agents keep coding, so no wait banner.
    data.merge_auto.round_shares = ['codex', 4];
    await page.evaluate(d => render(d), data);
    assert.doesNotMatch(await page.locator('#slots').innerText(), /New coding waits/);
    delete data.merge_auto.round_shares;
    data.merge_auto.running = {};
    data.merge_auto.why = 'The PC is reserved by another build';
    await page.evaluate(d => render(d), data);
    assert.match(await page.locator('#slots').innerText(), /PC is reserved by another build/);
    data.held = true;
    await page.evaluate(d => render(d), data);
    assert.equal(await page.locator('.worker-wait').count(), 0, 'manual pause takes precedence');
    data.held = false;
    data.merge_auto.priority = false;
    await page.evaluate(d => render(d), data);
    assert.equal(await page.locator('.worker-wait').count(), 0, 'wait disappears when priority clears');
    // Emergency repair is independent from zero normal workers and has its own hot control.
    const normalMax = data.max_parallel;
    data.max_parallel = 0;
    const emergencySession = {task: 1054, title: 'Repair merge compile failure', agent: 'codex-sol',
      kind: 'fix', stage: 'Fixing PR #988', since: now, actions: 3, emergency: true};
    data.live = [emergencySession];
    data.emergency_workers = {max_parallel: 1, live: [emergencySession], why: ''};
    await page.evaluate(d => render(d), data);
    assert.equal(await visible('#emergencyWorkers'), true);
    assert.equal(await page.locator('#slots details').count(), 0, 'emergency job is absent from ordinary worker rows');
    assert.match(await page.locator('#slots').innerText(), /Merging, builds and emergency repairs continue/);
    assert.equal(await page.locator('#emergencySlots details').count(), 1);
    assert.match(await page.locator('#emergencySlots').innerText(), /#1054 Repair merge compile failure[\s\S]*Fixing PR #988/);
    assert.match(await page.locator('#overviewStats').innerText(), /0 active · writing paused/);
    assert.match(await page.locator('#overviewStats').innerText(), /1 emergency repair/);
    assert.doesNotMatch(await page.locator('#state').innerText(), /Writing pauses once 1/);
    assert.match(await page.locator('#state').innerText(), /1 emergency repair running/);
    await page.locator('#emergencySlots summary').click();
    await page.evaluate(d => render(d), data);
    assert.equal(await page.locator('#emergencySlots details[open]').count(), 1, 'polling preserves emergency repair details');
    await page.locator('#workers').screenshot({path: path.join(out, 'workers-emergency-repair-light.png')});
    await page.selectOption('#themeChoice', 'dark');
    await page.setViewportSize({width: 420, height: 900});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, 'emergency repair fits a narrow window');
    await page.locator('#workers').screenshot({path: path.join(out, 'workers-emergency-repair-dark-mobile.png')});
    await page.setViewportSize({width: 1400, height: 1000});
    await page.selectOption('#themeChoice', 'light');
    await page.locator('#eDown').click();
    await page.waitForFunction(() => document.querySelector('#eVal').textContent === '0');
    assert(requests.some(r => r.url === '/api/settings/save' && r.token === 'fixture-token' && r.body.changes.emergency_workers === 0));
    assert.equal(await page.locator('#emergencySlots details').count(), 1, 'turning emergency workers off does not hide a running repair');
    assert.match(await page.locator('#emergencySlots').innerText(), /pause after the current session finishes/);
    data.live = [];
    data.emergency_workers.live = [];
    await page.evaluate(d => render(d), data);
    assert.match(await page.locator('#emergencySlots').innerText(), /repairs are paused/);
    await page.locator('#eUp').click();
    await page.waitForFunction(() => document.querySelector('#eVal').textContent === '1');
    assert(requests.some(r => r.url === '/api/settings/save' && r.body.changes.emergency_workers === 1));
    data.emergency_workers.why = 'Waiting for the PC reservation to clear';
    await page.evaluate(d => render(d), data);
    assert.match(await page.locator('#emergencySlots').innerText(), /Waiting for the PC reservation to clear/);
    delete data.emergency_workers;
    data.max_parallel = normalMax;
    data.live = [
      {task: 891, title: 'Shower interactions', agent: 'sonnet', stage: 'Reviewing', since: now, actions: 12},
      {task: 899, title: 'Preview reactions', agent: 'codex-sol', stage: 'Writing', since: now, actions: 4},
    ];
    data.merge_view = originalMergeView;
    data.merge_auto = originalMergeAuto;
    delete data.merge[0].round;
    delete data.merge[0].in_round_branch;
    await page.evaluate(next => { last = next; render(next); }, data);
    await page.selectOption('#themeChoice', 'dark');
    await page.locator('[data-view="tasks"]').click();
    assert.equal(await visible('#queue'), true);
    assert.equal(await visible('#workers'), false);
    await page.reload();
    assert.equal(await visible('#view-tasks'), true);
    assert.equal(await page.locator('html').getAttribute('data-theme'), 'dark');
    await page.goto(url);
    assert.equal(await visible('#view-tasks'), true, 'remember selected view without a hash');
    await page.locator('[data-view="agents"]').click();
    assert.equal(await visible('.usage'), true);
    await page.locator('[data-view="activity"]').click();
    assert.equal(await visible('#log'), true);
    await page.goto(url + '/#workers');
    assert.equal(await visible('#workers'), true, 'old links select their owning view');
    await page.locator('#attentionPreview button').click();
    assert.equal(await visible('#nyDlg'), true);
    assert.match(await page.locator('#nyh').innerText(), /Missing <asset> access/);
    await page.locator('#ny-cancel').click();
    await page.locator('#add').click();
    await page.locator('#f-title').fill('Unsaved task draft');
    await page.evaluate(() => render(last));
    assert.equal(await page.locator('#f-title').inputValue(), 'Unsaved task draft');
    await page.locator('#f-cancel').click();
    await page.locator('#hold').click();
    await page.waitForFunction(() => document.querySelector('#hold').textContent === 'Resume');
    assert(requests.some(r => r.url === '/api/pause' && r.token === 'fixture-token'));
    await page.selectOption('#themeChoice', 'system');
    await page.emulateMedia({colorScheme: 'dark'});
    await page.waitForFunction(() => document.documentElement.dataset.theme === 'dark');
    await page.setViewportSize({width: 420, height: 900});
    for (const view of ['overview', 'merge', 'tasks', 'agents', 'activity']) {
      await page.locator(`[data-view="${view}"]`).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${view} fits mobile width`);
    }
    await page.locator('[data-view="overview"]').click();
    await page.screenshot({path: path.join(out, 'overview-mobile.png'), fullPage: true});
    data.storage = {enabled: true, retention_hours: 24, requested: false, last: {freed_bytes: 1024 ** 3, retired: [{path: 'old-task'}]}};
    await page.evaluate(d => render(d), data);
    assert.match(await page.locator('#storageStatus').innerText(), /1 completed copies retired/);
    assert.match(await page.locator('#storageStatus').innerText(), /1.0 GiB removed/);
    await page.locator('#storageStatus button').click();
    await page.waitForFunction(() => document.querySelector('#storageStatus').textContent.includes('cleanup queued'));
    assert.equal(requests.at(-1).url, '/api/storage/cleanup');
    delete data.storage;
    await page.goto(url + '/?only=workers');
    assert.equal(await visible('#workers'), true);
    assert.equal(await visible('.sidebar'), false);
    // Planned windows and score-ranked capacity use the real controls against the fixture.
    data.integration_queue = {planned: false, requests: [], base: 'Test-Staging'};
    data.worker_handoffs = {};
    data.worker_ranks = {mode: 'automatic', limits: {low: 1, medium: 1, high: 1}, active: {}, models: {low: ['deepseek'], medium: [], high: ['expert']},
      scores: {deepseek: 59, expert: 85}, restrictions: {deepseek: ['easy']}, ready_levels: {easy: 0, medium: 2, hard: 3},
      thresholds: {low: 'below 60', medium: '60–74', high: '75–100'}, assignments: {'891': 'high', '899': 'low'}, agent_ranks: {sonnet: 'high', 'codex-sol': 'low'}};
    data.merge = [{pr: 1041, title: 'Safe <preview>', ready: true, pr_url: '#1041', unblocks: [], flags: []}];
    await page.goto(url + '/?view=overview');
    await page.waitForFunction(() => document.querySelector('#integrationPlans').hidden === false);
    assert.equal(await page.locator('#nowh').innerText(), 'Workers');
    assert.equal(await page.locator('[data-rank]').count(), 3);
    assert.equal(await page.locator('[data-worker="891"] .worker-rank-badge').textContent(), 'Strong');
    const scroll = await page.evaluate(() => {
      document.body.style.minHeight = '4000px';
      window.scrollTo(0, 1200);
      const before = window.scrollY;
      render(last);
      const after = window.scrollY;
      document.body.style.minHeight = '';
      window.scrollTo(0, 0);
      return {before, after};
    });
    assert.equal(scroll.before, 1200);
    assert.equal(scroll.after, scroll.before, 'refresh preserves the page scroll position');
    assert.match(await page.locator('#rankMode').textContent(), /0 easy/);
    // One number: the stepper is the only total; the split shows as a bar and its manual form sits under Advanced.
    assert.equal(await page.locator('#allWorkerTotal').count(), 0, 'one worker total control');
    assert.equal(await visible('#workerSplit'), true);
    assert.match(await page.locator('#workerSplit').innerText(), /1 strong[\s\S]*1 mid[\s\S]*1 light/);
    assert.equal(await visible('[data-rank-cap="low"]'), false, 'manual split is folded away');
    const before = Number(await page.locator('#wVal').textContent());
    await page.locator('#wUp').click();
    await page.waitForFunction(n => document.querySelector('#wVal').textContent === String(n), before + 1);
    assert.deepEqual(requests.at(-1).body, {max_parallel: before + 1});
    await page.locator('#workerAdvanced summary').click();
    await page.selectOption('[data-rank-cap="low"]', '7');
    await page.waitForFunction(() => document.querySelector('#rankMode').textContent.includes('by hand'));
    assert.equal(requests.at(-1).url, '/api/workers/ranks');
    assert.equal(requests.at(-1).body.limits.low, 7);
    assert.equal(await visible('#integrationPlans'), false, 'planning stays on the Merging page');
    await page.getByRole('link', {name: 'Round details →'}).click();
    assert.equal(await visible('#integrationPlans'), true);
    await page.locator('#planMerge').click();
    await page.locator('#planWhen').fill('2030-11-05T19:30');
    await page.locator('#planSubmit').click();
    await page.waitForFunction(() => !document.querySelector('#planDlg').open);
    assert.equal(requests.at(-1).url, '/api/queue/add');
    assert.deepEqual(requests.at(-1).body.prs, [1041]);
    assert.match(requests.at(-1).body.not_before, /2030-11/);
    const mergeId = data.integration_queue.requests[0].id;
    await page.locator('[data-view="build"]').click();
    assert.equal(await visible('#view-build'), true);
    assert.equal(await visible('#view-merge'), false);
    assert.equal(await page.locator('#lane').evaluate(el => el.closest('.view').id), 'view-build');
    await page.locator('#planPackage').click();
    await page.locator('#planVersion').fill('0.3.7');
    await page.selectOption('#planAfter', mergeId);
    assert.equal(await page.locator('#planTarget').isVisible(), false);
    await page.locator('#planSubmit').click();
    await page.waitForFunction(() => !document.querySelector('#planDlg').open);
    assert.equal(await page.locator('#overviewStats a[href="#merge"]').innerText(), 'Merge round 16 running');
    assert.equal(await page.locator('#overviewStats a[href="#build"]').innerText(), 'Package 0.3.7 queued');
    assert.match(await page.locator('#packageRows').textContent(), /Waiting for its merge window to land/);
    assert.equal(requests.at(-1).body.after, mergeId);
    assert.equal(requests.at(-1).body.target, '');
    assert.equal(requests.at(-1).token, 'fixture-token');
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('#packageRows [data-plan-cancel]').first().click();
    assert.equal(requests.at(-1).url, '/api/queue/add', 'declining cancellation leaves the request in place');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('#packageRows [data-plan-cancel]').first().click();
    await page.waitForFunction(() => document.querySelector('#packageRows .plans-history summary')?.textContent.includes('History'));
    assert.equal(await page.locator('#packageRows .plans-history').getAttribute('open'), null, 'history is collapsed by default');
    assert.equal(requests.at(-1).url, '/api/queue/cancel');
    const requestsBeforePreview = [...data.integration_queue.requests];
    data.integration_queue.requests.push(
      {id: 'busy', kind: 'merge', state: 'running', reason: 'Merge window running.', not_before: '2026-11-06T00:30:00Z',
        heads: Object.fromEntries(Array.from({length: 12}, (_, i) => [988 + i, 'abc'])), titles: {'988': 'Nursery actions <fixture>'}},
      {id: 'blocked', kind: 'package', version: '0.3.8', state: 'blocked', dispatch_started: now,
        reason: 'The selected source needs review.', receipt: {worker: {runId: 123456}}},
      {id: 'later', kind: 'merge', state: 'queued', not_before: '2030-12-05T20:30:00Z', heads: {'1047': 'def'}},
    );
    await page.evaluate(next => {last = next; render(next);}, data);
    await page.locator('[data-view="merge"]').click();
    await page.setViewportSize({width: 1400, height: 1000});
    const plans = page.locator('#mergePageMount #integrationPlans');
    await plans.waitFor({state: 'visible'});
    const busy = plans.locator('.plan-row.running');
    assert.match(await plans.locator('h4').first().innerText(), /In progress/);
    assert.match(await busy.innerText(), /Merge window · 12 PRs/);
    assert.equal(await busy.getByRole('link', {name: '#988', exact: true}).isVisible(), false);
    assert.equal(await busy.getByRole('button').count(), 0, 'a running request cannot be cancelled here');
    await page.locator('[data-view="build"]').click();
    const packages = page.locator('#packagePlans');
    assert.equal(await packages.locator('.plan-row.blocked').getByRole('button').count(), 0, 'a dispatched request cannot be cancelled here');
    assert.equal(await packages.getByRole('link', {name: 'Open build'}).getAttribute('href'), data.repo_url + '/actions/runs/123456');
    await packages.locator('.plans-history > summary').click();
    assert.match(await packages.locator('.plans-history').innerText(), /Cancelled/);
    await page.locator('[data-view="merge"]').click();
    await page.evaluate(() => document.querySelector('#planMessage').textContent = '');
    await plans.screenshot({path: path.join(out, 'merge-windows-dark.png')});
    await page.selectOption('#themeChoice', 'light');
    await plans.screenshot({path: path.join(out, 'merge-windows-light.png')});
    await busy.locator('summary').click();
    const pr = busy.getByRole('link', {name: '#988', exact: true});
    assert.equal(await pr.getAttribute('href'), data.repo_url + '/pull/988');
    await pr.focus();
    data.integration_queue.requests.find(r => r.id === 'busy').reason = 'Building the merge candidate.';
    await page.evaluate(next => {last = next; render(next);}, data);
    assert.equal(await busy.locator('details').getAttribute('open'), '');
    assert.equal(await pr.evaluate(el => el === document.activeElement), true, 'updates preserve focus in the PR list');
    await page.setViewportSize({width: 390, height: 900});
    assert.equal(await plans.evaluate(el => el.scrollWidth <= el.clientWidth), true, 'queue fits a narrow window');
    await plans.screenshot({path: path.join(out, 'merge-windows-narrow.png')});
    await page.setViewportSize({width: 1400, height: 1000});
    await page.locator('[data-view="overview"]').click();
    data.integration_queue.requests = requestsBeforePreview;
    await page.evaluate(next => {last = next; render(next);}, data);
    await page.locator('#handoffWorkers').click();
    await page.waitForFunction(() => document.querySelector('#handoffStatus').textContent.includes('next checkpoint'));
    assert.equal(requests.at(-1).url, '/api/workers/handoff');
    assert.equal(requests.at(-1).token, 'fixture-token');
    data.worker_handoffs['891'] = {task: 891, state: 'checkpointed', phase: 'writing', staged: ['Source/Feature.cpp'], worktree: 'owned/task-891'};
    await page.evaluate(() => refresh());
    assert.match(await page.locator('#handoffStatus').innerText(), /checkpoint saved.*1 staged/);
    await page.setViewportSize({width: 420, height: 900});
    await page.screenshot({path: path.join(out, 'queue-and-worker-ranks-mobile.png'), fullPage: true});
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 2), 'queue/rank controls fit mobile width');
    await page.setViewportSize({width: 1440, height: 1000});
    await page.screenshot({path: path.join(out, 'queue-and-worker-ranks.png'), fullPage: true});
    // Refresh reloads the screens; an out-of-date window reloads itself unless Alex is typing.
    assert.equal(await visible('#reloadPage'), true);
    await page.evaluate(() => { window.beforeRefresh = true; });
    await Promise.all([page.waitForEvent('load'), page.locator('#reloadPage').click()]);
    await page.waitForFunction(() => document.querySelector('#projName').textContent === 'Harbor');
    assert.equal(await page.evaluate(() => window.beforeRefresh), undefined, 'Refresh reloads the page');
    await page.evaluate(() => { window.beforeRefresh = true; });
    data.page_id = 'page-b';
    await page.locator('#textSize').focus();
    await page.evaluate(() => refresh());
    assert.equal(await page.evaluate(() => window.beforeRefresh), true, 'no reload while a control has focus');
    assert.match(await page.locator('#downBanner').innerText(), /Press Refresh/);
    await page.evaluate(() => document.activeElement.blur());
    await Promise.all([page.waitForEvent('load'), page.evaluate(() => refresh())]);
    await page.waitForFunction(() => document.querySelector('#projName').textContent === 'Harbor');
    assert.equal(await page.evaluate(() => window.beforeRefresh), undefined, 'a stale page reloads by itself');
    // Who holds the PC: a job Fletchery did not start is named under Building and where a round waits for it.
    await page.locator('[data-view="build"]').click();
    assert.equal(await visible('#pcCard'), false, 'no PC card while the PC is free');
    const savedView = data.merge_view, savedAuto = data.merge_auto;
    data.pc = {held: true, kind: 'outside', who: '0.3.7 release <build>', step: 'Packaging (Build.ps1)', script: 'Build.ps1',
      what: 'Build.ps1 in release-037', since: now, minutes: 72, pid: 14040, workspace: 'E:\\lifeshare-scratch\\release-037',
      note: {label: '0.3.7 release <build>', step: 'Packaging', updated: now}};
    data.merge_view = {...savedView, state: 'waiting', pc: data.pc, steps: [{name: 'Build (candidate C)', state: 'waiting'}, {name: 'Land', state: 'todo'}]};
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => !document.querySelector('#pcCard').hidden);
    assert.equal(await page.locator('#pcWho').innerText(), '0.3.7 release <build>');
    assert.match(await page.locator('#pcChip').innerText(), /Outside Fletchery/);
    assert.match(await page.locator('#pcStep').innerText(), /Packaging \(Build\.ps1\)[\s\S]*holding the PC for 1 h 12 min[\s\S]*process 14040[\s\S]*release-037/);
    assert.equal(await visible('#buildEmpty'), false, 'an outside job counts as build activity');
    await page.screenshot({path: path.join(out, 'pc-holder-building.png')});
    await page.locator('[data-view="merge"]').click();
    assert.match(await page.locator('#mergeNow .merge-pc-wait').innerText(), /0\.3\.7 release <build> is using the PC: Packaging \(Build\.ps1\), for 1 h 12 min/);
    data.merge_view = {active: false, round: '', state: 'idle', prs: [], steps: [], pc: data.pc, headline: ''};
    data.merge_auto = {on: true, held: false, running: {}, why: 'The PC heavy lease is held; merge checkout preparation waits', last: {}};
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => /Waiting for the PC\. 0\.3\.7 release <build> is using the PC/.test(document.querySelector('#mergeNow').textContent));
    data.pc = {held: false, text: '', note: {label: '0.3.7 release build', step: 'Cooking', updated: now}};
    data.merge_view = savedView; data.merge_auto = savedAuto;
    await page.locator('[data-view="build"]').click();
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => document.querySelector('#pcChip').textContent.includes('Between steps'));
    assert.match(await page.locator('#pcStep').innerText(), /Cooking[\s\S]*not holding the PC right now/);
    data.pc = {held: true, kind: 'round', who: 'the merge round', step: 'Build', script: 'Build.ps1', since: now, minutes: 3, note: {}};
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => document.querySelector('#pcChip').textContent.includes('Merge round'));
    delete data.pc;
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => document.querySelector('#pcCard').hidden);
    // Machines: the PC and each node, what it runs now, its last result, and the job list.
    assert.equal(await visible('#machinesCard'), false, 'no Machines card while machines are off');
    data.machines = {listen: '100.83.43.111:8771', shadow: true, error: '', queued: 1,
      recipes: {'node-checks': {context: 'Node checks'}, 'nightly-full': {context: 'Nightly full tests (node)'}, 'checkout-only': {context: ''}},
      machines: [
        {name: 'SF-NODE-01', online: true, revoked: false, last_seen: now, labels: ['os:windows', 'role:tests', 'unreal:5.8.4'], ram_gb: 24, version: '1',
         stats: {cpu_percent: 63, ram_free_gb: 9.5, ram_total_gb: 24, disk_free_gb: 412},
         job: {id: 7, recipe: 'node-checks', sha: 'c'.repeat(40), ref: 'claude/x', params: {only: 'RunFlowTest', strict_slots: false}, state: 'running', started: now, pr: 1666},
         last: {id: 6, state: 'failed', finished: now, summary: '1 of 12 failed on SF-NODE-01: RunM5aTest'}},
        {name: 'SF-NODE-02', online: false, revoked: false, last_seen: '', labels: [], ram_gb: 0, stats: {}, job: null, last: null}],
      jobs: [
        {id: 8, recipe: 'nightly-full', sha: 'd'.repeat(40), ref: 'main', params: {}, state: 'queued', machine: '', created: now, started: '', finished: '', summary: '', requested_by: 'nightly 2026-11-12', log_bytes: 0, pr: 0},
        {id: 7, recipe: 'node-checks', sha: 'c'.repeat(40), ref: 'claude/x', params: {only: 'RunFlowTest'}, state: 'running', machine: 'SF-NODE-01', created: now, started: now, finished: '', summary: '', requested_by: 'dashboard', log_bytes: 2048, pr: 1666},
        {id: 6, recipe: 'node-checks', sha: 'e'.repeat(40), ref: '', params: {}, state: 'failed', machine: 'SF-NODE-01', created: now, started: now, finished: now, summary: '1 of 12 failed on SF-NODE-01: RunM5aTest', requested_by: '', log_bytes: 9000, pr: 0}]};
    await page.locator('[data-view="build"]').click();
    await page.evaluate(() => refresh());
    await page.waitForFunction(() => !document.querySelector('#machinesCard').hidden);
    const machineText = await page.locator('#machineRows').innerText();
    assert.match(machineText, /This PC[\s\S]*Free right now/);
    assert.match(machineText, /SF-NODE-01[\s\S]*Running job 7: node-checks RunFlowTest #1666 at claude\/x cccccccccc/);
    assert.match(machineText, /Last: job 6 ✗ Failed[\s\S]*RunM5aTest/);
    assert.match(machineText, /CPU 63% · RAM 9\.5 of 24 GB free · disk 412 GB free/);
    assert.match(machineText, /SF-NODE-02[\s\S]*Offline, never connected/);
    assert.equal(await page.locator('#jobRows tr').count(), 4);
    assert.equal(await page.locator('#jobRows a[href="/api/jobs/log?id=7"]').count(), 1);
    await page.locator('#jobAdd summary').click();
    assert.equal(await page.locator('#jobRecipe option').count(), 3);
    assert.match(await page.locator('#machinesNote').innerText(), /100\.83\.43\.111:8771[\s\S]*shadow/);
    await page.locator('#jobOnly').fill('RunFlowTest');
    await page.locator('#jobForm button[type="submit"]').click();
    await page.waitForFunction(() => /2 queued/.test(document.querySelector('#jobsNote').textContent));
    assert.deepEqual(requests.at(-1).body, {recipe: 'node-checks', ref: 'main', only: 'RunFlowTest', strict_slots: false});
    page.once('dialog', d => d.accept());
    await page.locator('[data-job-cancel="7"]').click();
    await page.waitForFunction(() => true);
    await page.locator('.machine-add summary').click();
    await page.locator('#machineName').fill('SF-NODE-03');
    await page.locator('#machineForm button[type="submit"]').click();
    await page.waitForFunction(() => !document.querySelector('#machineKey').hidden);
    assert.match(await page.locator('#machineKey').innerText(), /Key for SF-NODE-03 \(shown once\):\s*fnk_test-key/);
    await page.screenshot({path: path.join(out, 'machines-building.png'), fullPage: true});
    assert.equal(requests.filter(r => r.url === '/api/jobs/cancel').at(-1).body.id, 7);
    delete data.machines;
    delete data.integration_queue; delete data.worker_ranks; delete data.worker_handoffs;
    const storageOff = await browser.newContext();
    await storageOff.addInitScript(() => Object.defineProperty(window, 'localStorage', {get() { throw new Error('Storage unavailable'); }}));
    const privatePage = await storageOff.newPage();
    privatePage.on('pageerror', error => errors.push(error.message));
    await privatePage.goto(url);
    await privatePage.waitForFunction(() => document.querySelector('#projName').textContent === 'Harbor');
    assert.equal(await privatePage.locator('#workers').isVisible(), true);
    await storageOff.close();
    assert.deepEqual(errors, []);
    console.log('PASS: navigation, themes, persistence, polling, actions, mobile, legacy links and unavailable storage');
    console.log('Screenshots:', out);
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
