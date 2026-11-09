// Electron shell of the local app; every change goes through projects.ps1/projects.sh.
const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { repositoryRoot, useEngine, enginePath, runProjects, connect } = require('./backend.cjs');
const { configFile, readConfig, configuredLibraries, isLibrary, locate } = require('./libraries.cjs');
const { deployBundledEngine, channel, versionOf } = require('./engine.cjs');
const { startUpdates, updateState, checkNow, installNow } = require('./updates.cjs');

const SKILL_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;
// A project entry: "name", or "owner/repo:name" for the copy of one library.
const ENTRY = /^(?:[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+:)?[a-z0-9]+(-[a-z0-9]+)*$/;
const LIBRARY_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$/;
const OBJECT_ID = /^[0-9a-f]{40}([0-9a-f]{24})?$/;
let window = null;

function settingsFile() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function readSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); } catch { return {}; }
}

function saveSettings(change) {
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  const next = { ...readSettings(), ...change };
  for (const key of Object.keys(next)) if (next[key] === undefined) delete next[key];
  fs.writeFileSync(settingsFile(), JSON.stringify(next, null, 2));
}

// A folder chosen for this app overrides the connected libraries, like --library:
// the environment, the app's own choice, a development checkout that is itself a library.
function override() {
  if (process.env.SKILLARIUM_LIBRARY) return { path: process.env.SKILLARIUM_LIBRARY, source: 'environment' };
  if (process.env.AGENT_SKILLS_LIBRARY) return { path: process.env.AGENT_SKILLS_LIBRARY, source: 'environment' };
  if (readSettings().library) return { path: readSettings().library, source: 'settings' };
  if (!app.isPackaged && isLibrary(repositoryRoot)) return { path: repositoryRoot, source: 'development' };
  return { path: '', source: '' };
}

// The engine starts from the override or the first reachable connected library; its status
// covers every connected library, so no single library is required beyond that.
function libraries() {
  const chosen = override();
  const found = locate({ override: chosen.path, libraries: configuredLibraries(readConfig(configFile())) });
  return { ...found, source: chosen.source, valid: Boolean(found.base) };
}

function text(value) {
  if (typeof value !== 'string' || !value) throw new Error('Invalid argument');
  return value;
}

function skill(value) {
  if (typeof value !== 'string' || !SKILL_NAME.test(value)) throw new Error('Invalid skill name');
  return value;
}

function entry(value) {
  if (typeof value !== 'string' || !ENTRY.test(value)) throw new Error('Invalid skill entry');
  return value;
}

// A connected library's name as status reports it: a folder name may contain spaces.
function libraryName(value) {
  if (typeof value !== 'string' || !value.trim() || value.startsWith('-') || /[\r\n\\/:]/.test(value)) {
    throw new Error('Invalid library name');
  }
  return value;
}

// One library's folder, as status reports it, for commands that work on one library.
function libraryPath(value) {
  if (typeof value !== 'string' || !path.isAbsolute(value)) throw new Error('Invalid library folder');
  return value;
}

const runIn = (library, ...args) => {
  const { base, source } = libraries();
  if (!base) return Promise.resolve({ code: 1, json: null, messages: ['Библиотека скиллов не подключена.'] });
  // Connected libraries need no argument: an explicit one would bring back a library that a
  // disconnect running at the same time removes. Only an override has to be passed on.
  return runProjects(library || (source ? base : null), args, base);
};
const run = (...args) => runIn(null, ...args);

ipcMain.handle('library', () => libraries());
ipcMain.handle('choose-library', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Папка библиотеки скиллов', properties: ['openDirectory'] });
  if (picked.canceled || !picked.filePaths.length) return null;
  const folder = picked.filePaths[0];
  if (!isLibrary(folder)) return { path: folder, valid: false };
  saveSettings({ library: folder });
  return { path: folder, valid: true };
});
// Back to the connected libraries of the Skillarium configuration.
ipcMain.handle('reset-library', () => { saveSettings({ library: undefined }); return libraries(); });
ipcMain.handle('default-folder', () => path.join(os.homedir(), 'Skillarium', 'library'));
ipcMain.handle('choose-folder', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Куда положить библиотеку', properties: ['openDirectory', 'createDirectory'] });
  return picked.canceled || !picked.filePaths.length ? null : picked.filePaths[0];
});
// The same steps as `skillarium client`, without downloading the app again: the first
// library, or one more added to the end of the connected ones.
ipcMain.handle('connect', async (_event, request) => {
  const url = String(request?.url || '').trim();
  const token = String(request?.token || '').trim();
  const folder = String(request?.folder || '').trim();
  const name = String(request?.name || '').trim();
  const problems = [];
  if (!/^https?:\/\/[^\s@/]+\/[^\s@]+\/[^\s@]+$/.test(url)) problems.push('Адрес библиотеки — ссылка вида https://сервер/владелец/репозиторий.git, без пробелов.');
  if (!token || /\s/.test(token)) problems.push('Токен — одна строка без пробелов.');
  if (folder && !path.isAbsolute(folder)) problems.push(`Папка должна быть полным путём, например ${path.join(os.homedir(), 'Skillarium', 'library')}.`);
  if (name && !LIBRARY_NAME.test(name)) problems.push('Имя библиотеки — латинские буквы, цифры, точка, дефис или подчёркивание, без пробелов.');
  if (problems.length) return { code: 2, messages: problems };
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'skillarium-connect-'));
  try {
    const tokenFile = path.join(temporary, 'token');
    fs.writeFileSync(tokenFile, token, { mode: 0o600 });
    const result = await connect({ url, folder, name, tokenFile });
    // A chosen folder that is not a library would keep hiding the new connection.
    if (result.code === 0 && readSettings().library && !isLibrary(readSettings().library)) saveSettings({ library: undefined });
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
// With a library the project gets that library's copy ("owner/repo:name"), else the global one.
ipcMain.handle('bind', (_event, project, name, library) => (library
  ? run('bind', text(project), skill(name), libraryName(library)) : run('bind', text(project), skill(name))));
// name: the entry as the project file has it.
ipcMain.handle('unbind', (_event, project, name, choice) => {
  if (choice !== undefined && choice !== 'global' && choice !== 'library') throw new Error('Invalid choice');
  return choice ? run('unbind', text(project), entry(name), choice) : run('unbind', text(project), entry(name));
});
ipcMain.handle('global', (_event, name, enabled) => run('global', skill(name), enabled ? 'on' : 'off'));
// The update policy, conflicts and their resolution belong to one library (its folder).
ipcMain.handle('policy', (_event, name, auto, library) => runIn(libraryPath(library), 'policy', skill(name), auto ? 'auto' : 'confirm'));
ipcMain.handle('conflicts', (_event, library) => runIn(libraryPath(library), 'conflicts'));
ipcMain.handle('prefer', (_event, name, library) => run('prefer', skill(name), library === 'auto' ? 'auto' : libraryName(library)));
ipcMain.handle('order', (_event, names) => {
  if (!Array.isArray(names) || !names.length || names.length > 20) throw new Error('Invalid library order');
  return run('order', ...names.map(libraryName));
});
// The engine refuses (exit 1, the reason on stderr) while the clone has unpublished changes.
ipcMain.handle('disconnect', (_event, name, force) => (force
  ? run('disconnect', libraryName(name), '--force') : run('disconnect', libraryName(name))));
ipcMain.handle('resolve', async (_event, library, request) => {
  libraryPath(library);
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
    return await runIn(library, 'resolve', file);
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
