"""Several libraries on one client: order, preferred copies, project entries naming a
library, isolated synchronization failures, disconnection and stale links.

Runs the native client of the current OS (Windows PowerShell with junctions, or Bash
with symlinks) from an engine folder that is not a library, so the connected libraries
come from the Skillarium configuration as on an installed machine. Everything lives in
temporary folders; the scheduler is never touched (SKILLARIUM_TASK_NAME).
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WINDOWS = os.name == 'nt'
PWSH = os.environ.get('AGENT_SKILLS_PWSH') or 'powershell.exe'
HAS_BASH_CLIENT = not WINDOWS and all(shutil.which(tool) for tool in ('bash', 'jq', 'flock'))
ENGINE_FILES = ['install.sh', 'install.ps1', 'projects.sh', 'projects.ps1',
                'scripts/client.sh', 'scripts/client.ps1', 'scripts/bindings.sh', 'scripts/bindings.ps1',
                'scripts/sync-hidden.vbs', 'scripts/locate.sh', 'scripts/locate.ps1']
AGENTS = ['.agents', '.claude']


def is_link(path):
    path = str(path)
    return os.path.islink(path) or (WINDOWS and os.path.isjunction(path))


def same_path(left, right):
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


@unittest.skipUnless(WINDOWS or HAS_BASH_CLIENT, 'native client tools unavailable')
class MultiLibraryTests(unittest.TestCase):
    def setUp(self):
        base = Path(tempfile.gettempdir()).resolve()
        self.temp = tempfile.TemporaryDirectory(prefix='skillarium multi ', dir=base)
        self.addCleanup(self.cleanup)
        self.root = Path(os.path.realpath(self.temp.name))
        self.home = self.root / 'home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        XDG_CACHE_HOME=str(self.home / '.cache'), XDG_STATE_HOME=str(self.home / '.local/state'),
                        XDG_CONFIG_HOME=str(self.home / '.config'), LOCALAPPDATA=str(self.home / 'AppData/Local'),
                        APPDATA=str(self.home / 'AppData/Roaming'), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM='1', AGENT_SKILLS_LOCK_TIMEOUT='30', SKILLARIUM_NO_BRIDGE='1',
                        SKILLARIUM_TASK_NAME='Skillarium-Test-' + os.urandom(4).hex(),
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        for key in ['XDG_DATA_HOME', 'GIT_DIR', 'GIT_WORK_TREE', 'SKILLARIUM_LIBRARY', 'AGENT_SKILLS_LIBRARY']:
            self.env.pop(key, None)
        self.engine = self.root / 'engine'
        for relative in ENGINE_FILES:
            (self.engine / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.engine / relative)
        self.personal = self.make_library('personal', ['shared-skill', 'own-skill'])
        self.team = self.make_library('team', ['shared-skill', 'team-skill'])
        self.write_config([('personal', self.personal), ('team', self.team)])

    def cleanup(self):
        for path in sorted(self.root.rglob('*'), key=lambda item: len(item.parts), reverse=True):
            if is_link(path):
                os.rmdir(path) if WINDOWS and os.path.isjunction(str(path)) else os.unlink(path)
        self.temp.cleanup()

    # --- helpers --------------------------------------------------------------
    def git(self, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, env=self.env, capture_output=True, text=True, check=True).stdout.strip()

    def make_library(self, owner, names):
        remote = self.root / 'remotes' / owner / 'skills.git'
        remote.parent.mkdir(parents=True)
        self.git(self.root, 'init', '--bare', '--initial-branch=main', str(remote))
        seed = self.root / 'seed' / owner
        seed.mkdir(parents=True)
        self.git(seed, 'init', '-b', 'main')
        shutil.copyfile(ROOT / 'templates/library/agent-skills.json', seed / 'agent-skills.json')
        registry = {'version': 2, 'skills': {}}
        for name in names:
            self.write_skill(seed, name, f'{owner} copy')
            registry['skills'][name] = {'source': {'kind': 'local'}}
        (seed / 'sources.json').write_text(json.dumps(registry, indent=2) + '\n', newline='\n')
        self.git(seed, 'add', '-A')
        self.git(seed, 'commit', '-m', 'Library')
        self.git(seed, 'remote', 'add', 'origin', str(remote))
        self.git(seed, 'push', '-u', 'origin', 'main')
        clone = self.root / 'clones' / owner
        clone.parent.mkdir(exist_ok=True)
        self.git(self.root, 'clone', str(remote), str(clone))
        return Path(os.path.realpath(clone))

    def write_skill(self, repo, name, body):
        folder = repo / 'skills' / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test\n---\n{body}\n', newline='\n')

    def write_config(self, libraries, prefer=None):
        config = {'libraries': [{'name': name, 'path': str(path), 'url': ''} for name, path in libraries]}
        if prefer:
            config['prefer'] = prefer
        for folder in (self.home / 'AppData/Roaming/Skillarium', self.home / '.config/skillarium'):
            folder.mkdir(parents=True, exist_ok=True)
            (folder / 'config.json').write_text(json.dumps(config), encoding='utf-8')

    def config(self):
        folder = self.home / ('AppData/Roaming/Skillarium' if WINDOWS else '.config/skillarium')
        return json.loads((folder / 'config.json').read_text(encoding='utf-8-sig'))

    def run_engine(self, script, *args, expected=0):
        if WINDOWS:
            command = [PWSH, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                       '-File', str(self.engine / (script + '.ps1')), *[str(arg) for arg in args]]
        else:
            command = ['bash', str(self.engine / (script + '.sh')), *[str(arg) for arg in args]]
        result = subprocess.run(command, env=self.env, capture_output=True)
        output = result.stdout.decode('utf-8', 'replace') + result.stderr.decode('utf-8', 'replace')
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        self.last_stdout = result.stdout.decode('utf-8', 'replace')
        return output

    def link(self, expected=0):
        return self.run_engine('install', *(['-Mode', 'Link'] if WINDOWS else ['--link-only']), expected=expected)

    def sync(self, expected=0):
        return self.run_engine('install', *(['-Mode', 'Sync'] if WINDOWS else ['--sync-only']), expected=expected)

    def projects(self, *args, expected=0):
        return self.run_engine('projects', *args, expected=expected)

    def status(self):
        self.projects('status')
        return json.loads([line for line in self.last_stdout.splitlines() if line.startswith('{')][-1])

    def assert_global_from(self, name, library):
        for agent in AGENTS:
            path = self.home / agent / 'skills' / name
            self.assertTrue(is_link(path), f'not a link: {path}')
            self.assertTrue(same_path(path, library / 'skills' / name), f'{path} is not from {library}')

    def assert_no_global(self, name):
        for agent in AGENTS:
            self.assertFalse(os.path.lexists(self.home / agent / 'skills' / name))

    def make_project(self, name):
        project = self.root / name
        project.mkdir()
        self.git(project, 'init', '-b', 'main')
        return Path(os.path.realpath(project))

    # --- tests ----------------------------------------------------------------
    def test_first_library_wins_a_name_and_a_preferred_copy_or_new_order_replaces_it(self):
        self.link()
        self.assert_global_from('shared-skill', self.personal)
        self.assert_global_from('own-skill', self.personal)
        self.assert_global_from('team-skill', self.team)
        rows = [row for row in self.status()['skills'] if row['name'] == 'shared-skill']
        self.assertEqual([(row['library'], row['active'], row['shadowedBy']) for row in rows],
                         [('personal', True, None), ('team', False, 'personal')])
        self.assertEqual(rows[1]['entry'], 'team/skills:shared-skill')
        libraries = self.status()['libraries']
        self.assertEqual([item['name'] for item in libraries], ['personal', 'team'])
        self.assertEqual(libraries[1]['repo'], 'team/skills')

        self.projects('prefer', 'shared-skill', 'team')
        self.assert_global_from('shared-skill', self.team)
        self.assertEqual(self.config()['prefer'], {'shared-skill': 'team'})
        self.projects('prefer', 'shared-skill', 'auto')
        self.assert_global_from('shared-skill', self.personal)
        self.assertNotIn('prefer', self.config())

        self.projects('order', 'team')
        self.assertEqual([item['name'] for item in self.config()['libraries']], ['team', 'personal'])
        self.assert_global_from('shared-skill', self.team)
        self.assertIn('CONFLICT shared-skill', self.projects('libraries'))
        self.projects('prefer', 'shared-skill', 'nowhere', expected=1)
        self.projects('order', 'nowhere', expected=1)

    def test_project_entry_names_a_library_and_survives_its_disconnection(self):
        self.link()
        project = self.make_project('project')
        self.projects('register', project)
        self.projects('bind', project, 'shared-skill', 'team')
        listed = json.loads((project / '.agent-skills.json').read_text())['skills']
        self.assertEqual(listed, ['team/skills:shared-skill'])
        for agent in AGENTS:
            self.assertTrue(same_path(project / agent / 'skills/shared-skill', self.team / 'skills/shared-skill'))
        bound = next(item for item in self.status()['projects'] if same_path(item['path'], project))['skills']
        self.assertEqual([(item['skill'], item['library'], item['state']) for item in bound],
                         [('shared-skill', 'team', 'applied')])
        # The plain name switches the project back to the copy linked globally.
        self.projects('bind', project, 'shared-skill')
        self.assertEqual(json.loads((project / '.agent-skills.json').read_text())['skills'], ['shared-skill'])
        for agent in AGENTS:
            self.assertTrue(same_path(project / agent / 'skills/shared-skill', self.personal / 'skills/shared-skill'))
        self.projects('bind', project, 'team-skill', 'team')
        self.projects('disconnect', 'team')
        self.assertFalse(os.path.lexists(project / '.agents/skills/team-skill'))
        bound = next(item for item in self.status()['projects'] if same_path(item['path'], project))['skills']
        self.assertIn(('team/skills:team-skill', 'library-missing'), [(item['name'], item['state']) for item in bound])

    def test_failure_of_one_library_does_not_stop_the_others(self):
        self.link()
        # personal gets a new skill upstream; team loses its remote.
        seed = self.root / 'seed' / 'personal'
        self.write_skill(seed, 'fresh-skill', 'new')
        registry = json.loads((seed / 'sources.json').read_text())
        registry['skills']['fresh-skill'] = {'source': {'kind': 'local'}}
        (seed / 'sources.json').write_text(json.dumps(registry, indent=2) + '\n', newline='\n')
        self.git(seed, 'add', '-A')
        self.git(seed, 'commit', '-m', 'Fresh')
        self.git(seed, 'push', 'origin', 'main')
        self.git(self.team, 'remote', 'set-url', 'origin', str(self.root / 'remotes' / 'gone.git'))
        self.sync(expected=1)
        self.assert_global_from('fresh-skill', self.personal)
        self.assert_global_from('team-skill', self.team)
        libraries = {item['name']: item for item in self.status()['libraries']}
        self.assertTrue(libraries['personal']['sync']['ok'])
        self.assertFalse(libraries['team']['sync']['ok'])
        self.assertTrue(libraries['team']['sync']['error'])

    def test_disconnect_keeps_unpublished_work_and_the_clone(self):
        self.link()
        self.write_skill(self.team, 'team-skill', 'local edit')
        self.assertIn('unpublished', self.projects('disconnect', 'team', expected=1))
        self.assert_global_from('team-skill', self.team)
        self.projects('disconnect', 'team', '--force')
        self.assert_no_global('team-skill')
        self.assert_global_from('shared-skill', self.personal)
        self.assertEqual([item['name'] for item in self.config()['libraries']], ['personal'])
        self.assertTrue((self.team / 'skills/team-skill/SKILL.md').is_file())
        # The last library can go as well; the background run then has nothing to do.
        self.projects('disconnect', 'personal')
        self.assert_no_global('shared-skill')
        self.assertEqual(self.config()['libraries'], [])
        self.assertIn('No library is connected', self.sync())
        self.assertIn('No library is connected', self.projects('libraries'))
        self.assertEqual(self.status()['libraries'], [])

    def test_a_machine_never_connected_reports_the_missing_library(self):
        for folder in (self.home / 'AppData/Roaming/Skillarium', self.home / '.config/skillarium'):
            (folder / 'config.json').unlink()
        self.assertIn('Skill library not found', self.sync(expected=None))
        self.assertIn('Skill library not found', self.projects('libraries', expected=None))

    def test_a_corrupt_configuration_is_an_error_not_an_empty_list(self):
        self.link()
        for folder in (self.home / 'AppData/Roaming/Skillarium', self.home / '.config/skillarium'):
            (folder / 'config.json').write_text('{"libraries": [', encoding='utf-8')
        output = self.sync(expected=None)
        self.assertNotIn('No library is connected', output)
        self.assert_global_from('team-skill', self.team)
        self.projects('libraries', expected=1)

    def test_an_unreadable_registry_keeps_the_library_links(self):
        self.link()
        project = self.make_project('project')
        self.projects('register', project)
        self.projects('bind', project, 'team-skill')
        # A project binding hides the global copy; bring it back to watch both kinds of links.
        self.projects('global', 'team-skill', 'on')
        self.assert_global_from('team-skill', self.team)
        (self.team / 'sources.json').write_text('{"version": 2, "skills": ', newline='\n')
        self.link(expected=None)
        self.assert_global_from('team-skill', self.team)
        self.assert_global_from('shared-skill', self.personal)
        for agent in AGENTS:
            self.assertTrue(same_path(project / agent / 'skills/team-skill', self.team / 'skills/team-skill'))
        libraries = {item['name']: item for item in self.status()['libraries']}
        self.assertFalse(libraries['team']['available'])
        # Its unpublished work still blocks a plain disconnection.
        self.assertIn('unpublished', self.projects('disconnect', 'team', expected=1))

    def test_a_skill_dropped_from_its_library_leaves_the_projects(self):
        self.link()
        project = self.make_project('project')
        self.projects('register', project)
        self.projects('bind', project, 'own-skill')
        shutil.rmtree(self.personal / 'skills/own-skill')
        registry = json.loads((self.personal / 'sources.json').read_text())
        del registry['skills']['own-skill']
        (self.personal / 'sources.json').write_text(json.dumps(registry, indent=2) + '\n', newline='\n')
        self.link(expected=None)
        for agent in AGENTS:
            self.assertFalse(os.path.lexists(project / agent / 'skills/own-skill'))

    def test_a_run_given_a_disconnected_library_does_not_bring_it_back(self):
        self.link()
        self.projects('disconnect', 'team')
        # A run that resolved its library before the disconnection (no explicit library).
        self.link()
        self.assert_no_global('team-skill')
        self.assertEqual([item['name'] for item in self.status()['libraries']], ['personal'])

    def test_disconnecting_the_winner_hands_the_name_to_the_next_library(self):
        self.link()
        self.projects('disconnect', 'personal')
        self.assert_global_from('shared-skill', self.team)
        self.assert_no_global('own-skill')

    def test_link_of_a_skill_the_library_dropped_is_removed(self):
        self.link()
        self.assert_global_from('own-skill', self.personal)
        shutil.rmtree(self.personal / 'skills/own-skill')
        registry = json.loads((self.personal / 'sources.json').read_text())
        del registry['skills']['own-skill']
        (self.personal / 'sources.json').write_text(json.dumps(registry, indent=2) + '\n', newline='\n')
        self.link()
        self.assert_no_global('own-skill')

    def test_turning_a_skill_off_removes_only_the_links_the_client_made(self):
        # The user linked one agent folder to the team copy by hand before the first run.
        manual = self.home / '.agents' / 'skills' / 'team-skill'
        manual.parent.mkdir(parents=True)
        if WINDOWS:
            import _winapi
            _winapi.CreateJunction(str(self.team / 'skills/team-skill'), str(manual))
        else:
            os.symlink(self.team / 'skills/team-skill', manual)
        self.link()
        self.assert_global_from('team-skill', self.team)
        # Not fully off while the user's own link stays: the command says so.
        self.assertIn('Foreign global install kept', self.projects('global', 'team-skill', 'off', expected=1))
        self.assertTrue(is_link(manual))
        self.assertFalse(os.path.lexists(self.home / '.claude' / 'skills' / 'team-skill'))

    def test_an_unavailable_library_keeps_its_project_links(self):
        self.link()
        project = self.make_project('project')
        self.projects('register', project)
        self.projects('bind', project, 'team-skill')
        offline = self.team.with_name('team offline')
        self.team.rename(offline)
        try:
            self.link(expected=None)
            self.assertTrue(os.path.lexists(project / '.agents/skills/team-skill'))
            bound = next(item for item in self.status()['projects'] if same_path(item['path'], project))['skills']
            self.assertEqual([(item['name'], item['state']) for item in bound], [('team-skill', 'library-unavailable')])
        finally:
            offline.rename(self.team)
        self.link()
        for agent in AGENTS:
            self.assertTrue(same_path(project / agent / 'skills/team-skill', self.team / 'skills/team-skill'))

    def test_legacy_single_library_configuration_is_one_library(self):
        for folder in (self.home / 'AppData/Roaming/Skillarium', self.home / '.config/skillarium'):
            (folder / 'config.json').write_text(json.dumps({'library': str(self.personal)}), encoding='utf-8')
        self.link()
        self.assert_global_from('shared-skill', self.personal)
        self.assert_no_global('team-skill')
        self.assertEqual([item['name'] for item in self.status()['libraries']], ['skills'])
        # The server tools name it the same way, from a configuration with a byte order mark.
        for folder in (self.home / 'AppData/Roaming/Skillarium', self.home / '.config/skillarium'):
            (folder / 'config.json').write_text(json.dumps({'library': str(self.personal)}), encoding='utf-8-sig')
        script = ('import sys; sys.path.insert(0, sys.argv[1]); import manage; '
                  'print(manage.configured_libraries()[0][0]); print(manage.named_library("skills"))')
        result = subprocess.run([sys.executable, '-c', script, str(ROOT / 'server')], env=self.env,
                                capture_output=True, text=True, check=True)
        name, path = result.stdout.splitlines()
        self.assertEqual(name, 'skills')
        self.assertTrue(same_path(path, self.personal))


if __name__ == '__main__':
    unittest.main()
