// Runs the engine's projects command against a library; the shell scripts own all file and Git changes.
const { execFile } = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const script = process.platform === 'win32' ? 'projects.ps1' : 'projects.sh';
// The engine ships with the app; in a development checkout it is the repository root.
const engine = path.resolve(__dirname, '..', '..');

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

// Resolves with the exit code, the JSON line printed for the UI and the remaining messages.
function runProjects(library, args) {
  const [file, argv] = command(library, args);
  return new Promise((resolve) => {
    execFile(file, argv, { cwd: library, windowsHide: true, encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 },
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

module.exports = { engine, isLibrary, runProjects };
