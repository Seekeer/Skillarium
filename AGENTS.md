# Agent Skills

Единый репозиторий: библиотека `skills/<name>/`, реестр `sources.json`,
общая конфигурация `agent-skills.json`, базы внешних версий `upstream/<name>/`,
нативные клиенты (`install.sh`, `install.ps1`, `scripts/`) и сервер (`server/`).
История прежнего agent-skills-updater присоединена; его `client/` больше не
существует — корневые клиентские файлы и есть эталон.

Все автоматические публикации идут через ветки `agent-skills/...` и PR с
автослиянием; прямой push в `main` запрещён. Правки скиллов принимаются от
любой машины; закрепления, политики, `upstream/` и прочее серверное состояние —
только от коммитов служебного автора `agent-skills/<server.machine>`
назначенного сервера (правило: `server/policy.py`). Назначение сервера меняется
только `manage.py assign-server`. Не меняй личные настройки автора Git
пользователя: служебная идентичность задаётся на одну команду.

Для добавления источника прочитай
[add-shared-skill](server/skills/add-shared-skill/SKILL.md).
Для обновлений и конфликтов —
[check-skill-updates](server/skills/check-skill-updates/SKILL.md).
Для сервера, назначения роли, хостинга и миграции —
[maintain-skill-catalogs](server/skills/maintain-skill-catalogs/SKILL.md).
Команды и границы автоматизации: [README.md](README.md),
[server/README.md](server/README.md).

Клиентам Python не нужен; Python нужен только исполнителю серверной логики.
Изменения клиентских файлов делай в корне и обновляй
`python3 server/manage.py client-manifest --write`; новые клиентские файлы
добавляй в `CLIENT_FILES` (server/absorb_updater.py). Изменения кода и
документации не попадают в автоматические клиентские коммиты — сохраняй это.
Движок не должен считать свою папку библиотекой: путь библиотеки
передаётся явно (`--library`/`-Library`/`--repo`, `SKILLARIUM_LIBRARY`,
конфиг Skillarium), а файлы движка берутся из папки скрипта. Формат
библиотеки (`format` в `agent-skills.json`) поднимается только вместе с
`supported_format` в `scripts/locate.*` и `SUPPORTED_FORMAT` в
`server/hosting.py`. План распространения:
[.scratch/skillarium-distribution/plan.md](.scratch/skillarium-distribution/plan.md).
Интерфейсы ссылок расширяемы (`link_roots`/`Get-LinkRoots`) для проектных
подключений.

Сохраняй чужие установки, локальные правки, staged-файлы, ручные коммиты,
историю, реестр и ручные изменения клиентских копий. Rebase, reset,
force-push и очистка пользовательской работы не являются способом
восстановления.

Проверяй на временных Git-каталогах с локальным bare remote, имитацией
хостинга `tests/fake_hosting.py` (настоящая проверка `server/policy.py`,
защита `main` хуком) и временными HOME/кешами; реальные пользовательские
каталоги не используй. Правила PR на настоящем Gitea проверяй в отдельном
тестовом репозитории. Linux-клиент проверяется на Linux (jq, flock, curl);
junction и Планировщик — только нативно на Windows (`tests/README.md`).
