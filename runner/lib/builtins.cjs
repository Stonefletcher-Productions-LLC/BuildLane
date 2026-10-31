'use strict';
// Actions that need GitHub's hosted services are replaced by local equivalents:
// upload/download-artifact keep artifacts in the local run store (served by the local API),
// setup-* use the tools already installed on this PC, cache always misses.
const fs = require('fs');
const path = require('path');
const zip = require('./zip.cjs');
const { globToRegex } = require('./workflow.cjs');

function expandPaths(workspace, spec) {
  const lines = String(spec || '').split(/\r?\n/).map(s => s.trim()).filter(Boolean);
  const include = [], exclude = [];
  for (const l of lines) (l.startsWith('!') ? exclude : include).push(l.replace(/^!/, ''));
  const files = new Map(); // abs -> archive name
  for (const inc of include) {
    const abs = path.resolve(workspace, inc);
    if (!/[*?[]/.test(inc)) {
      if (!fs.existsSync(abs)) continue;
      if (fs.statSync(abs).isDirectory()) { for (const f of zip.listFiles(abs)) files.set(f, path.relative(abs, f)); }
      else files.set(abs, path.basename(abs));
      continue;
    }
    const firstGlob = inc.search(/[*?[]/);
    const base = path.resolve(workspace, inc.slice(0, inc.lastIndexOf('/', firstGlob) + 1) || '.');
    const re = globToRegex(path.resolve(workspace, inc).replace(/\\/g, '/'));
    if (!fs.existsSync(base)) continue;
    for (const f of zip.listFiles(base)) if (re.test(f.replace(/\\/g, '/'))) files.set(f, path.relative(base, f));
  }
  for (const exc of exclude) {
    const re = globToRegex(path.resolve(workspace, exc).replace(/\\/g, '/'));
    for (const f of [...files.keys()]) if (re.test(f.replace(/\\/g, '/'))) files.delete(f);
  }
  return files;
}

const uploadArtifact = {
  async run({ job, inputs, stepOutputs }) {
    const name = inputs.name || 'artifact';
    const files = expandPaths(job.workspace, inputs.path);
    const mode = inputs['if-no-files-found'] || 'warn';
    if (!files.size) {
      const msg = `No files were found with the provided path: ${inputs.path}. No artifacts will be uploaded.`;
      if (mode === 'error') { job.log.write(`##[error]${msg}`); return 'failure'; }
      job.log.write(mode === 'warn' ? `##[warning]${msg}` : msg); return 'success';
    }
    const entries = [...files].map(([abs, name]) => ({ name: name.replace(/\\/g, '/'), data: fs.readFileSync(abs) }));
    const art = job.o.store.addArtifact(job.o.run, name, zip.build(entries));
    job.log.write(`Artifact ${name} kept locally (${entries.length} files, ${art.size_in_bytes} bytes, id ${art.id}).`);
    stepOutputs['artifact-id'] = String(art.id);
    stepOutputs['artifact-url'] = '';
    return 'success';
  },
};

const downloadArtifact = {
  async run({ job, inputs }) {
    const s = job.o.store;
    const runId = inputs['run-id'] ? Number(inputs['run-id']) : job.o.run.id;
    const run = s.get(runId);
    if (!run) { job.log.write(`##[error]Run ${runId} is not in the local store`); return 'failure'; }
    let arts = run.artifacts || [];
    if (inputs.name) arts = arts.filter(a => a.name === inputs.name);
    else if (inputs.pattern) { const re = globToRegex(inputs.pattern); arts = arts.filter(a => re.test(a.name)); }
    if (inputs.name && !arts.length) { job.log.write(`##[error]Artifact '${inputs.name}' not found`); return 'failure'; }
    const dest = path.resolve(job.workspace, inputs.path || '.');
    const separate = !inputs.name && !(String(inputs['merge-multiple']) === 'true');
    for (const a of arts) {
      const out = separate ? path.join(dest, a.name) : dest;
      for (const e of zip.unzip(fs.readFileSync(path.join(s.artifactDir(run.id), a.file)))) {
        const target = path.resolve(out, e.name);
        if (!target.startsWith(path.resolve(out))) throw new Error(`Unsafe artifact entry ${e.name}`);
        fs.mkdirSync(path.dirname(target), { recursive: true }); fs.writeFileSync(target, e.data);
      }
      job.log.write(`Downloaded artifact ${a.name} to ${out}`);
    }
    return 'success';
  },
};

const noOp = what => ({ async run({ job }) { job.log.write(`${what}: using the tools installed on this PC.`); return 'success'; } });
const cacheMiss = { async run({ job, stepOutputs }) { job.log.write('actions/cache: no cache on the local runner (miss).'); stepOutputs['cache-hit'] = 'false'; return 'success'; } };

const TABLE = {
  'actions/upload-artifact': uploadArtifact,
  'actions/download-artifact': downloadArtifact,
  'actions/setup-node': noOp('setup-node'),
  'actions/setup-python': noOp('setup-python'),
  'actions/setup-dotnet': noOp('setup-dotnet'),
  'actions/cache': cacheMiss,
  'actions/cache/restore': cacheMiss,
  'actions/cache/save': noOp('cache save skipped'),
};

// Node actions shipped with Buildlane (no download): same inputs and defaults as the originals.
const LOCAL_ACTIONS = {
  'actions/github-script': {
    dir: path.join(__dirname, '..', 'actions', 'github-script'),
    meta: { inputs: { script: {}, 'github-token': { default: '${{ github.token }}' }, debug: { default: 'false' }, 'user-agent': { default: 'actions/github-script' },
      previews: {}, 'result-encoding': { default: 'json' }, retries: { default: '0' }, 'retry-exempt-status-codes': { default: '400,401,403,404,422' }, 'base-url': {} },
      runs: { using: 'node20', main: 'index.cjs' } },
  },
};
function localAction(uses) { const a = LOCAL_ACTIONS[String(uses).split('@')[0].toLowerCase()]; return a && !(process.env.BUILDLANE_REAL_ACTIONS || '').split(',').includes(String(uses).split('@')[0]) ? a : null; }

function find(uses) { return TABLE[String(uses).split('@')[0].toLowerCase()] || null; }

module.exports = { find, localAction, expandPaths };
