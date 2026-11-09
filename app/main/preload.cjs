// The renderer gets only these calls; it never touches files or processes itself.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('skills', {
  library: () => ipcRenderer.invoke('library'),
  chooseLibrary: () => ipcRenderer.invoke('choose-library'),
  resetLibrary: () => ipcRenderer.invoke('reset-library'),
  status: () => ipcRenderer.invoke('status'),
  register: () => ipcRenderer.invoke('register'),
  unregister: (project) => ipcRenderer.invoke('unregister', project),
  // library: a library name for its own copy; omitted for the copy linked globally.
  bind: (project, skill, library) => ipcRenderer.invoke('bind', project, skill, library),
  unbind: (project, entry, choice) => ipcRenderer.invoke('unbind', project, entry, choice),
  setGlobal: (skill, enabled) => ipcRenderer.invoke('global', skill, enabled),
  // library: the folder of the library the skill's registry belongs to.
  setPolicy: (skill, auto, library) => ipcRenderer.invoke('policy', skill, auto, library),
  prefer: (skill, library) => ipcRenderer.invoke('prefer', skill, library),
  order: (names) => ipcRenderer.invoke('order', names),
  disconnect: (name, force) => ipcRenderer.invoke('disconnect', name, force),
  conflicts: (library) => ipcRenderer.invoke('conflicts', library),
  resolve: (library, request) => ipcRenderer.invoke('resolve', library, request),
  defaultFolder: () => ipcRenderer.invoke('default-folder'),
  chooseFolder: () => ipcRenderer.invoke('choose-folder'),
  connect: (request) => ipcRenderer.invoke('connect', request),
  about: () => ipcRenderer.invoke('about'),
  updateState: () => ipcRenderer.invoke('update-state'),
  checkUpdate: () => ipcRenderer.invoke('check-update'),
  installUpdate: () => ipcRenderer.invoke('install-update'),
  onUpdate: (listener) => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on('update', handler);
    return () => ipcRenderer.removeListener('update', handler);
  },
});
