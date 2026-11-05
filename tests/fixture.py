"""Disposable library: upstream source, bare remote, fake hosting and clones.

Nothing here touches the real profile: HOME, caches and Git configuration of
child processes point into a temporary directory.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

from fake_hosting import FakeHosting, protect

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ('install.sh', 'install.ps1', 'scripts/client.sh', 'scripts/client.ps1', 'scripts/sync-hidden.vbs',
          'scripts/locate.sh', 'scripts/locate.ps1', 'scripts/bridge.sh', 'scripts/bridge.ps1',
          'projects.sh', 'projects.ps1', 'scripts/bindings.sh', 'scripts/bindings.ps1',
          'README.md', '.gitattributes', '.gitignore', '.agent-skills-client.json')
TEST_TASK = 'Skillarium-Test-' + uuid.uuid4().hex
HAS_BASH_CLIENT = os.name != 'nt' and all(shutil.which(tool) for tool in ('bash', 'jq', 'flock', 'curl'))
POWERSHELL = (shutil.which('powershell.exe') if os.name == 'nt'
              else os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'))


class Library:
    def __init__(self, case, server_machine='i3'):
        temp = tempfile.TemporaryDirectory(prefix='agent skills ')
        case.addCleanup(temp.cleanup)
        self.case = case
        self.root = Path(temp.name)
        self.home = self.root / 'user home'
        self.home.mkdir()
        (self.root / 'global.gitconfig').write_text('')
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        XDG_CACHE_HOME=str(self.home / '.cache'), LOCALAPPDATA=str(self.home / 'AppData' / 'Local'),
                        GIT_CONFIG_GLOBAL=str(self.root / 'global.gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid',
                        GIT_TERMINAL_PROMPT='0', PYTHONIOENCODING='utf-8',
                        # Legacy-layout syncs would install Skillarium from GitHub; tests/test_bridge.py covers it.
                        SKILLARIUM_NO_BRIDGE='1',
                        # Sync and Install modes change a task of this name: never the user's own.
                        SKILLARIUM_TASK_NAME=TEST_TASK, SKILLARIUM_UPDATE_TASK=TEST_TASK + '-Update')
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-q', '-b', 'main')
        self.write_skill('first-skill', 'version one')
        self.commit(self.upstream, 'Initial source')
        self.remote = self.root / 'library.git'
        self.git(self.root, 'init', '-q', '--bare', '--initial-branch=main', str(self.remote))
        seed = self.root / 'seed'
        seed.mkdir()
        self.git(seed, 'init', '-q', '-b', 'main')
        for relative in CLIENT:
            (seed / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, seed / relative)
        (seed / 'sources.json').write_text('{"version": 2, "skills": {}}\n')
        config = json.loads((ROOT / 'templates/library/agent-skills.json').read_text())
        config['server']['machine'] = server_machine
        (seed / 'agent-skills.json').write_text(json.dumps(config, indent=2) + '\n')
        self.commit(seed, 'Library')
        self.git(seed, 'push', '-q', str(self.remote), 'main')
        protect(self.remote)
        self.host = FakeHosting(self.remote, self.env)
        case.addCleanup(self.host.stop)
        self.server = self.clone('server', 'i3')

    def git(self, cwd, *args, env=None):
        result = subprocess.run(['git', '-C', str(cwd), *args], env=dict(self.env, **(env or {})),
                                capture_output=True, text=True, encoding='utf-8')
        if result.returncode:
            raise AssertionError(f'git {args} failed: {result.stdout}{result.stderr}')
        return result.stdout.strip()

    def commit(self, repo, message):
        self.git(repo, 'add', '-A', '.')
        self.git(repo, 'commit', '-q', '-m', message)

    def write_skill(self, name, content, root=None):
        folder = (root or self.upstream / 'skills') / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test skill\n---\n{content}', newline='\n')
        return folder

    def clone(self, name, machine):
        path = self.root / name
        self.git(self.root, 'clone', '-q', str(self.remote), str(path))
        self.configure(path, machine)
        return path

    def configure(self, path, machine):
        self.git(path, 'config', 'agent-skills.machine', machine)
        self.git(path, 'config', 'agent-skills.api', self.host.api)
        self.git(path, 'config', 'credential.helper', '!f() { echo username=test; echo password=secret; }; f')

    def manage(self, *args, repo=None, expected=0):
        result = subprocess.run([sys.executable, str(ROOT / 'server/manage.py'), '--repo', str(repo or self.server), *args],
                                env=self.env, capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        if expected is not None:
            self.case.assertEqual(result.returncode, expected, output)
        return output

    def register(self, name='first-skill', policy='auto'):
        return self.manage('register', name, '--git-url', self.upstream.as_uri(), '--path', 'skills/' + name,
                           '--policy', policy)

    def main(self, path=''):
        return self.git(self.remote, 'rev-parse', 'main' + (':' + path if path else ''))

    def show(self, path, revision='main'):
        return self.git(self.remote, 'show', f'{revision}:{path}')

    def registry(self, revision='main'):
        return json.loads(self.show('sources.json', revision))

    def pull(self, repo):
        self.git(repo, 'pull', '-q', '--ff-only')

    def sync(self, repo, mode='--sync-only', expected=0, home=None, args=()):
        env = dict(self.env, HOME=str(home or self.home), XDG_CACHE_HOME=str((home or self.home) / '.cache'))
        result = subprocess.run(['bash', str(repo / 'install.sh'), mode, *args], env=env,
                                capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        if expected is not None:
            self.case.assertEqual(result.returncode, expected, output)
        return output

    def powershell(self, repo, mode='Sync', expected=0, home=None, extra=()):
        env = dict(self.env, HOME=str(home or self.home), USERPROFILE=str(home or self.home),
                   LOCALAPPDATA=str((home or self.home) / 'AppData' / 'Local'))
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(repo / 'install.ps1'), '-Mode', mode, *extra],
                                env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.case.assertEqual(result.returncode, expected, output)
        return output

    def installed(self, name='first-skill', catalog='.agents', home=None):
        return (home or self.home) / catalog / 'skills' / name
