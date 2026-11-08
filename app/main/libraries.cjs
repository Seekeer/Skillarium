// Which libraries the app works with. The Skillarium configuration lists the connected
// libraries in priority order ("libraries"; the legacy single "library" is a list of one).
// The engine's status already covers all of them, so the app only needs one library to
// start the engine from: an explicit choice, or the first connected one that is reachable.
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');

function configFile(env = process.env, platform = process.platform) {
  return platform === 'win32'
    ? path.join(env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Skillarium', 'config.json')
    : path.join(env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'skillarium', 'config.json');
}

function readConfig(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return {}; }
}

// [{name, path, url}] in priority order; names may be empty for the legacy key.
function configuredLibraries(config) {
  if (Array.isArray(config?.libraries)) {
    return config.libraries.filter((item) => item && typeof item.path === 'string' && item.path)
      .map((item) => ({ name: String(item.name || ''), path: item.path, url: String(item.url || '') }));
  }
  if (typeof config?.library === 'string' && config.library) return [{ name: '', path: config.library, url: '' }];
  return [];
}

function isLibrary(folder) {
  return Boolean(folder) && fs.existsSync(path.join(folder, 'sources.json'))
    && fs.existsSync(path.join(folder, 'agent-skills.json'));
}

// override: a folder chosen for this app (environment, settings, development checkout).
// base: the library the engine is started from; null when there is nothing to work with.
function locate({ override = '', libraries = [], check = isLibrary } = {}) {
  if (override) {
    // Like the native clients, an override without a path separator may name a connected library.
    const named = /[\\/:]/.test(override) ? null : libraries.find((item) => item.name === override);
    const folder = named ? named.path : override;
    return { override: folder, base: check(folder) ? folder : null, libraries };
  }
  const reachable = libraries.find((item) => check(item.path));
  return { override: '', base: reachable ? reachable.path : null, libraries };
}

module.exports = { configFile, readConfig, configuredLibraries, isLibrary, locate };
