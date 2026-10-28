#!/usr/bin/env python3
"""Maintain skill catalogs and deploy the updater's native client templates."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
import sys

import skills

UPDATER = Path(__file__).resolve().parents[1]
CLIENT_FILES = ('install.sh', 'install.ps1', 'scripts/client.sh',
                'scripts/client.ps1', 'README.md', '.gitattributes')
MANIFEST = '.agent-skills-client.json'
TRAILER = 'Agent-Skills-Updater: 1'
LOCAL_IMPORT_COMMANDS = ('register-local', 'import-local')


def canonical(data):
    # Git may check out PowerShell templates as CRLF on either platform.
    return data.replace(b'\r\n', b'\n')


def digest(data):
    return hashlib.sha256(canonical(data)).hexdigest()


def read_catalogs(config, selected):
    data = json.loads(config.read_text(encoding='utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('catalogs'), list):
        raise ValueError('Expected catalogs.json version 1 with a catalogs list')
    names = set()
    result = []
    for entry in data['catalogs']:
        name = entry['name']
        if not isinstance(name, str) or not name or name in names:
            raise ValueError('Catalog names must be nonempty and unique')
        names.add(name)
        path = Path(entry['path'])
        entry = dict(entry, path=(config.parent / path).resolve())
        if not selected or name == selected:
            result.append(entry)
    if selected and not result:
        raise ValueError('Unknown catalog: ' + selected)
    return result


def ensure_catalog(entry, clean=False):
    path = entry['path']
    if path == UPDATER or not path.is_dir():
        raise ValueError('Catalog path must be a separate existing repository')
    skills.set_catalog(path)
    actual = Path(skills.git('rev-parse', '--show-toplevel').decode().strip()).resolve()
    if actual != path:
        raise ValueError('Catalog path is not its Git root')
    if skills.git('remote', 'get-url', 'origin').decode().strip() != entry['remote']:
        raise ValueError('Catalog origin differs from configured remote')
    if skills.git('symbolic-ref', '--short', 'HEAD').decode().strip() != entry['branch']:
        raise ValueError('Catalog is on a different branch')
    upstream = skills.git('rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{upstream}').decode().strip()
    if upstream != 'origin/' + entry['branch']:
        raise ValueError('Catalog upstream differs from configured origin/branch')
    if clean and skills.git('status', '--porcelain').strip():
        raise ValueError('Catalog has local changes; preserved')
    registry = skills.load_registry()
    for name, record in registry['skills'].items():
        skills.validate_entry(name, record)
        pattern = r'[0-9a-f]{40}|[0-9a-f]{64}' if record['source']['kind'] == 'git' else r'[0-9a-f]{64}'
        if not re.fullmatch(pattern, record.get('revision', '')) or not re.fullmatch(r'[0-9a-f]{64}', record.get('content_sha256', '')):
            raise ValueError('Catalog entry lacks a complete approved revision/hash: ' + name)
    return registry


def ensure_registration_clean(args):
    if args.command not in LOCAL_IMPORT_COMMANDS:
        if skills.git('status', '--porcelain', '--untracked-files=all').strip():
            raise ValueError('Catalog has local changes; preserved')
        return
    skills.validate_entry(args.name, dict(source=dict(kind='local', path='skills/' + args.name), auto_update=False))
    prefix = 'skills/' + args.name + '/'
    # An explicitly supplied new skill folder is the only permitted dirty path.
    for item in skills.git('status', '--porcelain', '--untracked-files=all', '-z').decode().split('\0'):
        if item and (not item.startswith('?? ') or not item[3:].startswith(prefix)):
            raise ValueError('Unrelated catalog changes preserved; registration stopped')


@contextmanager
def catalog_lock():
    path = Path(skills.git('rev-parse', '--git-path', 'agent-skills-sync.lock').decode().strip())
    if not path.is_absolute():
        path = skills.ROOT / path
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Catalog client/server operation already running')
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def safe_destination(root, relative):
    destination = root / relative
    current = destination
    while current != root:
        if current.is_symlink():
            raise ValueError('Managed path contains a symlink: ' + relative)
        current = current.parent
    return destination


def deployment_plan(root, client_dir):
    desired = {}
    hashes = {}
    for relative in CLIENT_FILES:
        source = client_dir / relative
        if not source.is_file() or source.is_symlink():
            raise ValueError('Missing regular client template: ' + relative)
        desired[relative] = canonical(source.read_bytes())
        hashes[relative] = digest(desired[relative])
    manifest_path = safe_destination(root, MANIFEST)
    previous = {}
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest.get('version') != 1 or not isinstance(manifest.get('files'), dict):
            raise ValueError('Invalid client deployment manifest')
        previous = manifest['files']
        if set(previous) - set(CLIENT_FILES):
            raise ValueError('Unknown managed files; review template migration manually')
    changes = {}
    for relative, content in desired.items():
        destination = safe_destination(root, relative)
        if destination.exists():
            if not destination.is_file():
                raise ValueError('Managed path is not a regular file: ' + relative)
            current = digest(destination.read_bytes())
            if current == hashes[relative]:
                continue
            if current != previous.get(relative):
                raise ValueError('Client file has unrecognized edits; preserved: ' + relative)
        changes[relative] = content
    manifest = dict(version=1, generator='agent-skills-updater/client', files=hashes)
    encoded = (json.dumps(manifest, indent=2, sort_keys=True) + '\n').encode()
    if not manifest_path.exists() or canonical(manifest_path.read_bytes()) != encoded:
        changes[MANIFEST] = encoded
    return changes


def shared_migration_plan(root):
    """Move only verified legacy versions; Git retains their history."""
    data = skills.load_registry()
    changes = {}
    registry_changed = False
    for name, entry in data['skills'].items():
        skills.validate_entry(name, entry)
        custom = entry.get('customization')
        if custom and not custom.get('path'):
            source = skills.shared_skill(name, entry)
            target = safe_destination(root, 'skills/' + name)
            if target.exists() and (not target.is_dir() or skills.tree_hash(target) not in (custom['content_sha256'], entry['content_sha256'])):
                raise ValueError('Existing shared skill differs; preserved: ' + name)
            for item in source.rglob('*'):
                if item.is_file():
                    relative = 'skills/' + name + '/' + item.relative_to(source).as_posix()
                    safe_destination(root, relative)
                    changes[relative] = (item.read_bytes(), item.stat().st_mode & 0o777)
            if target.exists():
                for item in target.rglob('*'):
                    if item.is_file() and not (source / item.relative_to(target)).exists():
                        changes[item.relative_to(root).as_posix()] = None
            custom['path'] = 'skills/' + name
            registry_changed = True
        legacy = safe_destination(root, 'customizations/' + name)
        if not legacy.exists():
            continue
        if not legacy.is_dir():
            raise ValueError('Unrecognized legacy skill data; preserved: ' + name)
        for version in legacy.iterdir():
            if (version.is_symlink() or not version.is_dir()
                    or not re.fullmatch(r'[0-9a-f]{64}', version.name)
                    or skills.tree_hash(version) != version.name):
                raise ValueError('Legacy skill has unrecognized edits; preserved: ' + name)
            for item in version.rglob('*'):
                if item.is_file():
                    changes[item.relative_to(root).as_posix()] = None
    if registry_changed:
        changes['sources.json'] = (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode()
    return changes


def deploy(root, changes):
    for relative, content in changes.items():
        destination = safe_destination(root, relative)
        if content is None:
            destination.unlink()
            parent = destination.parent
            while parent != root:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
            continue
        mode = None
        if isinstance(content, tuple):
            content, mode = content
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement avoids partially written installers.
        temporary = destination.with_name('.' + destination.name + '.updater-new')
        if temporary.exists() or temporary.is_symlink():
            raise ValueError('Existing deployment temporary file preserved')
        try:
            temporary.write_bytes(content)
            if mode is not None:
                temporary.chmod(mode)
            os.replace(temporary, destination)
        finally:
            if temporary.exists():
                temporary.unlink()


def ensure_publishable():
    for revision in skills.git('rev-list', '@{upstream}..HEAD').decode().splitlines():
        message = skills.git('show', '-s', '--format=%B', revision).decode()
        if TRAILER not in message.splitlines():
            raise ValueError('Unpublished manual commits exist; publish/reconcile them explicitly')


def publish(paths):
    ensure_publishable()
    skills.validate_catalog_windows_paths()
    allowed = set(CLIENT_FILES) | {MANIFEST, 'sources.json'} | set(paths)
    # A concurrent manual edit outside managed files must not join this commit.
    status = skills.git('-c', 'status.renames=false', 'status', '--porcelain', '--untracked-files=all', '-z').decode()
    changed = {item[3:] for item in status.split('\0') if item}
    folders = {'skills/' + name for name in skills.load_registry()['skills']}
    if any(path not in allowed and not any(path.startswith(folder + '/') for folder in folders) for path in changed):
        raise ValueError('Unrelated catalog changes detected before publication')
    metadata = sorted(set(paths) & (set(CLIENT_FILES) | {MANIFEST, 'sources.json'}))
    if paths:
        if metadata:
            skills.git('add', '-A', '--', *metadata)
        if skills.git('diff', '--cached', '--name-only').strip():
            skills.git('commit', '-m', 'Maintain skill catalog and client installers\n\n' + TRAILER)
    # Also retries a previous automation commit whose push failed.
    skills.git('push')


def describe_local_import(plan):
    files = [item for item in plan['source'].rglob('*') if item.is_file()]
    print('Проверка пройдена: ' + plan['name'])
    print('Источник: ' + str(plan['source']))
    print('Папка в каталоге: ' + str(plan['destination']))
    print(f'Файлов в исходной папке: {len(files)}; байт: {sum(item.stat().st_size for item in files)}')


def describe_import_result(entry, args):
    folder = skills.ROOT / 'skills' / args.name
    files = skills.content_paths(folder, catalog=True)
    print(f'Скилл {args.name} импортирован в {folder}')
    print(f'Файлов для Git: {len(files)}')
    for item in files:
        print('  ' + item.relative_to(folder).as_posix())
    ignored = sorted(item.relative_to(folder).as_posix() for item in folder.rglob('*')
                     if item.is_file() and item not in files)
    if ignored:
        print('Пропущено по правилам Git ignore: ' + ', '.join(ignored))
    if args.publish:
        revision = skills.git('rev-parse', '--short', 'HEAD').decode().strip()
        print(f'Опубликован коммит {revision}, ветка {entry["branch"]}.')
        print('Другие подключённые станции получат скилл при следующей ежечасной синхронизации.')
    else:
        print('Публикация отключена: проверьте и опубликуйте подготовленные изменения Git вручную.')
        return
    print('Для подключения выполните из копии общего каталога:')
    print('  Linux: bash install.sh --sync-only')
    print('  Windows: powershell -File .\\install.ps1 -Mode Sync')


def operate(entry, args):
    skills.set_catalog(entry['path'])
    with catalog_lock():
        ensure_catalog(entry)
        if args.command in LOCAL_IMPORT_COMMANDS:
            local_plan = skills.inspect_local_import(args.directory, getattr(args, 'name', None))
            args.name = local_plan['name']
            if args.command == 'import-local':
                describe_local_import(local_plan)
        if args.command != 'status':
            ensure_registration_clean(args)
        if args.command != 'status' and not args.dry_run:
            skills.git('pull', '--ff-only')
            ensure_catalog(entry)
            ensure_registration_clean(args)
        plan = deployment_plan(entry['path'], args.client_dir)
        print(entry['name'] + ': client ' + ('update available' if plan else 'current'))
        if args.command == 'status':
            return 0
        if args.command == 'import-local' and args.dry_run:
            ensure_publishable()
            print('Проверка завершена без изменений. Для импорта повторите команду без --dry-run.')
            return 0
        if args.publish:
            ensure_publishable()
        before = skills.REGISTRY.read_bytes()
        paths = set()
        if args.command in ('maintain', 'deploy-client'):
            migration = shared_migration_plan(entry['path'])
            if migration:
                print(entry['name'] + ': migrate shared skills to stable paths')
                if not args.dry_run:
                    deploy(entry['path'], migration)
                    skills.git('add', '-f', '-A', '--', *sorted(migration))
                    paths.update(migration)
        if args.command == 'resolve':
            result = skills.resolve(args)
        elif args.command in LOCAL_IMPORT_COMMANDS:
            result = skills.register_local(args)
            paths.add('skills/' + args.name)
        elif args.command == 'register':
            result = skills.register(args)
        elif args.command in ('maintain', 'check', 'approve'):
            result = skills.check(SimpleNamespace(
                publish=False,
                apply_auto=args.apply_auto and not args.dry_run,
                apply_catalog=args.command != 'check' and not args.dry_run,
                approve=[args.name] if args.command == 'approve' else [],
                expected_revision=args.revision if args.command == 'approve' else None))
        else:
            skills.populate_catalog(write=not args.dry_run)
            result = 0
        if not args.dry_run:
            paths.update('skills/' + name for name in skills.load_registry()['skills']
                         if (entry['path'] / 'skills' / name).exists())
        if skills.REGISTRY.read_bytes() != before:
            paths.add('sources.json')
        if args.command != 'check' and not args.dry_run:
            deploy(entry['path'], plan)
            paths.update(plan)
        if args.publish and not args.dry_run:
            try:
                publish(paths)
            except Exception:
                if args.command == 'import-local':
                    print('Публикация не завершена; импорт сохранён в ' + str(local_plan['destination']), file=sys.stderr)
                    if skills.git('status', '--porcelain').strip():
                        print('Проверьте подготовленные изменения и завершите commit/push вручную.', file=sys.stderr)
                    else:
                        print('Устраните ошибку Git и повторите публикацию: maintain --publish для этого каталога.', file=sys.stderr)
                raise
            print(entry['name'] + ': published')
        if args.command == 'import-local':
            describe_import_result(entry, args)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=UPDATER / 'server/catalogs.json')
    parser.add_argument('--catalog', help='Catalog name from config; all by default')
    parser.add_argument('--client-dir', type=Path, default=UPDATER / 'client')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'deploy-client', 'maintain', 'check', 'approve', 'register', 'resolve', 'register-local', 'import-local'):
        sub = commands.add_parser(command)
        sub.add_argument('--publish', action='store_true', default=command == 'import-local')
        sub.add_argument('--dry-run', action='store_true')
        if command in ('maintain', 'check'):
            sub.add_argument('--apply-auto', action='store_true')
        if command == 'approve':
            sub.add_argument('name')
            sub.add_argument('--revision', required=True)
        if command == 'resolve':
            sub.add_argument('name')
            sub.add_argument('--revision')
            sub.add_argument('--shared-hash', required=True)
            sub.add_argument('--strategy', choices=('keep-shared', 'take-upstream', 'detach'), required=True)
        if command == 'register-local':
            sub.add_argument('name')
            sub.add_argument('--directory', type=Path, required=True)
        if command == 'import-local':
            sub.description = 'Импорт собственной папки скилла; имя из SKILL.md, публикация включена по умолчанию.'
            sub.add_argument('directory', type=Path, help='Папка с SKILL.md, скриптами и справочными файлами')
            sub.add_argument('--no-publish', dest='publish', action='store_false', help='Импортировать без отправки в Git')
        if command == 'register':
            sub.add_argument('name')
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument('--git-url')
            source.add_argument('--url')
            sub.add_argument('--path', default='.')
            sub.add_argument('--ref', default='refs/heads/main')
            sub.add_argument('--auto-update', choices=('yes', 'no'), required=True)
    args = parser.parse_args()
    if args.command == 'import-local' and args.dry_run:
        args.publish = False
    if args.command == 'resolve' and args.strategy != 'detach' and not args.revision:
        parser.error('--revision is required except for --strategy detach')
    args.apply_auto = getattr(args, 'apply_auto', False)
    if args.command == 'check' and args.apply_auto:
        parser.error('Use maintain --apply-auto to apply automatic updates')
    if args.dry_run and (args.publish or args.command in ('register', 'approve', 'resolve', 'register-local')):
        parser.error('dry-run is for status/deploy-client/maintain/check without publish')
    try:
        entries = read_catalogs(args.config.resolve(), args.catalog)
        if args.command in ('register', 'approve', 'resolve', *LOCAL_IMPORT_COMMANDS) and len(entries) != 1:
            raise ValueError('Выберите один каталог через --catalog <name> для регистрации или разрешения конфликта')
        result = 0
        for entry in entries:
            try:
                result = max(result, operate(entry, args))
            except Exception as error:
                print(f'ERROR {entry["name"]}: {error}', file=sys.stderr)
                result = 1
        return result
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
