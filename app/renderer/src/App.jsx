import { useCallback, useEffect, useRef, useState } from 'react';
import { CELL_TEXT, basename, buildMatrix, cellOf, conflictSkills, globalCaption, parent, problems } from './model.js';
import Resolver from './Resolver.jsx';
import mark from '../../../assets/brand/skillarium-icon.svg';

const api = window.skills;

// Electron prefixes errors of the main process; the user needs only the reason.
const reason = (error) => String(error?.message || error).replace(/^Error invoking remote method '[^']+': (Error: )?/, '');

// Known failures of the client setup, said in plain words; the raw lines stay below.
function explainConnect(messages) {
  const text = messages.join('\n');
  if (/answered 401|rejected the token/.test(text)) return 'Gitea не принял токен. Проверьте, что он скопирован целиком и не истёк.';
  if (/cannot write to/.test(text)) return 'У этого токена нет права записи в библиотеку. Попросите токен служебного бота или доступ на запись.';
  if (/answered 404|cannot read/.test(text)) return 'Библиотека по этому адресу не найдена (или токен её не видит). Проверьте адрес.';
  if (/is a clone of|exists and is not a clone/.test(text)) return 'Выбранная папка уже занята другим содержимым. Укажите пустую или новую папку.';
  if (/is not a skill library/.test(text)) return 'По этому адресу репозиторий, но не библиотека скиллов.';
  if (/Unable to connect|could not be resolved|remote name|No connection|timed out|actively refused|answered [^0-9]/i.test(text)) {
    return 'Сервер Gitea по этому адресу не отвечает. Проверьте адрес и сеть.';
  }
  return 'Подключить библиотеку не удалось.';
}

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

// First run: connect this machine to a library in Gitea, or point at an existing clone.
function Connect({ library, onConnected, onChoose }) {
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');
  const [folder, setFolder] = useState('');
  const [working, setWorking] = useState(false);
  const [failure, setFailure] = useState(null);
  useEffect(() => { api.defaultFolder().then((value) => setFolder((current) => current || value)); }, []);

  async function submit(event) {
    event.preventDefault();
    setWorking(true);
    setFailure(null);
    try {
      const result = await api.connect({ url, token, folder });
      if (result.code === 0) {
        setToken('');
        onConnected();
      } else if (result.code === 2) {
        setFailure({ title: 'Проверьте поля формы.', details: result.messages });
      } else {
        const details = result.messages.map((line) => line.replace(/^ERROR: /, ''));
        setFailure({ title: explainConnect(details), details });
      }
    } catch (error) {
      setFailure({ title: 'Подключить библиотеку не удалось.', details: [reason(error)] });
    } finally {
      setWorking(false);
    }
  }

  async function pickFolder() {
    const picked = await api.chooseFolder();
    if (picked) setFolder(picked);
  }

  return (
    <main className="setup">
      <img className="brand-mark" src={mark} alt="" />
      <h1>skill<span>arium</span></h1>
      {library.path
        ? <p>В папке {library.path} нет библиотеки скиллов.</p>
        : <p>Подключите этот компьютер к библиотеке скиллов. Адрес и токен даёт тот, кто ставил сервер библиотеки.</p>}
      <form className="connect" onSubmit={submit} aria-label="Подключить библиотеку">
        <label>Адрес библиотеки
          <input type="url" required placeholder="https://gitea.example.org/owner/skills.git" value={url}
            onChange={(event) => setUrl(event.target.value)} disabled={working} />
        </label>
        <label>Токен Gitea
          <input type="password" required autoComplete="off" value={token}
            onChange={(event) => setToken(event.target.value)} disabled={working} />
        </label>
        <label>Папка на этом компьютере
          <span className="folder">
            <input type="text" required value={folder} onChange={(event) => setFolder(event.target.value)} disabled={working} />
            <button type="button" className="quiet" onClick={pickFolder} disabled={working}>Выбрать…</button>
          </span>
        </label>
        <p className="sub">Токен сохраняется в хранилище учётных данных Git и больше нигде не записывается.</p>
        <button type="submit" className="primary" disabled={working}>{working ? 'Подключаю…' : 'Подключить'}</button>
      </form>
      {failure && (
        <div className="notice error" role="alert">
          <p>{failure.title}</p>
          {failure.details.length > 0 && <pre>{failure.details.join('\n')}</pre>}
        </div>
      )}
      <button type="button" className="quiet" onClick={onChoose} disabled={working}>У меня уже есть клон — выбрать папку</button>
    </main>
  );
}

function UpdateBanner({ update }) {
  if (update?.state !== 'ready') return null;
  return (
    <div className="notice info" role="status" data-testid="update-ready">
      <p>Загружена новая версия Skillarium {update.version}. Она установится при выходе из приложения.</p>
      <button type="button" className="primary" onClick={() => api.installUpdate()}>Перезапустить и обновить</button>
    </div>
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
  const [update, setUpdate] = useState(null);

  useEffect(() => {
    api.updateState().then(setUpdate);
    return api.onUpdate(setUpdate);
  }, []);

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
      setNotice({ tone: 'error', text: reason(error) });
    } finally {
      await refresh();
      setBusy(null);
    }
  }

  function report(result, unapplied) {
    if (result.code === 0) return;
    if (result.code === 4 && unapplied) setNotice({ tone: 'warn', text: unapplied });
    else setNotice({ tone: 'error', text: 'Действие не завершилось. Таблица показывает текущее состояние; подробности ниже.', details: result.messages });
  }

  async function chooseLibrary() {
    const picked = await api.chooseLibrary();
    if (!picked) return;
    // A wrong folder never replaces a working library: say why and stay.
    if (!picked.valid && library?.valid) {
      setNotice({ tone: 'error', text: `В папке ${picked.path} нет библиотеки скиллов: в ней должны быть sources.json и agent-skills.json. Библиотека не изменилась.` });
      return;
    }
    setLibrary(picked);
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
        setNotice({ tone: 'info', text: result.json?.published
          ? 'Конфликт разрешён. Итог отправлен в общую библиотеку запросом на слияние и попадёт в неё после автоматических проверок.'
          : 'Конфликт разрешён и сохранён здесь. В общую библиотеку итог отправится при следующей синхронизации.' });
      } else if (result.code === 5) {
        setNotice({ tone: 'warn', text: 'Пока вы выбирали, версии изменились. Ничего не применено; разберите конфликт заново.' });
      } else report(result);
    });
  };

  if (!library) return null;
  if (!library.valid) {
    return <Connect library={library} onChoose={chooseLibrary} onConnected={() => api.library().then(setLibrary)} />;
  }

  const matrix = status ? buildMatrix(status) : null;
  const issues = status ? problems(status) : [];
  const removingProject = removing && status?.projects.find((project) => project.path === removing);

  return (
    <div className="shell">
      <header className="topbar">
        <div className="title">
          <img className="brand-mark" src={mark} alt="" />
          <div className="title-text">
            <div className="wordmark">
              <h1>skill<span>arium</span></h1>
              <span className="section">Скиллы в проектах</span>
            </div>
            <p className="library">
              Библиотека: <span title={library.path}>{library.path}</span>
              <button type="button" className="quiet" onClick={chooseLibrary}>Сменить</button>
            </p>
          </div>
        </div>
        <div className="actions">
          {busy && <span className="working" role="status">Применяю…</span>}
          <button type="button" className="quiet" onClick={() => act('refresh', async () => null, () => {})} disabled={Boolean(busy)}>Обновить</button>
          <button type="button" className="primary" onClick={register} disabled={Boolean(busy)}>Добавить проект</button>
        </div>
      </header>

      <SyncBanner sync={sync} busy={Boolean(busy)} onOpen={() => setResolving(sync)} />
      <UpdateBanner update={update} />

      {status?.format?.readOnly && (
        <div className="notice warn" role="status" data-testid="read-only">
          <p>
            Библиотека обновлена до формата {status.format.library}, а эта версия Skillarium понимает только
            формат {status.format.supported}. Скиллы работают, но правки не публикуются, пока вы не обновите Skillarium.
          </p>
          {update?.state !== 'development' && update?.state !== 'ready' && (
            <button type="button" className="quiet" disabled={update?.state === 'checking' || update?.state === 'downloading'}
              onClick={() => api.checkUpdate().then(setUpdate)}>
              {update?.state === 'downloading' ? `Загружаю ${update.version}…` : 'Проверить обновление'}
            </button>
          )}
        </div>
      )}

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
