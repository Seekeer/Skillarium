// Turns `projects status` JSON into the matrix and the problem list shown by the app.
export function basename(path) {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? parts[parts.length - 1] : path;
}

export function parent(path) {
  const index = Math.max(path.lastIndexOf('\\'), path.lastIndexOf('/'));
  return index > 0 ? path.slice(0, index) : '';
}

export const CELL_TEXT = {
  applied: 'подключён',
  pending: 'ждёт применения',
  conflict: 'место занято',
  'missing-skill': 'нет в библиотеке',
  'library-missing': 'библиотека не подключена',
  'library-unavailable': 'библиотека недоступна',
  unavailable: 'папка недоступна',
};

// Project entries whose skill is not a row of the library list, by the reason.
const MISSING_TAG = {
  'library-missing': 'библиотека не подключена',
  'library-unavailable': 'библиотека недоступна',
};

const skillOf = (entry) => entry.skill || entry.name;

// Whether a project entry is the copy a row stands for. Older engines report no library:
// their entries are the copy linked globally.
function sameCopy(entry, row) {
  if (skillOf(entry) !== row.name) return false;
  if (entry.library === undefined) return row.active;
  if (entry.library === null) return !row.inLibrary && row.library === null && row.entry === entry.name;
  return entry.library === row.library;
}

// One row per copy: a skill in several libraries has a row for each, the linked one first.
export function buildMatrix(status) {
  const libraries = status.libraries || [];
  const multiple = libraries.length > 1;
  const rank = (name) => { const index = libraries.findIndex((library) => library.name === name); return index < 0 ? libraries.length : index; };
  const rows = status.skills.map((skill) => {
    const active = skill.active !== false;
    const library = skill.library ?? null;
    return {
      key: `${library ?? ''}|${skill.name}`, name: skill.name, library, entry: skill.entry || skill.name,
      label: active ? skill.name : `${skill.name} (${library})`,
      active, shadowedBy: skill.shadowedBy || null, copies: skill.copies || [], preferred: Boolean(skill.preferred),
      inLibrary: true, missing: null, global: skill.global, update: skill.update || null,
    };
  });
  for (const project of status.projects) {
    for (const entry of project.skills) {
      if (rows.some((row) => sameCopy(entry, row))) continue;
      const library = entry.library ?? null;
      const name = skillOf(entry);
      const qualified = library !== null || entry.name !== name;
      rows.push({
        key: `${library ?? ''}|${library === null ? entry.name : name}`, name, library, entry: entry.name,
        label: !qualified ? name : library === null ? entry.name : `${name} (${library})`,
        active: !qualified, shadowedBy: null, copies: [], preferred: false,
        inLibrary: false, missing: MISSING_TAG[entry.state] || 'нет в библиотеке', global: null, update: null,
      });
    }
  }
  rows.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0)
    || Number(right.active) - Number(left.active) || rank(left.library) - rank(right.library));
  return { rows, projects: status.projects, libraries, multiple };
}

// Rows of one library; '' shows all of them.
export function filterRows(rows, library) {
  return library ? rows.filter((row) => row.library === library) : rows;
}

export function cellOf(project, row) {
  return project.skills.find((entry) => sameCopy(entry, row)) || null;
}

// The sync line of one library in the list.
export function syncCaption(library) {
  if (!library.available) return { tone: 'error', text: 'папка библиотеки недоступна' };
  const sync = library.sync;
  if (!sync) return { tone: 'none', text: 'ещё не синхронизировалась' };
  const when = new Date(sync.time);
  const at = Number.isNaN(when.getTime()) ? '' : ` ${when.toLocaleString('ru-RU')}`;
  if (sync.ok) return { tone: 'ok', text: `синхронизирована${at}` };
  return { tone: 'error', text: `синхронизация не удалась${at}`, error: sync.error || '' };
}

// The order of the connected libraries after moving one of them up (-1) or down (+1);
// null when it cannot move. Libraries given only for this run are not in the order.
export function moveLibrary(libraries, name, delta) {
  const names = libraries.filter((library) => library.configured !== false).map((library) => library.name);
  const from = names.indexOf(name);
  const to = from + delta;
  if (from < 0 || to < 0 || to >= names.length) return null;
  [names[from], names[to]] = [names[to], names[from]];
  return names;
}
// Update policy of an external skill; pending: switched here, not yet merged into the library.
export function policyCaption(update) {
  if (update.pending) return 'Ждёт принятия';
  return update.policy === 'auto' ? 'Принимаются сами' : 'С подтверждением';
}

// One line about app updates for the settings window (main/updates.cjs states).
export function updateSummary(update) {
  switch (update?.state) {
    case 'development': return 'Это сборка для разработки: она не обновляется. Обновления получает установленное приложение.';
    case 'disabled': return 'Автообновление выключено переменной SKILLARIUM_NO_UPDATES.';
    case 'checking': return 'Проверяю обновления…';
    case 'downloading': return `Загружаю версию ${update.version}${Number.isFinite(update.percent) ? ` — ${update.percent}%` : ''}…`;
    case 'ready': return update.install === false
      ? `Версия ${update.version} загружена, но установка выключена переменной SKILLARIUM_NO_INSTALL.`
      : `Версия ${update.version} загружена и установится при перезапуске.`;
    case 'current': return 'Установлена последняя версия.';
    case 'error': return `Проверить обновления не удалось: ${update.message}`;
    default: return 'Обновления ещё не проверялись.';
  }
}

export function checkedText(checkedAt) {
  if (!checkedAt) return '';
  const when = new Date(checkedAt);
  return Number.isNaN(when.getTime()) ? '' : `Последняя проверка: ${when.toLocaleString('ru-RU')}`;
}

export function globalCaption(global) {
  if (global.manual) return global.enabled ? 'Включено вручную' : 'Выключено вручную';
  if (global.pending) return 'Выключится после подключения';
  return global.enabled ? 'По умолчанию' : 'Выключено первым подключением';
}

// Lines carry no line ending: the result textarea reports CRLF as LF, so the ending is
// restored only when the result is saved (see Resolver.jsx, resultText).
export function lines(text) {
  if (text === null || text === undefined) return [];
  const list = text.split(/\r?\n/);
  if (list.length > 1 && list[list.length - 1] === '') list.pop();
  return list;
}

// Splits two versions into runs of common lines and numbered differences, as merge tools show them.
export function alignBlocks(left, right) {
  const a = lines(left);
  const b = lines(right);
  if (a.length * b.length > 4_000_000) return [{ type: 'diff', index: 0, left: a, right: b }];
  const table = Array.from({ length: a.length + 1 }, () => new Uint32Array(b.length + 1));
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] = a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1]);
    }
  }
  const blocks = [];
  let diffs = 0;
  const last = () => blocks[blocks.length - 1];
  const same = (line) => {
    if (last()?.type !== 'same') blocks.push({ type: 'same', lines: [] });
    last().lines.push(line);
  };
  const differ = (side, line) => {
    if (last()?.type !== 'diff') { blocks.push({ type: 'diff', index: diffs, left: [], right: [] }); diffs += 1; }
    last()[side].push(line);
  };
  let i = 0;
  let j = 0;
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) { same(a[i]); i += 1; j += 1; }
    else if (table[i + 1][j] >= table[i][j + 1]) { differ('left', a[i]); i += 1; }
    else { differ('right', b[j]); j += 1; }
  }
  for (; i < a.length; i += 1) differ('left', a[i]);
  for (; j < b.length; j += 1) differ('right', b[j]);
  return blocks;
}

// The editable result is one text split into a segment per block, so a later choice for a
// difference replaces exactly its own lines, even after hand edits elsewhere.
export const segmentText = (list) => list.map((line) => `${line}\n`).join('');

export const PICKS = {
  mine: (block) => block.left,
  library: (block) => block.right,
  'mine-library': (block) => [...block.left, ...block.right],
  'library-mine': (block) => [...block.right, ...block.left],
};

export function initialSegments(blocks) {
  return blocks.map((block) => segmentText(block.type === 'same' ? block.lines : block.left));
}

// Applies a whole-text edit to the segments: the changed range goes to the first segment it touches.
export function editSegments(segments, text) {
  const old = segments.join('');
  let start = 0;
  while (start < old.length && start < text.length && old[start] === text[start]) start += 1;
  let tail = 0;
  while (tail < old.length - start && tail < text.length - start
    && old[old.length - 1 - tail] === text[text.length - 1 - tail]) tail += 1;
  const end = old.length - tail;
  const starts = [];
  let offset = 0;
  for (const segment of segments) { starts.push(offset); offset += segment.length; }
  const span = (index) => [starts[index], starts[index] + segments[index].length];
  const indexes = segments.map((_, index) => index);
  let hit = start < end
    ? indexes.filter((index) => { const [from, to] = span(index); return from < end && to > start; })
    : indexes.filter((index) => { const [from, to] = span(index); return from <= start && start < to; });
  // Typing after the last character continues the segment that ends there.
  if (!hit.length) hit = [indexes.filter((index) => span(index)[1] === start && segments[index]).pop() ?? 0];
  const first = hit[0];
  const last = hit[hit.length - 1];
  const next = [...segments];
  next[first] = segments[first].slice(0, start - starts[first]) + text.slice(start, text.length - tail)
    + segments[last].slice(end - starts[last]);
  for (let index = first + 1; index <= last; index += 1) next[index] = '';
  return { segments: next, touched: indexes.slice(first, last + 1) };
}

export function conflictSkills(sync) {
  return [...new Set((sync?.files || []).map((file) => file.skill))];
}

function conflictPaths(links) {
  return Object.entries(links || {}).filter(([, state]) => state === 'conflict').map(([path]) => path);
}

export function problems(status) {
  const list = [];
  for (const project of status.projects) {
    const name = basename(project.path);
    if (!project.available) {
      list.push({ key: `${project.path}|unavailable`, text: `Папка «${name}» недоступна. Регистрация и подключения сохранены; обслуживание продолжится, когда папка вернётся.`, paths: [project.path] });
    } else if (project.error) {
      list.push({ key: `${project.path}|error`, text: `Список скиллов в «${name}» не читается. Исправьте .agent-skills.json; ссылки в папке не изменялись.`, paths: [project.path] });
    }
    for (const entry of project.skills) {
      if (entry.state === 'conflict') {
        list.push({ key: `${project.path}|${entry.name}`, text: `«${entry.name}» не подключён к «${name}»: место занято другой папкой или ссылкой. Она сохранена; освободите место, и подключение завершится при следующей синхронизации.`, paths: conflictPaths(entry.links) });
      } else if (entry.state === 'missing-skill') {
        list.push({ key: `${project.path}|${entry.name}`, text: `«${entry.name}» есть в списке «${name}», но отсутствует в библиотеке.`, paths: [] });
      } else if (entry.state === 'library-missing') {
        list.push({ key: `${project.path}|${entry.name}`, text: `«${entry.name}» есть в списке «${name}», но его библиотека не подключена на этом компьютере. Скилл подключится, когда вы подключите эту библиотеку.`, paths: [] });
      } else if (entry.state === 'library-unavailable') {
        list.push({ key: `${project.path}|${entry.name}`, text: `«${skillOf(entry)}» в «${name}»: папка библиотеки «${entry.library}» недоступна. Ссылки сохранены; подключение продолжится, когда папка вернётся.`, paths: [] });
      }
    }
  }
  // Global links belong to the copy that wins its name; another copy's links point elsewhere.
  for (const skill of status.skills) {
    if (skill.active === false) continue;
    const paths = conflictPaths(skill.global.links);
    if (!paths.length) continue;
    list.push({
      key: `global|${skill.name}`,
      text: skill.global.enabled
        ? `Глобальная установка «${skill.name}» занята чужой папкой или ссылкой.`
        : `«${skill.name}» остаётся доступен глобально через чужую установку; она сохранена.`,
      paths,
    });
  }
  return list;
}
