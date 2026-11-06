// The renderer gets only these calls; it never touches files or processes itself.
const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('skills', {
  library: () => ipcRenderer.invoke('library'),
  chooseLibrary: () => ipcRenderer.invoke('choose-library'),
  status: () => ipcRenderer.invoke('status'),
  register: () => ipcRenderer.invoke('register'),
  unregister: (project) => ipcRenderer.invoke('unregister', project),
  bind: (project, skill) => ipcRenderer.invoke('bind', project, skill),
  unbind: (project, skill, choice) => ipcRenderer.invoke('unbind', project, skill, choice),
  setGlobal: (skill, enabled) => ipcRenderer.invoke('global', skill, enabled),
  setPolicy: (skill, auto) => ipcRenderer.invoke('policy', skill, auto),
  conflicts: () => ipcRenderer.invoke('conflicts'),
  resolve: (request) => ipcRenderer.invoke('resolve', request),
  defaultFolder: () => ipcRenderer.invoke('default-folder'),
  chooseFolder: () => ipcRenderer.invoke('choose-folder'),
  connect: (request) => ipcRenderer.invoke('connect', request),
  updateState: () => ipcRenderer.invoke('update-state'),
  checkUpdate: () => ipcRenderer.invoke('check-update'),
  installUpdate: () => ipcRenderer.invoke('install-update'),
  onUpdate: (listener) => {
    const handler = (_event, state) => listener(state);
    ipcRenderer.on('update', handler);
    return () => ipcRenderer.removeListener('update', handler);
  },
});
