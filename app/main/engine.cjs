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

// Copies the bundled engine into versions/<version> (once) and points current at it.
// Returns the engine folder to run, or null when the app carries no engine.
function deployBundledEngine(bundled, { env = process.env, platform = process.platform } = {}) {
  const version = versionOf(bundled);
  if (!version || !/^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$/.test(version)) return null;
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
  if (versionOf(path.join(home, 'current')) !== version) switchCurrent(home, version, platform);
  return path.join(home, 'current');
}

function channel(env = process.env) {
  try {
    const value = fs.readFileSync(path.join(engineHome(env), 'channel'), 'utf8').trim();
    return value === 'beta' ? 'beta' : 'stable';
  } catch { return 'stable'; }
}

module.exports = { engineHome, deployBundledEngine, channel };
