'use strict';
// Fetches JavaScript and composite actions (owner/repo[/path]@ref) from GitHub once and keeps them
// under <root>/_actions/owner/repo/ref. A ref that looks like a commit SHA is fetched exactly.
const fs = require('fs');
const path = require('path');
const { spawnSync } = require('child_process');

class ActionCache {
  constructor(root, { git = 'git', token } = {}) { this.root = path.join(root, '_actions'); this.git = git; this.token = token; }

  resolve(uses, log = { write() {} }) {
    const m = /^([^/@]+)\/([^/@]+)((?:\/[^@]+)?)@(.+)$/.exec(uses);
    if (!m) throw new Error(`Cannot parse action reference '${uses}'`);
    const [, owner, repo, sub, ref] = m;
    if (!/^[\w.-]+$/.test(owner) || !/^[\w.-]+$/.test(repo) || !/^[\w./-]+$/.test(ref) || ref.includes('..')) throw new Error(`Unsafe action reference '${uses}'`);
    const dir = path.join(this.root, owner, repo, ref.replace(/\//g, '_'));
    if (!fs.existsSync(path.join(dir, '.buildlane-ok'))) {
      log.write(`Download action repository '${owner}/${repo}@${ref}'`);
      fs.rmSync(dir, { recursive: true, force: true });
      fs.mkdirSync(dir, { recursive: true });
      const url = `https://github.com/${owner}/${repo}.git`;
      const env = { ...process.env, GIT_TERMINAL_PROMPT: '0', GIT_LFS_SKIP_SMUDGE: '1' };
      const auth = this.token ? ['-c', `http.https://github.com/.extraheader=AUTHORIZATION: basic ${Buffer.from(`x-access-token:${this.token}`).toString('base64')}`] : [];
      const run = args => { const r = spawnSync(this.git, [...auth, ...args], { cwd: dir, env, encoding: 'utf8' }); if (r.status !== 0) throw new Error(`git ${args[0]} failed for ${uses}: ${(r.stderr || r.error || '').toString().trim()}`); };
      run(['init', '-q']);
      if (/^[0-9a-f]{40}$/i.test(ref)) run(['fetch', '-q', '--depth', '1', url, ref]);
      else { try { run(['fetch', '-q', '--depth', '1', url, `refs/tags/${ref}`]); } catch { run(['fetch', '-q', '--depth', '1', url, `refs/heads/${ref}`]); } }
      run(['checkout', '-q', 'FETCH_HEAD']);
      fs.writeFileSync(path.join(dir, '.buildlane-ok'), new Date().toISOString());
    }
    return sub ? path.join(dir, sub.slice(1)) : dir;
  }
}

module.exports = { ActionCache };
