"""Server role: registry maintenance and external updates as pull requests.

All work happens in a disposable worktree of origin/main or directly in Git
object storage. The active library copy of the server machine is never
changed here; it receives accepted results by its normal client sync.
"""
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sys
import tempfile
import time

import hosting
import skills
import trees

REPORT = 'skill-updates.json'
SERVER_PREFIX = 'agent-skills/server/'
UPDATE_PREFIX = 'agent-skills/update/'
MATERIALS_PREFIX = 'agent-skills/materials/'
ADMIN_PREFIX = 'agent-skills/admin/'
TRAILERS = ('Proposal', 'Status', 'Policy', 'Incoming-Revision', 'Base-Tree', 'Shared-Tree',
            'Incoming-Tree', 'Result-Tree', 'Key', 'Server')


def git(repo, *args, **kwargs):
    return hosting.run_git(repo, *args, **kwargs).stdout


def git_dir(repo):
    return Path(git(repo, 'rev-parse', '--absolute-git-dir').decode().strip())


@contextmanager
def library_lock(repo):
    """The lock shared with the native clients of the same working copy."""
    path = Path(git(repo, 'rev-parse', '--git-path', 'agent-skills-sync.lock').decode().strip())
    if not path.is_absolute():
        path = Path(repo) / path
    with path.open('a') as stream:
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ValueError('Another client/server operation is running on this copy; skipped')
        yield


class Context:
    def __init__(self, repo, need_server):
        self.repo = Path(repo).resolve()
        self.machine = hosting.machine_name(self.repo)
        fetched = hosting.run_git(self.repo, '-c', 'credential.interactive=false', 'fetch', '--prune', 'origin', check=False)
        if fetched.returncode:
            raise hosting.HostingError('network', 'Current main is unavailable; server publication pending')
        config = hosting.shared_config(self.repo, 'refs/remotes/origin/main')
        self.branch = hosting.main_branch(config)
        self.main = git(self.repo, 'rev-parse', f'refs/remotes/origin/{self.branch}^{{commit}}').decode().strip()
        self.config = hosting.shared_config(self.repo, self.main)
        self.assigned = self.config['server'].get('machine')
        if need_server and self.assigned != self.machine:
            raise ValueError(f'This machine ({self.machine}) is not the assigned server '
                             f'({self.assigned or "none"}); server publication not performed')
        self.env = hosting.author_env(self.machine)
        self._hosting = None

    @property
    def hosting(self):
        if self._hosting is None:
            self._hosting = hosting.Hosting(self.repo, self.branch)
        return self._hosting

    def tree(self, revision=None):
        return git(self.repo, 'rev-parse', (revision or self.main) + '^{tree}').decode().strip()

    def registry(self, revision=None):
        return json.loads(git(self.repo, 'show', f'{revision or self.main}:sources.json').decode('utf-8'))

    def commit(self, tree, message, parents=None, branch=None):
        parents = parents or [self.main]
        if branch:
            # Reuse the commit an interrupted earlier attempt already pushed.
            found = hosting.run_git(self.repo, 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}^{{commit}}', check=False)
            head = found.stdout.decode().strip()
            if found.returncode == 0 and self.tree(head) == tree and \
                    git(self.repo, 'log', '-1', '--format=%P', head).decode().split() == parents:
                return head
        return trees.commit(self.repo, tree, parents,
                            message + f'\n\nAgent-Skills-Server: {self.machine}\n', self.env)

    def publish(self, head, branch, title, body, automerge=True):
        result = hosting.publish(self.repo, head, branch, title, body, automerge=automerge, hosting=self.hosting)
        print(f'{branch}: {describe(result)}')
        return result

    def close_others(self, prefix, keep=()):
        for pull in self.hosting.pulls_with_prefix(prefix):
            if pull['head']['ref'] not in keep:
                self.hosting.close_pull(pull['number'])
                hosting.run_git(self.repo, 'push', 'origin', '--delete', pull['head']['ref'], check=False)
                print(f'Superseded proposal closed: #{pull["number"]} {pull["head"]["ref"]}')


def describe(result):
    if result['state'] == 'merged':
        return 'already merged'
    text = f'pull request #{result.get("number")} {result["state"]}'
    return text + (f' {result["url"]}' if result.get('url') else '')


def registry_blob(data):
    return (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode('utf-8')


def write_report(repo, reports):
    path = git_dir(repo) / REPORT
    skills.atomic_json(path, reports)
    print(f'Checked {len(reports)} skill(s); report: {path}')


@contextmanager
def scratch_worktree(ctx):
    root = git_dir(ctx.repo) / 'agent-skills-server'
    root.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix='work-', dir=root))
    git(ctx.repo, 'worktree', 'add', '--detach', str(work), ctx.main)
    previous = skills.ROOT
    try:
        skills.set_catalog(work)
        yield work
    finally:
        skills.set_catalog(previous)
        hosting.run_git(ctx.repo, 'worktree', 'remove', '--force', str(work), check=False)
        shutil.rmtree(work, ignore_errors=True)


def legacy_migration(work):
    """Move verified customizations/<name>/<hash>/ to skills/<name>/; Git keeps history."""
    data = skills.load_registry()
    changed = False
    for name, entry in data['skills'].items():
        skills.validate_entry(name, entry)
        custom = entry.get('customization')
        if custom and not custom.get('path'):
            source = skills.shared_skill(name, entry)
            target = work / 'skills' / name
            if target.exists() and skills.tree_hash(target) not in (custom['content_sha256'], entry['content_sha256']):
                raise ValueError('Existing shared skill differs; preserved: ' + name)
            tree = trees.folder_tree(work, source)
            git(work, 'rm', '-r', '-q', '--ignore-unmatch', '--', 'skills/' + name)
            git(work, 'read-tree', '--prefix=skills/' + name + '/', '-u', tree)
            custom['path'] = 'skills/' + name
            changed = True
        legacy = work / 'customizations' / name
        if legacy.exists():
            for version in legacy.iterdir():
                if (version.is_symlink() or not version.is_dir()
                        or not re.fullmatch(r'[0-9a-f]{64}', version.name)
                        or skills.tree_hash(version) != version.name):
                    raise ValueError('Legacy skill has unrecognized edits; preserved: ' + name)
            git(work, 'rm', '-r', '-q', '--', 'customizations/' + name)
            changed = True
    if changed:
        skills.save_registry(data)
        git(work, 'add', '--', 'sources.json')
        print('Legacy shared skills migrated to stable paths')


def base_tree(ctx, work, name, entry):
    """Verified upstream base of an external skill, from our own history first."""
    stored = trees.subtree(work, 'HEAD', 'upstream/' + name)
    expected = (entry['content_sha256'], entry.get('executable_sha256'))
    if stored and trees.hashes(work, stored) == expected:
        return stored
    shared = trees.subtree(work, 'HEAD', 'skills/' + name)
    if shared and not entry.get('customization') and trees.hashes(work, shared) == expected:
        return shared
    pinned = dict(entry)
    pinned.pop('customization', None)
    try:
        folder = skills.materialize(name, pinned)[0]
    except Exception as error:
        print(f'{name}: upstream base unavailable ({error}); later updates are conflicts', file=sys.stderr)
        return None
    return trees.folder_tree(work, folder)


def observe(ctx, work, reports):
    """Record published client edits, new local skills and upstream bases."""
    legacy_migration(work)
    data = skills.load_registry()
    for name, entry in list(data['skills'].items()):
        try:
            skills.prepare_catalog_skill(name, entry, write=True)
        except Exception as error:
            reports[name] = dict(status='error', error=str(error))
            print(f'ERROR {name}: {error}', file=sys.stderr)
    folders = {path.split('/')[1] for path in git(work, 'ls-files', '-z', '--', 'skills').decode().split('\0')
               if path.count('/') >= 2}
    for name in sorted(folders - set(data['skills'])):
        folder = work / 'skills' / name
        try:
            skills.validate_entry_name(name)
            digest = skills.validate_local_content(folder, name, catalog=True)
        except Exception as error:
            reports[name] = dict(status='error', error='Unregistered folder is not a valid skill: ' + str(error))
            print(f'ERROR {name}: {reports[name]["error"]}', file=sys.stderr)
            continue
        data['skills'][name] = dict(source=dict(kind='local', path='skills/' + name), auto_update=False,
            revision=digest, content_sha256=digest, executable_sha256=skills.executable_hash(folder, catalog=True))
        print(f'{name}: registered published local skill')
    skills.save_registry(data)
    git(work, 'add', '--', 'sources.json')
    for name, entry in data['skills'].items():
        if entry['source']['kind'] == 'local' or name in reports:
            continue
        tree = base_tree(ctx, work, name, entry)
        if tree and tree != trees.subtree(work, 'HEAD', 'upstream/' + name):
            git(work, 'rm', '-r', '-q', '--ignore-unmatch', '--', 'upstream/' + name)
            git(work, 'read-tree', '--prefix=upstream/' + name + '/', '-u', tree)
    for path in git(work, 'ls-files', '-z', '--', 'upstream').decode().split('\0'):
        parts = path.split('/')
        if len(parts) > 2 and (parts[1] not in data['skills'] or data['skills'][parts[1]]['source']['kind'] == 'local'):
            git(work, 'rm', '-r', '-q', '--', 'upstream/' + parts[1])
    return git(work, 'write-tree').decode().strip(), data


def pinned_entry(work, name, entry, revision, incoming, result):
    """Pin the incoming version as the new base; record a differing result."""
    entry = dict(entry)
    entry.pop('customization', None)
    content, modes = trees.hashes(work, incoming)
    entry.update(revision=revision, content_sha256=content, executable_sha256=modes)
    if result != incoming:
        shared, shared_modes = trees.hashes(work, result)
        entry['customization'] = dict(path='skills/' + name, content_sha256=shared, base_revision=revision,
                                      base_sha256=content, executable_sha256=shared_modes)
    return entry


def incoming_updates(ctx, work, data, observed, reports, approve=()):
    """Classify every external skill; return prepared update items."""
    items = []
    for name, entry in data['skills'].items():
        if name in reports:
            continue
        if entry['source']['kind'] == 'local':
            reports[name] = dict(status='local', content_sha256=entry['content_sha256'])
            print(f'{name}: local; no external source')
            continue
        base = trees.subtree(work, observed, 'upstream/' + name)
        ours = trees.subtree(work, observed, 'skills/' + name)
        shared_hash = trees.hashes(work, ours)[0] if ours else None
        try:
            folder, revision, digest = skills.materialize(name, entry, approved=False)
            incoming = trees.folder_tree(work, folder)
        except Exception as error:
            reports[name] = dict(status='source-unavailable', error=str(error),
                                 base=dict(revision=entry['revision'], content_sha256=entry['content_sha256']),
                                 shared=dict(content_sha256=shared_hash, path='skills/' + name))
            print(f'{name}: source unavailable; installed skill preserved ({error})', file=sys.stderr)
            continue
        pin = (entry['content_sha256'], entry.get('executable_sha256'))
        if incoming == base or (base is None and trees.hashes(work, incoming) == pin):
            status = 'customized' if entry.get('customization') else 'unchanged'
            reports[name] = dict(status=status, revision=entry['revision'], content_sha256=entry['content_sha256'])
            print(f'{name}: {status}')
            continue
        if ours in (incoming, base) and base is not None or ours == incoming:
            clean, result = True, incoming
        else:
            clean, result = trees.merge(work, base, ours, incoming, ctx.env)
        policy = skills.policy_name(entry)
        main_entry = json.dumps(ctx.registry().get('skills', {}).get(name), sort_keys=True)
        key = hashlib.sha256('\0'.join([name, base or '', ours or '', incoming, policy, revision, main_entry])
                             .encode()).hexdigest()[:12]
        item = dict(name=name, revision=revision, base=base, ours=ours, incoming=incoming, clean=clean,
                    result=result if clean else ours, markers=None if clean else result, policy=policy,
                    key=key, entry=entry, shared_hash=shared_hash, digest=digest)
        item['new_entry'] = pinned_entry(work, name, entry, revision, incoming, item['result'])
        items.append(item)
    return items


def proposal_message(ctx, item, status):
    words = {'confirm': 'requires confirmation', 'conflict': 'conflict requires resolution'}
    lines = [f'Propose {item["name"]} update to {item["revision"][:12]} ({words[status]})', '',
             'Prepared by the assigned server; the active installation is unchanged',
             'until this pull request is accepted.', '']
    values = dict(Proposal=item['name'], Status=status, Policy=item['policy'],
                  **{'Incoming-Revision': item['revision'], 'Base-Tree': item['base'] or 'none',
                     'Shared-Tree': item['ours'] or 'none', 'Incoming-Tree': item['incoming'],
                     'Result-Tree': item['result'] or 'none', 'Key': item['key']})
    lines += [f'Agent-Skills-{key}: {value}' for key, value in values.items()]
    return '\n'.join(lines)


def proposal_body(item, status):
    review = (f'На клиенте: `bash install.sh --review {item["name"]}` или '
              f'`install.ps1 -Mode Review -Skill {item["name"]}`; затем approve/resolve.')
    text = {'confirm': 'Внешнее обновление готово и ждёт подтверждения пользователя.',
            'conflict': 'Внешнее обновление конфликтует с общими доработками. Рабочая версия сохранена.'}[status]
    return (f'{text}\n\n- Скилл: `{item["name"]}`\n- Политика: `{item["policy"]}`\n'
            f'- Входящая версия: `{item["revision"]}`\n- Ключ предложения: `{item["key"]}`\n\n{review}\n')


def apply_item(work, tree, registry, item):
    registry['skills'][item['name']] = item['new_entry']
    return trees.edit(work, tree, subtrees={'skills/' + item['name']: item['result'],
                                            'upstream/' + item['name']: item['incoming']})


def build_proposal(ctx, work, item, observed_registry):
    """A pull request branch on top of main for one pending external update."""
    status = 'confirm' if item['clean'] else 'conflict'
    registry = ctx.registry()
    tree = apply_item(work, ctx.tree(), registry, item)
    tree = trees.edit(work, tree, blobs={'sources.json': registry_blob(registry)})
    branch = f'{UPDATE_PREFIX}{item["name"]}/{item["key"]}'
    found = hosting.run_git(ctx.repo, 'rev-parse', '--verify', '-q', f'refs/remotes/origin/{branch}^{{commit}}', check=False)
    existing = found.returncode == 0
    head = found.stdout.decode().strip() if existing else None
    if head is None:
        head = trees.commit(ctx.repo, tree, [ctx.main], proposal_message(ctx, item, status)
                            + f'\nAgent-Skills-Server: {ctx.machine}\n', ctx.env)
    title = (f'Обновление {item["name"]}: требуется подтверждение' if status == 'confirm'
             else f'Конфликт обновления {item["name"]}')
    if item['markers']:
        materials = trees.edit(work, tree, subtrees={'skills/' + item['name']: item['markers']})
        material_head = trees.commit(ctx.repo, materials, [head],
            f'Conflict materials for {item["name"]}\n\nNot for merging: skills/{item["name"]} contains markers.\n', ctx.env)
        if not existing:
            hosting.push_branch(ctx.repo, material_head, f'{MATERIALS_PREFIX}{item["name"]}/{item["key"]}')
    result = ctx.publish(head, branch, title, proposal_body(item, status), automerge=False)
    return branch, result, status


def maintain(repo, publish=True, apply_auto=True, dry_run=False, approve=()):
    """Daily service: observe, apply automatic updates, propose the rest."""
    ctx = Context(repo, need_server=publish and not dry_run)
    reports = {}
    with scratch_worktree(ctx) as work:
        observed, data = observe(ctx, work, reports)
        items = incoming_updates(ctx, work, data, observed, reports)
        automatic = [item for item in items if item['clean'] and item['policy'] == 'auto' and apply_auto]
        pending = [item for item in items if item not in automatic and (not item['clean'] or item['policy'] == 'confirm')]
        for item in items:
            if item in automatic:
                reports[item['name']] = dict(status='updated', revision=item['revision'], content_sha256=item['digest'])
            elif item in pending:
                reports[item['name']] = dict(status='upstream-conflict' if not item['clean'] else 'awaiting-confirmation',
                    base=dict(revision=item['entry']['revision'], content_sha256=item['entry']['content_sha256'], tree=item['base']),
                    shared=dict(content_sha256=item['shared_hash'], path='skills/' + item['name'], tree=item['ours']),
                    incoming=dict(revision=item['revision'], content_sha256=item['digest'], tree=item['incoming']),
                    result_tree=item['result'], key=item['key'], policy=item['policy'])
            else:
                reports[item['name']] = dict(status='available', revision=item['revision'], content_sha256=item['digest'])
            print(f'{item["name"]}: {reports[item["name"]]["status"]}')
        # Skills with a pending proposal keep main's records in the automatic branch.
        main_registry = ctx.registry()
        auto_registry = json.loads(json.dumps(data))
        tree = observed
        for item in pending + [item for item in items if item not in automatic and item not in pending]:
            name = item['name']
            if name in main_registry['skills']:
                auto_registry['skills'][name] = main_registry['skills'][name]
            tree = trees.edit(work, tree, subtrees={
                'skills/' + name: trees.subtree(ctx.repo, ctx.main, 'skills/' + name),
                'upstream/' + name: trees.subtree(ctx.repo, ctx.main, 'upstream/' + name)})
        for item in automatic:
            tree = apply_item(work, tree, auto_registry, item)
        tree = trees.edit(work, tree, blobs={'sources.json': registry_blob(auto_registry)})
        write_report(ctx.repo, reports)
        if dry_run or not publish:
            changed = tree != ctx.tree()
            print('Dry run: ' + ('server changes prepared, nothing published' if changed else 'no server changes'))
            return 1 if any(r['status'] in ('error', 'upstream-conflict') for r in reports.values()) else 0
        failed = False
        try:
            keep = set()
            if tree != ctx.tree():
                names = ', '.join(item['name'] for item in automatic) or 'registry'
                branch = f'{SERVER_PREFIX}{ctx.machine}/{tree[:12]}'
                head = ctx.commit(tree, f'Maintain skill registry ({names})', branch=branch)
                keep.add(branch)
                ctx.publish(head, branch, 'Обслуживание библиотеки скиллов', 'Автоматическое обслуживание выбранным сервером.')
            ctx.close_others(f'{SERVER_PREFIX}{ctx.machine}/', keep)
            proposals = set()
            for item in pending:
                branch, result, _ = build_proposal(ctx, work, item, data)
                proposals.add(branch)
                reports[item['name']]['proposal'] = dict(branch=branch, number=result.get('number'), url=result.get('url', ''))
            ctx.close_others(UPDATE_PREFIX, proposals)
        except hosting.HostingError as error:
            print(f'Publication pending: {error}', file=sys.stderr)
            failed = True
        write_report(ctx.repo, reports)
    bad = any(r['status'] in ('error', 'upstream-conflict', 'source-unavailable') for r in reports.values())
    return 1 if failed or bad else 0


def server_change(ctx, tree, label, title):
    branch = f'{SERVER_PREFIX}{ctx.machine}/{label}-{tree[:12]}'
    head = ctx.commit(tree, title, branch=branch)
    return ctx.publish(head, branch, title, 'Управляющее изменение выбранного сервера.')


def register(repo, name, source, policy):
    ctx = Context(repo, need_server=True)
    registry = ctx.registry()
    if name in registry['skills'] or trees.subtree(ctx.repo, ctx.main, 'skills/' + name):
        raise ValueError('Skill already registered; edit its entry explicitly')
    entry = dict(source=source, auto_update=policy == 'auto')
    folder, revision, digest = skills.materialize(name, entry, approved=False)
    skills.validate_local_content(folder, name)
    tree = trees.folder_tree(ctx.repo, folder)
    entry.update(revision=revision, content_sha256=digest, executable_sha256=skills.executable_hash(folder))
    skills.validate_entry(name, entry)
    registry['skills'][name] = entry
    result = trees.edit(ctx.repo, ctx.tree(), subtrees={'skills/' + name: tree, 'upstream/' + name: tree},
                        blobs={'sources.json': registry_blob(registry)})
    server_change(ctx, result, 'register-' + name, f'Register external skill {name}')
    print('Registered:', name)
    return 0


def set_policy(repo, name, policy):
    ctx = Context(repo, need_server=True)
    registry = ctx.registry()
    entry = registry['skills'].get(name)
    if not entry or entry['source']['kind'] == 'local':
        raise ValueError('Only external skills have an update policy')
    entry['auto_update'] = policy == 'auto'
    tree = trees.edit(ctx.repo, ctx.tree(), blobs={'sources.json': registry_blob(registry)})
    if tree == ctx.tree():
        print(f'{name}: policy already {policy}')
        return 0
    server_change(ctx, tree, f'policy-{name}-{policy}', f'Set {name} update policy to {policy}')
    return 0


def proposals(repo, name=None):
    """Pending proposals visible to every machine through origin branches."""
    found = []
    refs = git(repo, 'for-each-ref', '--format=%(refname)', 'refs/remotes/origin/' + UPDATE_PREFIX).decode().split()
    for ref in refs:
        message = git(repo, 'log', '--first-parent', '--grep=^Agent-Skills-Proposal: ', '-1', '--format=%B', ref).decode()
        values = {key: value for key, value in re.findall(r'^Agent-Skills-([A-Za-z-]+): (\S+)$', message, re.M)}
        if 'Proposal' in values and (name is None or values['Proposal'] == name):
            values['branch'] = ref[len('refs/remotes/origin/'):]
            values['head'] = git(repo, 'rev-parse', ref).decode().strip()
            found.append(values)
    return found


def current_proposal(ctx, name, revision, commit):
    found = [item for item in proposals(ctx.repo, name) if item['Incoming-Revision'] == revision]
    if not found:
        raise ValueError('No pending proposal for this skill and revision; source moved since review?')
    item = found[0]
    if item['head'] != commit:
        raise ValueError('Proposal changed since review; review it again')
    policy = 'auto' if ctx.registry()['skills'].get(name, {}).get('auto_update') else 'confirm'
    if policy != item['Policy']:
        raise ValueError('Update policy changed since the proposal; review it again')
    shared = trees.subtree(ctx.repo, ctx.main, 'skills/' + name) or 'none'
    if shared != item['Shared-Tree']:
        raise ValueError('Shared skill changed since the proposal; wait for the server to prepare a new one')
    return item


def accept(ctx, item, head, expected):
    """Merge the exact reviewed result, updating an outdated proposal safely."""
    pull = ctx.hosting.open_pull(item['branch'])
    if not pull:
        raise ValueError('Pull request of the proposal is not open')
    if pull['head']['sha'] != head:
        raise ValueError('Proposal changed since review; review it again')
    if not hosting.is_ancestor(ctx.repo, ctx.main, head):
        ctx.hosting.update_pull(pull['number'])
        for _ in range(30):
            pull = ctx.hosting.get_pull(pull['number'])
            if pull['head']['sha'] != head:
                break
            time.sleep(1)
        head = pull['head']['sha']
        git(ctx.repo, 'fetch', 'origin', item['branch'])
        for path, value in expected.items():
            if trees.subtree(ctx.repo, head, path) != value:
                raise ValueError('Updated proposal differs from the reviewed result; review it again')
    state = ctx.hosting.schedule_merge(pull['number'], head)
    print(f'{item["Proposal"]}: accepted ({state}) pull request #{pull["number"]}')
    return 0


def approve(repo, name, revision, commit):
    ctx = Context(repo, need_server=False)
    item = current_proposal(ctx, name, revision, commit)
    if item['Status'] != 'confirm':
        raise ValueError('Conflict proposals need resolve with keep-shared, take-upstream or a merged result')
    return accept(ctx, item, item['head'], {'skills/' + name: item['Result-Tree'], 'upstream/' + name: item['Incoming-Tree']})


def resolve(repo, name, strategy, shared_hash, revision=None, commit=None):
    ctx = Context(repo, need_server=strategy == 'detach')
    registry = ctx.registry()
    entry = registry['skills'].get(name)
    if not entry or entry['source']['kind'] == 'local':
        raise ValueError('Skill has no external source')
    shared = trees.subtree(ctx.repo, ctx.main, 'skills/' + name)
    if not shared or trees.hashes(ctx.repo, shared)[0] != shared_hash:
        raise ValueError('Shared customization moved since review')
    if strategy == 'detach':
        content, modes = trees.hashes(ctx.repo, shared)
        registry['skills'][name] = dict(source=dict(kind='local', path='skills/' + name), auto_update=False,
                                        revision=content, content_sha256=content, executable_sha256=modes)
        tree = trees.edit(ctx.repo, ctx.tree(), subtrees={'upstream/' + name: None},
                          blobs={'sources.json': registry_blob(registry)})
        server_change(ctx, tree, 'detach-' + name, f'Detach {name} from its external source')
        ctx.close_others(f'{UPDATE_PREFIX}{name}/')
        print(f'{name}: resolved (detach; kept as local skill)')
        return 0
    item = current_proposal(ctx, name, revision, commit)
    head = item['head']
    target = shared if strategy == 'keep-shared' else item['Incoming-Tree']
    if trees.subtree(ctx.repo, head, 'skills/' + name) != target:
        # Content-only commit on top of the server proposal; pins stay server-authored.
        tree = trees.edit(ctx.repo, ctx.tree(head), subtrees={'skills/' + name: target})
        head = trees.commit(ctx.repo, tree, [head], f'Resolve {name} update: {strategy}\n\nAgent-Skills-Client: 1\n', ctx.env)
        hosting.push_branch(ctx.repo, head, item['branch'])
        item = dict(item, head=head)
    result = accept(ctx, item, head, {'skills/' + name: target, 'upstream/' + name: item['Incoming-Tree']})
    print(f'{name}: resolved ({strategy})')
    return result


def assign_server(repo, machine):
    """Administrative operation: works without the previous server."""
    if not hosting.MACHINE.fullmatch(machine):
        raise ValueError('Invalid machine name')
    ctx = Context(repo, need_server=False)
    config = json.loads(json.dumps(ctx.config))
    previous = config['server'].get('machine')
    if previous == machine:
        print(f'Server already assigned: {machine}')
        return 0
    config['server']['machine'] = machine
    tree = trees.edit(ctx.repo, ctx.tree(), blobs={hosting.CONFIG: (json.dumps(config, indent=2) + '\n').encode()})
    # Personal identity of the administrator; no service author is required.
    head = git(ctx.repo, 'commit-tree', tree, '-p', ctx.main, '-F', '-',
               data=(f'Assign skill server role to {machine}\n\nPrevious server: {previous or "none"}\n\n'
                     'Agent-Skills-Admin: assign-server\n').encode()).decode().strip()
    ctx.publish(head, f'{ADMIN_PREFIX}assign-{machine}-{ctx.main[:12]}', f'Назначение сервера: {machine}',
                f'Административная передача серверной роли от `{previous}` к `{machine}`.')
    # Pending publications of the former server can no longer be accepted.
    ctx.close_others(SERVER_PREFIX)
    ctx.close_others(UPDATE_PREFIX)
    print(f'Server role assigned to {machine}; previous server proposals closed')
    return 0


def status(repo):
    repo = Path(repo).resolve()
    machine = hosting.machine_name(repo)
    try:
        config = hosting.shared_config(repo, 'refs/remotes/origin/main')
        assigned = config['server'].get('machine')
    except ValueError as error:
        assigned = None
        print('Shared configuration: ' + str(error))
    print(f'Machine: {machine}; assigned server: {assigned or "none"}'
          + ('; this machine is the server' if assigned == machine else ''))
    report = git_dir(repo) / REPORT
    if report.exists():
        for name, item in json.loads(report.read_text(encoding='utf-8')).items():
            print(f'  {name}: {item.get("status")}' + (f' ({item["error"]})' if item.get('error') else ''))
    for item in proposals(repo):
        print(f'Proposal {item["Proposal"]}: {item["Status"]}, policy {item["Policy"]}, '
              f'incoming {item["Incoming-Revision"][:12]}, commit {item["head"]}, branch {item["branch"]}')
    state = git_dir(repo) / 'agent-skills-publication'
    if state.exists():
        print('Client publication: ' + state.read_text(encoding='utf-8').strip())
    return 0
