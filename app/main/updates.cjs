// App updates from GitHub Releases (electron-updater, latest.yml / latest-linux.yml).
// The app updates itself and, with it, the engine it carries. The beta channel of
// `skillarium self-update --channel beta` also lets the app take pre-releases.
//
// SKILLARIUM_NO_UPDATES turns updates off. SKILLARIUM_NO_INSTALL keeps checking and
// downloading but never runs the installer, neither on quit nor from the window: a build
// under verification (app/scripts/verify-packaged.ps1) must not replace a real installation.
const CHECK_EVERY = 4 * 60 * 60 * 1000;

// Electron and electron-updater are passed in, so the state machine runs in plain Node tests.
function createUpdates({ app, loadUpdater, env = process.env, now = () => new Date() }) {
  let updater = null;
  let state = { state: app.isPackaged ? 'idle' : 'development' };
  const installs = !env.SKILLARIUM_NO_INSTALL;

  function start(send, channel) {
    if (!app.isPackaged) return;
    if (env.SKILLARIUM_NO_UPDATES) {
      state = { state: 'disabled' };
      return;
    }
    updater = loadUpdater();
    updater.allowPrerelease = channel === 'beta';
    updater.autoDownload = true;
    updater.autoInstallOnAppQuit = installs;
    const publish = (next) => { state = installs ? next : { ...next, install: false }; send(state); };
    const checked = () => now().toISOString();
    updater.on('checking-for-update', () => publish({ ...state, state: 'checking' }));
    updater.on('update-available', (info) => publish({ state: 'downloading', version: info.version, checkedAt: checked() }));
    updater.on('download-progress', (progress) => publish({ ...state, state: 'downloading', percent: Math.floor(progress.percent) }));
    updater.on('update-not-available', () => publish({ state: 'current', version: app.getVersion(), checkedAt: checked() }));
    updater.on('update-downloaded', (info) => publish({ state: 'ready', version: info.version, checkedAt: state.checkedAt || checked() }));
    updater.on('error', (error) => publish({ state: 'error', message: String(error?.message || error).split('\n')[0], checkedAt: checked() }));
    check();
    setInterval(check, CHECK_EVERY).unref();
  }

  // Scheduled and manual checks alike: a check while a version downloads or waits for a
  // restart would only fetch it again and take the restart away from the window.
  function check() {
    if (!updater || ['checking', 'downloading', 'ready'].includes(state.state)) return Promise.resolve(state);
    return updater.checkForUpdates().then(() => state, () => state);
  }

  function install() {
    if (updater && installs && state.state === 'ready') updater.quitAndInstall(false, true);
  }

  return { startUpdates: start, updateState: () => state, checkNow: check, installNow: install };
}

let updates = null;
function instance() {
  if (!updates) {
    const { app } = require('electron');
    updates = createUpdates({ app, loadUpdater: () => require('electron-updater').autoUpdater });
  }
  return updates;
}

module.exports = {
  createUpdates,
  startUpdates: (...args) => instance().startUpdates(...args),
  updateState: () => instance().updateState(),
  checkNow: () => instance().checkNow(),
  installNow: () => instance().installNow(),
};
