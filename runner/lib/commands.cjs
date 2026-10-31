'use strict';
// Workflow command and environment-file parsing (GITHUB_OUTPUT, GITHUB_ENV, GITHUB_STATE, ::cmd::).
const fs = require('fs');

function parseEnvFile(file) {
  const out = {};
  let text = '';
  try { text = fs.readFileSync(file, 'utf8'); } catch { return out; }
  const lines = text.replace(/^﻿/, '').split(/\r?\n/);
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];
    if (!line) continue;
    const h = /^([^=<]+)<<(.+)$/.exec(line);
    if (h) {
      const delim = h[2]; const buf = [];
      for (i++; i < lines.length && lines[i] !== delim; i++) buf.push(lines[i]);
      if (i >= lines.length) throw new Error(`Matching delimiter '${delim}' not found in ${file}`);
      out[h[1]] = buf.join('\n');
      continue;
    }
    const eq = line.indexOf('=');
    if (eq > 0) out[line.slice(0, eq)] = line.slice(eq + 1);
    else throw new Error(`Invalid format '${line}' in ${file}`);
  }
  return out;
}

function unescapeData(s) { return s.replace(/%0D/gi, '\r').replace(/%0A/gi, '\n').replace(/%25/g, '%'); }
function unescapeProp(s) { return unescapeData(s).replace(/%3A/gi, ':').replace(/%2C/gi, ','); }

// Returns { cmd, props, data } or null.
function parseCommand(line) {
  const m = /^::([A-Za-z-]+)(?: ([^:]*))?::(.*)$/.exec(line.trim());
  if (!m) return null;
  const props = {};
  if (m[2]) for (const kv of m[2].split(',')) { const i = kv.indexOf('='); if (i > 0) props[kv.slice(0, i).trim()] = unescapeProp(kv.slice(i + 1)); }
  return { cmd: m[1].toLowerCase(), props, data: unescapeData(m[3]) };
}

module.exports = { parseEnvFile, parseCommand };
