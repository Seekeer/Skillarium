// Three-pane merge in the spirit of WinMerge: both versions aligned on top, the result below.
import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { alignBlocks, assemble, conflictSkills, lines } from './model.js';
import './merge.css';

const SIDES = { mine: 'На этом компьютере', library: 'В общей библиотеке' };

// Deleted and binary files have no lines to align; they are taken as a whole.
const wholeFile = (file) => !file.mine || !file.library || file.mine.binary || file.library.binary;
const trailingNewline = (file) => Boolean(file.mine?.text?.endsWith('\n') || file.library?.text?.endsWith('\n'));
const diffsOf = (blocks) => blocks.filter((block) => block.type === 'diff');
const fileName = (file) => file.path.slice(`skills/${file.skill}/`.length);

function resolved(file, state, blocks) {
  if (!state) return false;
  if (wholeFile(file)) return Boolean(state.take);
  return state.manual != null || diffsOf(blocks).every((block) => state.picks?.[block.index]);
}

function resolution(file, state, blocks) {
  const base = { path: file.path, mine: file.mine?.id ?? null, library: file.library?.id ?? null };
  if (wholeFile(file)) return { ...base, take: state.take };
  if (state.manual != null) return { ...base, take: 'edited', text: state.manual };
  const picks = diffsOf(blocks).map((block) => state.picks[block.index]);
  // Untouched versions are taken byte for byte rather than rebuilt from lines.
  if (picks.every((pick) => pick === 'mine')) return { ...base, take: 'mine' };
  if (picks.every((pick) => pick === 'library')) return { ...base, take: 'library' };
  return { ...base, take: 'edited', text: assemble(blocks, state.picks, trailingNewline(file)) };
}

function DiffPanes({ blocks, picks, current, locked, onSelect, onPick }) {
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
    const pick = picks[block.index];
    const state = `${block.index === current ? ' current' : ''}${pick ? ` picked-${pick}` : ''}`;
    const height = Math.max(block.left.length, block.right.length);
    for (let row = 0; row < height; row += 1) {
      const l = block.left[row];
      const r = block.right[row];
      if (l !== undefined) left += 1;
      if (r !== undefined) right += 1;
      const select = () => onSelect(block.index);
      rows.push(
        <Fragment key={`${blockKey}-${row}`}>
          <span className={`num diff${state}`}>{l !== undefined ? left : ''}</span>
          <span className={`code diff side-mine${state}${l === undefined ? ' filler' : ''}`} onClick={select}>{l ?? ''}</span>
          {row === 0 ? (
            <span className={`gutter diff${state}`} data-block={block.index} style={{ gridRow: `span ${height}` }}>
              <span className="hunk-no">{block.index + 1}</span>
              <button type="button" disabled={locked} aria-pressed={pick === 'mine'} title="Взять левую версию (Alt+←)"
                aria-label={`Различие ${block.index + 1}: взять мою версию`} onClick={() => onPick(block.index, 'mine')}>◀</button>
              <button type="button" disabled={locked} aria-pressed={pick === 'both'} title="Взять обе: сначала левую"
                aria-label={`Различие ${block.index + 1}: взять обе версии`} onClick={() => onPick(block.index, 'both')}>◆</button>
              <button type="button" disabled={locked} aria-pressed={pick === 'library'} title="Взять правую версию (Alt+→)"
                aria-label={`Различие ${block.index + 1}: взять версию библиотеки`} onClick={() => onPick(block.index, 'library')}>▶</button>
            </span>
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
        <span>{SIDES.mine}</span><span /><span>{SIDES.library}</span>
      </div>
      <div className="diff-grid" ref={scroller}>{rows}</div>
    </section>
  );
}

function ResultPane({ file, blocks, state, current, onSelect, onManual }) {
  const scroller = useRef(null);
  useEffect(() => {
    scroller.current?.querySelector(`[data-block="${current}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [current]);
  const manual = state?.manual;
  const picks = state?.picks || {};
  let body;
  if (manual != null) {
    body = (
      <textarea className="result-edit" aria-label={`Итоговый текст ${file.path}`} spellCheck={false}
        value={manual} onChange={(event) => onManual(event.target.value)} />
    );
  } else {
    let number = 0;
    const rows = [];
    for (const [blockKey, block] of blocks.entries()) {
      if (block.type === 'same') {
        for (const line of block.lines) {
          number += 1;
          rows.push(<Fragment key={`${blockKey}-${number}`}><span className="num">{number}</span><span className="code">{line}</span></Fragment>);
        }
        continue;
      }
      const pick = picks[block.index];
      const current_ = block.index === current ? ' current' : '';
      if (!pick) {
        rows.push(
          <Fragment key={`${blockKey}-open`}>
            <span className={`num unresolved${current_}`} data-block={block.index} />
            <button type="button" className={`code unresolved${current_}`} onClick={() => onSelect(block.index)}>
              Различие {block.index + 1} не выбрано: возьмите версию слева, справа или обе
            </button>
          </Fragment>,
        );
        continue;
      }
      const taken = [
        ...(pick === 'mine' || pick === 'both' ? block.left.map((line) => ['mine', line]) : []),
        ...(pick === 'library' || pick === 'both' ? block.right.map((line) => ['library', line]) : []),
      ];
      if (!taken.length) {
        rows.push(
          <Fragment key={`${blockKey}-empty`}>
            <span className={`num${current_}`} data-block={block.index} />
            <span className={`code removed${current_}`} onClick={() => onSelect(block.index)}>строки удалены</span>
          </Fragment>,
        );
      }
      for (const [row, [side, line]] of taken.entries()) {
        number += 1;
        rows.push(
          <Fragment key={`${blockKey}-${row}`}>
            <span className={`num from-${side}${current_}`} data-block={row === 0 ? block.index : undefined}>{number}</span>
            <span className={`code from-${side}${current_}`} onClick={() => onSelect(block.index)}>{line}</span>
          </Fragment>,
        );
      }
    }
    body = <div className="result-grid" ref={scroller}>{rows}</div>;
  }
  return (
    <section className="merge-bottom" aria-label="Итог">
      <div className="pane-titles single">
        <span>Итог — так файл будет выглядеть здесь и в библиотеке</span>
        {manual != null ? (
          <button type="button" className="quiet small" onClick={() => onManual(null)}>Вернуть выбор по различиям</button>
        ) : (
          <button type="button" className="quiet small"
            onClick={() => onManual(assemble(blocks, picks, trailingNewline(file), 'mine'))}>Редактировать вручную</button>
        )}
      </div>
      {body}
    </section>
  );
}

function WholeFile({ file, state, onTake }) {
  const side = (key) => {
    const version = file[key];
    let body;
    if (!version) body = <p className="version-note">Файл удалён</p>;
    else if (version.binary) body = <p className="version-note">Двоичный файл, показать нельзя</p>;
    else body = <pre className="version-text">{lines(version.text).join('\n')}</pre>;
    return <section className="whole-side" aria-label={SIDES[key]}><h4>{SIDES[key]}</h4>{body}</section>;
  };
  const option = (take, label) => (
    <label className={`take${state?.take === take ? ' picked' : ''}`}>
      <input type="radio" name={file.path} checked={state?.take === take} onChange={() => onTake(take)} />
      {label}
    </label>
  );
  return (
    <>
      <div className="merge-top whole">{side('mine')}{side('library')}</div>
      <section className="merge-bottom" aria-label="Итог">
        <div className="pane-titles single"><span>Итог — этот файл берётся целиком</span></div>
        <div className="takes" role="radiogroup" aria-label={`Итог для ${file.path}`}>
          {option('mine', file.mine ? 'Оставить мою версию' : 'Удалить, как у меня')}
          {option('library', file.library ? 'Взять версию библиотеки' : 'Удалить, как в библиотеке')}
        </div>
      </section>
    </>
  );
}

export default function Resolver({ sync, onCancel, onApply }) {
  const files = sync.files;
  const [selected, setSelected] = useState(files[0].path);
  const [states, setStates] = useState({});
  const [currents, setCurrents] = useState({});
  const blocksByPath = useMemo(() => Object.fromEntries(files.map((file) => [file.path,
    wholeFile(file) ? [] : alignBlocks(file.mine.text, file.library.text)])), [files]);
  const file = files.find((item) => item.path === selected);
  const blocks = blocksByPath[file.path];
  const diffs = diffsOf(blocks);
  const state = states[file.path];
  const current = currents[file.path] ?? 0;
  const done = files.filter((item) => resolved(item, states[item.path], blocksByPath[item.path])).length;

  const update = (path, change) => setStates((old) => ({ ...old, [path]: change(old[path] || { picks: {} }) }));
  const select = (index) => setCurrents((old) => ({ ...old, [file.path]: Math.max(0, Math.min(diffs.length - 1, index)) }));
  const pick = (index, choice) => {
    update(file.path, (old) => ({ ...old, picks: { ...old.picks, [index]: choice } }));
    // Move on to the next difference still waiting for a choice, as merge tools do.
    const next = diffs.find((block) => block.index > index && !state?.picks?.[block.index]);
    select(next ? next.index : index);
  };
  const pickAll = (choice) => update(file.path, () => ({ picks: Object.fromEntries(diffs.map((block) => [block.index, choice])) }));

  const box = useRef(null);
  useEffect(() => { box.current?.focus(); }, []);
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') { onCancel(); return; }
      if (!event.altKey || wholeFile(file) || event.target.tagName === 'TEXTAREA') return;
      const actions = {
        ArrowDown: () => select(current + 1),
        ArrowUp: () => select(current - 1),
        ArrowLeft: () => state?.manual == null && pick(current, 'mine'),
        ArrowRight: () => state?.manual == null && pick(current, 'library'),
      };
      if (actions[event.key]) { event.preventDefault(); actions[event.key](); }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  });

  const apply = () => onApply({
    upstream: sync.upstream,
    files: files.map((item) => resolution(item, states[item.path], blocksByPath[item.path])),
  });

  return (
    <div className="backdrop">
      <div className="resolver merge" role="dialog" aria-modal="true" aria-labelledby="resolver-title" tabIndex={-1} ref={box}>
        <header className="merge-header">
          <div>
            <h2 id="resolver-title">Разбор конфликта синхронизации</h2>
            <p>Слева ваша версия, справа — общей библиотеки, внизу итог. Он заменит обе версии
              здесь и в библиотеке; до нажатия «Применить» ничего не меняется.</p>
          </div>
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
        </nav>
        <main className="merge-main">
          <div className="merge-toolbar">
            <code className="merge-path">{file.path}</code>
            {!wholeFile(file) && (
              <>
                <span className="merge-count">Различие {Math.min(current + 1, diffs.length)} из {diffs.length}</span>
                <button type="button" className="quiet small" aria-label="Предыдущее различие" title="Alt+↑"
                  disabled={current <= 0} onClick={() => select(current - 1)}>↑</button>
                <button type="button" className="quiet small" aria-label="Следующее различие" title="Alt+↓"
                  disabled={current >= diffs.length - 1} onClick={() => select(current + 1)}>↓</button>
                <span className="merge-sep" />
                <button type="button" className="quiet small" disabled={state?.manual != null} onClick={() => pickAll('mine')}>Всё слева</button>
                <button type="button" className="quiet small" disabled={state?.manual != null} onClick={() => pickAll('library')}>Всё справа</button>
              </>
            )}
          </div>
          {wholeFile(file) ? (
            <WholeFile file={file} state={state} onTake={(take) => update(file.path, () => ({ take }))} />
          ) : (
            <>
              <DiffPanes blocks={blocks} picks={state?.picks || {}} current={current} locked={state?.manual != null}
                onSelect={select} onPick={pick} />
              <ResultPane file={file} blocks={blocks} state={state} current={current} onSelect={select}
                onManual={(text) => update(file.path, (old) => ({ ...old, manual: text }))} />
            </>
          )}
        </main>
        <footer className="merge-footer">
          <span className="progress">Итог выбран для {done} из {files.length} файлов</span>
          <button type="button" className="quiet" onClick={onCancel}>Отмена</button>
          <button type="button" className="primary" disabled={done < files.length} onClick={apply}>Применить</button>
        </footer>
      </div>
    </div>
  );
}
