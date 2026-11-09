import { useCallback, useEffect, useRef, useState } from 'react';
import {
  CELL_TEXT, basename, buildMatrix, cellOf, checkedText, conflictSkills, filterRows, globalCaption, moveLibrary, parent,
  policyCaption, problems, syncCaption, updateSummary,
} from './model.js';
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
  if (/library named .* is connected already|Invalid library name/.test(text)) return 'Библиотека с таким именем уже подключена. Укажите другое имя.';
  if (/Unable to connect|could not be resolved|remote name|No connection|timed out|actively refused|answered [^0-9]/i.test(text)) {
    return 'Сервер Gitea по этому адресу не отвечает. Проверьте адрес и сеть.';
  }
  return 'Подключить библиотеку не удалось.';
}

const FOCUSABLE = 'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';
const focusable = (root) => [...(root?.querySelectorAll(FOCUSABLE) || [])].filter((item) => !item.disabled);

function Modal({ title, children, onCancel }) {
  const box = useRef(null);
  // Focus starts on an enabled control of the dialog and returns where it was on close.
  useEffect(() => {
    const previous = document.activeElement;
    (box.current?.querySelector('[data-autofocus]:not(:disabled)') || focusable(box.current)[0])?.focus();
    return () => { if (previous?.isConnected) previous.focus(); };
  }, []);
  useEffect(() => {
    const onKey = (event) => {
      if (event.key === 'Escape') { onCancel(); return; }
      if (event.key !== 'Tab') return;
      // Tab stays inside the dialog: the page behind it is not reachable.
      const items = focusable(box.current);
      if (!items.length) { event.preventDefault(); return; }
      const first = items[0];
      const last = items[items.length - 1];
      const inside = box.current.contains(document.activeElement);
      if (!inside || (event.shiftKey && document.activeElement === first)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
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

// Where a skill present in several libraries comes from: the linked copy may be reset to
// the order of the libraries, another copy may be chosen instead.
function CopyChoice({ row, busy, onPrefer }) {
  if (!row.inLibrary || row.copies.length < 2) return null;
  if (!row.active) {
    return (
      <div className="copy-choice">
        <span className="tag warn">не подключён: перекрыт {row.shadowedBy}</span>
        <button type="button" className="quiet small" disabled={busy}
          aria-label={`Подключать ${row.name} из ${row.library}`} onClick={() => onPrefer(row.name, row.library)}>
          Подключать эту копию
        </button>
      </div>
    );
  }
  return (
    <div className="copy-choice">
      <span className="global-caption">
        {row.preferred ? 'Эта копия выбрана вручную' : 'Библиотека выше в списке'}; копии: {row.copies.join(', ')}
      </span>
      {row.preferred && (
        <button type="button" className="quiet small" disabled={busy}
          aria-label={`Выбирать копию ${row.name} по порядку библиотек`} onClick={() => onPrefer(row.name, 'auto')}>
          По порядку библиотек
        </button>
      )}
    </div>
  );
}

function PolicySwitch({ row, busy, onToggle }) {
  const auto = row.update.policy === 'auto';
  return (
    <div className="global-cell" title={`Источник: ${row.update.source}`}>
      <button type="button" role="switch" aria-checked={auto} className="switch" disabled={busy}
        aria-label={`Обновлять автоматически: ${row.label}`} onClick={onToggle}>
        <span className="switch-knob" />
      </button>
      <span className="global-caption">{policyCaption(row.update)}</span>
    </div>
  );
}

const MARKED = new Set(['conflict', 'missing-skill', 'library-missing', 'library-unavailable']);

function Socket({ row, project, entry, busy, onToggle }) {
  const state = entry ? entry.state : 'none';
  const blocked = busy || !project.available || Boolean(project.error) || (!row.inLibrary && !entry);
  const label = `${row.label} в ${basename(project.path)}: ${entry ? CELL_TEXT[state] || state : 'не подключён'}`;
  return (
    <button type="button" className="socket" data-state={state} aria-pressed={Boolean(entry)} aria-label={label}
      title={entry ? `${CELL_TEXT[state] || state}. Нажмите, чтобы отключить` : 'Подключить к проекту'}
      disabled={blocked} onClick={onToggle}>
      <span className="socket-mark" aria-hidden="true">{MARKED.has(state) ? '!' : ''}</span>
    </button>
  );
}

// One banner per library whose synchronization stopped or could not be checked.
function SyncBanner({ sync, multiple, busy, onOpen }) {
  const region = multiple ? `Синхронизация библиотеки ${sync.title}` : 'Синхронизация библиотеки';
  const which = multiple ? `библиотеки «${sync.title}»` : 'библиотеки';
  if (sync.state === 'error') {
    return (
      <div className="sync-banner" role="region" aria-label={region}>
        <p><strong>Не удалось проверить синхронизацию {which}.</strong></p>
        {sync.messages?.length > 0 && <pre>{sync.messages.join('\n')}</pre>}
      </div>
    );
  }
  if (sync.state !== 'conflict') return null;
  const names = conflictSkills(sync).map((name) => `«${name}»`).join(', ');
  return (
    <div className="sync-banner" role="region" aria-label={region}>
      <div>
        <p><strong>Синхронизация {which} остановлена.</strong> Правки {names} на этом компьютере расходятся
          с общей библиотекой.</p>
        <p className="sub">Обе версии сохранены. Пока вы не выберете итог, обновления скиллов этой библиотеки
          не приходят, а ваши правки в ней не публикуются.</p>
      </div>
      <button type="button" className="primary" disabled={busy} onClick={onOpen}>Разобрать конфликт</button>
    </div>
  );
}

// Connects this machine to a library in Gitea: the first one, or one more.
function ConnectForm({ first, onConnected, onCancel }) {
  const [url, setUrl] = useState('');
  const [token, setToken] = useState('');
  const [folder, setFolder] = useState('');
  const [name, setName] = useState('');
  const [working, setWorking] = useState(false);
  const [failure, setFailure] = useState(null);
  // The first library goes to the usual folder; the client setup names the folder of another one.
  useEffect(() => { if (first) api.defaultFolder().then((value) => setFolder((current) => current || value)); }, [first]);

  async function submit(event) {
    event.preventDefault();
    setWorking(true);
    setFailure(null);
    try {
      const result = await api.connect({ url, token, folder, name });
      if (result.code === 0) {
        setToken('');
        onConnected(result);
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
    <>
      <form className="connect" onSubmit={submit} aria-label="Подключить библиотеку">
        <label>Адрес библиотеки
          <input type="url" required placeholder="https://gitea.example.org/owner/skills.git" value={url}
            onChange={(event) => setUrl(event.target.value)} disabled={working} data-autofocus />
        </label>
        <label>Токен Gitea
          <input type="password" required autoComplete="off" value={token}
            onChange={(event) => setToken(event.target.value)} disabled={working} />
        </label>
        <label>Папка на этом компьютере
          <span className="folder">
            <input type="text" required={first} value={folder} placeholder={first ? '' : 'по умолчанию Skillarium\\<имя библиотеки>'}
              onChange={(event) => setFolder(event.target.value)} disabled={working} />
            <button type="button" className="quiet" onClick={pickFolder} disabled={working}>Выбрать…</button>
          </span>
        </label>
        {!first && (
          <label>Имя библиотеки на этом компьютере
            <input type="text" value={name} placeholder="по умолчанию — имя репозитория"
              onChange={(event) => setName(event.target.value)} disabled={working} />
          </label>
        )}
        <p className="sub">Токен сохраняется в хранилище учётных данных Git и больше нигде не записывается.
          {!first && ' Новая библиотека встанет последней в списке: её скиллы с теми же именами, что в библиотеках выше, будут перекрыты.'}</p>
        <div className="form-actions">
          {onCancel && <button type="button" className="quiet" onClick={onCancel} disabled={working}>Отмена</button>}
          <button type="submit" className="primary" disabled={working}>{working ? 'Подключаю…' : 'Подключить'}</button>
        </div>
      </form>
      {failure && (
        <div className="notice error" role="alert">
          <p>{failure.title}</p>
          {failure.details.length > 0 && <pre>{failure.details.join('\n')}</pre>}
        </div>
      )}
    </>
  );
}

// First run, or no connected library is reachable.
function Connect({ library, onConnected, onChoose }) {
  const offline = !library.override && library.libraries.length > 0;
  return (
    <main className="setup">
      <img className="brand-mark" src={mark} alt="" />
      <h1>skill<span>arium</span></h1>
      {library.override ? <p>В папке {library.override} нет библиотеки скиллов.</p>
        : offline ? <p>Папки подключённых библиотек недоступны: {library.libraries.map((item) => item.path).join(', ')}.
          Подключите библиотеку заново или выберите её клон.</p>
          : <p>Подключите этот компьютер к библиотеке скиллов. Адрес и токен даёт тот, кто ставил сервер библиотеки.</p>}
      <ConnectForm first={library.libraries.length === 0} onConnected={onConnected} />
      <button type="button" className="quiet" onClick={onChoose}>У меня уже есть клон — выбрать папку</button>
    </main>
  );
}

function UpdateBanner({ update }) {
  if (update?.state !== 'ready') return null;
  if (update.install === false) {
    return (
      <div className="notice info" role="status" data-testid="update-ready">
        <p>Загружена новая версия Skillarium {update.version}. Эта сборка её не устанавливает: установка выключена
          переменной SKILLARIUM_NO_INSTALL.</p>
      </div>
    );
  }
  return (
    <div className="notice info" role="status" data-testid="update-ready">
      <p>Загружена новая версия Skillarium {update.version}. Она установится при выходе из приложения.</p>
      <button type="button" className="primary" onClick={() => api.installUpdate()}>Перезапустить и обновить</button>
    </div>
  );
}

const CHANNEL_TEXT = { stable: 'стабильный', beta: 'предварительные выпуски (beta)' };
const OVERRIDE_TEXT = {
  environment: 'Папка задана переменной окружения SKILLARIUM_LIBRARY',
  settings: 'Папка выбрана в приложении',
  development: 'Папка репозитория разработки',
};

function Settings({ library, libraries, update, onUpdate, onChooseLibrary, onResetLibrary, onClose }) {
  const [about, setAbout] = useState(null);
  useEffect(() => { api.about().then(setAbout); }, []);
  const state = update?.state;
  const canCheck = !['development', 'disabled', 'checking', 'downloading', 'ready'].includes(state);
  const canInstall = state === 'ready' && update.install !== false;
  const engine = about?.engine;
  return (
    <Modal title="Настройки" onCancel={onClose}>
      <dl className="settings" data-testid="settings">
        <dt>Приложение</dt>
        <dd>{about ? `Skillarium ${about.app}${about.packaged ? '' : ' (сборка для разработки)'}` : '…'}</dd>
        <dt>Движок</dt>
        <dd>
          {engine ? (engine.version ? `версия ${engine.version}` : 'из папки репозитория') : '…'}
          {engine && <code title={engine.path}>{engine.path}</code>}
        </dd>
        <dt>Канал обновлений</dt>
        <dd>{about ? CHANNEL_TEXT[about.channel] : '…'}</dd>
        <dt>Библиотеки</dt>
        <dd>
          {libraries.map((item) => <code key={item.name} title={item.path}>{item.name}: {item.path}</code>)}
          {library.override && (
            <span className="sub">{OVERRIDE_TEXT[library.source] || 'Папка выбрана'}: <code title={library.override}>{library.override}</code></span>
          )}
          <span className="settings-buttons">
            <button type="button" className="quiet small" onClick={onChooseLibrary}>Выбрать клон вручную…</button>
            {library.source === 'settings' && (
              <button type="button" className="quiet small" onClick={onResetLibrary}>Вернуться к подключённым библиотекам</button>
            )}
          </span>
        </dd>
      </dl>
      <section className="settings-update" aria-labelledby="settings-update-title">
        <h3 id="settings-update-title">Обновление</h3>
        <p role="status" data-testid="update-summary">{updateSummary(update)}</p>
        {checkedText(update?.checkedAt) && <p className="sub">{checkedText(update.checkedAt)}</p>}
        <p className="sub">Приложение проверяет обновления само раз в несколько часов и обновляет вместе с собой движок.</p>
      </section>
      <div className="modal-actions">
        {/* Focus starts on the update action; on Close while there is nothing to do. */}
        <button type="button" className="quiet" data-autofocus={(!canInstall && !canCheck) || undefined}
          onClick={onClose}>Закрыть</button>
        {state === 'ready' ? (
          <button type="button" className="primary" data-autofocus disabled={!canInstall}
            onClick={() => api.installUpdate()}>Перезапустить и обновить</button>
        ) : (
          <button type="button" className="primary" data-autofocus disabled={!canCheck}
            onClick={() => api.checkUpdate().then(onUpdate)}>
            {state === 'checking' ? 'Проверяю…' : 'Проверить обновления'}
          </button>
        )}
      </div>
    </Modal>
  );
}

// Connected libraries in priority order: a skill in several of them is linked from the higher one.
function LibraryList({ libraries, busy, onMove, onDisconnect, onConnect }) {
  const configured = libraries.filter((library) => library.configured !== false);
  return (
    <section className="libraries" aria-labelledby="libraries-title">
      <h2 id="libraries-title">Библиотеки</h2>
      <ol>
        {libraries.map((library) => {
          const caption = syncCaption(library);
          const index = configured.indexOf(library);
          return (
            <li key={library.name} aria-label={`Библиотека ${library.name}`}>
              <div className="library-head">
                <strong>{library.name}</strong>
                {library.repo && <span className="sub">{library.repo}</span>}
              </div>
              <code title={library.path}>{library.path}</code>
              <p className={`sync-line ${caption.tone}`}>
                {caption.text}{library.available && `, скиллов: ${library.skills}`}
              </p>
              {caption.error && <pre className="sync-error">{caption.error}</pre>}
              {library.format?.readOnly && (
                <span className="tag warn">формат {library.format.library}: правки не публикуются до обновления Skillarium</span>
              )}
              {library.configured === false ? (
                <span className="tag">выбрана только для приложения</span>
              ) : (
                <div className="library-actions">
                  <button type="button" className="quiet small" disabled={busy || index === 0}
                    aria-label={`Поднять ${library.name}`} title="Выше в списке" onClick={() => onMove(library, -1)}>↑</button>
                  <button type="button" className="quiet small" disabled={busy || index === configured.length - 1}
                    aria-label={`Опустить ${library.name}`} title="Ниже в списке" onClick={() => onMove(library, 1)}>↓</button>
                  <button type="button" className="quiet small" disabled={busy}
                    aria-label={`Отключить ${library.name}`} title="Отключить библиотеку"
                    onClick={() => onDisconnect(library)}>Отключить</button>
                </div>
              )}
            </li>
          );
        })}
      </ol>
      <button type="button" className="quiet" disabled={busy} onClick={onConnect}>Подключить библиотеку</button>
    </section>
  );
}

export default function App() {
  const [library, setLibrary] = useState(null);
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(null);
  const [notice, setNotice] = useState(null);
  const [decision, setDecision] = useState(null);
  const [removing, setRemoving] = useState(null);
  const [syncs, setSyncs] = useState([]);
  const [resolving, setResolving] = useState(null);
  const [update, setUpdate] = useState(null);
  const [settings, setSettings] = useState(false);
  const [filter, setFilter] = useState('');
  const [connecting, setConnecting] = useState(false);
  const [disconnecting, setDisconnecting] = useState(null);
  // Stable, so update events re-rendering the open window do not move the focus.
  const closeSettings = useCallback(() => setSettings(false), []);
  const closeConnecting = useCallback(() => setConnecting(false), []);

  useEffect(() => {
    api.updateState().then(setUpdate);
    return api.onUpdate(setUpdate);
  }, []);

  // Status covers every library; conflicts are asked of each reachable library separately.
  const refresh = useCallback(async () => {
    const result = await api.status();
    if (result.code !== 0 || !result.json) {
      setNotice({ tone: 'error', text: 'Не удалось прочитать состояние подключений.', details: result.messages });
      return;
    }
    setStatus(result.json);
    const reachable = (result.json.libraries || [{ name: '', path: result.json.library, available: true }])
      .filter((item) => item.available);
    const answers = await Promise.all(reachable.map(async (item) => {
      const conflicts = await api.conflicts(item.path).catch((error) => ({ code: 1, messages: [reason(error)] }));
      if (conflicts.code === 0 && conflicts.json) return { ...conflicts.json, title: item.name, path: item.path };
      return { state: 'error', messages: conflicts.messages, title: item.name, path: item.path };
    }));
    setSyncs(answers);
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
      if (result) await handle(result);
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
    setLibrary(await api.library());
  }

  const libraries = status?.libraries || [];
  const folderOf = (name) => libraries.find((item) => item.name === name)?.path || library?.base;

  // A copy other than the one linked globally is bound by its library ("owner/repo:name").
  const bind = (project, row) => act(`${project.path}|${row.key}`,
    () => (row.active ? api.bind(project.path, row.name) : api.bind(project.path, row.name, row.library)),
    (result) => report(result, `«${row.label}» добавлен в список «${basename(project.path)}», но не подключён: место занято. Подробности ниже.`));

  const unbind = (project, entry, choice) => act(`${project.path}|${entry}`, () => api.unbind(project.path, entry, choice),
    (result) => {
      if (result.code === 3 && result.json) setDecision({ project, entry, name: result.json.skill || entry, global: result.json.global });
      else report(result);
    });

  const toggleGlobal = (row) => act(`global|${row.name}`, () => api.setGlobal(row.name, !row.global.enabled), (result) => report(result));
  const togglePolicy = (row) => {
    const wanted = row.update.policy === 'auto' ? 'confirm' : 'auto';
    return act(`policy|${row.key}`, () => api.setPolicy(row.name, wanted === 'auto', folderOf(row.library)), async (result) => {
      if (result.code === 0) return;
      // A failed run may or may not have committed the switch: the library state decides what to say.
      const after = await api.status();
      const update = after?.json?.skills?.find((skill) => skill.name === row.name
        && (skill.library ?? null) === row.library)?.update;
      if (update?.policy === wanted && update.pending) {
        setNotice({ tone: 'warn', text: `Политика «${row.label}» изменена на этом компьютере и будет опубликована при следующей синхронизации.`, details: result.messages });
      } else report(result);
    });
  };
  const prefer = (name, choice) => act(`prefer|${name}`, () => api.prefer(name, choice), (result) => {
    if (result.code === 0) {
      setNotice({ tone: 'info', text: choice === 'auto'
        ? `«${name}» снова подключается из библиотеки, которая выше в списке.`
        : `«${name}» теперь подключается из библиотеки «${choice}» на этом компьютере.` });
    } else report(result);
  });
  const move = (item, delta) => {
    const names = moveLibrary(libraries, item.name, delta);
    if (!names) return undefined;
    return act('order', () => api.order(names), (result) => report(result));
  };
  const disconnect = (item, force) => {
    setDisconnecting(null);
    act(`disconnect|${item.name}`, () => api.disconnect(item.name, force), (result) => {
      if (result.code === 0) {
        setNotice({ tone: 'info', text: `Библиотека «${item.name}» отключена. Её клон остался в ${item.path}.` });
        // Without libraries the app offers to connect one again.
        api.library().then(setLibrary);
      } else {
        // The engine says why (unpublished changes); the dialog offers to disconnect anyway.
        setDisconnecting({ library: item, refused: result.messages });
      }
    });
  };
  const connected = async () => {
    setConnecting(false);
    setNotice({ tone: 'info', text: 'Библиотека подключена. Её скиллы с теми же именами, что в библиотеках выше, перекрыты; копию можно выбрать в таблице.' });
    setLibrary(await api.library());
  };
  const register = () => act('register', () => api.register(),
    (result) => report(result, 'Проект добавлен, но часть скиллов из его списка не подключена. Подробности ниже.'));
  const resolve = (request) => {
    const { path } = resolving;
    setResolving(null);
    act('resolve', () => api.resolve(path, request), (result) => {
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
  // A filter on a library that is gone (disconnected) or no longer selectable shows everything.
  const activeFilter = libraries.length > 1 && libraries.some((item) => item.name === filter) ? filter : '';
  const shown = matrix ? filterRows(matrix.rows, activeFilter) : [];
  const issues = status ? problems(status) : [];
  const removingProject = removing && status?.projects.find((project) => project.path === removing);
  const multiple = libraries.length > 1;

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
              {multiple ? 'Библиотеки' : 'Библиотека'}:{' '}
              <span title={libraries.map((item) => item.path).join('\n') || library.base}>
                {libraries.length ? libraries.map((item) => item.name).join(', ') : library.base}
              </span>
            </p>
          </div>
        </div>
        <div className="actions">
          {busy && <span className="working" role="status">Применяю…</span>}
          <button type="button" className="quiet" onClick={() => act('refresh', async () => null, () => {})} disabled={Boolean(busy)}>Обновить</button>
          <button type="button" className="primary" onClick={register} disabled={Boolean(busy)}>Добавить проект</button>
          <button type="button" className="quiet" onClick={() => setSettings(true)}>
            Настройки{update?.state === 'ready' && <span className="dot" aria-label="есть обновление" />}
          </button>
        </div>
      </header>

      {syncs.map((sync) => (
        <SyncBanner key={sync.path} sync={sync} multiple={multiple} busy={Boolean(busy)} onOpen={() => setResolving(sync)} />
      ))}
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
            {multiple && (
              <label className="filter">Библиотека
                <select value={activeFilter} onChange={(event) => setFilter(event.target.value)} aria-label="Фильтр по библиотеке">
                  <option value="">Все библиотеки</option>
                  {libraries.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
                </select>
              </label>
            )}
            <table className="matrix">
              <thead>
                <tr>
                  <th scope="col" className="skill-col">Скилл</th>
                  <th scope="col" className="library-col">Библиотека</th>
                  <th scope="col" className="global-col">Глобально<span className="sub">в папках пользователя</span></th>
                  <th scope="col" className="global-col">Обновлять автоматически<span className="sub">внешние скиллы</span></th>
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
                {shown.map((row) => (
                  <tr key={row.key} data-active={row.active} className={row.active ? undefined : 'shadowed'}>
                    <th scope="row" className="skill-col">
                      <span className="skill-name">{row.name}</span>
                      {row.missing && <span className="tag warn">{row.missing}</span>}
                    </th>
                    <td className="library-col">{row.library ?? (row.inLibrary ? '' : row.entry.split(':')[0])}</td>
                    <td className="global-col">
                      {row.global && row.active ? <GlobalSwitch row={row} busy={Boolean(busy)} onToggle={() => toggleGlobal(row)} /> : null}
                      <CopyChoice row={row} busy={Boolean(busy)} onPrefer={prefer} />
                    </td>
                    <td className="global-col">
                      {row.update ? <PolicySwitch row={row} busy={Boolean(busy)} onToggle={() => togglePolicy(row)} />
                        : row.inLibrary ? <span className="global-caption">свой скилл</span> : null}
                    </td>
                    {matrix.projects.map((project) => {
                      const entry = cellOf(project, row);
                      return (
                        <td key={project.path} className="project-cell">
                          <Socket row={row} project={project} entry={entry} busy={Boolean(busy)}
                            onToggle={() => (entry ? unbind(project, entry.name) : bind(project, row))} />
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
            <LibraryList libraries={libraries} busy={Boolean(busy)} onMove={move}
              onDisconnect={(item) => setDisconnecting({ library: item, refused: null })} onConnect={() => setConnecting(true)} />
            <section className="legend" aria-labelledby="legend-title">
              <h2 id="legend-title">Обозначения</h2>
              <ul>
                <li><span className="socket" data-state="applied" aria-hidden="true" /> подключён: проект видит общую папку скилла</li>
                <li><span className="socket" data-state="pending" aria-hidden="true" /> в списке проекта, ссылки ещё создаются</li>
                <li><span className="socket" data-state="conflict" aria-hidden="true"><span className="socket-mark">!</span></span> в списке, но место занято чужим содержимым или библиотека не подключена</li>
                <li><span className="socket" data-state="unavailable" aria-hidden="true" /> в списке, но папка проекта недоступна</li>
                <li><span className="socket" data-state="none" aria-hidden="true" /> не подключён</li>
              </ul>
              <p>Правка скилла через любой проект сразу видна во всех проектах этого компьютера. Список скиллов
                проекта хранится в его файле .agent-skills.json; закоммитьте его, когда будете готовы.</p>
              {multiple && (
                <p>Скилл с одним именем в нескольких библиотеках подключается из той, что выше в списке.
                  Проект может взять другую копию: нажмите гнездо в её строке.</p>
              )}
            </section>
          </aside>
        </main>
      )}

      {decision && (
        <Modal title={`Отключить «${decision.name}» от последнего проекта?`} onCancel={() => setDecision(null)}>
          <p>После этого «{decision.name}» не останется ни в одном проекте. Сейчас глобальный доступ
            {decision.global.enabled ? ' включён' : ' выключен'}. Где скилл должен быть доступен дальше?</p>
          <div className="choices">
            <button type="button" className="choice" onClick={() => { const d = decision; setDecision(null); unbind(d.project, d.entry, 'global'); }}>
              <strong>Доступен глобально</strong>
              <span>Агенты увидят скилл в любой папке через профиль пользователя.</span>
            </button>
            <button type="button" className="choice" onClick={() => { const d = decision; setDecision(null); unbind(d.project, d.entry, 'library'); }}>
              <strong>Оставить только в библиотеке</strong>
              <span>Скилл сохранится в библиотеке; агенты его не увидят, пока вы его не подключите.</span>
            </button>
          </div>
          <div className="modal-actions">
            <button type="button" className="quiet" data-autofocus onClick={() => setDecision(null)}>Отмена, оставить подключение</button>
          </div>
        </Modal>
      )}

      {resolving && <Resolver sync={multiple ? resolving : { ...resolving, title: '' }} onCancel={() => setResolving(null)} onApply={resolve} />}

      {settings && (
        <Settings library={library} libraries={libraries} update={update} onUpdate={setUpdate} onClose={closeSettings}
          onChooseLibrary={() => { setSettings(false); chooseLibrary(); }}
          onResetLibrary={async () => { setSettings(false); setLibrary(await api.resetLibrary()); }} />
      )}

      {connecting && (
        <Modal title="Подключить ещё одну библиотеку" onCancel={closeConnecting}>
          <div className="connect-modal">
            <ConnectForm first={false} onConnected={connected} onCancel={closeConnecting} />
          </div>
        </Modal>
      )}

      {disconnecting && (
        <Modal title={`Отключить библиотеку «${disconnecting.library.name}»?`} onCancel={() => setDisconnecting(null)}>
          {disconnecting.refused ? (
            <>
              <div className="notice error" role="alert">
                <p>Skillarium не отключил библиотеку: в её клоне есть правки, которые ещё не опубликованы.</p>
                {disconnecting.refused.length > 0 && <pre>{disconnecting.refused.join('\n')}</pre>}
              </div>
              <p>Дождитесь синхронизации, чтобы правки ушли в библиотеку, или отключите её сейчас: правки останутся
                в клоне {disconnecting.library.path}, но не будут опубликованы.</p>
            </>
          ) : (
            <p>Ссылки на скиллы этой библиотеки в профиле и в проектах будут удалены, клон останется в
              {' '}{disconnecting.library.path}. Проекты, где выбрана копия из этой библиотеки, покажут «библиотека не
              подключена»; скиллы с теми же именами из других библиотек подключатся вместо её копий.</p>
          )}
          <div className="modal-actions">
            <button type="button" className="quiet" data-autofocus onClick={() => setDisconnecting(null)}>Отмена</button>
            {disconnecting.refused ? (
              <button type="button" className="danger" onClick={() => disconnect(disconnecting.library, true)}>Отключить принудительно</button>
            ) : (
              <button type="button" className="danger" onClick={() => disconnect(disconnecting.library, false)}>Отключить</button>
            )}
          </div>
        </Modal>
      )}

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
