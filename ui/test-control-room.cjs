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
  merge_view: {round: 16, state: 'running', headline: 'Game tests running · 47 min elapsed', prs: [1041], steps: [
    {name: 'Compile', state: 'done'}, {name: 'Imports', state: 'done'}, {name: 'Game tests', state: 'running'}, {name: 'Land', state: 'todo'},
  ]},
  merge_auto: {on: true, held: false, running: {round: 16, agent: 'codex-sol', started: now, prs: [1041]}, last: {}},
  history: [{task: 840, result: 'waiting_dependencies', at: now, agent: 'codex-sol', cost_usd: 0}],
  log: [{at: now, line: 'Merge round 16 started'}], usage_week: [],
};
const requests = [];
const pageSource = fs.readFileSync(path.join(__dirname, 'index.html'), 'utf8').replace('__FLETCHERY_TOKEN__', 'fixture-token');
const server = http.createServer((req, res) => {
  if (req.url === '/favicon.ico') { res.writeHead(204); return res.end(); }
  if (req.method === 'POST') {
    requests.push({url: req.url, token: req.headers['x-fletchery-token']});
    if (req.url === '/api/pause') data.held = true;
    if (req.url === '/api/resume') data.held = false;
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
    for (const view of ['overview', 'tasks', 'agents', 'activity']) {
      await page.locator(`[data-view="${view}"]`).click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true, `${view} fits mobile width`);
    }
    await page.locator('[data-view="overview"]').click();
    await page.screenshot({path: path.join(out, 'overview-mobile.png'), fullPage: true});
    await page.goto(url + '/?only=workers');
    assert.equal(await visible('#workers'), true);
    assert.equal(await visible('.sidebar'), false);
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
