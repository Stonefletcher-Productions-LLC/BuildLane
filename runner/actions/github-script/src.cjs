'use strict';
// Local stand-in for actions/github-script@v7 (same inputs, arguments and outputs), bundled so the
// build PC does not need to download it. Source of the bundle in index.cjs: this file + @actions/*.
const path = require('path');
const core = require('@actions/core');
const exec = require('@actions/exec');
const glob = require('@actions/glob');
const io = require('@actions/io');
const { context, getOctokit } = require('@actions/github');

const AsyncFunction = Object.getPrototypeOf(async () => null).constructor;

function wrapRequire(moduleId) {
  if (moduleId.startsWith('.')) return require(path.resolve(process.cwd(), moduleId));
  try { return require(require.resolve(moduleId, { paths: [path.join(process.cwd(), 'node_modules')] })); }
  catch { return require(moduleId); }
}

async function main() {
  const token = core.getInput('github-token', { required: true });
  const debug = core.getBooleanInput('debug');
  const userAgent = core.getInput('user-agent') || 'actions/github-script';
  const previews = core.getInput('previews');
  const baseUrl = core.getInput('base-url');
  const opts = { userAgent };
  if (debug) opts.log = console;
  if (previews) opts.previews = previews.split(',');
  if (baseUrl) opts.baseUrl = baseUrl;
  const github = getOctokit(token, opts);
  const script = core.getInput('script', { required: true });
  const fn = new AsyncFunction('require', '__original_require__', 'github', 'octokit', 'context', 'core', 'exec', 'glob', 'io', 'fetch', script);
  const result = await fn(wrapRequire, require, github, github, context, core, exec, glob, io, fetch);
  const encoding = core.getInput('result-encoding') || 'json';
  let output;
  if (encoding === 'json') output = JSON.stringify(result);
  else if (encoding === 'string') output = String(result);
  else throw new Error('"result-encoding" must be either "string" or "json"');
  core.setOutput('result', output === undefined ? '' : output);
}

process.on('unhandledRejection', err => { core.error(err && err.stack || String(err)); core.setFailed(`Unhandled error: ${err}`); });
main().catch(err => { core.error(err && err.stack || String(err)); core.setFailed(`Unhandled error: ${err}`); });
