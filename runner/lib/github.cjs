'use strict';
// Small GitHub REST client for the runner itself (polling, statuses, workflow files, command comments).
class GitHub {
  constructor({ token, api = 'https://api.github.com', owner, repo, timeoutMs = 60000 }) { this.token = token; this.api = api.replace(/\/$/, ''); this.owner = owner; this.repo = repo; this.timeoutMs = timeoutMs; }
  get base() { return `/repos/${this.owner}/${this.repo}`; }
  async request(method, path, body, { raw = false, accept } = {}) {
    const url = path.startsWith('http') ? path : this.api + path;
    const headers = { accept: accept || 'application/vnd.github+json', 'user-agent': 'Buildlane-Runner', 'x-github-api-version': '2022-11-28' };
    if (this.token) headers.authorization = `Bearer ${this.token}`;
    if (body !== undefined) headers['content-type'] = 'application/json';
    // Every request has a deadline covering the body too: a stalled connection must never freeze the runner's loop.
    const signal = AbortSignal.timeout(this.timeoutMs);
    let r, text;
    try {
      r = await fetch(url, { method, headers, body: body === undefined ? undefined : JSON.stringify(body), signal });
      if (raw) return r;
      text = await r.text();
    } catch (e) {
      if (e && (e.name === 'TimeoutError' || e.name === 'AbortError')) { const t = new Error(`GitHub ${method} ${path}: no answer within ${this.timeoutMs / 1000} s`); t.timeout = true; throw t; }
      throw e;
    }
    let data = null; try { data = text ? JSON.parse(text) : null; } catch { data = text; }
    if (!r.ok) { const e = new Error(`GitHub ${method} ${path}: ${r.status} ${data && data.message ? data.message : text.slice(0, 200)}`); e.status = r.status; e.data = data; throw e; }
    return data;
  }
  get(path) { return this.request('GET', path); }
  post(path, body) { return this.request('POST', path, body); }
  async paginate(path, key, max = 30) {
    const out = [];
    for (let p = 1; p <= max; p++) {
      const sep = path.includes('?') ? '&' : '?';
      const data = await this.get(`${path}${sep}per_page=100&page=${p}`);
      const items = key ? data[key] : data;
      out.push(...items);
      if (items.length < 100) break;
    }
    return out;
  }
  async fileAt(file, ref) {
    try {
      const d = await this.get(`${this.base}/contents/${file.split('/').map(encodeURIComponent).join('/')}?ref=${encodeURIComponent(ref)}`);
      return Buffer.from(d.content, 'base64').toString('utf8');
    } catch (e) { if (e.status === 404) return null; throw e; }
  }
  async listDir(dir, ref) {
    try { return await this.get(`${this.base}/contents/${dir}?ref=${encodeURIComponent(ref)}`); }
    catch (e) { if (e.status === 404) return []; throw e; }
  }
}
module.exports = { GitHub };
