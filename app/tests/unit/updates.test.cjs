// The app's update state machine (main/updates.cjs) with a stand-in for electron-updater.
const test = require('node:test');
const assert = require('node:assert');
const { EventEmitter } = require('node:events');
const { createUpdates } = require('../../main/updates.cjs');

function fakeUpdater() {
  const updater = new EventEmitter();
  updater.checks = 0;
  updater.installs = 0;
  updater.checkForUpdates = async () => { updater.checks += 1; };
  updater.quitAndInstall = () => { updater.installs += 1; };
  return updater;
}

function start(env = {}) {
  const updater = fakeUpdater();
  const sent = [];
  const app = { isPackaged: true, getVersion: () => '0.1.1' };
  const updates = createUpdates({ app, loadUpdater: () => updater, env, now: () => new Date('2026-11-07T10:00:00Z') });
  updates.startUpdates((state) => sent.push(state), 'stable');
  return { updates, updater, sent };
}

test('a downloaded update installs on quit and from the window', async () => {
  const { updates, updater } = start();
  assert.strictEqual(updater.autoInstallOnAppQuit, true);
  updater.emit('update-available', { version: '0.1.2' });
  updater.emit('download-progress', { percent: 42.7 });
  assert.deepStrictEqual(updates.updateState(),
    { state: 'downloading', version: '0.1.2', percent: 42, checkedAt: '2026-11-07T10:00:00.000Z' });
  updater.emit('update-downloaded', { version: '0.1.2' });
  updates.installNow();
  assert.strictEqual(updater.installs, 1);
});

test('SKILLARIUM_NO_INSTALL downloads but never installs, on quit or from the window', async () => {
  const { updates, updater, sent } = start({ SKILLARIUM_NO_INSTALL: '1' });
  assert.strictEqual(updater.autoInstallOnAppQuit, false);
  updater.emit('update-available', { version: '0.1.2' });
  updater.emit('update-downloaded', { version: '0.1.2' });
  assert.strictEqual(updates.updateState().state, 'ready');
  assert.strictEqual(updates.updateState().install, false);
  assert.ok(sent.every((state) => state.install === false));
  updates.installNow();
  assert.strictEqual(updater.installs, 0);
});

test('no check runs while a version downloads or waits for a restart', async () => {
  const { updates, updater } = start();
  assert.strictEqual(updater.checks, 1);
  updater.emit('update-not-available');
  await updates.checkNow();
  assert.strictEqual(updater.checks, 2);
  for (const event of [['update-available', { version: '0.1.2' }], ['update-downloaded', { version: '0.1.2' }]]) {
    updater.emit(...event);
    await updates.checkNow();
    assert.strictEqual(updater.checks, 2, event[0]);
  }
  assert.strictEqual(updates.updateState().state, 'ready');
});

test('updates can be switched off, and a development run has none', async () => {
  const off = createUpdates({ app: { isPackaged: true }, loadUpdater: () => assert.fail('loaded'), env: { SKILLARIUM_NO_UPDATES: '1' } });
  off.startUpdates(() => {}, 'stable');
  assert.strictEqual(off.updateState().state, 'disabled');
  const development = createUpdates({ app: { isPackaged: false }, loadUpdater: () => assert.fail('loaded'), env: {} });
  development.startUpdates(() => {}, 'stable');
  assert.strictEqual(development.updateState().state, 'development');
  assert.deepStrictEqual(await development.checkNow(), { state: 'development' });
});
