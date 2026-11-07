// App updates from GitHub Releases (electron-updater, latest.yml / latest-linux.yml).
// The app updates itself and, with it, the engine it carries. The beta channel of
// `skillarium self-update --channel beta` also lets the app take pre-releases.
const { app } = require('electron');

const CHECK_EVERY = 4 * 60 * 60 * 1000;
let updater = null;
let state = { state: app.isPackaged ? 'idle' : 'development' };

function startUpdates(send, channel) {
  if (!app.isPackaged) return;
  if (process.env.SKILLARIUM_NO_UPDATES) {
    state = { state: 'disabled' };
    return;
  }
  ({ autoUpdater: updater } = require('electron-updater'));
  updater.allowPrerelease = channel === 'beta';
  updater.autoDownload = true;
  updater.autoInstallOnAppQuit = true;
  const publish = (next) => { state = next; send(state); };
  const checked = () => new Date().toISOString();
  updater.on('checking-for-update', () => publish({ ...state, state: 'checking' }));
  updater.on('update-available', (info) => publish({ state: 'downloading', version: info.version, checkedAt: checked() }));
  updater.on('download-progress', (progress) => publish({ ...state, state: 'downloading', percent: Math.floor(progress.percent) }));
  updater.on('update-not-available', () => publish({ state: 'current', version: app.getVersion(), checkedAt: checked() }));
  updater.on('update-downloaded', (info) => publish({ state: 'ready', version: info.version, checkedAt: state.checkedAt || checked() }));
  updater.on('error', (error) => publish({ state: 'error', message: String(error?.message || error).split('\n')[0], checkedAt: checked() }));
  checkNow();
  setInterval(checkNow, CHECK_EVERY).unref();
}

function updateState() {
  return state;
}

// Scheduled and manual checks alike: a check while a version downloads or waits for a
// restart would only fetch it again and take the restart away from the window.
function checkNow() {
  if (!updater || ['checking', 'downloading', 'ready'].includes(state.state)) return Promise.resolve(state);
  return updater.checkForUpdates().then(() => state, () => state);
}

function installNow() {
  if (updater && state.state === 'ready') updater.quitAndInstall(false, true);
}

module.exports = { startUpdates, updateState, checkNow, installNow };
