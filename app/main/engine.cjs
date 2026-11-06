// The engine the app runs. A packaged app carries the engine of its own release
// in resources/engine and lays it out like `skillarium install` does:
// versions/<version> plus a current link that schedules and the skillarium
// command go through. A development checkout runs the repository itself.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function engineHome(env = process.env, platform = process.platform) {
  if (env.SKILLARIUM_HOME) return env.SKILLARIUM_HOME;
  if (platform === 'win32') {
    return path.join(env.LOCALAPPDATA || path.join(os.homedir(), 'AppData', 'Local'), 'Skillarium');
  }
  return path.join(env.XDG_DATA_HOME || path.join(os.homedir(), '.local', 'share'), 'skillarium');
}

function versionOf(folder) {
  try { return fs.readFileSync(path.join(folder, 'VERSION'), 'utf8').trim(); } catch { return null; }
}

function recordHistory(home, version) {
  const file = path.join(home, 'history');
  let lines = [];
  try { lines = fs.readFileSync(file, 'utf8').split(/\r?\n/).filter((line) => line && line !== version); } catch { /* first */ }
  fs.writeFileSync(`${file}.new`, `${[...lines, version].join('\n')}\n`);
  fs.renameSync(`${file}.new`, file);
}

const KEEP = 2;
const VERSION = /^(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?$/;

// Semantic version precedence: negative, zero or positive like a comparator.
function compareVersions(a, b) {
  const x = VERSION.exec(a);
  const y = VERSION.exec(b);
  for (let i = 1; i <= 3; i += 1) {
    const difference = Number(x[i]) - Number(y[i]);
    if (difference) return difference;
  }
  if (x[4] === y[4]) return 0;
  if (x[4] === undefined) return 1;
  if (y[4] === undefined) return -1;
  const left = x[4].split('.');
  const right = y[4].split('.');
  for (let i = 0; i < Math.max(left.length, right.length); i += 1) {
    if (i >= left.length) return -1;
    if (i >= right.length) return 1;
    if (left[i] === right[i]) continue;
    const ln = /^\d+$/.test(left[i]);
    const rn = /^\d+$/.test(right[i]);
    if (ln && rn) return Number(left[i]) - Number(right[i]);
    if (ln) return -1;
    if (rn) return 1;
    return left[i] < right[i] ? -1 : 1;
  }
  return 0;
}

// Keeps the current version and the two most recently active others, like `skillarium`.
function prune(home, current) {
  let history = [];
  try { history = fs.readFileSync(path.join(home, 'history'), 'utf8').split(/\r?\n/).filter(Boolean); } catch { /* none */ }
  const kept = history.reverse().filter((version) => version !== current).slice(0, KEEP);
  for (const name of fs.readdirSync(path.join(home, 'versions'))) {
    if (name.startsWith('.') || name === current || kept.includes(name)) continue;
    fs.rmSync(path.join(home, 'versions', name), { recursive: true, force: true });
  }
}

// Removes a symlink or junction itself, never the folder it points to.
function removeLink(link) {
  try { fs.unlinkSync(link); } catch (error) { if (error.code !== 'ENOENT') throw error; }
}

function switchCurrent(home, version, platform) {
  const current = path.join(home, 'current');
  const next = path.join(home, 'current.new');
  removeLink(next);
  if (platform === 'win32') {
    fs.symlinkSync(path.join(home, 'versions', version), next, 'junction');
    // A junction cannot be renamed over another one: the gap is a few milliseconds.
    removeLink(current);
  } else {
    fs.symlinkSync(path.join('versions', version), next);
  }
  fs.renameSync(next, current);
  recordHistory(home, version);
}

// Copies the bundled engine into versions/<version> (once) and points current at it, unless
// current is already newer (a beta self-update): the app never downgrades the engine.
// Returns the engine folder to run, or null when the app carries no engine.
function deployBundledEngine(bundled, { env = process.env, platform = process.platform } = {}) {
  const version = versionOf(bundled);
  if (!version || !VERSION.test(version)) return null;
  const home = engineHome(env, platform);
  const target = path.join(home, 'versions', version);
  fs.mkdirSync(path.join(home, 'versions'), { recursive: true });
  if (versionOf(target) !== version) {
    const incoming = fs.mkdtempSync(path.join(home, 'versions', '.incoming-'));
    try {
      fs.cpSync(bundled, path.join(incoming, 'engine'), { recursive: true });
      fs.rmSync(target, { recursive: true, force: true });
      fs.renameSync(path.join(incoming, 'engine'), target);
    } finally {
      fs.rmSync(incoming, { recursive: true, force: true });
    }
  }
  const installed = versionOf(path.join(home, 'current'));
  if (!installed || !VERSION.test(installed) || compareVersions(version, installed) > 0) {
    switchCurrent(home, version, platform);
    prune(home, version);
  }
  return path.join(home, 'current');
}

function channel(env = process.env) {
  try {
    const value = fs.readFileSync(path.join(engineHome(env), 'channel'), 'utf8').trim();
    return value === 'beta' ? 'beta' : 'stable';
  } catch { return 'stable'; }
}

module.exports = { engineHome, deployBundledEngine, channel, compareVersions, versionOf };
