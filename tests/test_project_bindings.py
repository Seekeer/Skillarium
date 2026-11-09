"""Project bindings: user actions of the "Projects" backend and background service.

Runs the native client of the current OS: Windows PowerShell with real NTFS
junctions on Windows, Bash with symlinks on Linux. AGENT_SKILLS_PWSH selects
another PowerShell executable on Windows. All Git repositories, user profiles
and caches are temporary; the scheduler is never touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from fake_hosting import FakeHosting, protect

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == 'nt'
CLIENT = 'powershell' if WINDOWS else 'bash'
PWSH = os.environ.get('AGENT_SKILLS_PWSH') or 'powershell.exe'
CLIENT_FILES = ['install.sh', 'install.ps1', 'projects.sh', 'projects.ps1', '.gitattributes',
                'scripts/client.sh', 'scripts/client.ps1', 'scripts/bindings.sh', 'scripts/bindings.ps1',
                'scripts/sync-hidden.vbs', 'scripts/locate.sh', 'scripts/locate.ps1', 'scripts/bridge.sh', 'scripts/bridge.ps1',
                'agent-skills.json']
AGENTS = ['.agents', '.claude']
SHARED, OTHER = 'shared-skill', 'other-skill'


def is_link(path):
    path = str(path)
    return os.path.islink(path) or (WINDOWS and os.path.isjunction(path))


def same_path(left, right):
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def make_link(path, target):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        import _winapi
        _winapi.CreateJunction(str(target), str(path))
    else:
        os.symlink(target, path)


class ProjectBindingTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='agent skills bindings ', dir=base)
        self.addCleanup(self.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.home = self.root / 'user home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        XDG_CACHE_HOME=str(self.home / '.cache'), XDG_STATE_HOME=str(self.home / '.local/state'),
                        XDG_CONFIG_HOME=str(self.home / '.config'), LOCALAPPDATA=str(self.home / 'AppData/Local'),
                        APPDATA=str(self.home / 'AppData/Roaming'),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', AGENT_SKILLS_LOCK_TIMEOUT='30',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        for key in ['XDG_DATA_HOME', 'GIT_DIR', 'GIT_WORK_TREE']:
            self.env.pop(key, None)
        self.remote = self.root / 'library.git'
        self.git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        seed = self.root / 'seed'
        seed.mkdir()
        self.git(seed, 'init', '-b', 'main')
        for relative in CLIENT_FILES:
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            source = ROOT / 'templates/library' / relative if relative == 'agent-skills.json' else ROOT / relative
            shutil.copyfile(source, seed / relative)
        self.registry = {'version': 2, 'skills': {}}
        for name in [SHARED, OTHER]:
            self.add_library_skill(seed, name)
        self.commit(seed, 'Library')
        self.git(seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(seed, 'push', '-u', 'origin', 'main')
        self.library = self.root / 'library with spaces'
        self.git(self.root, 'clone', str(self.remote), str(self.library))
        self.background()

    def cleanup(self):
        # Junctions must go first so cleanup never follows them into the library.
        for path in sorted(self.root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if is_link(path):
                os.rmdir(path) if WINDOWS and os.path.isjunction(str(path)) else os.unlink(path)
        self.temp.cleanup()

    def test_links_into_a_vanished_temporary_library_are_cleaned(self):
        state = (self.home / 'AppData/Local/agent-skills' if WINDOWS else self.home / '.local/state/agent-skills')
        state = state / 'bindings.json'
        data = json.loads(state.read_text(encoding='utf-8'))
        # A test run left a link into a temporary library that is gone.
        vanished = self.root / 'deleted test library'
        target = vanished / 'skills' / 'ghost-skill'
        target.mkdir(parents=True)
        link = self.home / '.agents' / 'skills' / 'ghost-skill'
        make_link(link, target)
        shutil.rmtree(vanished)
        # Links of a library that is only unavailable (a disconnected drive, a moved clone)
        # or not connected here are never guessed away.
        unavailable = ('Z:\\Skillarium\\unavailable library' if WINDOWS else '/nonexistent/skillarium library')
        other = self.root / 'other library' / 'skills' / 'kept-skill'
        other.mkdir(parents=True)
        kept = self.home / '.agents' / 'skills' / 'kept-skill'
        make_link(kept, other)
        data['library'] = str(vanished)
        data['links'] = dict(data['links'], **{
            str(link): {'target': str(target), 'skill': 'ghost-skill', 'project': None, 'exclude': None},
            str(self.home / '.claude' / 'skills' / 'offline-skill'): {
                'target': unavailable + ('\\skills\\offline-skill' if WINDOWS else '/skills/offline-skill'),
                'skill': 'offline-skill', 'project': None, 'exclude': None},
            str(kept): {'target': str(other), 'skill': 'kept-skill', 'project': None, 'exclude': None}})
        state.write_text(json.dumps(data), encoding='utf-8')
        output = self.background()
        self.assertIn('UNLINKED', output)
        saved = json.loads(state.read_text(encoding='utf-8'))
        self.assertTrue(os.path.samefile(saved['library'], self.library))
        self.assertFalse(is_link(link) or link.exists())
        self.assertNotIn(str(link), saved['links'])
        self.assertIn(str(kept), saved['links'])
        self.assertTrue(is_link(kept))
        self.assertIn(str(self.home / '.claude' / 'skills' / 'offline-skill'), saved['links'])

    def test_exclude_cleanup_that_fails_is_retried(self):
        if not WINDOWS and os.geteuid() == 0:
            self.skipTest('root ignores the permissions that make cleanup fail')
        state = (self.home / 'AppData/Local/agent-skills' if WINDOWS else self.home / '.local/state/agent-skills')
        state = state / 'bindings.json'
        data = json.loads(state.read_text(encoding='utf-8'))
        exclude = self.root / 'project' / '.git' / 'info' / 'exclude'
        exclude.parent.mkdir(parents=True)
        exclude.write_text('# kept\n/.agents/skills/ghost-skill\n', encoding='utf-8', newline='\n')
        data['excludes'] = {str(exclude): ['/.agents/skills/ghost-skill']}
        state.write_text(json.dumps(data), encoding='utf-8')
        # The exclude file cannot be rewritten: the run fails and keeps the record.
        locked = exclude if WINDOWS else exclude.parent
        mode = locked.stat().st_mode
        locked.chmod(0o444 if WINDOWS else 0o555)
        try:
            self.assertIn('could not be applied', self.background(expected=1))
        finally:
            locked.chmod(mode)
        self.assertIn('ghost-skill', exclude.read_text(encoding='utf-8'))
        self.assertIn(str(exclude), json.loads(state.read_text(encoding='utf-8'))['excludes'])
        # Once the file is writable again, the next run finishes the cleanup.
        self.background()
        self.assertEqual(exclude.read_text(encoding='utf-8').strip(), '# kept')

    # Helpers -----------------------------------------------------------------
    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env, text=True,
                              capture_output=True, check=True).stdout.strip()

    def commit(self, repo, message):
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-m', message)

    def add_library_skill(self, repo, name):
        folder = repo / 'skills' / name
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test\n---\nBody\n', newline='\n')
        self.registry['skills'][name] = {'source': {'kind': 'local'}}
        (repo / 'sources.json').write_text(json.dumps(self.registry, indent=2) + '\n', newline='\n')

    def run_client(self, script, *args, expected=0, wait=True):
        if CLIENT == 'bash':
            command = ['bash', str(self.library / (script + '.sh')), *args]
        else:
            command = [PWSH, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', str(self.library / (script + '.ps1')), *args]
        if not wait:
            return subprocess.Popen(command, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        result = subprocess.run(command, env=self.env, capture_output=True)
        output = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        self.last_stdout = result.stdout.decode('utf-8', 'replace')
        return output

    def projects(self, *args, expected=0):
        return self.run_client('projects', *[str(arg) for arg in args], expected=expected)

    def background(self, expected=0):
        if CLIENT == 'bash':
            return self.run_client('install', '--link-only', expected=expected)
        return self.run_client('install', '-Mode', 'Link', expected=expected)

    def publish(self, expected=0):
        """Library Git synchronization without touching any scheduler."""
        if CLIENT == 'bash':
            return self.run_client('install', '--sync-only', expected=expected)
        harness = self.root / 'publish.ps1'
        harness.write_text(
            "$ErrorActionPreference = 'Stop'\n$repo = $args[0]\n"
            "function Invoke-Git { param([string[]]$GitArgs, [string]$Directory = $repo)\n"
            "  $output = & git -C $Directory @GitArgs\n"
            "  if ($LASTEXITCODE -ne 0) { throw \"git failed (exit $LASTEXITCODE).\" }\n  return $output }\n"
            ". (Join-Path $repo 'scripts/client.ps1')\nSync-Repository\n")
        result = subprocess.run([PWSH, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(harness), str(self.library)], env=self.env, capture_output=True)
        output = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')
        self.assertEqual(result.returncode != 0, expected != 0, output)
        return output

    def json_output(self):
        lines = [line for line in self.last_stdout.splitlines() if line.startswith('{')]
        self.assertTrue(lines, self.last_stdout)
        return json.loads(lines[-1])

    def status(self):
        self.projects('status')
        return self.json_output()

    def skill_status(self, name):
        return next(row for row in self.status()['skills'] if row['name'] == name)

    def make_project(self, name, remote=None):
        project = self.root / name
        if remote:
            self.git(self.root, 'clone', str(remote), str(project))
        else:
            project.mkdir()
            self.git(project, 'init', '-b', 'main')
            (project / 'README.md').write_text('project\n')
            self.commit(project, 'Initial project')
        return Path(os.path.realpath(project))

    def project_link(self, project, agent, name=SHARED):
        return project / agent / 'skills' / name

    def global_link(self, agent, name=SHARED):
        return self.home / agent / 'skills' / name

    def assert_linked(self, path, name=SHARED):
        self.assertTrue(is_link(path), f'not a link: {path}')
        self.assertTrue(same_path(path, self.library / 'skills' / name), f'wrong target: {path}')

    def assert_project_bound(self, project, name=SHARED):
        for agent in AGENTS:
            self.assert_linked(self.project_link(project, agent, name), name)

    def assert_project_unbound(self, project, name=SHARED):
        for agent in AGENTS:
            self.assertFalse(os.path.lexists(self.project_link(project, agent, name)))

    def assert_global(self, enabled, name=SHARED):
        for agent in AGENTS:
            if enabled:
                self.assert_linked(self.global_link(agent, name), name)
            else:
                self.assertFalse(os.path.lexists(self.global_link(agent, name)))

    def project_list(self, project):
        return json.loads((project / '.agent-skills.json').read_text())['skills']

    def write_list(self, project, names):
        (project / '.agent-skills.json').write_text(json.dumps({'version': 1, 'skills': names}) + '\n')

    # 1 -----------------------------------------------------------------------
    def test_new_library_skill_is_global_and_project_skill_folders_stay_unmanaged(self):
        self.assert_global(True, SHARED)
        self.assert_global(True, OTHER)
        self.add_library_skill(self.library, 'late-skill')
        self.commit(self.library, 'Pulled new library skill')
        self.background()
        self.assert_global(True, 'late-skill')
        self.assertEqual(self.skill_status('late-skill')['global'],
                         {'enabled': True, 'manual': False, 'pending': None, 'links': {
                             str(self.global_link(agent, 'late-skill')): 'linked' for agent in AGENTS}})

        project = self.make_project('project a')
        own = project / '.claude/skills/own-skill'
        own.mkdir(parents=True)
        (own / 'SKILL.md').write_text('---\nname: own-skill\ndescription: Project\n---\n')
        self.commit(project, 'Project skill')
        self.projects('register', project)
        self.background()
        self.assertEqual((own / 'SKILL.md').read_text(), '---\nname: own-skill\ndescription: Project\n---\n')
        self.assertFalse((self.library / 'skills/own-skill').exists())
        self.assertNotIn('own-skill', (self.library / 'sources.json').read_text())
        self.assertNotIn('own-skill', json.dumps(self.status()))
        self.assertEqual(self.git(project, 'status', '--porcelain'), '')

    def test_unregistered_client_skill_can_be_bound_and_keeps_global_choice(self):
        name = 'client-local'
        folder = self.library / 'skills' / name
        folder.mkdir()
        (folder / 'SKILL.md').write_text(
            f'---\nname: {name}\ndescription: Local client skill\n---\nLocal\n')
        self.background()
        self.assert_global(True, name)
        self.assertEqual(self.skill_status(name)['name'], name)
        project = self.make_project('local skill project')
        self.projects('register', project)
        self.projects('bind', project, name)
        self.assert_project_bound(project, name)
        self.assert_global(False, name)
        self.background()
        self.assert_project_bound(project, name)
        self.assert_global(False, name)
        self.projects('unbind', project, name, 'global')
        self.assert_project_unbound(project, name)
        self.assert_global(True, name)

    # 2 -----------------------------------------------------------------------
    def test_first_binding_moves_global_links_to_selected_projects(self):
        a, b, c = (self.make_project(name) for name in ['project a', 'project b', 'project c'])
        for project in (a, b, c):
            self.projects('register', project)
        self.projects('bind', a, SHARED)
        self.assert_project_bound(a)
        self.assert_global(False)
        self.assert_global(True, OTHER)
        self.assertEqual({k: v for k, v in self.skill_status(SHARED)['global'].items() if k != 'links'},
                         {'enabled': False, 'manual': False, 'pending': None})
        self.projects('bind', b, SHARED)
        self.assert_project_bound(b)
        for agent in AGENTS:
            self.assertTrue(same_path(self.project_link(a, agent), self.project_link(b, agent)))
        self.assert_project_unbound(c)
        self.assertEqual(sorted(self.skill_status(SHARED)['projects']), sorted([str(a), str(b)]))
        self.assertEqual(self.project_list(a), [SHARED])

    # 3 -----------------------------------------------------------------------
    def test_edit_through_one_project_reaches_all_bindings_and_unbinding_keeps_others(self):
        a, b = self.make_project('project a'), self.make_project('project b')
        for project in (a, b):
            self.projects('register', project)
            self.projects('bind', project, SHARED)
        (self.project_link(a, '.claude') / 'notes.md').write_text('edited in A\n')
        self.assertEqual((self.project_link(b, '.agents') / 'notes.md').read_text(), 'edited in A\n')
        self.assertEqual((self.library / f'skills/{SHARED}/notes.md').read_text(), 'edited in A\n')
        self.projects('unbind', a, SHARED)
        self.assert_project_unbound(a)
        self.assert_project_bound(b)
        self.assertTrue((self.library / f'skills/{SHARED}/notes.md').is_file())
        self.assertEqual(self.project_list(a), [])

    # 4 -----------------------------------------------------------------------
    def test_manual_global_choice_survives_binding_cycles(self):
        a, b = self.make_project('project a'), self.make_project('project b')
        for project in (a, b):
            self.projects('register', project)
        self.projects('global', SHARED, 'on')
        self.projects('bind', a, SHARED)
        self.projects('bind', b, SHARED)
        self.assert_global(True)
        self.assert_project_bound(a)
        self.assert_project_bound(b)
        self.projects('unbind', a, SHARED)
        self.projects('unbind', b, SHARED, 'global')
        self.projects('bind', a, SHARED)
        self.assert_global(True)
        # The same holds after the last binding disappears outside the UI.
        self.write_list(a, [])
        self.background()
        self.projects('bind', b, SHARED)
        self.assert_global(True)
        self.assert_project_bound(b)
        # And a manual "off" is equally kept.
        self.projects('global', OTHER, 'off')
        self.projects('bind', a, OTHER)
        self.projects('unbind', a, OTHER, 'library')
        self.projects('bind', b, OTHER)
        self.assert_global(False, OTHER)
        self.assertEqual(self.skill_status(OTHER)['global']['manual'], True)

    # 5 -----------------------------------------------------------------------
    def test_last_manual_unbind_asks_for_both_global_states(self):
        project = self.make_project('project a')
        self.projects('register', project)
        for initial, result, expected in [(None, 'global', True), ('on', 'library', False)]:
            if initial:
                self.projects('global', SHARED, initial)
            self.projects('bind', project, SHARED)
            before = self.skill_status(SHARED)['global']['enabled']
            self.projects('unbind', project, SHARED, expected=3)
            decision = self.json_output()
            self.assertEqual((decision['decision'], decision['skill'], decision['choices']),
                             ('last-project', SHARED, ['global', 'library']))
            self.assertEqual(decision['global']['enabled'], before)
            # Cancelling the dialog keeps the binding untouched.
            self.assert_project_bound(project)
            self.assertEqual(self.project_list(project), [SHARED])
            self.projects('unbind', project, SHARED, result)
            self.assert_project_unbound(project)
            self.assert_global(expected)
            choice = self.skill_status(SHARED)['global']
            self.assertEqual((choice['enabled'], choice['manual']), (expected, True))
            self.assertTrue((self.library / f'skills/{SHARED}/SKILL.md').is_file())

    # 6 -----------------------------------------------------------------------
    def test_last_binding_removed_outside_ui_keeps_global_setting(self):
        project = self.make_project('project a')
        self.projects('register', project)
        self.projects('bind', project, SHARED)
        self.commit(project, 'Bind shared skill')
        self.git(project, 'checkout', '-b', 'without-skill')
        self.write_list(project, [])
        self.commit(project, 'Unbind on branch')
        self.background()
        self.assert_project_unbound(project)
        self.assert_global(False)
        choice = self.skill_status(SHARED)['global']
        self.assertEqual((choice['enabled'], choice['manual'], choice['pending']), (False, False, None))
        self.git(project, 'checkout', 'main')
        self.background()
        self.assert_project_bound(project)
        self.assert_global(False)

    # 7 -----------------------------------------------------------------------
    def test_registered_clone_restores_bindings_and_worktrees_are_independent(self):
        origin = self.make_project('project origin')
        self.projects('register', origin)
        self.projects('bind', origin, SHARED)
        self.commit(origin, 'Bind shared skill')
        listed = (origin / '.agent-skills.json').read_text()
        self.assertEqual(listed, '{\n  "version": 1,\n  "skills": [\n    "shared-skill"\n  ]\n}\n')
        self.assertNotIn(str(self.root), listed)
        bare = self.root / 'project.git'
        self.git(self.root, 'clone', '--bare', str(origin), str(bare))
        clone = self.make_project('project clone', remote=bare)
        self.assert_project_unbound(clone)
        self.projects('register', clone)
        self.assert_project_bound(clone)
        self.assertEqual(self.git(clone, 'status', '--porcelain'), '')
        worktree = self.root / 'project worktree'
        self.git(clone, 'worktree', 'add', '-b', 'other', str(worktree))
        worktree = Path(os.path.realpath(worktree))
        self.write_list(worktree, [OTHER])
        self.commit(worktree, 'Other skill on branch')
        self.projects('register', worktree)
        self.assert_project_bound(worktree, OTHER)
        self.assert_project_unbound(worktree, SHARED)
        self.assert_project_bound(clone, SHARED)
        self.assert_project_unbound(clone, OTHER)
        self.assertEqual(self.git(worktree, 'status', '--porcelain'), '')
        self.assertEqual(self.git(clone, 'status', '--porcelain'), '')
        self.projects('unbind', worktree, OTHER, 'library')
        self.assert_project_bound(clone, SHARED)

    # 8 -----------------------------------------------------------------------
    def test_ui_saves_project_list_without_committing(self):
        project = self.make_project('project a')
        (project / 'manual.txt').write_text('manual commit\n')
        self.commit(project, 'Manual work')
        (project / 'staged.txt').write_text('staged\n')
        self.git(project, 'add', 'staged.txt')
        head = self.git(project, 'rev-parse', 'HEAD')
        self.projects('register', project)
        self.projects('bind', project, SHARED)
        self.assertEqual(self.git(project, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(project, 'diff', '--cached', '--name-only'), 'staged.txt')
        self.assertEqual(self.git(project, 'status', '--porcelain'), 'A  staged.txt\n?? .agent-skills.json')

    # 9 -----------------------------------------------------------------------
    def test_foreign_folders_links_and_changed_links_are_preserved(self):
        a, b, c = (self.make_project(name) for name in ['project a', 'project b', 'project c'])
        for project in (a, b, c):
            self.projects('register', project)
        foreign = self.project_link(a, '.claude')
        foreign.mkdir(parents=True)
        (foreign / 'SKILL.md').write_text('foreign\n')
        self.projects('bind', a, SHARED, expected=4)
        self.assertEqual((foreign / 'SKILL.md').read_text(), 'foreign\n')
        self.assert_global(True)
        row = next(row for row in self.status()['projects'] if row['path'] == str(a))
        self.assertEqual(row['skills'][0]['state'], 'conflict')
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        make_link(self.project_link(b, '.agents'), elsewhere)
        self.projects('bind', b, SHARED, expected=4)
        self.assertTrue(same_path(self.project_link(b, '.agents'), elsewhere))
        self.projects('bind', c, SHARED)  # A and B keep their conflicts
        self.assert_project_bound(c)
        self.assert_global(True)  # the automatic choice waits for project A
        changed = self.project_link(c, '.claude')
        os.rmdir(changed) if WINDOWS else os.unlink(changed)
        make_link(changed, elsewhere)
        self.projects('unbind', c, SHARED)
        self.assertTrue(same_path(changed, elsewhere))
        self.assertFalse(os.path.lexists(self.project_link(c, '.agents')))
        # Resolving the conflict lets the requested connection finish.
        shutil.rmtree(foreign)
        self.background(expected=1)  # project B keeps its conflict
        self.assert_project_bound(a)
        self.assert_global(False)
        global_folder = self.global_link('.claude', OTHER)
        os.rmdir(global_folder) if WINDOWS else os.unlink(global_folder)
        global_folder.mkdir()
        self.projects('global', OTHER, 'off')
        self.assertTrue(global_folder.is_dir() and not is_link(global_folder))

    # 10 ----------------------------------------------------------------------
    def test_unavailable_project_keeps_registration_and_global_choice(self):
        a, b = self.make_project('project a'), self.make_project('project b')
        for project in (a, b):
            self.projects('register', project)
            self.projects('bind', project, SHARED)
        moved = self.root / 'project a moved'
        a.rename(moved)
        self.background()
        row = next(row for row in self.status()['projects'] if row['path'] == str(a))
        self.assertEqual((row['available'], row['skills'][0]['state']), (False, 'unavailable'))
        self.assert_global(False)
        self.projects('unbind', a, SHARED, expected=1)
        self.projects('unbind', b, SHARED)  # A still counts as bound
        self.assert_global(False)
        moved.rename(a)
        self.background()
        self.assert_project_bound(a)
        self.projects('unbind', a, SHARED, expected=3)
        # Only an explicit unregister ends the registration; global access stays as chosen.
        a.rename(moved)
        self.projects('unregister', a)
        self.assertEqual([row['path'] for row in self.status()['projects']], [str(b)])
        self.assert_global(False)
        moved.rename(a)
        self.background()
        self.assertEqual(self.project_list(a), [SHARED])

    # 11 ----------------------------------------------------------------------
    def test_restart_retry_and_concurrent_background_runs(self):
        project = self.make_project('project a')
        self.projects('register', project)
        self.projects('register', project)
        self.assertEqual(len(self.status()['projects']), 1)
        # An interrupted bind: the list was saved but links were not created.
        self.write_list(project, [SHARED])
        self.projects('bind', project, SHARED)
        self.projects('bind', project, SHARED)
        self.assertEqual(self.project_list(project), [SHARED])
        self.assert_global(False)
        state = json.loads(Path(self.status()['state']).read_text(encoding='utf-8'))
        self.assertEqual(len([link for link in state['links'].values() if link['project']]), 2)
        # An interrupted link creation leaves an unrecorded link; a retry adopts it.
        self.projects('global', OTHER, 'on')
        make_link(self.project_link(project, '.agents', OTHER), self.library / 'skills' / OTHER)
        self.projects('bind', project, OTHER)
        self.projects('unbind', project, OTHER, 'global')
        self.assert_project_unbound(project, OTHER)
        self.assertTrue(self.skill_status(OTHER)['global']['manual'])
        # UI and background serialize on the lock of the links, which span all libraries.
        lock_path = Path(self.status()['state']).parent / 'bindings.lock'
        with open(lock_path, 'a') as lock:
            if not WINDOWS:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_EX)
            background = self.run_client('install', *(['-Mode', 'Link'] if WINDOWS else ['--link-only']), wait=False)
            waiting = self.run_client('projects', 'unbind', str(project), SHARED, 'global', wait=False)
            time.sleep(2)
            self.assertIsNone(background.poll())
            self.assertIsNone(waiting.poll())
        self.assertEqual(background.wait(timeout=60), 0, background.communicate())
        self.assertEqual(waiting.wait(timeout=60), 0, waiting.communicate())
        self.assert_global(True)
        self.assert_project_unbound(project)

    # 12 ----------------------------------------------------------------------
    def test_local_settings_stay_local_and_skill_edits_are_published(self):
        project = self.make_project('project a')
        self.projects('register', project)
        self.projects('bind', project, SHARED)
        self.projects('global', OTHER, 'off')
        (self.project_link(project, '.agents') / 'notes.md').write_text('shared edit\n', newline='\n')
        protect(self.remote)
        host = FakeHosting(self.remote, self.env)
        self.addCleanup(host.stop)
        self.git(self.library, 'config', 'agent-skills.machine', 'bindings-test')
        self.git(self.library, 'config', 'agent-skills.api', host.api)
        self.git(self.library, 'config', 'credential.helper',
                 '!f() { echo username=test; echo password=secret; }; f')
        self.publish()
        checkout = self.root / 'published'
        self.git(self.root, 'clone', str(self.remote), str(checkout))
        self.assertEqual((checkout / f'skills/{SHARED}/notes.md').read_text(), 'shared edit\n')
        files = self.git(checkout, 'ls-files').splitlines()
        self.assertNotIn('.agent-skills.json', files)
        self.assertFalse(any('bindings' in name and name.endswith('.json') for name in files))
        for name in files:
            data = (checkout / name).read_bytes()
            self.assertNotIn(str(project).encode(), data)
            self.assertNotIn(b'"manual"', data)
        self.assertEqual(self.git(self.library, 'status', '--porcelain'), '')
        state = Path(self.status()['state'])
        self.assertTrue(state.is_file())
        self.assertFalse(same_path(os.path.commonpath([state, self.library]), self.library))

    # 13 ----------------------------------------------------------------------
    def write_skill(self, folder, body):
        (folder / 'SKILL.md').write_text(f'---\nname: {SHARED}\ndescription: Test\n---\n{body}\n',
                                         encoding='utf-8', newline='\n')

    def conflicting_edits(self):
        """A teammate and this computer change the same line; the teammate deletes a file edited here."""
        seed = self.root / 'seed'
        skill = seed / 'skills' / SHARED
        (skill / 'notes.md').write_text('notes\n', newline='\n')
        self.commit(seed, 'Teammate notes')
        self.git(seed, 'push')
        self.publish()
        self.write_skill(skill, 'Teammate edit')
        (skill / 'notes.md').unlink()
        self.commit(seed, 'Teammate edit')
        self.git(seed, 'push')
        local = self.library / 'skills' / SHARED
        self.write_skill(local, 'Моя правка')
        (local / 'notes.md').write_text('local notes\n', newline='\n')
        self.publish(expected=1)

    def hosting(self):
        protect(self.remote)
        host = FakeHosting(self.remote, self.env)
        self.addCleanup(host.stop)
        self.git(self.library, 'config', 'agent-skills.machine', 'bindings-test')
        self.git(self.library, 'config', 'agent-skills.api', host.api)
        self.git(self.library, 'config', 'credential.helper',
                 '!f() { echo username=test; echo password=secret; }; f')
        return host

    def conflicts(self):
        self.projects('conflicts')
        return self.json_output()

    def resolve(self, request, expected=0):
        path = self.root / 'request.json'
        path.write_text(json.dumps(request, ensure_ascii=False), encoding='utf-8')
        return self.projects('resolve', path, expected=expected)

    def worktrees(self):
        return self.git(self.library, 'worktree', 'list', '--porcelain').count('worktree ')

    def test_sync_conflict_is_listed_and_resolved_without_losing_either_side(self):
        self.assertEqual(self.conflicts(), {'state': 'ok', 'upstream': None, 'files': []})
        self.conflicting_edits()
        self.hosting()
        # Users may have no Git identity; the trial merge must not need one.
        for key in ['GIT_AUTHOR_NAME', 'GIT_AUTHOR_EMAIL', 'GIT_COMMITTER_NAME', 'GIT_COMMITTER_EMAIL']:
            self.env.pop(key)
        before = self.git(self.library, 'rev-parse', 'HEAD')
        report = self.conflicts()
        self.assertEqual(report['state'], 'conflict')
        files = {item['path']: item for item in report['files']}
        skill_md, notes = f'skills/{SHARED}/SKILL.md', f'skills/{SHARED}/notes.md'
        self.assertEqual(sorted(files), [skill_md, notes])
        self.assertEqual(files[skill_md]['skill'], SHARED)
        self.assertIn('Моя правка', files[skill_md]['mine']['text'])
        self.assertIn('Teammate edit', files[skill_md]['library']['text'])
        self.assertEqual(files[notes]['mine']['text'], 'local notes\n')
        self.assertIsNone(files[notes]['library'])
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), before)
        self.assertEqual(self.worktrees(), 1)

        # An edit made after the report makes the choice stale; nothing is merged.
        local = self.library / 'skills' / SHARED
        self.write_skill(local, 'Моя правка\nЕщё строка')
        request = {'upstream': report['upstream'], 'files': [
            {'path': path, 'take': 'library', 'mine': item['mine'] and item['mine']['id'],
             'library': item['library'] and item['library']['id']} for path, item in files.items()]}
        self.resolve(request, expected=5)
        self.assertIn('Ещё строка', (local / 'SKILL.md').read_text(encoding='utf-8'))
        self.assertTrue((local / 'notes.md').exists())

        report = self.conflicts()
        files = {item['path']: item for item in report['files']}
        self.assertIn('Ещё строка', files[skill_md]['mine']['text'])
        edited = f'---\nname: {SHARED}\ndescription: Test\n---\nОбщая версия\n'
        request = {'upstream': report['upstream'], 'files': [
            {'path': skill_md, 'take': 'edited', 'text': 'Общая версия без заголовка\n',
             'mine': files[skill_md]['mine']['id'], 'library': files[skill_md]['library']['id']},
            {'path': notes, 'take': 'library', 'mine': files[notes]['mine']['id'], 'library': None}]}
        # A result that is no longer a valid skill is refused before anything is merged.
        head = self.git(self.library, 'rev-parse', 'HEAD')
        self.assertIn('needs frontmatter', self.resolve(request, expected=1))
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.worktrees(), 1)
        request['files'][0]['text'] = edited
        output = self.resolve(request)
        # The resolution travels like any client edit: a proposal pull request merged by the hosting.
        self.assertEqual(self.json_output(), {'resolved': True, 'published': True}, output)
        self.assertEqual((local / 'SKILL.md').read_bytes(), edited.encode())
        self.assertFalse((local / 'notes.md').exists())
        self.assertEqual(self.conflicts()['state'], 'ok')
        self.assertEqual(self.git(self.library, 'status', '--porcelain'), '')
        self.assertEqual(len(self.git(self.library, 'rev-list', '--parents', '-n', '1', 'HEAD').split()), 3)
        self.assertEqual(self.worktrees(), 1)
        published = subprocess.run(['git', '-C', str(self.remote), 'show', f'main:{skill_md}'], env=self.env,
                                   capture_output=True, check=True).stdout
        self.assertEqual(published, edited.encode())
        self.publish()
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), self.git(self.library, 'rev-parse', '@{upstream}'))
        self.assert_global(True)


if __name__ == '__main__':
    unittest.main()
