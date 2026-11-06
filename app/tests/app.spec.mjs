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
