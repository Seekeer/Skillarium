import { useCallback, useEffect, useRef, useState } from 'react';
import { CELL_TEXT, basename, buildMatrix, cellOf, changedLines, conflictSkills, globalCaption, lines, parent, problems } from './model.js';

const api = window.skills;

function Modal({ title, children, onCancel }) {
  const box = useRef(null);
  useEffect(() => {
    box.current?.querySelector('[data-autofocus]')?.focus();
    const onKey = (event) => { if (event.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);
  return (
    <div className="backdrop">
      <div className="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title" ref={box}>
        <h2 id="modal-title">{title}</h2>
        {children}
      </div>
    </div>
  );
}

function GlobalSwitch({ row, busy, onToggle }) {
  const { enabled } = row.global;
  return (
    <div className="global-cell">
      <button type="button" role="switch" aria-checked={enabled} className="switch" disabled={busy}
        aria-label={`Глобальный доступ: ${row.name}`} onClick={onToggle}>
        <span className="switch-knob" />
      </button>
      <span className="global-caption">{globalCaption(row.global)}</span>
    </div>
  );
}

function Socket({ row, project, entry, busy, onToggle }) {
  const state = entry ? entry.state : 'none';
  const blocked = busy || !project.available || Boolean(project.error) || (!row.inLibrary && !entry);
  const label = `${row.name} в ${basename(project.path)}: ${entry ? CELL_TEXT[state] : 'не подключён'}`;
  return (
    <button type="button" className="socket" data-state={state} aria-pressed={Boolean(entry)} aria-label={label}
      title={entry ? `${CELL_TEXT[state]}. Нажмите, чтобы отключить` : 'Подключить к проекту'}
      disabled={blocked} onClick={onToggle}>
      <span className="socket-mark" aria-hidden="true">{state === 'conflict' || state === 'missing-skill' ? '!' : ''}</span>
    </button>
  );
}

function Version({ title, side, changed }) {
  let body;
  if (!side) body = <p className="version-note">Файл удалён</p>;
  else if (side.binary) body = <p className="version-note">Двоичный файл, показать нельзя</p>;
  else {
    body = (
      <pre className="version-text">
        {lines(side.text).map((line, index) => (
          <span key={index} className={changed[index] ? 'line changed' : 'line'}>{line || ' '}</span>
        ))}
      </pre>
    );
  }
  return (
    <section className="version" aria-label={title}>
      <h4>{title}</h4>
      {body}
    </section>
  );
}

function ConflictFile({ file, choice, onChoose }) {
  const diff = changedLines(file.mine?.text, file.library?.text);
  const editable = !(file.mine?.binary || file.library?.binary);
  const option = (take, label) => (
    <label className={`take${choice?.take === take ? ' picked' : ''}`}>
      <input type="radio" name={file.path} checked={choice?.take === take}
        onChange={() => onChoose({ take, text: take === 'edited' ? (choice?.text ?? file.mine?.text ?? file.library?.text ?? '') : undefined })} />
      {label}
    </label>
  );
  return (
    <li className="conflict-file">
      <h3><code>{file.path.slice(`skills/${file.skill}/`.length)}</code></h3>
      <div className="versions">
        <Version title="На этом компьютере" side={file.mine} changed={diff.left} />
        <Version title="В общей библиотеке" side={file.library} changed={diff.right} />
      </div>
      <div className="takes" role="radiogroup" aria-label={`Итог для ${file.path}`}>
        {option('mine', file.mine ? 'Оставить мою версию' : 'Удалить, как у меня')}
        {option('library', file.library ? 'Взять версию библиотеки' : 'Удалить, как в библиотеке')}
        {editable && option('edited', 'Свой вариант')}
      </div>
      {choice?.take === 'edited' && (
        <textarea className="merged" aria-label={`Итоговый текст ${file.path}`} spellCheck={false}
          rows={Math.min(18, Math.max(6, lines(choice.text).length + 1))}
          value={choice.text} onChange={(event) => onChoose({ take: 'edited', text: event.target.value })} />
      )}
    </li>
  );
}

function Resolver({ sync, onCancel, onApply }) {
  const [choices, setChoices] = useState({});
  const box = useRef(null);
  useEffect(() => {
    box.current?.focus();
    const onKey = (event) => { if (event.key === 'Escape') onCancel(); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onCancel]);
  const files = sync.files;
  const ready = files.every((file) => choices[file.path]);
  const apply = () => onApply({
    upstream: sync.upstream,
    files: files.map((file) => ({ path: file.path, mine: file.mine?.id ?? null, library: file.library?.id ?? null, ...choices[file.path] })),
  });
  return (
    <div className="backdrop">
      <div className="resolver" role="dialog" aria-modal="true" aria-labelledby="resolver-title" tabIndex={-1} ref={box}>
        <header>
          <h2 id="resolver-title">Разбор конфликта синхронизации</h2>
          <p>Эти файлы изменили и здесь, и в общей библиотеке. Выберите итог для каждого: он заменит обе версии
            здесь и в библиотеке. До нажатия «Применить» ничего не меняется.</p>
        </header>
        <div className="resolver-body">
          {conflictSkills(sync).map((name) => (
            <section key={name} aria-label={`Скилл ${name}`}>
              <h3 className="resolver-skill">{name}</h3>
              <ul>
                {files.filter((file) => file.skill === name).map((file) => (
                  <ConflictFile key={file.path} file={file} choice={choices[file.path]}
                    onChoose={(choice) => setChoices((old) => ({ ...old, [file.path]: choice }))} />
                ))}
              </ul>
            </section>
          ))}
        </div>
        <footer className="modal-actions">
          <span className="progress">Выбрано {files.filter((file) => choices[file.path]).length} из {files.length}</span>
          <button type="button" className="quiet" onClick={onCancel}>Отмена</button>
          <button type="button" className="primary" disabled={!ready} onClick={apply}>Применить</button>
        </footer>
      </div>
    </div>
  );
}

function SyncBanner({ sync, busy, onOpen }) {
  if (sync?.state === 'error') {
    return (
      <div className="sync-banner" role="region" aria-label="Синхронизация библиотеки">
        <p><strong>Не удалось проверить синхронизацию библиотеки.</strong></p>
        {sync.messages?.length > 0 && <pre>{sync.messages.join('\n')}</pre>}
      </div>
    );
  }
  if (sync?.state !== 'conflict') return null;
  const names = conflictSkills(sync).map((name) => `«${name}»`).join(', ');
  return (
    <div className="sync-banner" role="region" aria-label="Синхронизация библиотеки">
      <div>
        <p><strong>Синхронизация библиотеки остановлена.</strong> Правки {names} на этом компьютере расходятся
          с общей библиотекой.</p>
        <p className="sub">Обе версии сохранены. Пока вы не выберете итог, обновления скиллов не приходят,
          а ваши правки не публикуются.</p>
      </div>
      <button type="button" className="primary" disabled={busy} onClick={onOpen}>Разобрать конфликт</button>
    </div>
  );
}

function Setup({ library, onChoose }) {
  return (
    <main className="setup">
      <h1>Скиллы в проектах</h1>
      <p>В папке {library.path} нет библиотеки скиллов. Укажите рабочую копию agent-skills: в ней лежат sources.json и папка skills.</p>
      <button type="button" className="primary" onClick={onChoose}>Выбрать папку библиотеки</button>
    </main>
  );
}

export default function App() {
  const [library, setLibrary] = useState(null);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [decision, setDecision] = useState(null);
  const [removing, setRemoving] = useState(null);
  const [sync, setSync] = useState(null);
  const [resolving, setResolving] = useState(null);

  const refresh = useCallback(async () => {
    const [result, conflicts] = await Promise.all([api.status(), api.conflicts()]);
    if (result.code === 0 && result.json) setStatus(result.json);
    else setNotice({ tone: 'error', text: 'Не удалось прочитать состояние подключений.', details: result.messages });
    if (conflicts.code === 0 && conflicts.json) setSync(conflicts.json);
    else setSync({ state: 'error', messages: conflicts.messages });
  }, []);

  useEffect(() => { api.library().then(setLibrary); }, []);
  useEffect(() => {
    if (!library?.valid) return undefined;
    refresh();
    // Background synchronization and branch switches change links outside the app.
    const timer = setInterval(refresh, 60_000);
    window.addEventListener('focus', refresh);
    return () => { clearInterval(timer); window.removeEventListener('focus', refresh); };
  }, [library, refresh]);

  async function act(key, call, handle) {
    setBusy(key);
    setNotice(null);
    try {
      const result = await call();
      if (result) handle(result);
    } catch (error) {
      setNotice({ tone: 'error', text: String(error?.message || error) });
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  function report(result, unapplied) {
    if (result.code === 0) return;
    if (result.code === 4 && unapplied) setNotice({ tone: 'warn', text: unapplied });
    else setNotice({ tone: 'error', text: 'Действие не выполнено, файлы не изменены.', details: result.messages });
  }

  async function chooseLibrary() {
    const picked = await api.chooseLibrary();
    if (picked) setLibrary(picked);
  }

  const bind = (project, name) => act(`${project.path}|${name}`, () => api.bind(project.path, name),
    (result) => report(result, `«${name}» добавлен в список «${basename(project.path)}», но не подключён: место занято. Подробности ниже.`));

  const unbind = (project, name, choice) => act(`${project.path}|${name}`, () => api.unbind(project.path, name, choice),
    (result) => {
      if (result.code === 3 && result.json) setDecision({ project, name, global: result.json.global });
      else report(result);
    });

  const toggleGlobal = (row) => act(`global|${row.name}`, () => api.setGlobal(row.name, !row.global.enabled), (result) => report(result));
  const register = () => act('register', () => api.register(),
    (result) => report(result, 'Проект добавлен, но часть скиллов из его списка не подключена. Подробности ниже.'));
  const resolve = (request) => {
    setResolving(null);
    act('resolve', () => api.resolve(request), (result) => {
      if (result.code === 0) {
        setNotice({ tone: 'info', text: result.json?.pushed
          ? 'Конфликт разрешён, итог опубликован в общей библиотеке. Синхронизация снова работает.'
          : 'Конфликт разрешён и сохранён здесь. В библиотеку итог отправится при следующей синхронизации.' });
      } else if (result.code === 5) {
        setNotice({ tone: 'warn', text: 'Пока вы выбирали, версии изменились. Ничего не применено; разберите конфликт заново.' });
      } else report(result);
    });
  };

  if (!library) return null;
  if (!library.valid) return <Setup library={library} onChoose={chooseLibrary} />;

  const matrix = status ? buildMatrix(status) : null;
  const issues = status ? problems(status) : [];
  const removingProject = removing && status?.projects.find((project) => project.path === removing);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="title">
          <h1>Скиллы в проектах</h1>
          <p className="library">
            Библиотека: <span title={library.path}>{library.path}</span>
            <button type="button" className="quiet" onClick={chooseLibrary}>Сменить</button>
          </p>
        </div>
        <div className="actions">
          {busy && <span className="working" role="status">Применяю…</span>}
          <button type="button" className="quiet" onClick={() => act('refresh', async () => null, () => {})} disabled={Boolean(busy)}>Обновить</button>
          <button type="button" className="primary" onClick={register} disabled={Boolean(busy)}>Добавить проект</button>
        </div>
      </header>

      <SyncBanner sync={sync} busy={Boolean(busy)} onOpen={() => setResolving(sync)} />

      {notice && (
        <div className={`notice ${notice.tone}`} role="alert">
          <p>{notice.text}</p>
          {notice.details?.length > 0 && <pre>{notice.details.join('\n')}</pre>}
          <button type="button" className="quiet" onClick={() => setNotice(null)}>Скрыть</button>
        </div>
      )}

      {!matrix ? <p className="loading">Читаю подключения…</p> : (
        <main className="board">
          <div className="matrix-scroll">
            <table className="matrix">
              <thead>
                <tr>
                  <th scope="col" className="skill-col">Скилл</th>
                  <th scope="col" className="global-col">Глобально<span className="sub">в папках пользователя</span></th>
                  {matrix.projects.map((project) => (
                    <th scope="col" key={project.path} className={`project-col${project.available ? '' : ' offline'}`}>
                      <span className="project-name" title={project.path}>{basename(project.path)}</span>
                      <span className="sub" title={project.path}>{parent(project.path)}</span>
                      {!project.available && <span className="tag">папка недоступна</span>}
                      {project.error && <span className="tag warn">список не читается</span>}
                      <button type="button" className="quiet small" disabled={Boolean(busy)}
                        aria-label={`Убрать проект ${basename(project.path)}`} onClick={() => setRemoving(project.path)}>Убрать</button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {matrix.rows.map((row) => (
                  <tr key={row.name}>
                    <th scope="row" className="skill-col">
                      <span className="skill-name">{row.name}</span>
                      {!row.inLibrary && <span className="tag warn">нет в библиотеке</span>}
                    </th>
                    <td className="global-col">
                      {row.global ? <GlobalSwitch row={row} busy={Boolean(busy)} onToggle={() => toggleGlobal(row)} /> : null}
                    </td>
                    {matrix.projects.map((project) => {
                      const entry = cellOf(project, row.name);
                      return (
                        <td key={project.path} className="project-cell">
                          <Socket row={row} project={project} entry={entry} busy={Boolean(busy)}
                            onToggle={() => (entry ? unbind(project, row.name) : bind(project, row.name))} />
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
            {matrix.projects.length === 0 && (
              <div className="empty">
                <p>Проектов пока нет. Добавьте папку проекта, чтобы подключать к ней скиллы библиотеки.
                  Без подключений скиллы доступны глобально, как и раньше.</p>
                <button type="button" className="primary" onClick={register} disabled={Boolean(busy)}>Добавить проект</button>
              </div>
            )}
          </div>

          <aside className="side">
            {issues.length > 0 && (
              <section className="issues" aria-labelledby="issues-title">
                <h2 id="issues-title">Требует внимания</h2>
                <ul>
                  {issues.map((issue) => (
                    <li key={issue.key}>
                      <p>{issue.text}</p>
                      {issue.paths.map((path) => <code key={path}>{path}</code>)}
                    </li>
                  ))}
                </ul>
              </section>
            )}
            <section className="legend" aria-labelledby="legend-title">
              <h2 id="legend-title">Обозначения</h2>
              <ul>
                <li><span className="socket" data-state="applied" aria-hidden="true" /> подключён: проект видит общую папку скилла</li>
                <li><span className="socket" data-state="pending" aria-hidden="true" /> в списке проекта, ссылки ещё создаются</li>
                <li><span className="socket" data-state="conflict" aria-hidden="true"><span className="socket-mark">!</span></span> в списке, но место занято чужим содержимым</li>
                <li><span className="socket" data-state="unavailable" aria-hidden="true" /> в списке, но папка проекта недоступна</li>
                <li><span className="socket" data-state="none" aria-hidden="true" /> не подключён</li>
              </ul>
              <p>Правка скилла через любой проект сразу видна во всех проектах этого компьютера. Список скиллов
                проекта хранится в его файле .agent-skills.json; закоммитьте его, когда будете готовы.</p>
            </section>
          </aside>
        </main>
      )}

      {decision && (
        <Modal title={`Отключить «${decision.name}» от последнего проекта?`} onCancel={() => setDecision(null)}>
          <p>После этого «{decision.name}» не останется ни в одном проекте. Сейчас глобальный доступ
            {decision.global.enabled ? ' включён' : ' выключен'}. Где скилл должен быть доступен дальше?</p>
          <div className="choices">
            <button type="button" className="choice" onClick={() => { const d = decision; setDecision(null); unbind(d.project, d.name, 'global'); }}>
              <strong>Доступен глобально</strong>
              <span>Агенты увидят скилл в любой папке через профиль пользователя.</span>
            </button>
            <button type="button" className="choice" onClick={() => { const d = decision; setDecision(null); unbind(d.project, d.name, 'library'); }}>
              <strong>Оставить только в библиотеке</strong>
              <span>Скилл сохранится в библиотеке; агенты его не увидят, пока вы его не подключите.</span>
            </button>
          </div>
          <div className="modal-actions">
            <button type="button" className="quiet" data-autofocus onClick={() => setDecision(null)}>Отмена, оставить подключение</button>
          </div>
        </Modal>
      )}

      {resolving && <Resolver sync={resolving} onCancel={() => setResolving(null)} onApply={resolve} />}

      {removingProject && (
        <Modal title={`Убрать проект «${basename(removingProject.path)}»?`} onCancel={() => setRemoving(null)}>
          <p>Ссылки на скиллы в этой папке будут удалены. Файл .agent-skills.json и сами скиллы останутся,
            глобальный доступ не изменится. Проект можно добавить снова.</p>
          <div className="modal-actions">
            <button type="button" className="quiet" data-autofocus onClick={() => setRemoving(null)}>Отмена</button>
            <button type="button" className="danger" onClick={() => {
              const path = removingProject.path;
              setRemoving(null);
              act(`remove|${path}`, () => api.unregister(path), (result) => report(result));
            }}>Убрать проект</button>
          </div>
        </Modal>
      )}
    </div>
  );
}
