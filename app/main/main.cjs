// Electron shell of the local app; every change goes through projects.ps1/projects.sh.
const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { repositoryRoot, useEngine, enginePath, isLibrary, runProjects, connect } = require('./backend.cjs');
const { deployBundledEngine, channel, versionOf } = require('./engine.cjs');
const { startUpdates, updateState, checkNow, installNow } = require('./updates.cjs');

const SKILL_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const OBJECT_ID = /^[0-9a-f]{40}([0-9a-f]{24})?$/;
let window = null;

function settingsFile() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function readSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); } catch { return {}; }
}

// The Skillarium configuration shared with the native clients and the server.
function engineConfig() {
  const root = process.platform === 'win32'
    ? path.join(process.env.APPDATA || path.join(os.homedir(), 'AppData', 'Roaming'), 'Skillarium')
    : path.join(process.env.XDG_CONFIG_HOME || path.join(os.homedir(), '.config'), 'skillarium');
  try { return JSON.parse(fs.readFileSync(path.join(root, 'config.json'), 'utf8')); } catch { return {}; }
}

// Environment, the app's own choice (like --library), then the order of the native
// scripts: an engine folder that is itself a library (legacy layout), the shared configuration.
function library() {
  const legacy = !app.isPackaged && isLibrary(repositoryRoot) ? repositoryRoot : '';
  return process.env.SKILLARIUM_LIBRARY || process.env.AGENT_SKILLS_LIBRARY || readSettings().library
    || legacy || engineConfig().library || '';
}

function saveSettings(change) {
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  fs.writeFileSync(settingsFile(), JSON.stringify({ ...readSettings(), ...change }, null, 2));
}

function text(value) {
  if (typeof value !== 'string' || !value) throw new Error('Invalid argument');
  return value;
}

function skill(value) {
  if (typeof value !== 'string' || !SKILL_NAME.test(value)) throw new Error('Invalid skill name');
  return value;
}

const run = (...args) => (isLibrary(library())
  ? runProjects(library(), args)
  : Promise.resolve({ code: 1, json: null, messages: ['Библиотека скиллов не выбрана.'] }));

ipcMain.handle('library', () => ({ path: library(), valid: isLibrary(library()) }));
ipcMain.handle('choose-library', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Папка библиотеки скиллов', properties: ['openDirectory'] });
  if (picked.canceled || !picked.filePaths.length) return null;
  const folder = picked.filePaths[0];
  if (!isLibrary(folder)) return { path: folder, valid: false };
  saveSettings({ library: folder });
  return { path: folder, valid: true };
});
ipcMain.handle('default-folder', () => path.join(os.homedir(), 'Skillarium', 'library'));
ipcMain.handle('choose-folder', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Куда положить библиотеку', properties: ['openDirectory', 'createDirectory'] });
  return picked.canceled || !picked.filePaths.length ? null : picked.filePaths[0];
});
// First run: the same steps as `skillarium client`, without downloading the app again.
ipcMain.handle('connect', async (_event, request) => {
  const url = String(request?.url || '').trim();
  const token = String(request?.token || '').trim();
  const folder = String(request?.folder || '').trim();
  const problems = [];
  if (!/^https?:\/\/[^\s@/]+\/[^\s@]+\/[^\s@]+$/.test(url)) problems.push('Адрес библиотеки — ссылка вида https://сервер/владелец/репозиторий.git, без пробелов.');
  if (!token || /\s/.test(token)) problems.push('Токен — одна строка без пробелов.');
  if (!path.isAbsolute(folder)) problems.push(`Папка должна быть полным путём, например ${path.join(os.homedir(), 'Skillarium', 'library')}.`);
  if (problems.length) return { code: 2, messages: problems };
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'skillarium-connect-'));
  try {
    const tokenFile = path.join(temporary, 'token');
    fs.writeFileSync(tokenFile, token, { mode: 0o600 });
    const result = await connect(url, folder, tokenFile);
    if (result.code === 0) saveSettings({ library: folder });
    return { code: result.code, messages: result.messages };
  } finally {
    fs.rmSync(temporary, { recursive: true, force: true });
  }
});
// Versions for the settings window; a development checkout runs the repository's engine.
ipcMain.handle('about', () => ({
  app: app.getVersion(),
  packaged: app.isPackaged,
  engine: { path: enginePath(), version: versionOf(enginePath()) },
  channel: channel(),
}));
ipcMain.handle('update-state', () => updateState());
ipcMain.handle('check-update', () => checkNow());
ipcMain.handle('install-update', () => installNow());
ipcMain.handle('status', () => run('status'));
ipcMain.handle('register', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Папка проекта', properties: ['openDirectory'] });
  if (picked.canceled || !picked.filePaths.length) return null;
  return run('register', picked.filePaths[0]);
});
ipcMain.handle('unregister', (_event, project) => run('unregister', text(project)));
ipcMain.handle('bind', (_event, project, name) => run('bind', text(project), skill(name)));
ipcMain.handle('unbind', (_event, project, name, choice) => {
  if (choice !== undefined && choice !== 'global' && choice !== 'library') throw new Error('Invalid choice');
  return choice ? run('unbind', text(project), skill(name), choice) : run('unbind', text(project), skill(name));
});
ipcMain.handle('global', (_event, name, enabled) => run('global', skill(name), enabled ? 'on' : 'off'));
ipcMain.handle('policy', (_event, name, auto) => run('policy', skill(name), auto ? 'auto' : 'confirm'));
ipcMain.handle('conflicts', () => run('conflicts'));
ipcMain.handle('resolve', async (_event, request) => {
  const id = (value) => value === null || (typeof value === 'string' && OBJECT_ID.test(value));
  const valid = request && typeof request.upstream === 'string' && OBJECT_ID.test(request.upstream)
    && Array.isArray(request.files) && request.files.length > 0
    && request.files.every((file) => typeof file.path === 'string' && file.path.startsWith('skills/')
      && ['mine', 'library', 'edited'].includes(file.take) && id(file.mine) && id(file.library)
      && (file.take !== 'edited' || typeof file.text === 'string'));
  if (!valid) throw new Error('Invalid resolution');
  // The choice can be large and multi-line, so it travels as a file rather than an argument.
  const folder = fs.mkdtempSync(path.join(os.tmpdir(), 'agent-skills-'));
  try {
    const file = path.join(folder, 'resolution.json');
    const files = request.files.map(({ path: name, take, mine, library, text: body }) => ({ path: name, take, mine, library, text: body }));
    fs.writeFileSync(file, JSON.stringify({ upstream: request.upstream, files }), 'utf8');
    return await run('resolve', file);
  } finally {
    fs.rmSync(folder, { recursive: true, force: true });
  }
});

function createWindow() {
  window = new BrowserWindow({
    width: 1180,
    height: 760,
    minWidth: 720,
    minHeight: 480,
    title: 'Skillarium — скиллы в проектах',
    backgroundColor: '#f4f5fb',
    icon: app.isPackaged ? path.join(process.resourcesPath, 'icon.png')
      : path.join(__dirname, '..', '..', 'assets', 'brand', 'png', 'skillarium-icon-256.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  window.removeMenu();
  window.loadFile(path.join(__dirname, '..', 'dist', 'index.html'));
}

app.whenReady().then(() => {
  if (app.isPackaged) {
    // The app's own engine becomes current; a failed copy still runs it from resources.
    const bundled = path.join(process.resourcesPath, 'engine');
    let deployed = null;
    try { deployed = deployBundledEngine(bundled); } catch (error) { console.error(error); }
    useEngine(deployed || bundled);
  }
  createWindow();
  startUpdates((state) => window?.webContents.send('update', state), channel());
});
app.on('window-all-closed', () => app.quit());
