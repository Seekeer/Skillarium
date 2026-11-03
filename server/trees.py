"""Git object helpers: build proposal commits without touching a work tree."""
import hashlib
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile

import hosting

EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'


def git(repo, *args, env=None, data=None, check=True):
    return hosting.run_git(repo, *args, env=env, data=data, check=check).stdout


def subtree(repo, revision, path):
    result = hosting.run_git(repo, 'rev-parse', '--verify', '-q', f'{revision}:{path}', check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def files(repo, tree):
    """Return {relative path: (mode, blob)} of a tree."""
    result = {}
    for record in git(repo, 'ls-tree', '-r', '-z', tree).split(b'\0'):
        if record:
            meta, path = record.split(b'\t', 1)
            mode, kind, blob = meta.decode().split()
            if kind != 'blob' or mode not in ('100644', '100755'):
                raise ValueError('Only regular files are supported in skills: ' + os.fsdecode(path))
            result[os.fsdecode(path)] = (mode, blob)
    return result


def hashes(repo, tree):
    """content_sha256 and executable_sha256 exactly as skills.tree_hash computes them."""
    entries = files(repo, tree or EMPTY_TREE)
    order = sorted(entries, key=lambda path: PurePosixPath(path).parts)
    blobs = {}
    if order:
        data = ''.join(entries[path][1] + '\n' for path in order).encode()
        output = subprocess.run(['git', '-C', str(repo), 'cat-file', '--batch'], input=data,
                                capture_output=True, check=True).stdout
        offset = 0
        for path in order:
            header_end = output.index(b'\n', offset)
            size = int(output[offset:header_end].split()[2])
            blobs[path] = output[header_end + 1:header_end + 1 + size]
            offset = header_end + 1 + size + 1
    content, modes = hashlib.sha256(), hashlib.sha256()
    for path in order:
        content.update(path.encode('utf-8') + b'\0' + hashlib.sha256(blobs[path]).digest() + b'\0')
        modes.update(path.encode() + b'\0' + (b'x' if entries[path][0] == '100755' else b'-'))
    return content.hexdigest(), modes.hexdigest()


def folder_tree(repo, folder):
    """Store exact bytes and executable bits of a verified folder as a tree."""
    records = []
    for item in sorted(Path(folder).rglob('*')):
        if item.is_file() and not item.is_symlink():
            blob = git(repo, 'hash-object', '-w', '--stdin', data=item.read_bytes()).decode().strip()
            mode = '100755' if item.stat().st_mode & 0o111 else '100644'
            records.append(f'{mode} {blob}\t'.encode() + os.fsencode(item.relative_to(folder).as_posix()) + b'\0')
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        git(repo, 'read-tree', '--empty', env=env)
        if records:
            git(repo, 'update-index', '-z', '--index-info', env=env, data=b''.join(records))
        return git(repo, 'write-tree', env=env).decode().strip()


def edit(repo, base_tree, subtrees=None, blobs=None):
    """Replace subtrees ({path: tree or None}) and files ({path: bytes}) of a tree."""
    with tempfile.TemporaryDirectory() as temporary:
        env = dict(GIT_INDEX_FILE=str(Path(temporary) / 'index'))
        git(repo, 'read-tree', base_tree, env=env)
        for path, tree in (subtrees or {}).items():
            git(repo, 'rm', '-r', '-q', '--cached', '--ignore-unmatch', '--', path, env=env)
            if tree and tree != EMPTY_TREE:
                git(repo, 'read-tree', '--prefix=' + path + '/', tree, env=env)
        for path, data in (blobs or {}).items():
            blob = git(repo, 'hash-object', '-w', '--stdin', data=data).decode().strip()
            git(repo, 'update-index', '--add', '--cacheinfo', f'100644,{blob},{path}', env=env)
        return git(repo, 'write-tree', env=env).decode().strip()


def commit(repo, tree, parents, message, env):
    args = ['commit-tree', tree]
    for parent in parents:
        args += ['-p', parent]
    return git(repo, *args, '-F', '-', env=env, data=message.encode()).decode().strip()


def merge(repo, base, ours, theirs, env):
    """Three-way merge of skill trees in object storage.

    Returns (clean, tree): the merged tree, or a tree with conflict markers
    that only serves as review material.
    """
    empty = git(repo, 'mktree', data=b'').decode().strip()
    base_commit = commit(repo, base or empty, [], 'Upstream base', env)
    ours_commit = commit(repo, ours or empty, [base_commit], 'Shared version', env)
    theirs_commit = commit(repo, theirs or empty, [base_commit], 'Incoming version', env)
    result = hosting.run_git(repo, 'merge-tree', '--write-tree', '--no-messages',
                             ours_commit, theirs_commit, env=env, check=False)
    if result.returncode in (0, 1) and result.stdout:
        return result.returncode == 0, result.stdout.decode().split()[0]
    # Git before 2.38: merge in a disposable work tree outside any installation.
    with tempfile.TemporaryDirectory(prefix='agent-skills-merge-') as temporary:
        work = Path(temporary) / 'work'
        git(repo, 'worktree', 'add', '--detach', str(work), ours_commit)
        try:
            merged = hosting.run_git(work, '-c', 'core.hooksPath=' + os.devnull, 'merge', '--no-ff',
                                     '--no-edit', theirs_commit, env=env, check=False)
            if merged.returncode == 0:
                return True, git(work, 'rev-parse', 'HEAD^{tree}').decode().strip()
            git(work, 'add', '-A')
            return False, git(work, 'write-tree').decode().strip()
        finally:
            git(repo, 'worktree', 'remove', '--force', str(work))
