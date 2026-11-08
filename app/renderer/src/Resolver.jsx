// Three-pane merge in the spirit of WinMerge: both versions aligned on top, the editable result below.
import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { PICKS, alignBlocks, conflictSkills, editSegments, initialSegments, lines, segmentText } from './model.js';
import './merge.css';

const LINE = 20;
const STATUS = {
  mine: 'левая',
  library: 'правая',
  'mine-library': 'обе, левая первой',
  'library-mine': 'обе, правая первой',
  edited: 'правка вручную',
};
const KEYS = [
  ['Alt+↑ / Alt+↓', 'предыдущее / следующее различие'],
  ['Alt+Home / Alt+End', 'первое / последнее различие'],
  ['Alt+← / Alt+→', 'взять левую / правую'],
  ['Alt+Shift+← / Alt+Shift+→', 'обе: левая / правая первой'],
  ['Alt+Backspace', 'сбросить различие'],
  ['Ctrl+Alt+← / Ctrl+Alt+→', 'весь файл слева / справа'],
  ['Alt+PgUp / Alt+PgDn', 'предыдущий / следующий файл'],
  ['Ctrl+Enter', 'применить файл и открыть следующий; на последнем — применить всё'],
];

// Deleted and binary files have no lines to align; they are taken as a whole.
const wholeFile = (file) => !file.mine || !file.library || file.mine.binary || file.library.binary;
const trailingNewline = (file) => Boolean(file.mine?.text?.endsWith('\n') || file.library?.text?.endsWith('\n'));
const diffsOf = (blocks) => blocks.filter((block) => block.type === 'diff');
const fileName = (file) => file.path.slice(`skills/${file.skill}/`.length);

// The shared version decides the line ending when the sides disagree.
const lineEnding = (file) => ((file.library ?? file.mine)?.text?.includes('\r\n') ? '\r\n' : '\n');

function resultText(file, state) {
  let text = state.segments.join('');
  if (!trailingNewline(file) && text.endsWith('\n')) text = text.slice(0, -1);
  return lineEnding(file) === '\n' ? text : text.replace(/\n/g, '\r\n');
}

function resolved(file, state, blocks) {
  if (!state) return false;
  if (wholeFile(file)) return Boolean(state.take);
  return diffsOf(blocks).every((block) => state.status[block.index]);
}

function resolution(file, state) {
  const base = { path: file.path, mine: file.mine?.id ?? null, library: file.library?.id ?? null };
  if (wholeFile(file)) return { ...base, take: state.take };
  const text = resultText(file, state);
  // An untouched side is taken byte for byte rather than rebuilt from lines.
  if (text === file.mine.text) return { ...base, take: 'mine' };
  if (text === file.library.text) return { ...base, take: 'library' };
  return { ...base, take: 'edited', text };
}

function Keys({ children }) {
  return <kbd className="keys">{children}</kbd>;
}

function Action({ label, keys, onClick, disabled, pressed }) {
  return (
    <button type="button" className="action" disabled={disabled} aria-pressed={pressed}
      onClick={onClick}>
      <span>{label}</span><Keys>{keys}</Keys>
    </button>
  );
}

function DiffPanes({ blocks, status, current, onSelect }) {
  const scroller = useRef(null);
  useEffect(() => {
    scroller.current?.querySelector(`[data-block="${current}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [current]);
  let left = 0;
  let right = 0;
  const rows = [];
  for (const [blockKey, block] of blocks.entries()) {
    if (block.type === 'same') {
      for (const line of block.lines) {
        left += 1; right += 1;
        rows.push(
          <Fragment key={`${blockKey}-${left}`}>
            <span className="num">{left}</span><span className="code">{line}</span><span className="gutter" />
            <span className="num">{right}</span><span className="code">{line}</span>
          </Fragment>,
        );
      }
      continue;
    }
    const choice = status[block.index];
    const state = `${block.index === current ? ' current' : ''}${choice ? ` picked-${choice}` : ''}`;
    const height = Math.max(block.left.length, block.right.length);
    const select = () => onSelect(block.index);
    for (let row = 0; row < height; row += 1) {
      const l = block.left[row];
      const r = block.right[row];
      if (l !== undefined) left += 1;
      if (r !== undefined) right += 1;
      rows.push(
        <Fragment key={`${blockKey}-${row}`}>
          <span className={`num diff${state}`}>{l !== undefined ? left : ''}</span>
          <span className={`code diff side-mine${state}${l === undefined ? ' filler' : ''}`} onClick={select}>{l ?? ''}</span>
          {row === 0 ? (
            <button type="button" className={`gutter diff${state}`} data-block={block.index} style={{ gridRow: `span ${height}` }}
              aria-label={`Различие ${block.index + 1}: ${choice ? STATUS[choice] : 'не выбрано'}`} onClick={select}>
              <span className="hunk-no">{block.index + 1}</span>
              <span className="hunk-state">{choice ? STATUS[choice] : 'не выбрано'}</span>
            </button>
          ) : null}
          <span className={`num diff${state}`}>{r !== undefined ? right : ''}</span>
          <span className={`code diff side-library${state}${r === undefined ? ' filler' : ''}`} onClick={select}>{r ?? ''}</span>
        </Fragment>,
      );
    }
  }
  return (
    <section className="merge-top" aria-label="Сравнение версий">
      <div className="pane-titles">
        <span>Левая — на этом компьютере</span><span /><span>Правая — в общей библиотеке</span>
      </div>
      <div className="diff-grid" ref={scroller}>{rows}</div>
    </section>
  );
}

// A plain textarea over a layer of line backgrounds that show where each result line came from.
function ResultEditor({ file, blocks, state, current, onEdit, onSelect }) {
  const area = useRef(null);
  const [scroll, setScroll] = useState(0);
  const text = state.segments.join('');
  // Each line takes the look of the segment its first character belongs to.
  const segmentLook = (index) => {
    const block = blocks[index];
    if (block.type === 'same') return { kind: 'same', isCurrent: false };
    const choice = state.status[block.index];
    return { kind: choice ? `from-${choice}` : 'unresolved', isCurrent: block.index === current };
  };
  const starts = [];
  let offset = 0;
  for (const segment of state.segments) { starts.push(offset); offset += segment.length; }
  const lineOf = (position) => text.slice(0, position).split('\n').length - 1;
  const textLines = text.split('\n');
  const rows = [];
  let lineStart = 0;
  for (const line of textLines) {
    const index = state.segments.findIndex((segment, i) => starts[i] <= lineStart && lineStart < starts[i] + segment.length);
    rows.push(index === -1 ? { kind: 'same', isCurrent: false } : segmentLook(index));
    lineStart += line.length + 1;
  }
  // A difference whose result is empty is marked by a line between its neighbours.
  const gaps = state.segments.flatMap((segment, index) => (segment === '' && blocks[index].type === 'diff'
    ? [{ ...segmentLook(index), at: lineOf(starts[index]), key: index }] : []));
  const total = textLines.length;

  const startLine = (blockIndex) => {
    const position = blocks.findIndex((block) => block.type === 'diff' && block.index === blockIndex);
    return state.segments.slice(0, position).join('').split('\n').length - 1;
  };
  useEffect(() => {
    const node = area.current;
    if (!node) return;
    const top = startLine(current) * LINE;
    if (top < node.scrollTop || top > node.scrollTop + node.clientHeight - LINE * 3) node.scrollTop = Math.max(0, top - LINE * 3);
  }, [current, file.path]);

  // Moving the caret into a difference makes it the current one.
  const follow = () => {
    const caret = area.current?.selectionStart ?? 0;
    let offset = 0;
    for (const [index, segment] of state.segments.entries()) {
      offset += segment.length;
      if (caret < offset || index === state.segments.length - 1) {
        if (blocks[index].type === 'diff') onSelect(blocks[index].index);
        return;
      }
    }
  };

  return (
    <section className="merge-bottom" aria-label="Итог">
      <div className="pane-titles single">
        <span>Итог — так файл будет выглядеть здесь и в библиотеке. Его можно править прямо здесь</span>
        <span className="legend-chips">
          <span className="chip from-mine">левая</span><span className="chip from-library">правая</span>
          <span className="chip from-edited">вручную</span><span className="chip unresolved">не выбрано</span>
        </span>
      </div>
      <div className="result-editor">
        <div className="result-numbers" aria-hidden="true">
          <div style={{ transform: `translateY(${-scroll}px)` }}>
            {Array.from({ length: total }, (_, index) => (
              <span key={index} className={rows[index] ? `${rows[index].kind}${rows[index].isCurrent ? ' current' : ''}` : ''}>{index + 1}</span>
            ))}
          </div>
        </div>
        <div className="result-layer" aria-hidden="true">
          <div style={{ transform: `translateY(${-scroll}px)` }}>
            {Array.from({ length: total }, (_, index) => (
              <div key={index} className={`result-line ${rows[index]?.kind ?? 'same'}${rows[index]?.isCurrent ? ' current' : ''}`} />
            ))}
            {gaps.map((gap) => (
              <div key={gap.key} className={`result-gap ${gap.kind}${gap.isCurrent ? ' current' : ''}`}
                style={{ top: gap.at * LINE + 4 }} />
            ))}
          </div>
        </div>
        <textarea ref={area} className="result-text" wrap="off" spellCheck={false} value={text}
          aria-label={`Итоговый текст ${file.path}`}
          onChange={(event) => onEdit(event.target.value)}
          onScroll={(event) => setScroll(event.currentTarget.scrollTop)}
          onClick={follow} onKeyUp={follow} />
      </div>
    </section>
  );
}

function WholeFile({ file, state }) {
  const side = (key, title) => {
    const version = file[key];
    let body;
    if (!version) body = <p className="version-note">Файл удалён</p>;
    else if (version.binary) body = <p className="version-note">Двоичный файл, показать нельзя</p>;
    else body = <pre className="version-text">{lines(version.text).join('\n')}</pre>;
    return <section className="whole-side" aria-label={title}><h4>{title}</h4>{body}</section>;
  };
  const chosen = state?.take;
  const describe = (take) => {
    if (!take) return 'не выбран';
    const version = file[take];
    return version ? `версия ${take === 'mine' ? 'слева' : 'справа'}` : 'файл удаляется';
  };
  return (
    <>
      <div className="merge-top whole">
        {side('mine', 'Левая — на этом компьютере')}
        {side('library', 'Правая — в общей библиотеке')}
      </div>
      <section className="merge-bottom" aria-label="Итог">
        <div className="pane-titles single"><span>Итог — этот файл берётся целиком: {describe(chosen)}</span></div>
        <p className="whole-hint">Удалённый или двоичный файл нельзя слить по строкам. Выберите сторону кнопками
          «Взять левую» или «Взять правую» выше.</p>
      </section>
    </>
  );
}

export default function Resolver({ sync, onCancel, onApply }) {
  const files = sync.files;
  const [selected, setSelected] = useState(files[0].path);
  const blocksByPath = useMemo(() => Object.fromEntries(files.map((file) => [file.path,
    wholeFile(file) ? [] : alignBlocks(file.mine.text, file.library.text)])), [files]);
  const [states, setStates] = useState(() => Object.fromEntries(files.map((file) => [file.path,
    wholeFile(file) ? { take: null } : { segments: initialSegments(blocksByPath[file.path]), status: {} }])));
  const [currents, setCurrents] = useState({});
  const [touched, setTouched] = useState(false);

  const fileIndex = files.findIndex((item) => item.path === selected);
  const file = files[fileIndex];
  const blocks = blocksByPath[file.path];
  const diffs = diffsOf(blocks);
  const state = states[file.path];
  const whole = wholeFile(file);
  const current = currents[file.path] ?? 0;
  const done = files.filter((item) => resolved(item, states[item.path], blocksByPath[item.path])).length;
  const ready = done === files.length;

  const change = (update) => { setTouched(true); setStates((old) => ({ ...old, [file.path]: update(old[file.path]) })); };
  const select = (index) => setCurrents((old) => ({ ...old, [file.path]: Math.max(0, Math.min(diffs.length - 1, index)) }));
  const position = (index) => blocks.findIndex((block) => block.type === 'diff' && block.index === index);

  const take = (pick, index = current) => {
    if (whole) {
      if (pick === 'mine' || pick === 'library') change(() => ({ take: pick }));
      return;
    }
    const block = blocks[position(index)];
    const status = { ...state.status, [index]: pick };
    change((old) => {
      const segments = [...old.segments];
      segments[position(index)] = segmentText(PICKS[pick](block));
      return { segments, status: { ...old.status, [index]: pick } };
    });
    // Move on to the next difference still waiting for a choice, as merge tools do.
    const next = diffs.find((item) => item.index > index && !status[item.index]);
    if (next) select(next.index);
  };
  const reset = () => {
    if (whole) { change(() => ({ take: null })); return; }
    const block = blocks[position(current)];
    change((old) => {
      const segments = [...old.segments];
      segments[position(current)] = segmentText(block.left);
      const status = { ...old.status };
      delete status[current];
      return { segments, status };
    });
  };
  const takeAll = (pick) => {
    if (whole) { take(pick); return; }
    change(() => ({
      segments: blocks.map((block) => segmentText(block.type === 'same' ? block.lines : PICKS[pick](block))),
      status: Object.fromEntries(diffs.map((block) => [block.index, pick])),
    }));
  };
  const edit = (text) => change((old) => {
    const result = editSegments(old.segments, text);
    const status = { ...old.status };
    for (const index of result.touched) if (blocks[index].type === 'diff') status[blocks[index].index] = 'edited';
    return { segments: result.segments, status };
  });
  const openFile = (index) => setSelected(files[(index + files.length) % files.length].path);
  const apply = () => onApply({ upstream: sync.upstream, files: files.map((item) => resolution(item, states[item.path])) });
  const fileDone = resolved(file, state, blocks);
  // Only this file is left (or nothing is): its button saves everything, there is no next file.
  const lastToResolve = done - (fileDone ? 1 : 0) >= files.length - 1;
  // Applying a file moves on to the next one still waiting; the last one saves everything.
  const applyFile = () => {
    if (ready) { apply(); return; }
    if (!fileDone) return;
    const order = [...files.slice(fileIndex + 1), ...files.slice(0, fileIndex)];
    const next = order.find((item) => !resolved(item, states[item.path], blocksByPath[item.path]));
    if (next) setSelected(next.path);
  };
  const cancel = () => {
    if (!touched || window.confirm('Закрыть разбор? Сделанный выбор будет потерян, файлы не изменятся.')) onCancel();
  };

  const box = useRef(null);
  useEffect(() => { box.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event) => {
      const { key, altKey, ctrlKey, shiftKey } = event;
      let action = null;
      if (key === 'Escape') action = cancel;
      else if (ctrlKey && !altKey && key === 'Enter') action = applyFile;
      else if (altKey && ctrlKey && key === 'ArrowLeft') action = () => takeAll('mine');
      else if (altKey && ctrlKey && key === 'ArrowRight') action = () => takeAll('library');
      else if (altKey && !ctrlKey) {
        action = {
          ArrowLeft: () => take(shiftKey ? 'mine-library' : 'mine'),
          ArrowRight: () => take(shiftKey ? 'library-mine' : 'library'),
          ArrowDown: () => select(current + 1),
          ArrowUp: () => select(current - 1),
          Home: () => select(0),
          End: () => select(diffs.length - 1),
          Backspace: reset,
          PageDown: () => openFile(fileIndex + 1),
          PageUp: () => openFile(fileIndex - 1),
        }[key];
      }
      if (!action) return;
      event.preventDefault();
      action();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const choice = whole ? state.take : state.status[current];
  const both = !whole;
  return (
    <div className="backdrop">
      <div className="resolver merge" role="dialog" aria-modal="true" aria-labelledby="resolver-title" tabIndex={-1} ref={box}>
        <header className="merge-header">
          <h2 id="resolver-title">Разбор конфликта синхронизации</h2>
          {sync.title && <p className="merge-library">Библиотека «{sync.title}»</p>}
          <p>Левая — ваша версия, правая — версия общей библиотеки, внизу итог. Он заменит обе версии
            здесь и в библиотеке. «Применить» принимает итог файла и открывает следующий; файлы меняются
            только после «Применить всё».</p>
        </header>
        <nav className="merge-files" aria-label="Файлы с конфликтом">
          {conflictSkills(sync).map((name) => (
            <div key={name}>
              <h3>{name}</h3>
              {files.filter((item) => item.skill === name).map((item) => {
                const ok = resolved(item, states[item.path], blocksByPath[item.path]);
                return (
                  <button type="button" key={item.path} aria-current={item.path === selected ? 'true' : undefined}
                    className={`file-tab${item.path === selected ? ' active' : ''}${ok ? ' done' : ''}`}
                    onClick={() => setSelected(item.path)}>
                    <span className="mark" aria-hidden="true">{ok ? '✓' : '●'}</span>
                    <span className="file-name">{fileName(item)}</span>
                    <span className="visually-hidden">{ok ? ', итог выбран' : ', итог не выбран'}</span>
                  </button>
                );
              })}
            </div>
          ))}
          <dl className="merge-keys" aria-label="Горячие клавиши">
            {KEYS.map(([keys, text]) => <Fragment key={keys}><dt><Keys>{keys}</Keys></dt><dd>{text}</dd></Fragment>)}
          </dl>
        </nav>
        <main className="merge-main">
          <div className="merge-toolbar" role="toolbar" aria-label="Выбор версии">
            <div className="toolbar-line">
              <code className="merge-path">{file.path}</code>
              {both && (
                <>
                  <span className="merge-count">Различие {Math.min(current + 1, diffs.length)} из {diffs.length}:
                    {' '}<strong>{choice ? STATUS[choice] : 'не выбрано'}</strong></span>
                  <Action label="↑ Предыдущее" keys="Alt+↑" disabled={current <= 0} onClick={() => select(current - 1)} />
                  <Action label="↓ Следующее" keys="Alt+↓" disabled={current >= diffs.length - 1} onClick={() => select(current + 1)} />
                  <span className="merge-sep" />
                  <Action label="Весь файл слева" keys="Ctrl+Alt+←" onClick={() => takeAll('mine')} />
                  <Action label="Весь файл справа" keys="Ctrl+Alt+→" onClick={() => takeAll('library')} />
                </>
              )}
            </div>
            <div className="toolbar-line actions-line">
              <Action label="← Взять левую" keys="Alt+←" pressed={choice === 'mine'} onClick={() => take('mine')} />
              <Action label="Взять правую →" keys="Alt+→" pressed={choice === 'library'} onClick={() => take('library')} />
              {both && (
                <>
                  <Action label="Обе: сначала левая" keys="Alt+Shift+←" pressed={choice === 'mine-library'} onClick={() => take('mine-library')} />
                  <Action label="Обе: сначала правая" keys="Alt+Shift+→" pressed={choice === 'library-mine'} onClick={() => take('library-mine')} />
                </>
              )}
              <Action label="Сбросить" keys="Alt+Backspace" disabled={!choice} onClick={reset} />
            </div>
          </div>
          {whole ? (
            <WholeFile file={file} state={state} />
          ) : (
            <>
              <DiffPanes blocks={blocks} status={state.status} current={current} onSelect={select} />
              <ResultEditor file={file} blocks={blocks} state={state} current={current} onEdit={edit} onSelect={select} />
            </>
          )}
        </main>
        <footer className="merge-footer">
          <span className="progress">Итог выбран для {done} из {files.length} файлов</span>
          {files.length > 1 && (
            <>
              <Action label="Предыдущий файл" keys="Alt+PgUp" onClick={() => openFile(fileIndex - 1)} />
              <Action label="Следующий файл" keys="Alt+PgDn" onClick={() => openFile(fileIndex + 1)} />
              <span className="merge-sep" />
            </>
          )}
          <Action label="Отмена" keys="Esc" onClick={cancel} />
          <button type="button" className="primary apply" disabled={!ready && !fileDone} onClick={applyFile}>
            {lastToResolve ? 'Применить всё' : 'Применить и к следующему файлу'} <Keys>Ctrl+Enter</Keys>
          </button>
        </footer>
      </div>
    </div>
  );
}
