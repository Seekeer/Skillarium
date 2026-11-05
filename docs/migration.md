# Переход библиотеки со встроенным движком на Skillarium

Для библиотеки, в которой движок ещё лежит рядом со скиллами (`install.*`,
`scripts/`, `server/`, `app/` в том же репозитории, как в `dim/agent-skills`).
После перехода машины работают движком, установленным из выпусков Skillarium, а
в библиотеке остаются только её данные.

Порядок важен: файлы движка уходят из библиотеки последними. Машина, которая
к этому моменту ещё запускает движок из своего клона, перестанет
синхронизироваться.

## 0. Что нужно заранее

- Опубликован выпуск Skillarium (`v0.1.0` или новее) на GitHub, ссылки
  `releases/latest/download/skillarium.sh` и `.ps1` открываются.
- Раннер Actions библиотеки видит GitHub: новая проверка PR скачивает
  закреплённый выпуск политики.

## 1. Мостовой выпуск в библиотеке

Слейте в `main` библиотеки ветку с движком Skillarium и мостом (для
`dim/agent-skills` — `skillarium/bridge`) обычным PR. Дальше каждая машина при
очередной синхронизации сама:

- ставит Skillarium в `~/.local/share/skillarium` (`%LOCALAPPDATA%\Skillarium`);
- записывает свой клон в конфиг Skillarium;
- переводит своё расписание на `current` установленного движка, не меняя
  время запуска.

Если выпуск недоступен, синхронизация проходит как раньше и пишет
«Skillarium bridge postponed»; переход повторится через час.

Проверка на машине: `skillarium status` показывает версию и библиотеку; на
Linux `systemctl --user cat agent-skills.service` содержит
`.../skillarium/current/install.sh --library ...`; на Windows действие задачи
`AgentSkills-Hourly-*` запускает `...\Skillarium\current\scripts\sync-hidden.vbs`.

## 2. Сервер

На назначенном сервере (i3) подключите существующую библиотеку к движку из
выпуска, указав текущий клон:

```bash
curl -fsSL https://github.com/Seekeer/Skillarium/releases/latest/download/skillarium.sh | bash -s -- server --repo https://gitea.example.org/dim/agent-skills.git --path ~/agent-skills
```

Установщик узнаёт библиотеку и сервер (роль не меняется), переписывает таймер
`agent-skills-server` на `current/server/run.sh` и включает самообновление.
Проверка: `systemctl --user cat agent-skills-server.service` показывает путь
через `current`, а не `versions/<версия>`.
При первом обслуживании сервер закрепляет выпуск политики (`pin-policy`) своим
PR; его проверяет ещё старая политика из библиотеки, в мостовом выпуске она уже
знает это правило. Можно не ждать суток:

```bash
python3 ~/.local/share/skillarium/current/server/manage.py --repo ~/agent-skills pin-policy
```

Дождитесь слияния: в `agent-skills.json` на `main` появится `policy_engine`.

## 3. Убедиться, что все машины переведены

Пройдите по всем машинам: `skillarium status` (Linux) или
`& "$env:LOCALAPPDATA\Skillarium\bin\skillarium.cmd" status` (Windows) должен
показывать установленную версию. Машину, которая давно не включалась, проще
перевести вручную: запустить на ней синхронизацию (`install.sh --sync-only`,
`install.ps1 -Mode Sync` из клона) или клиентскую установку из README.

## 4. Убрать движок из библиотеки

На любой машине с чистым клоном и правом записи:

```bash
python3 release/migrate_library.py --library ~/agent-skills
git -C ~/agent-skills push origin skillarium/remove-engine
```

Скрипт (из репозитория Skillarium) отказывается, пока `policy_engine` не
закреплён, а при SSH-адресе `origin` просит `--url https://...`. Коммит
меняет workflow, поэтому подписан служебным автором назначенного сервера
(`agent-skills/<server.machine>`) — так его примет проверка. Он удаляет все пути движка, оставляет `skills/`, `sources.json`,
`agent-skills.json`, `upstream/` и прочие ваши папки, ставит проверку PR из
шаблона и дописывает в README команды подключения машин. Откройте PR в `main`:
проверка принимает его как изменение файлов проекта.

После слияния клоны на машинах теряют файлы движка, а расписания уже идут через
`current`.

## 5. Приложение

На машинах с графикой поставьте приложение командой клиента из README
библиотеки (готовый клон и расписание она примет как есть) или скачайте
установщик со страницы выпусков. Дальше приложение обновляется само и
обновляет движок.

## Если что-то пошло не так

- До шага 4 всё обратимо: `SKILLARIUM_NO_BRIDGE=1` оставляет машину на
  встроенном движке, а `install.ps1 -Mode Install` / `install.sh` из клона
  возвращают прежнее расписание.
- Неудачный выпуск движка: `skillarium rollback` на машине.
- PR с удалением движка можно отменить обычным revert-PR; машины продолжат
  работать установленным движком.
