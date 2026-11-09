// Which library the app starts the engine from, and the engine command lines (main/libraries.cjs, main/backend.cjs).
const test = require('node:test');
const assert = require('node:assert');
const { configuredLibraries, locate } = require('../../main/libraries.cjs');
const { command, connectCommand } = require('../../main/backend.cjs');

test('the connected libraries come from "libraries", or from the legacy "library"', () => {
  assert.deepStrictEqual(configuredLibraries({ libraries: [{ name: 'team', path: '/t', url: 'https://g/o/t.git' }, { path: '' }, { name: 'me', path: '/m' }] }),
    [{ name: 'team', path: '/t', url: 'https://g/o/t.git' }, { name: 'me', path: '/m', url: '' }]);
  assert.deepStrictEqual(configuredLibraries({ library: '/old' }), [{ name: '', path: '/old', url: '' }]);
  // The list wins over the legacy key that newer engines keep for older apps.
  assert.deepStrictEqual(configuredLibraries({ library: '/t', libraries: [{ name: 'team', path: '/t' }] }).map((item) => item.path), ['/t']);
  assert.deepStrictEqual(configuredLibraries({}), []);
});

test('the engine starts from the override or the first reachable connected library', () => {
  const libraries = [{ name: 'a', path: '/a' }, { name: 'b', path: '/b' }];
  const reachable = (set) => (folder) => set.includes(folder);
  assert.strictEqual(locate({ libraries, check: reachable(['/a', '/b']) }).base, '/a');
  // An unreachable first library does not stop the others.
  assert.strictEqual(locate({ libraries, check: reachable(['/b']) }).base, '/b');
  assert.strictEqual(locate({ libraries, check: reachable([]) }).base, null);
  assert.strictEqual(locate({ override: '/x', libraries, check: reachable(['/x']) }).base, '/x');
  assert.strictEqual(locate({ override: '/x', libraries, check: reachable(['/a']) }).base, null);
});

test('command lines pass the library and the connection options only when given', () => {
  const [, plain] = command(null, ['status'], 'win32');
  assert.ok(!plain.includes('-Library'));
  assert.deepStrictEqual(plain.slice(-1), ['status']);
  const [, named] = command('team', ['policy', 'x', 'auto'], 'linux');
  assert.deepStrictEqual(named.slice(-5), ['--library', 'team', 'policy', 'x', 'auto']);
  const [, first] = connectCommand({ url: 'https://g/o/r.git', folder: 'C:\\lib', tokenFile: 't' }, 'win32');
  assert.ok(first.includes('-Path') && !first.includes('-Name'));
  const [, another] = connectCommand({ url: 'https://g/o/r.git', folder: '', name: 'team', tokenFile: 't' }, 'linux');
  assert.ok(!another.includes('--path'));
  assert.deepStrictEqual(another.slice(another.indexOf('--name'), another.indexOf('--name') + 2), ['--name', 'team']);
});

test('an override may name a connected library, as with the native clients', () => {
  const libraries = [{ name: 'a', path: '/a' }, { name: 'team', path: '/t' }];
  const located = locate({ override: 'team', libraries, check: (folder) => folder === '/t' });
  assert.strictEqual(located.base, '/t');
  assert.strictEqual(locate({ override: 'gone', libraries, check: () => false }).base, null);
});
