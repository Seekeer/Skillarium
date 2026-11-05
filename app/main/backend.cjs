// Runs the engine's projects command against a library; the shell scripts own all file and Git changes.
const { execFile } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const script = process.platform === 'win32' ? 'projects.ps1' : 'projects.sh';
// A development checkout runs the repository itself; the packaged app switches to
// the engine it deployed (main/engine.cjs) before the first call.
const repositoryRoot = path.resolve(__dirname, '..', '..');
let engine = repositoryRoot;

function useEngine(folder) {
  engine = folder;
}

function isLibrary(folder) {
  return Boolean(folder) && fs.existsSync(path.join(folder, 'sources.json'))
    && fs.existsSync(path.join(folder, 'agent-skills.json'));
}

function command(library, args) {
  if (process.platform === 'win32') {
    return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.join(engine, script), '-Library', library, ...args]];
  }
  return ['bash', [path.join(engine, script), '--library', library, ...args]];
}

// Connects this machine to a library with the engine's client setup; the token travels in a
// private file that is removed right after. The app is already installed: no download.
function connectCommand(url, folder, tokenFile) {
  if (process.platform === 'win32') {
    return ['powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
      '-File', path.join(engine, 'scripts', 'setup-client.ps1'), '-Library', url, '-Path', folder,
      '-TokenFile', tokenFile, '-FromApp']];
  }
  return ['bash', [path.join(engine, 'scripts', 'setup-client.sh'), '--library', url, '--path', folder,
    '--token-file', tokenFile, '--from-app']];
}

// Resolves with the exit code, the JSON line printed for the UI and the remaining messages.
function runProjects(library, args) {
  return execute(command(library, args), library);
}

function connect(url, folder, tokenFile) {
  return execute(connectCommand(url, folder, tokenFile), path.dirname(tokenFile));
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

module.exports = { repositoryRoot, useEngine, isLibrary, runProjects, connect };
