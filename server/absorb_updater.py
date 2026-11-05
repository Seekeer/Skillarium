#!/usr/bin/env python3
"""Absorb the agent-skills-updater history into the agent-skills library.

The result is one merge commit whose parents are the library HEAD and the
updater commit, so both histories stay reachable. The command refuses dirty
copies and colliding paths, writes nothing until the merge commit is complete
and only fast-forwards the library branch. A repeated run is a no-op.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

CLIENT_FILES = ('install.sh', 'install.ps1', 'scripts/client.sh', 'scripts/locate.sh', 'scripts/locate.ps1',
                'scripts/client.ps1', 'scripts/sync-hidden.vbs', 'projects.sh', 'projects.ps1',
                'scripts/bindings.sh', 'scripts/bindings.ps1', 'scripts/bridge.sh', 'scripts/bridge.ps1',
                'README.md', '.gitattributes')
MANIFEST = '.agent-skills-client.json'
# Updater root files that would collide with library entry points.
RENAMED = {'README.md': 'server/README.md'}
UNION = ('.gitattributes', '.gitignore')
TRAILER = 'Agent-Skills-Migration: absorb-updater'


def git(repo, *args, env=None, data=None):
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, input=data,
                            env=dict(os.environ, **(env or {})), timeout=300)
    if result.returncode:
        raise ValueError(f'git {args[0]} failed: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout


def canonical(data):
    return data.replace(b'\r\n', b'\n')


def dirty(repo):
    return git(repo, 'status', '--porcelain', '--untracked-files=all').decode().splitlines()


def tree_files(repo, rev):
    files = {}
    for record in git(repo, 'ls-tree', '-r', '-z', '--full-tree', rev).split(b'\0'):
        if record:
            meta, path = record.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            if kind != 'blob':
                raise ValueError('Unsupported tree entry: ' + os.fsdecode(path))
            files[os.fsdecode(path)] = (mode, blob)
    return files


def union_lines(first, second):
    lines = canonical(first).decode().splitlines()
    for line in canonical(second).decode().splitlines():
        if line not in lines:
            lines.append(line)
    return ('\n'.join(lines) + '\n').encode()


def plan(library, rev):
    ours = tree_files(library, 'HEAD')
    theirs = tree_files(library, rev)
    blob = lambda value: git(library, 'cat-file', 'blob', value)
    manifest = {}
    if MANIFEST in ours:
        manifest = json.loads(blob(ours[MANIFEST][1])).get('files', {})
    updates, collisions = {}, []
    for path, (mode, value) in sorted(theirs.items()):
        if path.startswith('client/') and path[7:] in CLIENT_FILES:
            target = path[7:]
            if target in ours:
                current = blob(ours[target][1])
                owned = hashlib.sha256(canonical(current)).hexdigest() == manifest.get(target)
                if canonical(current) != canonical(blob(value)) and not owned:
                    collisions.append(f'{target}: library copy has unrecognized edits')
                    continue
                if target in UNION:
                    continue
            updates[target] = (mode, value)
            continue
        target = RENAMED.get(path, path)
        if target in UNION and target in ours:
            merged = union_lines(blob(ours[target][1]), blob(value))
            updates[target] = ('100644', git(library, 'hash-object', '-w', '--stdin', data=merged).decode().strip())
            continue
        if target in ours and ours[target][1] != value:
            collisions.append(f'{path}: differs from library {target}')
            continue
        updates[target] = (mode, value)
    return updates, collisions


def absorb(library, updater, revision='HEAD'):
    library, updater = Path(library), Path(updater)
    try:
        for name, repo in (('library', library), ('updater', updater)):
            changes = dirty(repo)
            if changes:
                print(f'ERROR: {name} has local changes; preserved:', file=sys.stderr)
                for line in changes:
                    print('  ' + line, file=sys.stderr)
                return 1
        git(library, 'symbolic-ref', '-q', 'HEAD')
        rev = git(updater, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
        git(library, 'fetch', '--no-tags', str(updater.resolve()), rev)
        if subprocess.run(['git', '-C', str(library), 'merge-base', '--is-ancestor', rev, 'HEAD']).returncode == 0:
            print('Updater history already absorbed: ' + rev)
            return 0
        updates, collisions = plan(library, rev)
        if collisions:
            print('ERROR: colliding paths preserved for review:', file=sys.stderr)
            for item in collisions:
                print('  ' + item, file=sys.stderr)
            return 1
        with tempfile.TemporaryDirectory() as temporary:
            env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
            git(library, 'read-tree', 'HEAD', env=env)
            records = b''.join(f'{mode} {value}\t'.encode() + os.fsencode(path) + b'\0'
                               for path, (mode, value) in updates.items())
            git(library, 'update-index', '-z', '--index-info', env=env, data=records)
            tree = git(library, 'write-tree', env=env).decode().strip()
        message = ('Absorb agent-skills-updater history\n\n'
                   'Both histories stay reachable; client templates replace their\n'
                   'verified deployed copies at the repository root.\n\n'
                   f'{TRAILER} {rev}\n')
        commit = git(library, 'commit-tree', tree, '-p', 'HEAD', '-p', rev, '-F', '-',
                     data=message.encode()).decode().strip()
        # Only a fast-forward: an interrupted run leaves the branch untouched.
        git(library, 'merge', '--ff-only', commit)
        print(f'Absorbed updater {rev} as {commit}; {len(updates)} path(s) placed.')
        return 0
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--library', type=Path, required=True)
    parser.add_argument('--updater', type=Path, required=True)
    parser.add_argument('--revision', default='HEAD', help='Updater commit to absorb')
    args = parser.parse_args()
    return absorb(args.library, args.updater, args.revision)


if __name__ == '__main__':
    sys.exit(main())
