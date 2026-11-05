// App updates from GitHub Releases (electron-updater, latest.yml / latest-linux.yml).
// The app updates itself and, with it, the engine it carries. The beta channel of
// `skillarium self-update --channel beta` also lets the app take pre-releases.
const { app } = require('electron');

const CHECK_EVERY = 4 * 60 * 60 * 1000;
let updater = null;
let state = { state: app.isPackaged ? 'idle' : 'development' };

function startUpdates(send, channel) {
  if (!app.isPackaged || process.env.SKILLARIUM_NO_UPDATES) return;
  ({ autoUpdater: updater } = require('electron-updater'));
  updater.allowPrerelease = channel === 'beta';
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = true;
  const publish = (next) => { state = next; send(state); };
  updater.on('checking-for-update', () => publish({ state: 'checking' }));
  updater.on('update-available', (info) => publish({ state: 'downloading', version: info.version }));
  updater.on('update-not-available', () => publish({ state: 'current', version: app.getVersion() }));
  updater.on('update-downloaded', (info) => publish({ state: 'ready', version: info.version }));
  updater.on('error', (error) => publish({ state: 'error', message: String(error?.message || error).split('\n')[0] }));
  const check = () => updater.checkForUpdates().catch(() => {});
  check();
  setInterval(check, CHECK_EVERY).unref();
}

function updateState() {
  return state;
}

function checkNow() {
  if (!updater) return Promise.resolve(state);
  return updater.checkForUpdates().then(() => state, () => state);
}

function installNow() {
  if (updater && state.state === 'ready') updater.quitAndInstall(false, true);
}

module.exports = { startUpdates, updateState, checkNow, installNow };
