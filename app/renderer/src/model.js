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
