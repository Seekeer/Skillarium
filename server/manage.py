#!/usr/bin/env python3
"""Manage the unified skill library: server role, updates and local imports.

The library is this repository. Every command publishes through pull
requests; the protected main branch is never pushed directly.
"""
import argparse
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import absorb_updater
import hosting
import links
import maintenance
import skills

# The path as invoked (…/current/…), not resolved: schedules written from here must keep
# following current, or they would stay on one version and break when it is pruned.
ENGINE = Path(os.path.abspath(__file__)).parents[1]
CLIENT_FILES = absorb_updater.CLIENT_FILES
MANIFEST = absorb_updater.MANIFEST
SERVER_COMMANDS = ('maintain', 'register', 'set-policy', 'deploy-client')


def is_library(path):
    return (path / 'sources.json').is_file() and (path / hosting.CONFIG).is_file()


def config_path():
    """The Skillarium configuration shared with the native clients."""
    if os.name == 'nt':
        return Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Skillarium' / 'config.json'
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'skillarium' / 'config.json'


def library_name(path):
    """Name of a connected library without one, as the clients give it: the repository
    name of the clone's origin (a local remote counts too), else the folder name."""
    try:
        url = subprocess.run(['git', '-C', str(path), 'config', '--get', 'remote.origin.url'],
                             capture_output=True, text=True, check=False).stdout.strip()
    except OSError:
        url = ''
    text = url.replace('\\', '/').rstrip('/')
    if text.endswith('.git'):
        text = text[:-4]
    match = re.search(r'[/:]([^/:]+)/([^/:]+)$', text)
    return match.group(2) if match else Path(path).name


def configured_libraries():
    """Connected libraries in priority order as (name, path); the legacy "library" is one."""
    try:
        config = json.loads(config_path().read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return []
    if not isinstance(config, dict):
        return []
    if config.get('libraries') is not None:
        items = [item for item in config['libraries'] or [] if isinstance(item, dict) and isinstance(item.get('path'), str) and item['path']]
        return [(str(item.get('name') or '') or library_name(item['path']), Path(item['path'])) for item in items]
    library = config.get('library')
    return [(library_name(library), Path(library))] if isinstance(library, str) and library else []


def named_library(value):
    """A folder, or (without a path separator) the name of a connected library, as the clients read it."""
    if not any(separator in value for separator in '/\\:'):
        for known, path in configured_libraries():
            if known == value:
                return path
        if not is_library(Path(value)):
            raise ValueError(f'Library is not connected: {value} '
                             f'(connected: {", ".join(known for known, _ in configured_libraries())})')
    return Path(value)


def default_library(name=None):
    """Same order as the clients: SKILLARIUM_LIBRARY, the engine folder itself
    when it is a library (legacy layout), then the first connected library whose
    folder is a library. A name or folder picks a library instead."""
    if name:
        return named_library(name)
    if os.environ.get('SKILLARIUM_LIBRARY'):
        # Evaluated as an argparse default: an unknown value is reported later as a missing library.
        try:
            return named_library(os.environ['SKILLARIUM_LIBRARY'])
        except ValueError:
            return Path(os.environ['SKILLARIUM_LIBRARY'])
    if is_library(ENGINE):
        return ENGINE
    libraries = configured_libraries()
    reachable = [path for _, path in libraries if is_library(path)]
    return (reachable or [path for _, path in libraries] or [None])[0]


def manifest_plan(repo):
    """Ownership manifest of the native client files at the repository root."""
    hashes = {}
    for relative in CLIENT_FILES:
        path = repo / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError('Missing regular client file: ' + relative)
        hashes[relative] = hashlib.sha256(absorb_updater.canonical(path.read_bytes())).hexdigest()
    manifest = dict(version=1, generator='agent-skills/client', files=hashes)
    return manifest, (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()


def client_manifest(repo, write):
    manifest, encoded = manifest_plan(repo)
    path = repo / MANIFEST
    current = absorb_updater.canonical(path.read_bytes()) if path.exists() else b''
    if current == encoded:
        print('Client manifest current')
        return 0
    old = json.loads(current or b'{}').get('files', {})
    for relative in CLIENT_FILES:
        if old.get(relative) != manifest['files'][relative]:
            print(f'Client file changed: {relative}')
    if not write:
        print('Client manifest outdated: run manage.py client-manifest --write in the same change', file=sys.stderr)
        return 1
    path.write_bytes(encoded)
    print('Client manifest updated')
    return 0


def describe_local_import(plan):
    files = [item for item in plan['source'].rglob('*') if item.is_file()]
    print('Проверка пройдена: ' + plan['name'])
    print('Источник: ' + str(plan['source']))
    print('Папка в каталоге: ' + str(plan['destination']))
    print(f'Файлов в исходной папке: {len(files)}; байт: {sum(item.stat().st_size for item in files)}')


def client_sync(repo):
    if os.name == 'nt':
        command = ['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                   '-File', str(ENGINE / 'install.ps1'), '-Mode', 'Sync', '-Library', str(repo)]
    else:
        command = ['bash', str(ENGINE / 'install.sh'), '--library', str(repo), '--sync-only']
    return subprocess.run(command).returncode


def import_local(repo, directory, name, dry_run, publish):
    """Place a new own skill in the library; the client sync publishes it."""
    skills.set_catalog(repo)
    with maintenance.library_lock(repo):
        plan = skills.inspect_local_import(directory, name)
        describe_local_import(plan)
        if dry_run:
            print('Проверка завершена без изменений. Для импорта повторите команду без --dry-run.')
            return 0
        source, destination = plan['source'], plan['destination']
        if source != destination:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.import-') as temporary:
                staged = Path(temporary) / 'content'
                shutil.copytree(source, staged)
                if skills.validate_local_content(staged, plan['name']) != plan['content_sha256']:
                    raise ValueError('Local skill changed during import; retry')
                staged.rename(destination)
        files = skills.content_paths(destination, catalog=True)
    print(f'Скилл {plan["name"]} импортирован в {destination}')
    print(f'Файлов для Git: {len(files)}')
    for item in files:
        print('  ' + item.relative_to(destination).as_posix())
    ignored = sorted(item.relative_to(destination).as_posix() for item in destination.rglob('*')
                     if item.is_file() and item not in files)
    if ignored:
        print('Пропущено по правилам Git ignore: ' + ', '.join(ignored))
    if not publish:
        print('Публикация отключена: файлы останутся локальными до следующей синхронизации клиента.')
        return 0
    result = client_sync(repo)
    if result:
        print('Публикация не завершена; импорт сохранён в ' + str(destination), file=sys.stderr)
        print('Синхронизация клиента повторит публикацию через PR.', file=sys.stderr)
        return result
    revision = skills.git('rev-parse', '--short', 'HEAD').decode().strip()
    print(f'Опубликован коммит {revision} через PR; запись в реестре добавит сервер.')
    print('Другие подключённые станции получат скилл при следующей ежечасной синхронизации:')
    print('  Linux: bash install.sh --sync-only')
    print('  Windows: powershell -File .\\install.ps1 -Mode Sync')
    return 0


def source_skill_name(source):
    """The name from SKILL.md of the source; a folder of several skills is listed back."""
    if source['kind'] == 'url':
        return skills.validate_skill_text(skills.download(source['url']).decode('utf-8-sig'))
    revision = skills.latest_revision(source)
    found = skills.git_skill_files(source, revision)
    if 'SKILL.md' in found:
        return skills.validate_skill_text(skills.git_skill_text(source, revision))
    if not found:
        raise ValueError('По этой ссылке нет SKILL.md.')
    folders = [str(Path(item).parent.as_posix()) for item in found]
    shown = ', '.join(folders[:20]) + (f' и ещё {len(folders) - 20}' if len(folders) > 20 else '')
    raise ValueError(f'По этой ссылке несколько скиллов: {shown}. '
                     'Дайте ссылку на папку одного из них или добавьте --path <папка>.')


def ask_policy():
    missing = 'Укажите политику обновлений: --policy auto или --policy confirm.'
    if not sys.stdin or not sys.stdin.isatty():
        raise ValueError(missing)
    while True:
        try:
            answer = input('Применять совместимые обновления автоматически (auto) или подтверждать каждое (confirm)? ')
        except EOFError:
            print()
            raise ValueError(missing) from None
        if answer.strip().lower() in ('auto', 'confirm'):
            return answer.strip().lower()


def add(repo, args):
    """One entry point for a new skill: an own folder, or a link to an external source."""
    if Path(args.link).is_dir():
        if args.policy or args.path or args.ref:
            raise ValueError('Для своей папки --policy, --path и --ref не нужны: у неё нет внешнего источника.')
        return import_local(repo, Path(args.link), args.name, args.dry_run, args.publish)
    source = links.parse(args.link)
    if source['kind'] == 'git':
        if args.path:
            source['path'] = str(Path(args.path).as_posix()).strip('/') or '.'
        if args.ref:
            source['ref'] = args.ref if args.ref.startswith('refs/') else 'refs/heads/' + args.ref
    elif args.path or args.ref:
        raise ValueError('Ссылка ведёт на один файл: --path и --ref к ней не применяются.')
    name = source_skill_name(source)
    if args.name and args.name != name:
        raise ValueError(f'В SKILL.md указано имя {name}, а не {args.name}.')
    where = source['url'] if source['kind'] == 'url' else f"{source['url']} {source['ref']} {source['path']}"
    print(f'Скилл {name}: {where}')
    if args.dry_run:
        print('Проверка завершена без изменений.')
        return 0
    policy = args.policy or ask_policy()
    skills.set_catalog(repo)
    with maintenance.library_lock(repo):
        return maintenance.register(repo, name, source, policy)


def dispatch(args):
    # The client manifest describes the engine's own files, wherever the library is.
    if args.command == 'client-manifest':
        return client_manifest(ENGINE if args.repo is None else args.repo.resolve(), args.write)
    if getattr(args, 'library', None):
        args.repo = default_library(args.library)
    if args.repo is None:
        raise ValueError(f'Skill library not found: pass --repo <folder> or connect one with skillarium client ({config_path()})')
    repo = args.repo.resolve()
    if args.command == 'absorb-updater':
        return absorb_updater.absorb(repo, args.updater, args.revision)
    if args.command in ('register-local', 'import-local'):
        return import_local(repo, args.directory, getattr(args, 'name', None), args.dry_run, args.publish)
    if args.command == 'add':
        return add(repo, args)
    if args.command == 'status':
        # Only a library that still carries the engine files has a client manifest.
        if (repo / MANIFEST).is_file():
            client_manifest(repo, False)
        return maintenance.status(repo)
    skills.set_catalog(repo)
    with maintenance.library_lock(repo):
        if args.command in ('maintain', 'check', 'deploy-client'):
            check_only = args.command == 'check' or args.dry_run
            return maintenance.maintain(repo, publish=not check_only, apply_auto=args.apply_auto,
                                        dry_run=check_only)
        if args.command == 'approve':
            return maintenance.approve(repo, args.name, args.revision, args.commit)
        if args.command == 'resolve':
            return maintenance.resolve(repo, args.name, args.strategy, args.shared_hash, args.revision, args.commit)
        if args.command == 'set-policy':
            return maintenance.set_policy(repo, args.name, args.policy)
        if args.command == 'assign-server':
            return maintenance.assign_server(repo, args.machine)
        if args.command == 'pin-policy':
            release = args.release or ENGINE / 'release.json'
            if not release.is_file():
                print('This engine was not installed from a release; the policy engine stays as it is.')
                return 0
            return maintenance.pin_policy(repo, json.loads(release.read_text(encoding='utf-8')))
        if args.command == 'register':
            source = (dict(kind='git', url=args.git_url, path=str(Path(args.path).as_posix()), ref=args.ref)
                      if args.git_url else dict(kind='url', url=args.url))
            return maintenance.register(repo, args.name, source, args.policy)
    raise ValueError('Unknown command')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=default_library(),
                        help='Library working copy (default: SKILLARIUM_LIBRARY, this repository, the first connected library)')
    parser.add_argument('--catalog', help='Compatibility: the only library is agent-skills')
    commands = parser.add_subparsers(dest='command', required=True)
    names = ('status', 'add', 'maintain', 'check', 'approve', 'register', 'resolve', 'set-policy', 'assign-server',
             'pin-policy', 'register-local', 'import-local', 'deploy-client', 'client-manifest', 'absorb-updater')
    for command in names:
        sub = commands.add_parser(command)
        # Publication is always a pull request; --publish remains for compatibility.
        sub.add_argument('--publish', action='store_true', default=True)
        sub.add_argument('--dry-run', action='store_true')
        if command in ('maintain', 'check', 'deploy-client'):
            sub.add_argument('--apply-auto', action='store_true')
        if command == 'approve':
            sub.add_argument('name')
            sub.add_argument('--revision', required=True)
            sub.add_argument('--commit', required=True)
        if command == 'resolve':
            sub.add_argument('name')
            sub.add_argument('--revision')
            sub.add_argument('--commit')
            sub.add_argument('--shared-hash', required=True)
            sub.add_argument('--strategy', choices=('keep-shared', 'take-upstream', 'detach'), required=True)
        if command == 'set-policy':
            sub.add_argument('name')
            sub.add_argument('policy', choices=('auto', 'confirm'))
        if command == 'assign-server':
            sub.add_argument('machine')
        if command == 'pin-policy':
            sub.description = 'Pin the PR check of the library to the release of this engine (server).'
            sub.add_argument('--release', type=Path, help='Release feed (default: release.json of this engine)')
        if command == 'register-local':
            sub.add_argument('name')
            sub.add_argument('--directory', type=Path, required=True)
        if command == 'import-local':
            sub.description = 'Импорт собственной папки скилла; имя из SKILL.md, публикация через PR клиента.'
            sub.add_argument('directory', type=Path, help='Папка с SKILL.md, скриптами и справочными файлами')
            sub.add_argument('--no-publish', dest='publish', action='store_false', help='Скопировать без синхронизации')
        if command == 'add':
            sub.description = ('Добавить скилл: своя папка (публикуется синхронизацией клиента) или ссылка на '
                               'репозиторий, папку скилла или его SKILL.md (регистрирует назначенный сервер).')
            sub.add_argument('link', help='Папка скилла или ссылка, скопированная из браузера')
            sub.add_argument('--name', help='Проверить, что SKILL.md называет скилл так')
            sub.add_argument('--policy', choices=('auto', 'confirm'), help='Без него команда спросит')
            sub.add_argument('--path', help='Папка скилла в репозитории, если ссылка ведёт на сам репозиторий')
            sub.add_argument('--ref', help='Ветка или полный ref вместо основной ветки')
            sub.add_argument('--library', help='Имя подключённой библиотеки; по умолчанию первая в списке')
            sub.add_argument('--no-publish', dest='publish', action='store_false', help='Своя папка: только скопировать')
        if command == 'client-manifest':
            sub.add_argument('--write', action='store_true')
        if command == 'absorb-updater':
            sub.add_argument('--updater', type=Path, required=True)
            sub.add_argument('--revision', default='HEAD')
        if command == 'register':
            sub.add_argument('name')
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument('--git-url')
            source.add_argument('--url')
            sub.add_argument('--path', default='.')
            sub.add_argument('--ref', default='refs/heads/main')
            policy = sub.add_mutually_exclusive_group(required=True)
            policy.add_argument('--policy', choices=('auto', 'confirm'))
            policy.add_argument('--auto-update', choices=('yes', 'no'))
    args = parser.parse_args()
    if args.command == 'register' and args.policy is None:
        args.policy = 'auto' if args.auto_update == 'yes' else 'confirm'
    if args.command == 'resolve' and args.strategy != 'detach' and (not args.revision or not args.commit):
        parser.error('--revision and --commit are required except for --strategy detach')
    if args.command in ('import-local', 'add') and args.dry_run:
        args.publish = False
    args.apply_auto = getattr(args, 'apply_auto', False)
    if args.catalog not in (None, 'agent-skills') and (args.repo is None or args.catalog != args.repo.resolve().name):
        parser.error('Unknown catalog: ' + args.catalog)
    try:
        return dispatch(args)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
