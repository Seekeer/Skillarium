// The packaged app lays its engine out as versions/<version> + current (main/engine.cjs).
const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { deployBundledEngine, engineHome, compareVersions } = require('../../main/engine.cjs');

function bundle(root, version) {
  const folder = path.join(root, `bundle-${version}`);
  fs.mkdirSync(path.join(folder, 'scripts'), { recursive: true });
  fs.writeFileSync(path.join(folder, 'VERSION'), `${version}\n`);
  fs.writeFileSync(path.join(folder, 'scripts', 'client.ps1'), `# engine ${version}\n`);
  return folder;
}

test('the bundled engine becomes current and follows app updates', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillarium app engine '));
  try {
    const env = { SKILLARIUM_HOME: path.join(root, 'home') };
    const current = deployBundledEngine(bundle(root, '0.1.0'), { env });
    assert.strictEqual(current, path.join(env.SKILLARIUM_HOME, 'current'));
    assert.strictEqual(fs.readFileSync(path.join(current, 'VERSION'), 'utf8').trim(), '0.1.0');
    assert.ok(fs.lstatSync(current).isSymbolicLink());
    // A second start of the same version changes nothing.
    const linkBefore = fs.readlinkSync(current);
    deployBundledEngine(bundle(root, '0.1.0'), { env });
    assert.strictEqual(fs.readlinkSync(current), linkBefore);
    deployBundledEngine(bundle(root, '0.2.0'), { env });
    assert.match(fs.readFileSync(path.join(current, 'scripts', 'client.ps1'), 'utf8'), /engine 0\.2\.0/);
    assert.deepStrictEqual(fs.readdirSync(path.join(env.SKILLARIUM_HOME, 'versions')).sort(), ['0.1.0', '0.2.0']);
    assert.deepStrictEqual(fs.readFileSync(path.join(env.SKILLARIUM_HOME, 'history'), 'utf8').trim().split('\n'),
      ['0.1.0', '0.2.0']);
    // An older app (or a newer engine from a beta self-update) never downgrades current.
    deployBundledEngine(bundle(root, '0.1.0'), { env });
    assert.strictEqual(fs.readFileSync(path.join(current, 'VERSION'), 'utf8').trim(), '0.2.0');
    for (const version of ['0.3.0', '0.4.0', '0.5.0']) deployBundledEngine(bundle(root, version), { env });
    assert.deepStrictEqual(fs.readdirSync(path.join(env.SKILLARIUM_HOME, 'versions')).sort(), ['0.3.0', '0.4.0', '0.5.0']);
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('a bundle without a valid VERSION is not deployed', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'skillarium app engine '));
  try {
    fs.mkdirSync(path.join(root, 'empty'));
    assert.strictEqual(deployBundledEngine(path.join(root, 'empty'), { env: { SKILLARIUM_HOME: path.join(root, 'home') } }), null);
    assert.ok(!fs.existsSync(path.join(root, 'home')));
  } finally {
    fs.rmSync(root, { recursive: true, force: true });
  }
});

test('versions follow semantic precedence', () => {
  const order = ['0.9.0', '1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-beta.2', '1.0.0-beta.11', '1.0.0', '1.0.1', '1.10.0'];
  assert.deepStrictEqual([...order].reverse().sort(compareVersions), order);
});

test('engine home follows the native scripts', () => {
  assert.strictEqual(engineHome({ LOCALAPPDATA: 'C:\\L' }, 'win32'), path.join('C:\\L', 'Skillarium'));
  assert.strictEqual(engineHome({ XDG_DATA_HOME: '/d' }, 'linux'), path.join('/d', 'skillarium'));
  assert.strictEqual(engineHome({ SKILLARIUM_HOME: '/x', LOCALAPPDATA: 'C:\\L' }, 'win32'), '/x');
});
