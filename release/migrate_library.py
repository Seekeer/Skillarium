#!/usr/bin/env python3
"""Prepare the commit that removes the engine from a legacy library (stage 7).

    python release/migrate_library.py --library ~/agent-skills [--url https://host/owner/skills.git]

The library keeps its data (skills/, sources.json, agent-skills.json,
upstream/ and any other folder of its own); every path that belongs to the
Skillarium engine repository is removed, and the library template supplies
the PR check workflow, .gitattributes and README. The commit is made on a new
branch of the clone (default skillarium/remove-engine) from origin/main and
is not pushed: open the pull request yourself once every machine runs the
installed engine (`skillarium status` / the bridge) and the server has pinned
the policy engine (`manage.py pin-policy`).
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import hosting  # noqa: E402
import setup  # noqa: E402

TEMPLATE = ROOT / 'templates' / 'library'
# Library data is never removed, even where the engine repository has the same path.
KEEP = ('skills/', 'upstream/', 'sources.json', 'agent-skills.json', 'customizations/', 'versions/', 'docs/')


def git(repo, *args, data=None):
    result = subprocess.run(['git', '-C', str(repo), *args], input=data, capture_output=True)
    if result.returncode:
        raise SystemExit(f'git {args[0]} failed: ' + result.stderr.decode(errors='replace').strip())
    return result.stdout.decode().strip()


def engine_paths():
    """Top-level names and files of the engine repository at HEAD."""
    names = git(ROOT, 'ls-tree', '-r', '--name-only', '-z', 'HEAD').split('\0')
    return {name for name in names if name} | {name.split('/', 1)[0] + '/' for name in names if '/' in name}


def is_engine(path, engine):
    if path.startswith(KEEP):
        return False
    top = path.split('/', 1)[0] + '/' if '/' in path else path
    return path in engine or top in engine or path == '.agent-skills-client.json'


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--library', type=Path, required=True, help='A clone of the legacy library')
    parser.add_argument('--branch', default='skillarium/remove-engine')
    parser.add_argument('--url', help='Library URL for the client commands in README (default: origin)')
    args = parser.parse_args()
    repo = args.library.resolve()
    git(repo, '-c', 'credential.interactive=false', 'fetch', '-q', 'origin')
    config = hosting.shared_config(repo, 'refs/remotes/origin/main')
    if hosting.policy_engine(config) is None:
        raise SystemExit('The PR check is not pinned yet: install the server from a release '
                         '(skillarium server --repo ...) and let it run manage.py pin-policy first.')
    if git(repo, 'status', '--porcelain'):
        raise SystemExit(f'{repo} has local changes; use a clean clone')
    url = args.url or git(repo, 'config', '--get', 'remote.origin.url')
    try:
        base, owner, name = setup.parse_repository(url)
    except setup.SetupError:
        raise SystemExit(f'origin is {url}; pass the https address of the library with --url')
    server = config['server'].get('machine')
    if not server:
        raise SystemExit('No server is assigned in agent-skills.json')
    git(repo, 'checkout', '-q', '-B', args.branch, 'refs/remotes/origin/main')
    engine = engine_paths()
    tracked = [name for name in git(repo, 'ls-files', '-z').split('\0') if name]
    # Old workflows ran the engine's own tests; only the template's policy check stays.
    removed = [name for name in tracked if is_engine(name, engine)
               or (name.startswith('.gitea/workflows/') and name != '.gitea/workflows/policy.yml')]
    if removed:
        for start in range(0, len(removed), 200):
            git(repo, 'rm', '-q', '--', *removed[start:start + 200])
    for item in TEMPLATE.rglob('*'):
        if item.is_file():
            target = repo / item.relative_to(TEMPLATE)
            if target.name in ('agent-skills.json', 'sources.json'):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    readme = repo / 'README.md'
    readme.write_text(setup.readme_with_commands(readme.read_text(encoding='utf-8'), f'{base}/{owner}/{name}.git'),
                      encoding='utf-8', newline='\n')
    git(repo, 'add', '-A', '--', '.gitattributes', '.gitea', 'README.md')
    # The workflow change is server state: the commit carries the assigned server's service author.
    environment = dict(os.environ, **hosting.author_env(server))
    subprocess.run(['git', '-C', str(repo), 'commit', '-q', '-m', 'Move the engine out of the library\n\n'
        'The library keeps its skills, registry and configuration; machines run the engine installed '
        'from Skillarium releases, and the PR check runs the pinned Skillarium policy.\n\n'
        f'Agent-Skills-Server: {server}\n'], env=environment, check=True)
    print(f'Branch {args.branch}: {len(removed)} engine files removed. Next:')
    print(f'  git -C "{repo}" push origin {args.branch}')
    print('  then open a pull request into main; the policy check accepts it like any project-file change.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
