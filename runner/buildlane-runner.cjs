#!/usr/bin/env node
'use strict';
// Buildlane local runner: runs a repository's GitHub Actions workflows on this PC instead of on GitHub.
//
//   node runner/buildlane-runner.cjs serve   [--config <file>] [--parent-pid <pid>]   watch GitHub and run jobs (the service)
//   node runner/buildlane-runner.cjs once    [--config <file>]      one poll + run until idle (testing)
//   node runner/buildlane-runner.cjs run <workflow.yml> [--ref main] [name=value ...]   queue a manual run
//   node runner/buildlane-runner.cjs cancel <runId> | rerun <runId> [--failed]
//   node runner/buildlane-runner.cjs list [--active] [--limit N] | show <runId> | log <runId> [job]
//   node runner/buildlane-runner.cjs hold <workflow.yml> [minutes] | unhold <workflow.yml>
//        pause schedule and workflow_run starts of one workflow (default 15 minutes); manual runs still start
//   node runner/buildlane-runner.cjs exec <workflow.yml> [--event push|pull_request|...] [--job id] [name=value ...]
//        run a workflow from a local checkout without polling (offline check of a workflow on this PC)
const fs = require('fs');
const path = require('path');
const os = require('os');
const { spawnSync } = require('child_process');

function parseArgs(argv) {
  const out = { _: [], flags: {} };
  for (let i = 0; i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith('--')) { const [k, v] = a.slice(2).split('='); if (v !== undefined) out.flags[k] = v; else if (argv[i + 1] && !argv[i + 1].startsWith('--') && !['failed', 'live', 'quiet', 'active'].includes(k)) out.flags[k] = argv[++i]; else out.flags[k] = true; }
    else out._.push(a);
  }
  return out;
}

function expandEnv(s) { return typeof s === 'string' ? s.replace(/%([^%]+)%/g, (m, k) => process.env[k] ?? m) : s; }

function readToken(cfg) {
  if (process.env.BUILDLANE_TOKEN) return process.env.BUILDLANE_TOKEN.trim();
  for (const candidate of [].concat(cfg.tokenFile || [])) {
    const file = expandEnv(candidate);
    if (fs.existsSync(file)) {
      if (process.platform === 'win32') {
        // Buildlane stores the token with Windows DPAPI for the signed-in user (ConvertFrom-SecureString).
        const ps = "$s=(Get-Content -LiteralPath $env:BL_TOKEN_FILE -Raw).Trim() | ConvertTo-SecureString; [Console]::Out.Write([Net.NetworkCredential]::new('', $s).Password)";
        const r = spawnSync('powershell.exe', ['-NoProfile', '-NonInteractive', '-Command', ps], { env: { ...process.env, BL_TOKEN_FILE: file }, encoding: 'utf8', windowsHide: true });
        if (r.status === 0 && r.stdout.trim()) return r.stdout.trim();
      } else return fs.readFileSync(file, 'utf8').trim();
    }
  }
  if (process.env.GH_TOKEN) return process.env.GH_TOKEN.trim();
  const gh = spawnSync('gh', ['auth', 'token'], { encoding: 'utf8', windowsHide: true });
  if (gh.status === 0 && gh.stdout.trim()) return gh.stdout.trim();
  return '';
}

function loadConfig(flags) {
  const file = flags.config || process.env.BUILDLANE_RUNNER_CONFIG || path.join(defaultRoot(), 'runner.json');
  if (!fs.existsSync(file)) throw new Error(`No runner config at ${file}. Copy runner/runner.example.json there and edit it.`);
  const cfg = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));
  cfg.root = expandEnv(cfg.root || path.dirname(file));
  for (const k of ['pauseFile', 'bashPath', 'pwshPath', 'pythonPath', 'nodePath']) if (cfg[k]) cfg[k] = expandEnv(cfg[k]);
  for (const p of Object.values(cfg.pools || {})) { if (p.workRoot) p.workRoot = expandEnv(p.workRoot); if (p.workspace) p.workspace = expandEnv(p.workspace); }
  if (flags.live) cfg.mode = 'live';
  if (flags.quiet) cfg.quiet = true;
  cfg.token = readToken(cfg);
  cfg.secrets = { ...(cfg.secrets || {}) };
  for (const [k, v] of Object.entries(cfg.secretFiles || {})) { const f = expandEnv(v); if (fs.existsSync(f)) cfg.secrets[k] = readToken({ tokenFile: f }); }
  cfg.configFile = file;
  return cfg;
}

function defaultRoot() {
  // Not under AppData: apps packaged as MSIX (such as Claude desktop) see a private copy of AppData, so a
  // config written from one would be invisible to the logon task.
  return process.platform === 'win32' ? path.join(os.homedir(), 'buildlane-local') : path.join(os.homedir(), '.buildlane', 'local-runner');
}

function writeRequest(cfg, req) {
  const dir = path.join(cfg.root, 'requests'); fs.mkdirSync(dir, { recursive: true });
  const name = `${Date.now()}-${process.pid}.json`;
  fs.writeFileSync(path.join(dir, name), JSON.stringify(req, null, 1));
  return path.join(dir, name);
}

async function waitDone(file, seconds = 120) {
  const done = file.replace(/\.json$/, '.done');
  for (let i = 0; i < seconds; i++) { if (fs.existsSync(done)) return JSON.parse(fs.readFileSync(done, 'utf8')).result; await new Promise(r => setTimeout(r, 1000)); }
  return { pending: true, note: 'The runner has not picked this up yet. Is the service running?' };
}

async function main() {
  const { _: args, flags } = parseArgs(process.argv.slice(2));
  const cmd = args.shift() || 'help';
  if (cmd === 'help' || flags.help) { console.log(fs.readFileSync(__filename, 'utf8').split('\n').filter(l => l.startsWith('//')).map(l => l.slice(3)).join('\n')); return; }
  if (cmd === 'exec') return require('./lib/offline.cjs').exec(args, flags);
  const cfg = loadConfig(flags);
  const { LocalRunner } = require('./lib/runner.cjs');
  const { Store } = require('./lib/store.cjs');
  const [owner, repo] = cfg.repo.split('/');
  const store = new Store(cfg.root, { owner, repo });
  switch (cmd) {
    case 'serve': {
      const lock = path.join(cfg.root, 'runner.lock');
      let other = 0; try { other = Number(fs.readFileSync(lock, 'utf8')); } catch {}
      const alive = pid => { try { process.kill(pid, 0); return true; } catch (e) { return e.code === 'EPERM'; } };
      if (other && other !== process.pid && alive(other)) throw new Error(`Another runner (pid ${other}) is already serving ${cfg.root}`);
      fs.writeFileSync(lock, String(process.pid));
      const r = new LocalRunner(cfg);
      await r.start();
      const shutdown = async () => { r.stopping = true; await r.stop(); try { fs.unlinkSync(lock); } catch {} process.exit(0); };
      process.on('SIGINT', shutdown); process.on('SIGTERM', shutdown);
      // Stopping the scheduled task ends only its launcher; --parent-pid makes the service follow it out.
      const parent = Number(flags['parent-pid']);
      if (parent) setInterval(() => { if (!alive(parent)) { r.log(`parent ${parent} exited; stopping`); shutdown(); } }, 5000).unref();
      await r.loop();
      return;
    }
    case 'once': {
      const r = new LocalRunner(cfg);
      await r.start();
      await r.poll();
      for (let i = 0; i < 100000; i++) { await r.tick(); if (!r.active.size && !(r.pendingEvents || []).length) break; await new Promise(res => setTimeout(res, 1000)); }
      await r.stop();
      return;
    }
    case 'run': {
      const file = args.shift(); if (!file) throw new Error('run needs a workflow file name');
      const inputs = Object.fromEntries(args.map(a => { const i = a.indexOf('='); if (i < 1) throw new Error(`'${a}' is not name=value`); return [a.slice(0, i), a.slice(i + 1)]; }));
      console.log(JSON.stringify(await waitDone(writeRequest(cfg, { kind: 'dispatch', workflow: file, ref: flags.ref, inputs }), 90)));
      return;
    }
    case 'cancel': console.log(JSON.stringify(await waitDone(writeRequest(cfg, { kind: 'cancel', runId: Number(args[0]) }), 30))); return;
    case 'rerun': console.log(JSON.stringify(await waitDone(writeRequest(cfg, { kind: 'rerun', runId: Number(args[0]), failedOnly: !!flags.failed }), 30))); return;
    case 'hold': case 'unhold': {
      const file = args.shift(); if (!file) throw new Error(`${cmd} needs a workflow file name`);
      const r = new LocalRunner(cfg); const f = r.holdFile(file);
      if (cmd === 'unhold') { fs.rmSync(f, { force: true }); console.log(`${path.basename(file)}: hold released`); return; }
      const minutes = Number(args[0] || 15); if (!(minutes > 0 && minutes <= 240)) throw new Error('minutes must be 1 to 240');
      fs.mkdirSync(path.dirname(f), { recursive: true });
      const until = new Date(Date.now() + minutes * 60000).toISOString();
      fs.writeFileSync(f, JSON.stringify({ until, by: os.userInfo().username }));
      console.log(`${path.basename(file)}: schedule and workflow_run starts held until ${until}`);
      return;
    }
    case 'list': {
      const shown = store.listForDisplay({ limit: flags.limit, active: !!flags.active });
      for (const r of shown) console.log(`${r.id}  ${(r.conclusion || r.status).padEnd(10)} ${r.name} - ${r.display_title} (${r.event}, ${String(r.head_sha).slice(0, 8)}, ${r.created_at})`);
      return;
    }
    case 'show': { const r = store.get(args[0]); if (!r) throw new Error('no such run'); console.log(JSON.stringify(store.restRun(r), null, 1)); for (const j of r.jobs) { console.log(`job ${j.id} ${j.name}: ${j.conclusion || j.status}`); for (const s of j.steps) console.log(`   ${String(s.number).padStart(2)} ${(s.conclusion || s.status).padEnd(9)} ${s.name}`); } return; }
    case 'log': {
      const r = store.get(args[0]); if (!r) throw new Error('no such run');
      for (const j of r.jobs) if (!args[1] || String(j.id) === args[1] || j.name === args[1]) { const p = store.logPath(r.id, j.id); console.log(`==== ${j.name} (${p})`); if (fs.existsSync(p)) process.stdout.write(fs.readFileSync(p, 'utf8')); }
      return;
    }
    default: throw new Error(`Unknown command ${cmd}. Try help.`);
  }
}

main().catch(e => { console.error(e.message || e); process.exit(1); });
