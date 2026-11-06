#!/usr/bin/env python3
"""Acceptance rule for pull requests into the protected main branch.

Skill content may change from any connected machine after technical checks, and
so may the update policy (auto_update) of an existing external skill. Pins,
upstream bases and other server state may only reach main in
the form produced by a commit authored by the server assigned in the
*current* main branch; changes of the assignment itself are a separate
administrative operation. The rule inspects the actual changes, not labels.
"""
import argparse
import json
from pathlib import PurePosixPath
import sys

import hosting
import skills

ADMIN_TRAILER = 'Agent-Skills-Admin: assign-server'
REGISTRY = 'sources.json'
MAX_SKILL = 32 * 1024 * 1024


def git(repo, *args, check=True):
    return hosting.run_git(repo, *args, check=check).stdout


def blob(repo, revision, path):
    result = hosting.run_git(repo, 'cat-file', 'blob', f'{revision}:{path}', check=False)
    return result.stdout if result.returncode == 0 else None


def tree_id(repo, revision, path):
    result = hosting.run_git(repo, 'rev-parse', '--verify', '-q', f'{revision}:{path}', check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else None


def registry_units(repo, revision):
    data = blob(repo, revision, REGISTRY)
    if data is None:
        return {}
    registry = json.loads(data.decode('utf-8'))
    units = {('registry', key): json.dumps(value, sort_keys=True)
             for key, value in registry.items() if key != 'skills'}
    for name, entry in (registry.get('skills') or {}).items():
        units[('registry', 'skills/' + name)] = json.dumps(entry, sort_keys=True)
    return units


def policy_only(before, after):
    """An existing external skill whose entry changed in auto_update alone.

    The update policy only decides whether an update waits for a person, so any
    machine may switch it, both ways; the rest of the entry stays server state.
    """
    if before is None or after is None:
        return False
    old, new = json.loads(before), json.loads(after)
    if not isinstance(old, dict) or not isinstance(new, dict) or (old.get('source') or {}).get('kind') == 'local':
        return False
    rest = lambda entry: {key: value for key, value in entry.items() if key != 'auto_update'}
    return (rest(old) == rest(new) and isinstance(new.get('auto_update'), bool)
            and old.get('auto_update') != new.get('auto_update'))


def unit_value(repo, revision, unit, cache):
    kind, key = unit
    if kind == 'registry':
        if revision not in cache:
            try:
                cache[revision] = registry_units(repo, revision)
            except ValueError:
                cache[revision] = {}
        return cache[revision].get(unit)
    if kind == 'upstream':
        return tree_id(repo, revision, 'upstream/' + key)
    if kind == 'workflow':
        return tree_id(repo, revision, key)
    return tree_id(repo, revision, 'skills/' + key) is not None


def commits(repo, base, head):
    """Non-merge commits of the proposal with author name and parent."""
    output = git(repo, 'log', '--no-merges', '--format=%H%x1f%P%x1f%an%x1f%B%x1e', f'{base}..{head}').decode()
    result = []
    for record in output.split('\x1e'):
        if record.strip():
            revision, parents, author, message = record.strip('\n').split('\x1f', 3)
            result.append(dict(revision=revision, parent=parents.split()[0] if parents else None,
                               author=author, message=message))
    return result


def check_skill(repo, head, name):
    """Technical checks only; the meaning of the instructions is not reviewed."""
    errors = []
    try:
        skills.validate_entry_name(name)
    except ValueError as error:
        return [f'skills/{name}: {error}']
    listing = git(repo, 'ls-tree', '-r', '-l', '-z', f'{head}:skills/{name}').split(b'\0')
    paths, total = [], 0
    for record in listing:
        if not record:
            continue
        meta, path = record.split(b'\t', 1)
        mode, kind, _, size = meta.decode().split()
        path = path.decode('utf-8', 'surrogateescape')
        if kind != 'blob' or mode not in ('100644', '100755'):
            errors.append(f'skills/{name}/{path}: links and submodules are not supported')
        total += int(size) if size.isdigit() else 0
        paths.append(path)
    try:
        skills.validate_windows_paths(['skills/' + name + '/' + path for path in paths])
    except ValueError as error:
        errors.append(str(error))
    if total > MAX_SKILL:
        errors.append(f'skills/{name}: exceeds 32 MiB limit')
    text = blob(repo, head, f'skills/{name}/SKILL.md')
    if text is None:
        errors.append(f'skills/{name}: SKILL.md is missing')
    else:
        try:
            skills.validate_skill_text(text.decode('utf-8-sig'), name)
        except (ValueError, UnicodeDecodeError) as error:
            errors.append(f'skills/{name}/SKILL.md: {error}')
    return errors


def pin_only(repo, config, head):
    """True when the configuration change touches nothing but policy_engine."""
    try:
        after = json.loads(blob(repo, head, hosting.CONFIG).decode('utf-8'))
    except (ValueError, AttributeError):
        return False
    before = dict(config)
    return {key: value for key, value in before.items() if key != 'policy_engine'} == \
        {key: value for key, value in after.items() if key != 'policy_engine'} and \
        before.get('policy_engine') != after.get('policy_engine')


def pinned_policy(repo, config, server, changed, history, base, head):
    """The assigned server moves the pinned policy release forward, nothing else."""
    errors = []
    if changed != [hosting.CONFIG]:
        errors.append('The policy engine must be pinned in a separate pull request')
    if not server:
        return errors + ['No server is assigned in main; the policy engine cannot change'], []
    try:
        new = hosting.policy_engine(hosting.shared_config(repo, head))
        old = hosting.policy_engine(config)
    except ValueError as error:
        return errors + [str(error)], []
    if new is None:
        errors.append('The policy engine cannot be unpinned')
    elif old is not None and hosting.version_key(new['version']) <= hosting.version_key(old['version']):
        errors.append(f'The policy engine only moves forward (pinned {old["version"]})')
    author = hosting.service_author(server)
    touching = [item for item in history
                if tree_id(repo, item['revision'], hosting.CONFIG) != tree_id(repo, item['parent'] or base, hosting.CONFIG)]
    if not touching or any(item['author'] != author for item in touching):
        errors.append(f'The policy engine is pinned only by the assigned server {author}')
    return errors, [f'Policy engine pinned by {author}']


def evaluate(repo, base, head):
    """Return (errors, notes) for merging head into base."""
    errors, notes = [], []
    if not hosting.is_ancestor(repo, base, head):
        return ['Proposal is outdated: update it with the current main branch before merging'], notes
    try:
        config = hosting.shared_config(repo, base)
    except ValueError as error:
        config, server = None, None
        notes.append(str(error))
    else:
        server = config['server'].get('machine')
    changed = [path.decode('utf-8', 'surrogateescape') for path in
               git(repo, 'diff', '--no-renames', '--name-only', '-z', base, head).split(b'\0') if path]
    if not changed:
        return errors, ['No changes']
    history = commits(repo, base, head)
    if hosting.CONFIG in changed and config is not None and pin_only(repo, config, head):
        return pinned_policy(repo, config, server, changed, history, base, head)
    if hosting.CONFIG in changed:
        if changed != [hosting.CONFIG]:
            errors.append('Server assignment must be changed in a separate administrative pull request')
        try:
            hosting.shared_config(repo, head)
        except ValueError as error:
            errors.append(str(error))
        touching = [item for item in history
                    if tree_id(repo, item['revision'], hosting.CONFIG) != tree_id(repo, item['parent'] or base, hosting.CONFIG)]
        if not touching or any(ADMIN_TRAILER not in item['message'].splitlines() for item in touching):
            errors.append('Server assignment changes require the explicit assign-server operation')
        notes.append('Administrative server assignment')
        return errors, notes
    units = set()
    skill_names = set()
    for path in changed:
        parts = PurePosixPath(path).parts
        if path == REGISTRY:
            data = blob(repo, head, REGISTRY)
            try:
                registry = json.loads(data.decode('utf-8'))
                if registry.get('version') != 2 or not isinstance(registry.get('skills'), dict):
                    raise ValueError('Expected sources.json version 2')
                for name, entry in registry['skills'].items():
                    skills.validate_entry(name, entry)
            except (ValueError, AttributeError, KeyError, TypeError) as error:
                errors.append(f'{REGISTRY}: {error}')
                continue
            cache = {}
            before, after = registry_units(repo, base) if blob(repo, base, REGISTRY) else {}, registry_units(repo, head)
            for unit in set(before) | set(after):
                if before.get(unit) == after.get(unit):
                    continue
                if policy_only(before.get(unit), after.get(unit)):
                    notes.append(f'{unit[1]}: update policy changed')
                else:
                    units.add(unit)
        elif parts[0] == 'upstream' and len(parts) > 2:
            units.add(('upstream', parts[1]))
        elif parts[0] == 'upstream':
            errors.append(f'{path}: unexpected server state path')
        elif parts[0] in ('.gitea', '.github'):
            # The required check itself: a proposal replacing it could approve anything.
            units.add(('workflow', path))
        elif parts[0] == 'skills' and len(parts) > 2:
            skill_names.add(parts[1])
        elif parts[0] == 'skills':
            errors.append(f'{path}: files directly under skills/ are not supported')
        else:
            notes.append('Project file: ' + path)
    for name in sorted(skill_names):
        if tree_id(repo, head, 'skills/' + name) is None:
            # Removing a whole skill keeps the explicit management procedure.
            units.add(('removal', name))
        else:
            errors += check_skill(repo, head, name)
    if units:
        if not server:
            errors.append('No server is assigned in main; control changes are rejected')
        else:
            author = hosting.service_author(server)
            cache = {}
            for unit in sorted(units):
                final = unit_value(repo, head, unit, cache)
                endorsed = any(item['author'] == author and item['parent'] and
                               unit_value(repo, item['revision'], unit, cache) == final and
                               unit_value(repo, item['parent'], unit, cache) != final
                               for item in history)
                label = 'upstream/' + unit[1] if unit[0] == 'upstream' else unit[1]
                if not endorsed:
                    errors.append(f'{label}: control change must come from the assigned server {author}')
            notes.append(f'Control changes checked against assigned server {server}')
    return errors, notes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--repo', default='.')
    parser.add_argument('--base', required=True, help='Current main commit')
    parser.add_argument('--head', required=True, help='Proposal head commit')
    args = parser.parse_args()
    try:
        errors, notes = evaluate(args.repo, args.base, args.head)
    except Exception as error:
        errors, notes = [f'Policy check failed: {error}'], []
    for note in notes:
        print('NOTE: ' + note)
    for error in errors:
        print('REJECTED: ' + error, file=sys.stderr)
    if not errors:
        print('ACCEPTED')
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main())
