// UI checks of the "Projects" section: the real Electron app drives the real
// projects.ps1/projects.sh against a temporary library, projects and profile.
import { test, expect, _electron as electron } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const appDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.resolve(appDir, '..');
const windows = process.platform === 'win32';
const CLIENT_FILES = ['install.sh', 'install.ps1', 'projects.sh', 'projects.ps1', '.gitattributes',
  'scripts/client.sh', 'scripts/client.ps1', 'scripts/bindings.sh', 'scripts/bindings.ps1',
  'scripts/sync-hidden.vbs', 'scripts/locate.sh', 'scripts/locate.ps1', 'scripts/bridge.sh', 'scripts/bridge.ps1',
  'agent-skills.json'];
const SHARED = 'shared-skill';
const OTHER = 'other-skill';

function run(file, args, env, cwd) {
  return execFileSync(file, args, { env, cwd, encoding: 'utf8', windowsHide: true }).trim();
}

function git(cwd, env, ...args) {
  return run('git', ['-C', cwd, ...args], env);
}

function makeFixture() {
  const base = fs.realpathSync.native(os.tmpdir());
  const root = fs.realpathSync.native(fs.mkdtempSync(path.join(base, 'agent skills app ')));
  const home = path.join(root, 'user home');
  fs.mkdirSync(home);
  const env = {
    ...process.env,
    HOME: home,
    USERPROFILE: home,
    APPDATA: path.join(home, 'AppData', 'Roaming'),
    LOCALAPPDATA: path.join(home, 'AppData', 'Local'),
    XDG_STATE_HOME: path.join(home, '.local', 'state'),
    XDG_CACHE_HOME: path.join(home, '.cache'),
    XDG_CONFIG_HOME: path.join(home, '.config'),
    GIT_CONFIG_GLOBAL: windows ? 'NUL' : '/dev/null',
    GIT_CONFIG_NOSYSTEM: '1',
    SKILLARIUM_NO_BRIDGE: '1',
    // Sync and Install modes change a scheduled task of this name: never the user's own.
    SKILLARIUM_TASK_NAME: `Skillarium-Test-${path.basename(root).replace(/\W/g, '')}`,
    SKILLARIUM_UPDATE_TASK: `Skillarium-Test-${path.basename(root).replace(/\W/g, '')}-Update`,
    GIT_AUTHOR_NAME: 'Test', GIT_AUTHOR_EMAIL: 'test@example.invalid',
    GIT_COMMITTER_NAME: 'Test', GIT_COMMITTER_EMAIL: 'test@example.invalid',
  };
  const remote = path.join(root, 'library.git');
  git(root, env, 'init', '--bare', '--initial-branch=main', remote);
  const seed = path.join(root, 'seed');
  fs.mkdirSync(seed);
  git(seed, env, 'init', '-b', 'main');
  for (const relative of CLIENT_FILES) {
    fs.mkdirSync(path.dirname(path.join(seed, relative)), { recursive: true });
    const source = relative === 'agent-skills.json' ? path.join(repo, 'templates', 'library', relative) : path.join(repo, relative);
    fs.copyFileSync(source, path.join(seed, relative));
  }
  const registry = { version: 2, skills: {} };
  for (const name of [SHARED, OTHER]) {
    fs.mkdirSync(path.join(seed, 'skills', name), { recursive: true });
    fs.writeFileSync(path.join(seed, 'skills', name, 'SKILL.md'), `---\nname: ${name}\ndescription: Test\n---\nBody\n`);
    registry.skills[name] = { source: { kind: 'local' } };
  }
  fs.writeFileSync(path.join(seed, 'sources.json'), `${JSON.stringify(registry, null, 2)}\n`);
  git(seed, env, 'add', '-A');
  git(seed, env, 'commit', '-m', 'Library');
  git(seed, env, 'remote', 'add', 'origin', remote);
  git(seed, env, 'push', '-u', 'origin', 'main');
  const library = path.join(root, 'library with spaces');
  git(root, env, 'clone', remote, library);
  // The first background run installs the default global links.
  if (windows) {
    run('powershell.exe', ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
      path.join(library, 'install.ps1'), '-Mode', 'Link'], env, library);
  } else {
    run('bash', [path.join(library, 'install.sh'), '--link-only'], env, library);
  }
  const project = (name) => {
    const folder = path.join(root, name);
    fs.mkdirSync(folder);
    git(folder, env, 'init', '-b', 'main');
    fs.writeFileSync(path.join(folder, 'README.md'), 'project\n');
    git(folder, env, 'add', '-A');
    git(folder, env, 'commit', '-m', 'Initial project');
    return folder;
  };
  return { root, home, env, seed, library, alpha: project('project alpha'), beta: project('project beta') };
}

// Another library: a bare remote under remotes/<owner>/<name>.git, so its origin names
// "owner/name" like a hosted one, and a clone next to the first library.
function makeLibrary(fixture, owner, name, skills) {
  const { root, env } = fixture;
  const remote = path.join(root, 'remotes', owner, `${name}.git`).replace(/\\/g, '/');
  fs.mkdirSync(path.dirname(remote), { recursive: true });
  git(root, env, 'init', '--bare', '--initial-branch=main', remote);
  const seed = path.join(root, `seed ${name}`);
  fs.mkdirSync(seed);
  git(seed, env, 'init', '-b', 'main');
  fs.copyFileSync(path.join(repo, 'templates', 'library', 'agent-skills.json'), path.join(seed, 'agent-skills.json'));
  const registry = { version: 2, skills: {} };
  for (const skill of skills) {
    fs.mkdirSync(path.join(seed, 'skills', skill), { recursive: true });
    fs.writeFileSync(path.join(seed, 'skills', skill, 'SKILL.md'), `---\nname: ${skill}\ndescription: ${name}\n---\nBody of ${name}\n`);
    registry.skills[skill] = { source: { kind: 'local' } };
  }
  fs.writeFileSync(path.join(seed, 'sources.json'), `${JSON.stringify(registry, null, 2)}\n`);
  git(seed, env, 'add', '-A');
  git(seed, env, 'commit', '-m', 'Library');
  git(seed, env, 'remote', 'add', 'origin', remote);
  git(seed, env, 'push', '-u', 'origin', 'main');
  const clone = path.join(root, name);
  git(root, env, 'clone', remote, clone);
  return { path: clone, url: remote, seed };
}

const configPath = (fixture) => (windows
  ? path.join(fixture.env.APPDATA, 'Skillarium', 'config.json')
  : path.join(fixture.env.XDG_CONFIG_HOME, 'skillarium', 'config.json'));

function writeConfig(fixture, libraries) {
  fs.mkdirSync(path.dirname(configPath(fixture)), { recursive: true });
  fs.writeFileSync(configPath(fixture), `${JSON.stringify({ libraries, library: libraries[0].path }, null, 2)}\n`);
}

const readConfig = (fixture) => JSON.parse(fs.readFileSync(configPath(fixture), 'utf8'));

// The result of the last background synchronization of one library.
function writeSyncStatus(fixture, library, value) {
  const gitDir = git(library, fixture.env, 'rev-parse', '--absolute-git-dir');
  fs.writeFileSync(path.join(gitDir, 'agent-skills-sync-status.json'), JSON.stringify(value));
}

// The app with the connected libraries of the configuration, no folder chosen for it.
async function launchConnected(fixture) {
  const env = { ...fixture.env };
  delete env.AGENT_SKILLS_LIBRARY;
  delete env.SKILLARIUM_LIBRARY;
  const app = await electron.launch({ args: [appDir], env });
  return { app, page: await app.firstWindow() };
}

function linksTo(link, target) {
  try {
    return fs.lstatSync(link).isSymbolicLink() && fs.realpathSync.native(link) === fs.realpathSync.native(target);
  } catch {
    return false;
  }
}

async function launch(fixture) {
  const app = await electron.launch({ args: [appDir], env: { ...fixture.env, AGENT_SKILLS_LIBRARY: fixture.library } });
  const page = await app.firstWindow();
  return { app, page };
}

// The native folder picker is replaced by a fixed answer; everything after it is real.
async function answerFolderPicker(app, folder) {
  await app.evaluate(({ dialog }, picked) => {
    dialog.showOpenDialog = async () => ({ canceled: false, filePaths: [picked] });
  }, folder);
}

const row = (page, name) => page.locator('tbody tr', { has: page.getByText(name, { exact: true }) });

let fixture;
let session;

test.beforeEach(() => { fixture = makeFixture(); });
test.afterEach(async () => {
  await session?.app.close();
  session = null;
  fs.rmSync(fixture.root, { recursive: true, force: true });
});

test('connects a skill, asks on the last disconnect and shows global access separately', async () => {
  session = await launch(fixture);
  const { app, page } = session;
  const sharedGlobal = page.getByRole('switch', { name: `Глобальный доступ: ${SHARED}` });
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'true');
  await expect(row(page, SHARED).getByText('По умолчанию')).toBeVisible();

  await answerFolderPicker(app, fixture.alpha);
  await page.getByRole('button', { name: 'Добавить проект' }).first().click();
  const cell = page.getByRole('button', { name: new RegExp(`^${SHARED} в project alpha:`) });
  await expect(cell).toHaveAttribute('aria-pressed', 'false');

  await cell.click();
  await expect(cell).toHaveAccessibleName(`${SHARED} в project alpha: подключён`);
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'false');
  await expect(row(page, SHARED).getByText('Выключено первым подключением')).toBeVisible();
  await expect(page.getByRole('switch', { name: `Глобальный доступ: ${OTHER}` })).toHaveAttribute('aria-checked', 'true');
  const librarySkill = path.join(fixture.library, 'skills', SHARED);
  expect(linksTo(path.join(fixture.alpha, '.claude', 'skills', SHARED), librarySkill)).toBe(true);
  expect(fs.existsSync(path.join(fixture.home, '.claude', 'skills', SHARED))).toBe(false);

  // Cancelling the last-project dialog keeps the connection.
  await cell.click();
  let dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Сейчас глобальный доступ выключен');
  await dialog.getByRole('button', { name: 'Отмена, оставить подключение' }).click();
  await expect(dialog).toBeHidden();
  await expect(cell).toHaveAttribute('aria-pressed', 'true');

  await cell.click();
  await page.getByRole('dialog').getByRole('button', { name: /Доступен глобально/ }).click();
  await expect(cell).toHaveAttribute('aria-pressed', 'false');
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'true');
  await expect(row(page, SHARED).getByText('Включено вручную')).toBeVisible();

  // The manual choice survives the next connection; the dialog appears with global access on too.
  await cell.click();
  await expect(cell).toHaveAttribute('aria-pressed', 'true');
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'true');
  await cell.click();
  dialog = page.getByRole('dialog');
  await expect(dialog).toContainText('Сейчас глобальный доступ включён');
  await dialog.getByRole('button', { name: /Оставить только в библиотеке/ }).click();
  await expect(cell).toHaveAttribute('aria-pressed', 'false');
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'false');
  await expect(row(page, SHARED).getByText('Выключено вручную')).toBeVisible();
  expect(fs.existsSync(path.join(librarySkill, 'SKILL.md'))).toBe(true);

  await sharedGlobal.click();
  await expect(sharedGlobal).toHaveAttribute('aria-checked', 'true');
  expect(linksTo(path.join(fixture.home, '.agents', 'skills', SHARED), librarySkill)).toBe(true);
});

test('shows a stopped synchronization and resolves the conflict with an edited version', async () => {
  const skillFile = (folder) => path.join(folder, 'skills', SHARED, 'SKILL.md');
  const text = (body) => `---\nname: ${SHARED}\ndescription: Test\n---\n${body}\n`;
  // The state the background synchronization leaves: an automatic local commit and a diverged origin.
  fs.writeFileSync(skillFile(fixture.seed), text('Правка коллеги'));
  git(fixture.seed, fixture.env, 'commit', '-am', 'Teammate edit');
  git(fixture.seed, fixture.env, 'push');
  fs.writeFileSync(skillFile(fixture.library), text('Моя правка'));
  git(fixture.library, fixture.env, 'commit', '-am', 'Share local skill edits\n\nAgent-Skills-Client: 1');
  git(fixture.library, fixture.env, 'fetch', 'origin');

  session = await launch(fixture);
  const { page } = session;
  const banner = page.getByRole('region', { name: 'Синхронизация библиотеки' });
  await expect(banner).toContainText('Синхронизация библиотеки остановлена');
  await expect(banner).toContainText(`«${SHARED}»`);

  await banner.getByRole('button', { name: 'Разобрать конфликт' }).click();
  let dialog = page.getByRole('dialog', { name: 'Разбор конфликта синхронизации' });
  const versions = dialog.getByRole('region', { name: 'Сравнение версий' });
  await expect(versions).toContainText('Моя правка');
  await expect(versions).toContainText('Правка коллеги');
  const merged = dialog.getByRole('textbox', { name: `Итоговый текст skills/${SHARED}/SKILL.md` });
  const apply = dialog.getByRole('button', { name: /^Применить/ });
  await expect(merged).toHaveValue(text('Моя правка'));
  await expect(dialog.getByRole('button', { name: 'Различие 1: не выбрано' })).toBeVisible();
  await expect(apply).toBeDisabled();
  await dialog.getByRole('button', { name: /^Отмена/ }).click();
  await expect(dialog).toBeHidden();
  expect(fs.readFileSync(skillFile(fixture.library), 'utf8')).toBe(text('Моя правка'));

  await banner.getByRole('button', { name: 'Разобрать конфликт' }).click();
  dialog = page.getByRole('dialog', { name: 'Разбор конфликта синхронизации' });
  // Buttons and WinMerge-style hotkeys put a side into the editable result below.
  await dialog.getByRole('button', { name: /Взять правую/ }).click();
  await expect(merged).toHaveValue(text('Правка коллеги'));
  await expect(dialog.getByRole('button', { name: 'Различие 1: правая' })).toBeVisible();
  await expect(apply).toBeEnabled();
  await page.keyboard.press('Alt+Shift+ArrowLeft');
  await expect(merged).toHaveValue(text('Моя правка\nПравка коллеги'));
  await page.keyboard.press('Alt+ArrowLeft');
  await expect(merged).toHaveValue(text('Моя правка'));
  await merged.fill(text('Моя правка и правка коллеги'));
  await expect(dialog.getByRole('button', { name: 'Различие 1: правка вручную' })).toBeVisible();
  await page.keyboard.press('Control+Enter');

  // This fixture has no hosting API, so the merge stays local until the next synchronization
  // publishes it through a proposal pull request (covered by the Python tests).
  await expect(page.getByRole('alert')).toContainText('Конфликт разрешён и сохранён здесь');
  await expect(banner).toBeHidden();
  expect(fs.readFileSync(skillFile(fixture.library), 'utf8')).toBe(text('Моя правка и правка коллеги'));
  const parents = git(fixture.library, fixture.env, 'rev-list', '--parents', '-n', '1', 'HEAD').split(' ');
  expect(parents.slice(1)).toContain(git(fixture.library, fixture.env, 'rev-parse', 'origin/main'));
  expect(git(fixture.library, fixture.env, 'status', '--porcelain')).toBe('');
});

test('resolves a conflict whose local version was saved with CRLF line endings', async () => {
  const skillFile = (folder) => path.join(folder, 'skills', SHARED, 'SKILL.md');
  const text = (first, second, eol = '\n') => [`---`, `name: ${SHARED}`, 'description: Test', '---',
    first, 'Общая строка', second, ''].join(eol);
  fs.writeFileSync(skillFile(fixture.seed), text('Коллега 1', 'Коллега 2'));
  git(fixture.seed, fixture.env, 'commit', '-am', 'Teammate edit');
  git(fixture.seed, fixture.env, 'push');
  // skills/** is -text, so an editor on Windows can leave CRLF in both the file and its blob.
  fs.writeFileSync(skillFile(fixture.library), text('Моя 1', 'Моя 2', '\r\n'));
  git(fixture.library, fixture.env, 'commit', '-am', 'Share local skill edits\n\nAgent-Skills-Client: 1');
  git(fixture.library, fixture.env, 'fetch', 'origin');

  session = await launch(fixture);
  const { page } = session;
  await page.getByRole('region', { name: 'Синхронизация библиотеки' }).getByRole('button', { name: 'Разобрать конфликт' }).click();
  const dialog = page.getByRole('dialog', { name: 'Разбор конфликта синхронизации' });
  // Line endings alone are no difference: only the two changed lines are.
  await expect(dialog.getByRole('button', { name: 'Различие 1: не выбрано' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Различие 2: не выбрано' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: /^Различие 3/ })).toHaveCount(0);

  await dialog.getByRole('button', { name: /Взять правую/ }).click();
  await expect(dialog.getByRole('button', { name: 'Различие 1: правая' })).toBeVisible();
  // The textarea reports CRLF as LF; typing in one difference must not take over the others.
  const merged = dialog.getByRole('textbox', { name: `Итоговый текст skills/${SHARED}/SKILL.md` });
  await merged.evaluate((area) => {
    const at = area.value.indexOf('Моя 2') + 'Моя 2'.length;
    area.focus();
    area.setSelectionRange(at, at);
  });
  await page.keyboard.type('!');
  await expect(dialog.getByRole('button', { name: 'Различие 2: правка вручную' })).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'Различие 1: правая' })).toBeVisible();
  await page.keyboard.press('Control+Enter');

  await expect(page.getByRole('alert')).toContainText('Конфликт разрешён и сохранён здесь');
  // When the sides disagree, the shared version decides the line ending.
  expect(fs.readFileSync(skillFile(fixture.library), 'utf8')).toBe(text('Коллега 1', 'Моя 2!'));
});

test('explains unapplied connections and unavailable projects', async () => {
  const foreign = path.join(fixture.beta, '.claude', 'skills', OTHER);
  fs.mkdirSync(foreign, { recursive: true });
  fs.writeFileSync(path.join(foreign, 'SKILL.md'), 'foreign\n');
  session = await launch(fixture);
  const { app, page } = session;

  await answerFolderPicker(app, fixture.beta);
  await page.getByRole('button', { name: 'Добавить проект' }).first().click();
  const cell = page.getByRole('button', { name: new RegExp(`^${OTHER} в project beta:`) });
  await cell.click();
  await expect(page.getByRole('alert')).toContainText(`«${OTHER}» добавлен в список «project beta», но не подключён: место занято`);
  await expect(cell).toHaveAccessibleName(`${OTHER} в project beta: место занято`);
  const issues = page.getByRole('region', { name: 'Требует внимания' });
  await expect(issues).toContainText(foreign);
  expect(fs.readFileSync(path.join(foreign, 'SKILL.md'), 'utf8')).toBe('foreign\n');
  await expect(page.getByRole('switch', { name: `Глобальный доступ: ${OTHER}` })).toHaveAttribute('aria-checked', 'true');

  await answerFolderPicker(app, fixture.alpha);
  await page.getByRole('button', { name: 'Добавить проект' }).first().click();
  const alphaCell = page.getByRole('button', { name: new RegExp(`^${SHARED} в project alpha:`) });
  await alphaCell.click();
  await expect(alphaCell).toHaveAccessibleName(`${SHARED} в project alpha: подключён`);
  fs.renameSync(fixture.alpha, `${fixture.alpha} moved`);
  await page.getByRole('button', { name: 'Обновить' }).click();
  await expect(page.getByRole('columnheader', { name: /project alpha/ })).toContainText('папка недоступна');
  await expect(issues).toContainText('Папка «project alpha» недоступна');
  await expect(page.getByRole('button', { name: `${SHARED} в project alpha: папка недоступна` })).toBeDisabled();
  await expect(page.getByRole('switch', { name: `Глобальный доступ: ${SHARED}` })).toHaveAttribute('aria-checked', 'false');
});

test('switches the update policy of an external skill and shows it as pending until published', async () => {
  // OTHER comes from an external source; the library has no hosting, so publication waits.
  const registry = JSON.parse(fs.readFileSync(path.join(fixture.seed, 'sources.json'), 'utf8'));
  registry.skills[OTHER] = {
    source: { kind: 'git', url: 'https://example.org/team/skills.git', path: `skills/${OTHER}`, ref: 'refs/heads/main' },
    auto_update: true, revision: 'a'.repeat(40), content_sha256: 'b'.repeat(64), executable_sha256: 'c'.repeat(64),
  };
  fs.writeFileSync(path.join(fixture.seed, 'sources.json'), `${JSON.stringify(registry, null, 2)}\n`);
  git(fixture.seed, fixture.env, 'commit', '-am', 'External skill');
  git(fixture.seed, fixture.env, 'push', 'origin', 'main');
  git(fixture.library, fixture.env, 'pull', '-q', '--ff-only');
  session = await launch(fixture);
  const { page } = session;
  const policy = page.getByRole('switch', { name: `Обновлять автоматически: ${OTHER}` });
  await expect(policy).toHaveAttribute('aria-checked', 'true');
  await expect(row(page, OTHER).getByText('Принимаются сами')).toBeVisible();
  await expect(row(page, SHARED).getByText('свой скилл')).toBeVisible();
  await expect(page.getByRole('switch', { name: `Обновлять автоматически: ${SHARED}` })).toHaveCount(0);

  await policy.click();
  await expect(policy).toHaveAttribute('aria-checked', 'false');
  await expect(row(page, OTHER).getByText('Ждёт принятия')).toBeVisible();
  await expect(page.getByRole('alert')).toContainText('будет опубликована при следующей синхронизации');
  const local = JSON.parse(fs.readFileSync(path.join(fixture.library, 'sources.json'), 'utf8'));
  expect(local.skills[OTHER].auto_update).toBe(false);
  expect(git(fixture.library, fixture.env, 'log', '-1', '--format=%s')).toBe(`Set ${OTHER} update policy to confirm`);

  // While that switch waits, another one stops before changing anything and says so.
  await policy.click();
  await expect(page.getByRole('alert')).toContainText('Действие не завершилось');
  await expect(policy).toHaveAttribute('aria-checked', 'false');
  await expect(row(page, OTHER).getByText('Ждёт принятия')).toBeVisible();
  expect(git(fixture.library, fixture.env, 'log', '-1', '--format=%s')).toBe(`Set ${OTHER} update policy to confirm`);
});

test('a library of a newer format is shown as read-only with an update check', async () => {
  const config = path.join(fixture.library, 'agent-skills.json');
  const data = JSON.parse(fs.readFileSync(config, 'utf8'));
  fs.writeFileSync(config, `${JSON.stringify({ ...data, format: 2 }, null, 2)}\n`);
  session = await launch(fixture);
  const { page } = session;
  const banner = page.getByTestId('read-only');
  await expect(banner).toContainText('формата 2');
  // A development run has no updater: the button is not offered.
  await expect(banner.getByRole('button', { name: 'Проверить обновление' })).toHaveCount(0);
});

test('settings show versions and follow the app update to a restart', async () => {
  session = await launch(fixture);
  const { app, page } = session;
  const opener = page.getByRole('button', { name: 'Настройки' });
  const dialog = page.getByRole('dialog', { name: 'Настройки' });
  // Keyboard: focus starts inside, Tab cannot leave the dialog, Escape returns to the opener.
  await opener.click();
  await expect(dialog.getByRole('button', { name: 'Закрыть' })).toBeFocused();
  const focusInside = () => page.evaluate(() => Boolean(document.activeElement?.closest('[role="dialog"]')));
  for (const key of ['Tab', 'Tab', 'Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab']) {
    await page.keyboard.press(key);
    expect(await focusInside()).toBe(true);
  }
  await page.keyboard.press('Escape');
  await expect(dialog).toHaveCount(0);
  await expect(opener).toBeFocused();
  await opener.click();
  const settings = dialog.getByTestId('settings');
  await expect(settings).toContainText(/Skillarium \d+\.\d+\.\d+ \(сборка для разработки\)/);
  await expect(settings).toContainText('из папки репозитория');
  await expect(settings).toContainText('стабильный');
  await expect(settings).toContainText(fixture.library);
  const summary = dialog.getByTestId('update-summary');
  // A development run has no updater: it says so and offers no check.
  await expect(summary).toContainText('сборка для разработки');
  const check = dialog.getByRole('button', { name: 'Проверить обновления' });
  await expect(check).toBeDisabled();
  // The packaged updater's events reach the window through the same channel.
  const push = (state) => app.evaluate(({ BrowserWindow }, value) => {
    BrowserWindow.getAllWindows()[0].webContents.send('update', value);
  }, state);
  await push({ state: 'current', version: '0.1.2', checkedAt: '2026-11-07T10:15:00.000Z' });
  await expect(summary).toHaveText('Установлена последняя версия.');
  await expect(dialog).toContainText('Последняя проверка:');
  await expect(check).toBeEnabled();
  await push({ state: 'error', message: 'net::ERR_INTERNET_DISCONNECTED' });
  await expect(summary).toContainText('Проверить обновления не удалось: net::ERR_INTERNET_DISCONNECTED');
  await expect(check).toBeEnabled();
  await push({ state: 'downloading', version: '0.2.0', percent: 42 });
  await expect(summary).toHaveText('Загружаю версию 0.2.0 — 42%…');
  await expect(check).toBeDisabled();
  // A build under verification downloads but offers no installation.
  await push({ state: 'ready', version: '0.2.0', install: false });
  await expect(summary).toContainText('установка выключена переменной SKILLARIUM_NO_INSTALL');
  await expect(dialog.getByRole('button', { name: 'Перезапустить и обновить' })).toBeDisabled();
  await expect(page.getByTestId('update-ready').getByRole('button')).toHaveCount(0);
  await push({ state: 'ready', version: '0.2.0' });
  await expect(summary).toContainText('Версия 0.2.0 загружена');
  await expect(dialog.getByRole('button', { name: 'Перезапустить и обновить' })).toBeEnabled();
  await expect(page.getByRole('button', { name: /Настройки/ }).getByLabel('есть обновление')).toBeVisible();
  await dialog.getByRole('button', { name: 'Закрыть' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByTestId('update-ready')).toContainText('0.2.0');
});

test('first run offers to connect a library and reports a failed connection', async () => {
  const env = { ...fixture.env };
  delete env.AGENT_SKILLS_LIBRARY;
  delete env.SKILLARIUM_LIBRARY;
  const app = await electron.launch({ args: [appDir], env });
  session = { app };
  const page = await app.firstWindow();
  const form = page.getByRole('form', { name: 'Подключить библиотеку' });
  await expect(form).toBeVisible();
  await expect(form.getByLabel('Папка на этом компьютере')).toHaveValue(path.join(fixture.home, 'Skillarium', 'library'));
  await form.getByLabel('Адрес библиотеки').fill('http://127.0.0.1:9/owner/skills.git');
  await form.getByLabel('Токен Gitea').fill('not-a-token');
  await form.getByRole('button', { name: 'Подключить' }).click();
  // The engine's client setup runs and fails before changing anything: nothing listens there.
  await expect(page.getByRole('alert')).toContainText('Сервер Gitea по этому адресу не отвечает');
  await expect(page.getByRole('alert')).not.toContainText('FullyQualifiedErrorId');
  expect(fs.existsSync(path.join(fixture.home, 'Skillarium', 'library'))).toBe(false);
  // An existing clone can still be chosen instead.
  await answerFolderPicker(app, fixture.library);
  await page.getByRole('button', { name: 'У меня уже есть клон — выбрать папку' }).click();
  await expect(page.getByRole('switch', { name: `Глобальный доступ: ${SHARED}` })).toBeVisible();
});

test('several libraries: library list, column and filter, a shadowed copy, its project binding, preference and order', async () => {
  const TEAM_ONLY = 'team-skill';
  const team = makeLibrary(fixture, 'org', 'team', [SHARED, TEAM_ONLY]);
  writeConfig(fixture, [{ name: 'personal', path: fixture.library, url: '' }, { name: 'team', path: team.path, url: team.url }]);
  writeSyncStatus(fixture, fixture.library, { time: '2026-11-08T09:00:00Z', ok: true, error: null });
  writeSyncStatus(fixture, team.path, { time: '2026-11-08T09:05:00Z', ok: false, error: 'fetch failed: host unreachable' });
  session = await launchConnected(fixture);
  const { app, page } = session;

  const list = page.getByRole('region', { name: 'Библиотеки' });
  const personalItem = list.getByRole('listitem', { name: 'Библиотека personal' });
  const teamItem = list.getByRole('listitem', { name: 'Библиотека team' });
  await expect(personalItem).toContainText('синхронизирована');
  await expect(teamItem).toContainText('синхронизация не удалась');
  await expect(teamItem).toContainText('fetch failed: host unreachable');
  await expect(teamItem).toContainText('org/team');
  await expect(personalItem.getByRole('button', { name: 'Поднять personal' })).toBeDisabled();

  // The same name in both libraries: the higher library's copy is linked, the other one is marked.
  const copies = page.locator('tbody tr', { has: page.getByText(SHARED, { exact: true }) });
  await expect(copies).toHaveCount(2);
  const shadowed = page.locator('tbody tr', { hasText: 'перекрыт personal' });
  await expect(shadowed).toHaveCount(1);
  await expect(shadowed.locator('td.library-col')).toHaveText('team');
  await expect(page.getByRole('switch', { name: `Глобальный доступ: ${SHARED}` })).toHaveCount(1);

  const filter = page.getByLabel('Фильтр по библиотеке');
  await filter.selectOption('team');
  await expect(page.locator('tbody tr')).toHaveCount(2);
  await expect(page.locator('tbody tr', { hasText: TEAM_ONLY })).toHaveCount(1);
  await expect(page.locator('tbody tr', { hasText: OTHER })).toHaveCount(0);
  await filter.selectOption('');
  await expect(page.locator('tbody tr')).toHaveCount(4);

  // A project may take the shadowed copy: its list names the library, its links point into it.
  await answerFolderPicker(app, fixture.alpha);
  await page.getByRole('button', { name: 'Добавить проект' }).first().click();
  const teamCell = page.getByRole('button', { name: new RegExp(`^${SHARED} \\(team\\) в project alpha:`) });
  await teamCell.click();
  await expect(teamCell).toHaveAccessibleName(`${SHARED} (team) в project alpha: подключён`);
  const list1 = JSON.parse(fs.readFileSync(path.join(fixture.alpha, '.agent-skills.json'), 'utf8'));
  expect(list1.skills).toEqual([`org/team:${SHARED}`]);
  expect(linksTo(path.join(fixture.alpha, '.claude', 'skills', SHARED), path.join(team.path, 'skills', SHARED))).toBe(true);
  await expect(page.getByRole('button', { name: `${SHARED} в project alpha: не подключён` })).toBeVisible();

  // The copy linked on this machine is chosen per skill and reset to the order of the libraries.
  await page.getByRole('button', { name: `Подключать ${SHARED} из team` }).click();
  await expect(page.locator('tbody tr', { hasText: 'перекрыт team' }).locator('td.library-col')).toHaveText('personal');
  await expect(page.getByRole('alert')).toContainText(`«${SHARED}» теперь подключается из библиотеки «team»`);
  expect(readConfig(fixture).prefer).toEqual({ [SHARED]: 'team' });
  await expect(page.locator('tbody tr', { hasText: 'Эта копия выбрана вручную' }).locator('td.library-col')).toHaveText('team');
  await page.getByRole('button', { name: `Выбирать копию ${SHARED} по порядку библиотек` }).click();
  await expect(page.locator('tbody tr', { hasText: 'перекрыт personal' }).locator('td.library-col')).toHaveText('team');
  expect(readConfig(fixture).prefer).toBeUndefined();

  // Raising a library makes its copies win.
  await teamItem.getByRole('button', { name: 'Поднять team' }).click();
  await expect(list.getByRole('listitem').first()).toHaveAccessibleName('Библиотека team');
  await expect(page.locator('tbody tr', { hasText: 'перекрыт team' }).locator('td.library-col')).toHaveText('personal');
  expect(readConfig(fixture).libraries.map((item) => item.name)).toEqual(['team', 'personal']);
  await expect(page.getByRole('button', { name: `${SHARED} в project alpha: подключён` })).toBeVisible();
});

test('connects another library and disconnects one with unpublished changes only when forced', async () => {
  const team = makeLibrary(fixture, 'org', 'team', [SHARED, 'team-skill']);
  writeConfig(fixture, [{ name: 'personal', path: fixture.library, url: '' }]);
  session = await launchConnected(fixture);
  const { app, page } = session;
  const list = page.getByRole('region', { name: 'Библиотеки' });
  await expect(list.getByRole('listitem')).toHaveCount(1);
  // Even the only library can be disconnected (the app then offers to connect one).
  await expect(list.getByRole('button', { name: 'Отключить personal' })).toBeEnabled();

  // The client setup itself needs Gitea (covered by the engine tests): here it is replaced by
  // its result, the configuration with the library appended, and the request is recorded.
  await app.evaluate(({ ipcMain }) => {
    ipcMain.removeHandler('connect');
    ipcMain.handle('connect', async (_event, request) => {
      globalThis.connectRequests = [...(globalThis.connectRequests || []), request];
      return { code: 0, messages: ['CONNECTED'] };
    });
  });
  await list.getByRole('button', { name: 'Подключить библиотеку' }).click();
  const dialog = page.getByRole('dialog', { name: 'Подключить ещё одну библиотеку' });
  await expect(dialog.getByLabel('Адрес библиотеки')).toBeFocused();
  await expect(dialog.getByLabel('Папка на этом компьютере')).toHaveValue('');
  await dialog.getByLabel('Адрес библиотеки').fill('https://gitea.example.org/org/team.git');
  await dialog.getByLabel('Токен Gitea').fill('test-token');
  await dialog.getByLabel('Имя библиотеки на этом компьютере').fill('team');
  writeConfig(fixture, [{ name: 'personal', path: fixture.library, url: '' }, { name: 'team', path: team.path, url: team.url }]);
  await dialog.getByRole('button', { name: 'Подключить' }).click();
  await expect(dialog).toHaveCount(0);
  await expect(list.getByRole('listitem', { name: 'Библиотека team' })).toBeVisible();
  await expect(page.locator('tbody tr', { hasText: 'перекрыт personal' })).toHaveCount(1);
  const requests = await app.evaluate(() => globalThis.connectRequests);
  expect(requests).toEqual([{ url: 'https://gitea.example.org/org/team.git', token: 'test-token', folder: '', name: 'team' }]);

  // A local commit not yet published: the engine refuses and says why.
  fs.writeFileSync(path.join(team.path, 'skills', SHARED, 'SKILL.md'), `---\nname: ${SHARED}\ndescription: team\n---\nLocal edit\n`);
  git(team.path, fixture.env, 'commit', '-am', 'Local edit');
  await list.getByRole('button', { name: 'Отключить team' }).click();
  let confirm = page.getByRole('dialog', { name: 'Отключить библиотеку «team»?' });
  await expect(confirm).toContainText(team.path);
  await confirm.getByRole('button', { name: 'Отключить', exact: true }).click();
  confirm = page.getByRole('dialog', { name: 'Отключить библиотеку «team»?' });
  await expect(confirm.getByRole('alert')).toContainText('unpublished changes');
  expect(readConfig(fixture).libraries.map((item) => item.name)).toEqual(['personal', 'team']);
  await confirm.getByRole('button', { name: 'Отключить принудительно' }).click();
  await expect(page.getByRole('alert')).toContainText('Библиотека «team» отключена');
  await expect(list.getByRole('listitem')).toHaveCount(1);
  await expect(page.locator('tbody tr', { hasText: 'перекрыт' })).toHaveCount(0);
  expect(readConfig(fixture).libraries.map((item) => item.name)).toEqual(['personal']);
  // The clone and its unpublished edit stay on disk.
  expect(fs.readFileSync(path.join(team.path, 'skills', SHARED, 'SKILL.md'), 'utf8')).toContain('Local edit');
});
