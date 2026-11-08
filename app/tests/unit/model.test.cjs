// The matrix and the library list the renderer builds from `projects status` (renderer/src/model.js).
const test = require('node:test');
const assert = require('node:assert');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const load = () => import(pathToFileURL(path.join(__dirname, '..', '..', 'renderer', 'src', 'model.js')).href);

const global = (enabled = true) => ({ enabled, manual: false, pending: null, links: {} });

// Two libraries: "shared" in both (team wins), "own" only in personal.
function status() {
  return {
    library: 'C:\\team',
    libraries: [
      { name: 'team', path: 'C:\\team', repo: 'org/team', available: true, configured: true, skills: 1, format: null,
        sync: { time: '2026-11-08T10:00:00Z', ok: true, error: null } },
      { name: 'personal', path: 'C:\\personal', repo: 'me/skills', available: true, configured: true, skills: 2, format: null,
        sync: { time: '2026-11-08T10:00:00Z', ok: false, error: 'fetch failed' } },
    ],
    skills: [
      { name: 'shared', library: 'personal', entry: 'me/skills:shared', active: false, shadowedBy: 'team',
        copies: ['team', 'personal'], preferred: false, projects: [], update: null,
        global: { ...global(), links: { 'C:\\home\\.agents\\skills\\shared': 'conflict' } } },
      { name: 'own', library: 'personal', entry: 'own', active: true, shadowedBy: null, copies: ['personal'],
        preferred: false, projects: [], update: null, global: global() },
      { name: 'shared', library: 'team', entry: 'shared', active: true, shadowedBy: null, copies: ['team', 'personal'],
        preferred: false, projects: [], update: null, global: global() },
    ],
    projects: [{
      path: 'C:\\work\\alpha', available: true, error: null,
      skills: [
        { name: 'me/skills:shared', skill: 'shared', library: 'personal', state: 'applied', links: {} },
        { name: 'gone/repo:lost', skill: 'lost', library: null, state: 'library-missing', links: {} },
        { name: 'offline', skill: 'offline', library: 'personal', state: 'library-unavailable', links: {} },
      ],
    }],
  };
}

test('one row per copy: the linked copy first, the shadowed one marked and labelled with its library', async () => {
  const { buildMatrix } = await load();
  const matrix = buildMatrix(status());
  assert.deepStrictEqual(matrix.rows.map((row) => row.key),
    ['|gone/repo:lost', 'personal|offline', 'personal|own', 'team|shared', 'personal|shared']);
  assert.strictEqual(matrix.multiple, true);
  const [lost, offline, own, linked, shadowed] = matrix.rows;
  assert.strictEqual(linked.label, 'shared');
  assert.strictEqual(shadowed.label, 'shared (personal)');
  assert.strictEqual(shadowed.shadowedBy, 'team');
  assert.strictEqual(shadowed.active, false);
  assert.strictEqual(own.label, 'own');
  assert.deepStrictEqual([lost.inLibrary, lost.label, lost.missing], [false, 'gone/repo:lost', 'библиотека не подключена']);
  assert.deepStrictEqual([offline.inLibrary, offline.label, offline.missing], [false, 'offline (personal)', 'библиотека недоступна']);
});

test('a project cell belongs to the copy its entry names', async () => {
  const { buildMatrix, cellOf } = await load();
  const matrix = buildMatrix(status());
  const project = matrix.projects[0];
  const byKey = Object.fromEntries(matrix.rows.map((row) => [row.key, row]));
  assert.strictEqual(cellOf(project, byKey['team|shared']), null);
  assert.strictEqual(cellOf(project, byKey['personal|shared']).name, 'me/skills:shared');
  assert.strictEqual(cellOf(project, byKey['|gone/repo:lost']).state, 'library-missing');
  assert.strictEqual(cellOf(project, byKey['personal|offline']).state, 'library-unavailable');
});

test('older engines without libraries keep one row per skill', async () => {
  const { buildMatrix, cellOf } = await load();
  const matrix = buildMatrix({
    skills: [{ name: 'alpha', global: global(), update: null }],
    projects: [{ path: '/p', available: true, error: null, skills: [{ name: 'alpha', state: 'applied', links: {} },
      { name: 'beta', state: 'missing-skill', links: {} }] }],
  });
  assert.deepStrictEqual(matrix.rows.map((row) => [row.name, row.active, row.inLibrary, row.label]),
    [['alpha', true, true, 'alpha'], ['beta', true, false, 'beta']]);
  assert.strictEqual(matrix.multiple, false);
  assert.strictEqual(cellOf(matrix.projects[0], matrix.rows[0]).state, 'applied');
});

test('the library filter keeps the rows of one library', async () => {
  const { buildMatrix, filterRows } = await load();
  const { rows } = buildMatrix(status());
  assert.deepStrictEqual(filterRows(rows, 'team').map((row) => row.key), ['team|shared']);
  assert.strictEqual(filterRows(rows, '').length, rows.length);
});

test('library states are explained; global links of a shadowed copy are no problem', async () => {
  const { problems } = await load();
  const texts = problems(status()).map((item) => item.text);
  assert.ok(texts.some((text) => text.includes('«gone/repo:lost»') && text.includes('библиотека не подключена')));
  assert.ok(texts.some((text) => text.includes('«offline»') && text.includes('«personal» недоступна')));
  assert.ok(!texts.some((text) => text.includes('Глобальная установка')));
});

test('sync captions and the order after a move', async () => {
  const { syncCaption, moveLibrary } = await load();
  const [team, personal] = status().libraries;
  assert.strictEqual(syncCaption(team).tone, 'ok');
  assert.match(syncCaption(team).text, /^синхронизирована /);
  assert.deepStrictEqual([syncCaption(personal).tone, syncCaption(personal).error], ['error', 'fetch failed']);
  assert.strictEqual(syncCaption({ ...team, sync: null }).text, 'ещё не синхронизировалась');
  assert.strictEqual(syncCaption({ ...team, available: false }).text, 'папка библиотеки недоступна');
  const libraries = [...status().libraries, { name: 'local', configured: false }];
  assert.deepStrictEqual(moveLibrary(libraries, 'personal', -1), ['personal', 'team']);
  assert.deepStrictEqual(moveLibrary(libraries, 'team', 1), ['personal', 'team']);
  assert.strictEqual(moveLibrary(libraries, 'team', -1), null);
  assert.strictEqual(moveLibrary(libraries, 'local', -1), null);
});
