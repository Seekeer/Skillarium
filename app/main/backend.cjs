// Runs the engine's projects command; the shell scripts own all file and Git changes.
const { execFile } = require('node:child_process');
const os = require('node:os');
const path = require('node:path');
const { isLibrary } = require('./libraries.cjs');

// A development checkout runs the repository itself; the packaged app switches to
// the engine it deployed (main/engine.cjs) before the first call.
const repositoryRoot = path.resolve(__dirname, '..', '..');
let engine = repositoryRoot;

function useEngine(folder) {
  engine = folder;
}

function enginePath() {
  return engine;
}

// library: a folder or a connected library's name; without it the engine takes the first
// connected library. Links and bindings always cover every connected library.
function command(library, args, platform = process.platform) {
  if (platform === 'win32') {
    return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.join(engine, 'projects.ps1'), ...(library ? ['-Library', library] : []), ...args]];
  }
  return ['bash', [path.join(engine, 'projects.sh'), ...(library ? ['--library', library] : []), ...args]];
}

// Connects this machine to a library with the engine's client setup; the token travels in a
// private file that is removed right after. The app is already installed: no download.
// Another library goes to the end of the connected ones; without a folder or a name the
// client setup picks them (Skillarium/<name>, the repository name).
function connectCommand({ url, folder, name, tokenFile }, platform = process.platform) {
  if (platform === 'win32') {
    return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.join(engine, 'scripts', 'setup-client.ps1'), '-Library', url,
      ...(folder ? ['-Path', folder] : []), ...(name ? ['-Name', name] : []), '-TokenFile', tokenFile, '-FromApp']];
  }
  return ['bash', [path.join(engine, 'scripts', 'setup-client.sh'), '--library', url,
    ...(folder ? ['--path', folder] : []), ...(name ? ['--name', name] : []), '--token-file', tokenFile, '--from-app']];
}

// Resolves with the exit code, the JSON line printed for the UI and the remaining messages.
// folder: where the command runs (a library folder; the home folder otherwise).
function runProjects(library, args, folder = library) {
  const cwd = folder && path.isAbsolute(folder) && isLibrary(folder) ? folder : os.homedir();
  return execute(command(library, args), cwd);
}

function connect(request) {
  return execute(connectCommand(request), path.dirname(request.tokenFile));
}

function execute([file, argv], cwd) {
  return new Promise((resolve) => {
    execFile(file, argv, { cwd, windowsHide: true, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 },
      (error, stdout, stderr) => {
        let code = 0;
        if (error) code = typeof error.code === 'number' ? error.code : 1;
        const lines = `${stdout}\n${stderr}`.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
        const jsonLine = lines.filter((line) => line.startsWith('{')).pop();
        let json = null;
        try { json = jsonLine ? JSON.parse(jsonLine) : null; } catch { json = null; }
        const messages = lines.filter((line) => !line.startsWith('{'));
        if (error && typeof error.code !== 'number') messages.push(error.message);
        resolve({ code, json, messages });
      });
  });
}

module.exports = { repositoryRoot, useEngine, enginePath, isLibrary, runProjects, connect, command, connectCommand };
