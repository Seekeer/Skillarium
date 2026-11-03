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
  unavailable: 'папка недоступна',
};

export function buildMatrix(status) {
  const rows = status.skills.map((skill) => ({ name: skill.name, inLibrary: true, global: skill.global }));
  const known = new Set(rows.map((row) => row.name));
  for (const project of status.projects) {
    for (const entry of project.skills) {
      if (!known.has(entry.name)) {
        known.add(entry.name);
        rows.push({ name: entry.name, inLibrary: false, global: null });
      }
    }
  }
  rows.sort((left, right) => (left.name < right.name ? -1 : left.name > right.name ? 1 : 0));
  return { rows, projects: status.projects };
}

export function cellOf(project, name) {
  return project.skills.find((entry) => entry.name === name) || null;
}

export function globalCaption(global) {
  if (global.manual) return global.enabled ? 'Включено вручную' : 'Выключено вручную';
  if (global.pending) return 'Выключится после подключения';
  return global.enabled ? 'По умолчанию' : 'Выключено первым подключением';
}

export function lines(text) {
  if (text === null || text === undefined) return [];
  const list = text.split('\n');
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

// Joins common lines with the version picked for each difference; unpicked ones use `fallback`.
export function assemble(blocks, picks, trailingNewline, fallback = null) {
  const out = [];
  for (const block of blocks) {
    if (block.type === 'same') { out.push(...block.lines); continue; }
    const pick = picks[block.index] || fallback;
    if (pick === 'mine' || pick === 'both') out.push(...block.left);
    if (pick === 'library' || pick === 'both') out.push(...block.right);
  }
  return out.join('\n') + (trailingNewline && out.length ? '\n' : '');
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
      }
    }
  }
  for (const skill of status.skills) {
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
