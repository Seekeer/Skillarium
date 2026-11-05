# Проверки

```bash
python3 -m unittest discover -s tests -v
```

Все Git-каталоги, bare remote, внешние источники, HOME и кеши создаются во
временных директориях; глобальная и системная конфигурация Git отключены.
`fake_hosting.py` воспроизводит используемую часть Gitea API (поиск, создание,
обновление, закрытие PR, автослияние для точного head) и сливает PR только
после настоящей проверки `server/policy.py`; хук `pre-receive` защищает `main`
bare remote. Управляемые сбои: потерянный ответ на создание PR, ошибка
запроса слияния, отказ в доступе.

- `test_policy.py` — правило PR: правки от любой машины, управляющие данные
  только от назначенного сервера, смешанные PR, подмена назначения в
  предложении, отдельная операция назначения, устаревший PR, удаление скилла,
  слияние обновления ветки хостингом.
- `test_server.py` — серверные команды: регистрация и автообновление через
  PR, политика подтверждения и точное одобрение, смена входящей версии,
  объединение с доработками, конфликты (текст, бинарные файлы, права),
  материалы конфликта, keep/take/detach, недоступный источник, регистрация
  собственных скиллов, неназначенная машина, передача роли с закрытием PR
  прежнего сервера, повтор публикации без дублей, блокировка, сохранность
  реестра при изменении кода.
- `test_sync.py` — Linux-клиент (`install.sh`; нужны bash, jq, flock, curl):
  публикация через PR и получение второй станцией, новый собственный скилл,
  ignore, staged, ручные коммиты, посторонние файлы, некорректные скиллы и
  Windows-пути, параллельные станции, актуальная проверка после изменения
  main, повторы и ошибки доступа, review/approve/resolve, старый кеш, чужие
  установки.
- `test_powershell.py` — Windows-клиент (`install.ps1`, Windows PowerShell 5.1,
  настоящие junction): публикация, новые/некорректные скиллы, повторы,
  подтверждение и разрешение конфликтов.
- `test_migration.py` — объединение историй (обе истории, коллизии, грязные
  копии, прерывание и повтор), переключение серверного таймера с прежнего
  updater (systemctl подменён), публикация неотправленных коммитов старого
  клиента через PR.
- `test_windows_publication.py`, `test_windows_paths.py` — импорт собственных
  скиллов и правила путей Windows.
- `test_engine_layout.py` — движок отдельно от библиотеки: явная библиотека,
  переменная и конфиг, режим только чтения при новом формате.
- `test_engine_install.py` — `skillarium` против `fake_releases.py` (имитация
  GitHub Releases): установка, самообновление, битый sha512, оборванная
  загрузка, откат с пропуском версии, хранение двух прежних версий, канал
  beta, выпуск, не читающий формат библиотеки; загрузка из памяти, как
  `irm | scriptblock` и `curl | bash`.
- `test_server_setup.py` — `skillarium server` против `fake_gitea.py`
  (пользователи, токены, репозитории, защита веток, раннеры): создание,
  пустой репозиторий организации, существующая библиотека, чужой репозиторий,
  новый формат, служебный бот, неверный токен, повтор без изменений.
- `test_client_setup.py` — `skillarium client`: клон, ссылки, учётные данные,
  повтор, токен без права записи, чужой клон в папке, установщик приложения с
  неверной суммой; AppImage и пункт меню на Linux.
- `test_bridge.py` — мост старой раскладки: установка Skillarium при
  синхронизации, сохранение чужого конфига, недоступный выпуск, отключение,
  перевод таймера на `current`.
- `test_library_migration.py` — `release/migrate_library.py`: движок уходит
  одним коммитом, данные и свои папки остаются, проверка PR его принимает.
- `test_release.py` — выпуск: детерминированные архивы, `latest.json`,
  загрузочные скрипты.

Приложение: `npm test` в `app/` — юнит-тесты раскладки движка
(`tests/unit`, `node --test`) и Playwright-тесты интерфейса, в том числе
экрана первого запуска.

## Нативный Windows runner

CI движка — GitHub Actions (`.github/workflows/tests.yml`, `windows-latest`):
свой runner не нужен. Ниже — как запускать то же на собственном Windows
runner Gitea: в `.gitea/workflows/tests.yml` библиотеки со встроенным движком
есть Windows job. Нужен Gitea Actions runner
на Windows с меткой `windows-latest:host`, Git, Python 3.9+ в PATH, Windows
PowerShell 5.1, Windows Script Host с VBScript и доступной службой Task
Scheduler; для задачи с Interactive logon type — вошедший пользователь runner.
Без такого runner job остаётся в очереди. Используйте отдельную одноразовую
VM/учётную запись; не подключайте рабочую станцию для непроверенных PR.

Ручной запуск из корня репозитория:

```powershell
python -m unittest discover -s tests -v
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tests/windows_native.ps1
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tests/verify-windows.ps1
```

`windows_native.ps1` создаёт временный профиль, bare remote и каталог с
пробелами, проверяет настоящие NTFS junction, регистрацию задачи с уникальным
именем `AgentSkills-Test-*` (существующая задача пользователя не трогается),
её параметры, сохранение чужой папки и Uninstall. `verify-windows.ps1`
проверяет скрытую консоль, коды завершения, миграцию старой задачи с
сохранением расписания/учётной записи/настроек и настоящий запуск
Планировщиком. Временные файлы и задачи удаляются в `finally`.

Linux-проверки и разбор PowerShell не подтверждают junction или Task
Scheduler; Windows-результат нужен отдельно. Правила защиты ветки и
автослияние настоящего Gitea проверяются на отдельном тестовом репозитории.

## Local verification and issue tracker

This repository contains native CLI clients and a server-side CLI, not a
long-running application. The verification entry point is the unittest command
above, with the native Windows scripts for junctions and Task Scheduler; no
application URL, test identity, or database is required. Linux verification can
also run in an isolated container with Git, Python, Bash, jq, curl and flock,
using a temporary copy of the repository rather than the installed client.

Issues live on GitHub (`Seekeer/Skillarium`); never store tokens in this
repository. Close a ticket only after all its acceptance checks are complete. Live branch-protection tests require a separate test repository;
local fake-hosting results do not establish real Gitea runner availability.
