// Electron shell of the local app; every change goes through projects.ps1/projects.sh.
const { app, BrowserWindow, dialog, ipcMain } = require('electron');
const path = require('node:path');
const fs = require('node:fs');
const os = require('node:os');
const { isLibrary, runProjects } = require('./backend.cjs');

const SKILL_NAME = /^[a-z0-9]+(-[a-z0-9]+)*$/;
const OBJECT_ID = /^[0-9a-f]{40}([0-9a-f]{24})?$/;
let window = null;

function settingsFile() {
  return path.join(app.getPath('userData'), 'settings.json');
}

function readSettings() {
  try { return JSON.parse(fs.readFileSync(settingsFile(), 'utf8')); } catch { return {}; }
}

// The app lives in the library checkout; a saved choice or AGENT_SKILLS_LIBRARY may point elsewhere.
function library() {
  return process.env.AGENT_SKILLS_LIBRARY || readSettings().library || path.resolve(__dirname, '..', '..');
}

function text(value) {
  if (typeof value !== 'string' || !value) throw new Error('Invalid argument');
  return value;
}

function skill(value) {
  if (typeof value !== 'string' || !SKILL_NAME.test(value)) throw new Error('Invalid skill name');
  return value;
}

const run = (...args) => runProjects(library(), args);

ipcMain.handle('library', () => ({ path: library(), valid: isLibrary(library()) }));
ipcMain.handle('choose-library', async () => {
  const picked = await dialog.showOpenDialog(window, { title: 'Папка библиотеки скиллов', properties: ['openDirectory'] });
  if (picked.canceled || !picked.filePaths.length) return null;
  const folder = picked.filePaths[0];
  if (!isLibrary(folder)) return { path: folder, valid: false };
  fs.mkdirSync(path.dirname(settingsFile()), { recursive: true });
  fs.writeFileSync(settingsFile(), JSON.stringify({ ...readSettings(), library: folder }, null, 2));
  return { path: folder, valid: true };
});
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
    title: 'Скиллы в проектах',
    backgroundColor: '#eef1f4',
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

app.whenReady().then(createWindow);
app.on('window-all-closed', () => app.quit());
