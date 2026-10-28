# Проверки

Полный набор серверных и Linux-тестов:

```bash
python3 -m unittest discover -s tests -v
AGENT_SKILLS_PWSH=/path/to/pwsh python3 -m unittest discover -s tests -v
```

Нужны Python 3.9+, Git, Bash, jq и flock. Все Git-каталоги и пользовательские
папки создаются во временных директориях. Дополнительный PowerShell проверяет
Git-синхронизацию и отказы при несовместимых путях на Linux.

`test_windows_paths.py` работает также на Windows. Проверяются запрещённые
символы, device names с расширениями, завершающие точки/пробелы, предел
компонента в UTF-16, коллизии регистра файлов и родительских папок.
`test_windows_publication.py` проверяет отказы при импорте, обновлении,
повторной публикации и автокоммитах обоих клиентов.

## Нативный Windows runner

В `.gitea/workflows/tests.yml` есть отдельная Windows job. Для неё нужен
Gitea Actions runner на Windows с меткой `windows-latest:host`, Git,
Python 3.9+ в PATH, Windows PowerShell 5.1 и доступной службой Task Scheduler.
Метка `:host` при регистрации означает выполнение на самой Windows,
а не в Linux-контейнере. Gitea не выдаёт Windows VM по имени метки: если
такого runner нет, job останется в очереди. Linux job использует имеющуюся
метку `ubuntu-latest`.

Используйте отдельную одноразовую Windows VM/учётную запись, одну job на
учётную запись одновременно. Runner должен выполнять только доверенный код
этого проекта. Не подключайте рабочую пользовательскую станцию как host runner
для непроверенных pull requests.

Ручной запуск той же проверки из корня репозитория:

```powershell
python -m unittest discover -s tests -p test_windows_paths.py -v
powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File tests/windows_native.ps1
```

`windows_native.ps1` создаёт временные HOME/USERPROFILE, локальный bare remote
и каталог с пробелами в пути. Он проверяет настоящие NTFS junction для обоих
агентов, передачу локальных и внешних Git-правок, точные CRLF, сохранение чужой
папки, регистрацию/содержимое/удаление настоящей задачи Планировщика и то,
что Uninstall оставляет скиллы доступными. При наличии существующей задачи
agent-skills для текущей учётной записи тест останавливается до изменений.
В конце он удаляет свою задачу, junction и временные файлы.

Запланированную задачу тест не запускает: новый процесс Планировщика получил
бы настоящий профиль runner, а не временный HOME текущего теста. Проверяются
её часовой триггер, аргументы с кавычками, рабочая папка, logon type и запрет
перекрывающихся запусков. Исполнение Sync проверяется прямым запуском
установщика в изолированном профиле.

Linux-проверки и синтаксический разбор PowerShell не подтверждают работу
junction или Task Scheduler. Результат Windows job нужен отдельно.
