# Библиотека скиллов

Библиотека для [Skillarium](https://github.com/Seekeer/Skillarium): папки
`skills/<name>/`, реестр `sources.json`, общая конфигурация `agent-skills.json`
и базы внешних версий `upstream/<name>/`. Движок здесь не хранится: клиенты и
сервер ставятся из выпусков Skillarium и работают с этой библиотекой по пути из
своего конфига.

`format` в `agent-skills.json` поднимает только миграция Skillarium. Проверка
pull request (`.gitea/workflows/policy.yml`) запускает политику выпуска,
закреплённого в `policy_engine`.

## Подключение машин

Команды печатает и записывает сюда установщик сервера (`skillarium server`).
Токен в команды не входит: установка спрашивает его сама.

<!-- skillarium:clients -->
<!-- /skillarium:clients -->
