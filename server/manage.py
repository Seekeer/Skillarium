#!/usr/bin/env python3
"""Manage the unified skill library: server role, updates and local imports.

The library is this repository. Every command publishes through pull
requests; the protected main branch is never pushed directly.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import absorb_updater
import hosting
import maintenance
import skills

LIBRARY = Path(__file__).resolve().parents[1]
CLIENT_FILES = absorb_updater.CLIENT_FILES
MANIFEST = absorb_updater.MANIFEST
SERVER_COMMANDS = ('maintain', 'register', 'set-policy', 'deploy-client')


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
                   '-File', str(repo / 'install.ps1'), '-Mode', 'Sync']
    else:
        command = ['bash', str(repo / 'install.sh'), '--sync-only']
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


def dispatch(args):
    repo = args.repo.resolve()
    if args.command == 'client-manifest':
        return client_manifest(repo, args.write)
    if args.command == 'absorb-updater':
        return absorb_updater.absorb(repo, args.updater, args.revision)
    if args.command in ('register-local', 'import-local'):
        return import_local(repo, args.directory, getattr(args, 'name', None), args.dry_run, args.publish)
    if args.command == 'status':
        client_manifest(repo, False)
        return maintenance.status(repo)
    skills.set_catalog(repo)
    with maintenance.library_lock(repo):
        if args.command in ('maintain', 'check', 'deploy-client'):
            check_only = args.command == 'check' or args.dry_run
            return maintenance.maintain(repo, publish=not check_only, apply_auto=args.apply_auto,
                                        dry_run=check_only)
        if args.command == 'approve':
            return maintenance.approve(repo, args.name, args.revision)
        if args.command == 'resolve':
            return maintenance.resolve(repo, args.name, args.strategy, args.shared_hash, args.revision)
        if args.command == 'set-policy':
            return maintenance.set_policy(repo, args.name, args.policy)
        if args.command == 'assign-server':
            return maintenance.assign_server(repo, args.machine)
        if args.command == 'register':
            source = (dict(kind='git', url=args.git_url, path=str(Path(args.path).as_posix()), ref=args.ref)
                      if args.git_url else dict(kind='url', url=args.url))
            return maintenance.register(repo, args.name, source, args.policy)
    raise ValueError('Unknown command')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', type=Path, default=LIBRARY, help='Library working copy (default: this repository)')
    parser.add_argument('--catalog', help='Compatibility: the only library is agent-skills')
    commands = parser.add_subparsers(dest='command', required=True)
    names = ('status', 'maintain', 'check', 'approve', 'register', 'resolve', 'set-policy', 'assign-server',
             'register-local', 'import-local', 'deploy-client', 'client-manifest', 'absorb-updater')
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
        if command == 'resolve':
            sub.add_argument('name')
            sub.add_argument('--revision')
            sub.add_argument('--shared-hash', required=True)
            sub.add_argument('--strategy', choices=('keep-shared', 'take-upstream', 'detach'), required=True)
        if command == 'set-policy':
            sub.add_argument('name')
            sub.add_argument('policy', choices=('auto', 'confirm'))
        if command == 'assign-server':
            sub.add_argument('machine')
        if command == 'register-local':
            sub.add_argument('name')
            sub.add_argument('--directory', type=Path, required=True)
        if command == 'import-local':
            sub.description = 'Импорт собственной папки скилла; имя из SKILL.md, публикация через PR клиента.'
            sub.add_argument('directory', type=Path, help='Папка с SKILL.md, скриптами и справочными файлами')
            sub.add_argument('--no-publish', dest='publish', action='store_false', help='Скопировать без синхронизации')
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
    if args.command == 'resolve' and args.strategy != 'detach' and not args.revision:
        parser.error('--revision is required except for --strategy detach')
    if args.command == 'import-local' and args.dry_run:
        args.publish = False
    args.apply_auto = getattr(args, 'apply_auto', False)
    if args.catalog not in (None, 'agent-skills', args.repo.resolve().name):
        parser.error('Unknown catalog: ' + args.catalog)
    try:
        return dispatch(args)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
