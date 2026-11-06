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
const pageSource = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8').replace('__FLETCHERY_TOKEN__', 'fixture-token');
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
    res.setHeader('Content-Type', 'application/json');
    return res.end(JSON.stringify(req.url === '/api/issue/get' ? {body: 'Missing access', comments: []} : {ok: true}));
  }
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
    assert.equal(await visible('#queue'), false);
    assert.equal(await visible('#agents'), false);
    assert.equal(await visible('#lowerBox'), false);
    assert.equal(await page.locator('#slots details').count(), 2);
    assert.equal(await page.locator('#slots .free').count(), 1);
    assert.equal(await visible('#emergencyWorkers'), false, 'older payloads hide unavailable emergency controls');
    assert.match(await page.locator('.round-status').innerText(), /Game tests/);
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
    // Each step expands independently; polling preserves explicit choices and keyboard focus.
    await page.locator('[data-view="merge"]').click();
    assert.equal(await visible('#view-merge'), true);
    assert.equal(await visible('#mergeCard .merge-aside'), true);
    assert.match(await page.locator('#mergeNow .merge-health').innerText(), /Active/);
    assert.match(await page.locator('#mergeNow .merge-subline').innerText(), /Last activity/);
    assert.match(await page.locator('#mergeNow .merge-progress-label').innerText(), /2 of 4/);
    const activeStep=page.locator('.merge-step').nth(2);
    assert.equal(await activeStep.getAttribute('open'), '', 'current step expands automatically');
    assert.match(await activeStep.innerText(), /M4b Reconnect/);
    assert.equal(await page.locator('#mergeNow script').count(), 0, 'log lines are text, never markup');
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
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-active-failure-light.png')});
    data.merge_auto.running = {};
    assert.match(await page.locator('.round-recovery').innerText(), /Landing blocked[\s\S]*repair or exclude[\s\S]*rerun affected checks/);
    // A later failure must stay visible even with an older backend.
    data.merge_auto.running = {round: 20, agent: 'codex-sol', started: now, prs: [1041]};
    data.merge_view.state = 'running';
    data.merge_view.steps = [{name: '<Package>', state: 'running'}, {name: 'Game specs', state: 'failed'}];
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('#mergeNow').getAttribute('class'), 'merge-now failed');
    assert.match(await page.locator('.round-status').innerText(), /Game specs[\s\S]*failed/);
    assert.match(await page.locator('.round-recovery').innerText(), /Reported activity: <Package>[\s\S]*does not clear/);
    assert.equal(await page.locator('.round-recovery package').count(), 0, 'activity names are escaped');
    await page.locator('#mergeCard').screenshot({path: path.join(out, 'merge-recovery-light.png')});
    data.merge_view.steps = [{name: 'Game specs', state: 'running'}, {name: 'Old low-VRAM check', state: 'failed', required: false}, {name: 'Landing', state: 'todo'}];
    await page.evaluate(next => { last = next; render(next); }, data);
    assert.equal(await page.locator('.round-recovery').count(), 0);
    assert.match(await page.locator('[aria-label="Current round steps"]').innerText(), /Old low-VRAM check \(previous candidate\)/);
    data.update_pending=true;
    data.merge_auto.running = {};
    await page.evaluate(next=>{last=next;render(next);},data);
    assert.match(await page.locator('#holdBanner').innerText(),/Running sessions finish/);
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
    assert.equal(await page.locator('#nowh').innerText(), 'All workers');
    assert.equal(await page.locator('[data-rank]').count(), 3);
    assert.equal(await page.locator('[data-worker="891"] .worker-rank-badge').textContent(), 'High');
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
    assert.match(await page.locator('#rankMode').innerText(), /0 easy/);
    await page.selectOption('#allWorkerTotal', '12');
    await page.waitForFunction(() => document.querySelector('#wVal').textContent === '12');
    assert.deepEqual(requests.at(-1).body, {max_parallel: 12});
    await page.selectOption('[data-rank-cap="low"]', '7');
    await page.waitForFunction(() => document.querySelector('#rankMode').textContent.includes('Manual'));
    assert.equal(requests.at(-1).url, '/api/workers/ranks');
    assert.equal(requests.at(-1).body.limits.low, 7);
    await page.locator('#planMerge').click();
    await page.locator('#planWhen').fill('2030-11-05T19:30');
    await page.locator('#planSubmit').click();
    await page.waitForFunction(() => !document.querySelector('#planDlg').open);
    assert.equal(requests.at(-1).url, '/api/queue/add');
    assert.deepEqual(requests.at(-1).body.prs, [1041]);
    assert.match(requests.at(-1).body.not_before, /2030-11/);
    const mergeId = data.integration_queue.requests[0].id;
    await page.locator('#planPackage').click();
    await page.locator('#planVersion').fill('0.3.7');
    await page.selectOption('#planAfter', mergeId);
    assert.equal(await page.locator('#planTarget').isVisible(), false);
    await page.locator('#planSubmit').click();
    await page.waitForFunction(() => !document.querySelector('#planDlg').open);
    assert.match(await page.locator('#overviewStats').textContent(), /Merge round 16 running \/ Package 0\.3\.7 queued/);
    assert.match(await page.locator('#planRows').textContent(), /Waiting for its merge window to land/);
    assert.equal(requests.at(-1).body.after, mergeId);
    assert.equal(requests.at(-1).body.target, '');
    assert.equal(requests.at(-1).token, 'fixture-token');
    page.once('dialog', dialog => dialog.dismiss());
    await page.locator('[data-plan-cancel]').first().click();
    assert.equal(requests.at(-1).url, '/api/queue/add', 'declining cancellation leaves the request in place');
    page.once('dialog', dialog => dialog.accept());
    await page.locator('[data-plan-cancel]').first().click();
    await page.waitForFunction(() => document.querySelector('.plans-history summary')?.textContent.includes('History'));
    assert.equal(await page.locator('.plans-history').getAttribute('open'), null, 'history is collapsed by default');
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
    assert.equal(await plans.locator('.plan-row.blocked').getByRole('button').count(), 0, 'a dispatched request cannot be cancelled here');
    assert.equal(await plans.getByRole('link', {name: 'Open build'}).getAttribute('href'), data.repo_url + '/actions/runs/123456');
    await page.evaluate(() => document.querySelector('#planMessage').textContent = '');
    await plans.screenshot({path: path.join(out, 'merge-windows-dark.png')});
    await page.selectOption('#themeChoice', 'light');
    await plans.screenshot({path: path.join(out, 'merge-windows-light.png')});
    await plans.locator('.plans-history > summary').click();
    assert.match(await plans.locator('.plans-history').innerText(), /Cancelled/);
    await busy.locator('summary').click();
    const pr = busy.getByRole('link', {name: '#988', exact: true});
    assert.equal(await pr.getAttribute('href'), data.repo_url + '/pull/988');
    await pr.focus();
    data.integration_queue.requests.find(r => r.id === 'busy').reason = 'Building the merge candidate.';
    await page.evaluate(next => {last = next; render(next);}, data);
    assert.equal(await plans.locator('.plans-history').getAttribute('open'), '');
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
