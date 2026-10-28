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


def deploy(root, changes):
    for relative, content in changes.items():
        destination = safe_destination(root, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Atomic replacement avoids partially written installers.
        temporary = destination.with_name('.' + destination.name + '.updater-new')
        if temporary.exists() or temporary.is_symlink():
            raise ValueError('Existing deployment temporary file preserved')
        try:
            temporary.write_bytes(content)
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
    allowed = set(CLIENT_FILES) | {MANIFEST, 'sources.json'}
    # A concurrent manual edit outside managed files must not join this commit.
    status = skills.git('status', '--porcelain', '--untracked-files=all', '-z').decode()
    changed = {item[3:] for item in status.split('\0') if item}
    if changed - allowed:
        raise ValueError('Unrelated catalog changes detected before publication')
    if paths:
        skills.git('add', '--', *sorted(paths))
        if skills.git('diff', '--cached', '--name-only').strip():
            skills.git('commit', '-m', 'Maintain skill catalog and client installers\n\n' + TRAILER)
    # Also retries a previous automation commit whose push failed.
    skills.git('push')


def operate(entry, args):
    skills.set_catalog(entry['path'])
    with catalog_lock():
        ensure_catalog(entry, clean=args.command != 'status')
        if args.command != 'status' and not args.dry_run:
            skills.git('pull', '--ff-only')
            ensure_catalog(entry, clean=True)
        plan = deployment_plan(entry['path'], args.client_dir)
        print(entry['name'] + ': client ' + ('update available' if plan else 'current'))
        if args.command == 'status':
            return 0
        if args.publish:
            ensure_publishable()
        before = skills.REGISTRY.read_bytes()
        if args.command == 'register':
            result = skills.register(args)
        elif args.command in ('maintain', 'check', 'approve'):
            result = skills.check(SimpleNamespace(
                publish=False,
                apply_auto=args.apply_auto and not args.dry_run,
                approve=[args.name] if args.command == 'approve' else [],
                expected_revision=args.revision if args.command == 'approve' else None))
        else:
            result = 0
        paths = set()
        if skills.REGISTRY.read_bytes() != before:
            paths.add('sources.json')
        if args.command != 'check' and not args.dry_run:
            deploy(entry['path'], plan)
            paths.update(plan)
        if args.publish and not args.dry_run:
            publish(paths)
            print(entry['name'] + ': published')
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=UPDATER / 'server/catalogs.json')
    parser.add_argument('--catalog', help='Catalog name from config; all by default')
    parser.add_argument('--client-dir', type=Path, default=UPDATER / 'client')
    commands = parser.add_subparsers(dest='command', required=True)
    for command in ('status', 'deploy-client', 'maintain', 'check', 'approve', 'register'):
        sub = commands.add_parser(command)
        sub.add_argument('--publish', action='store_true')
        sub.add_argument('--dry-run', action='store_true')
        if command in ('maintain', 'check'):
            sub.add_argument('--apply-auto', action='store_true')
        if command == 'approve':
            sub.add_argument('name')
            sub.add_argument('--revision', required=True)
        if command == 'register':
            sub.add_argument('name')
            source = sub.add_mutually_exclusive_group(required=True)
            source.add_argument('--git-url')
            source.add_argument('--url')
            sub.add_argument('--path', default='.')
            sub.add_argument('--ref', default='refs/heads/main')
            sub.add_argument('--auto-update', choices=('yes', 'no'), required=True)
    args = parser.parse_args()
    args.apply_auto = getattr(args, 'apply_auto', False)
    if args.command == 'check' and args.apply_auto:
        parser.error('Use maintain --apply-auto to apply automatic updates')
    if args.dry_run and (args.publish or args.command in ('register', 'approve')):
        parser.error('dry-run is for status/deploy-client/maintain/check without publish')
    try:
        entries = read_catalogs(args.config.resolve(), args.catalog)
        if args.command in ('register', 'approve') and len(entries) != 1:
            raise ValueError('Select exactly one catalog for register/approve')
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
