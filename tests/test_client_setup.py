"""`skillarium client` through the bootstrap: an engine installed from a local
release, a library in a local Gitea stand-in, a temporary HOME.

Schedules are not registered (--no-timers / -NoSchedule); the app installer
is checked up to its verified download.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

from fake_gitea import FakeGitea
from fake_releases import FakeReleases
from fixture import HAS_BASH_CLIENT, POWERSHELL, ROOT

WINDOWS = os.name == 'nt'
SKILL = '---\nname: first-skill\ndescription: A skill of the library\n---\n\nShared text\n'


class ClientSetup:
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skillarium client ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gitea = FakeGitea(self.root / 'gitea')
        self.addCleanup(self.gitea.stop)
        self.releases = FakeReleases(self.root / 'releases')
        self.addCleanup(self.releases.stop)
        self.gitea.add_user('alice', 'alice-token')
        self.gitea.add_user('bob', 'bob-token')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.gitconfig = self.root / 'gitconfig'
        self.gitea.git_config(self.gitconfig)
        with self.gitconfig.open('a', encoding='utf-8') as stream:
            stream.write('[credential]\n\thelper = store\n')
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                        APPDATA=str(self.home / 'AppData' / 'Roaming'), LOCALAPPDATA=str(self.home / 'AppData' / 'Local'),
                        XDG_CONFIG_HOME=str(self.home / '.config'), XDG_CACHE_HOME=str(self.home / '.cache'),
                        SKILLARIUM_HOME=str(self.root / 'engine'), SKILLARIUM_BIN=str(self.root / 'bin'),
                        GIT_CONFIG_GLOBAL=str(self.gitconfig), GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@localhost',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@localhost', **self.releases.env())
        self.env.pop('SKILLARIUM_LIBRARY', None)
        self.gitea.create_repo('alice', 'skills')
        work = self.root / 'seed'
        self.git(self.root, 'clone', '-q', self.gitea.url('alice', 'skills'), str(work))
        config = json.loads((ROOT / 'templates' / 'library' / 'agent-skills.json').read_text(encoding='utf-8'))
        config['server']['machine'] = 'i3'
        files = {'agent-skills.json': json.dumps(config, indent=2) + '\n',
                 'sources.json': '{"version": 2, "skills": {}}\n', 'skills/first-skill/SKILL.md': SKILL}
        for relative, text in files.items():
            (work / relative).parent.mkdir(parents=True, exist_ok=True)
            (work / relative).write_text(text, encoding='utf-8', newline='\n')
        self.git(work, 'add', '-A')
        self.git(work, 'commit', '-q', '-m', 'Library')
        self.git(work, 'push', '-q', 'origin', 'HEAD:main')
        self.path = self.home / 'Skillarium' / 'library'

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env, check=True, capture_output=True,
                              text=True, encoding='utf-8').stdout.strip()

    def token(self, value):
        path = self.root / f'{value}.txt'
        path.write_text(value + '\n', encoding='utf-8')
        return str(path)

    def connect(self, *extra, token='alice-token', expected=0, app=False):
        raise NotImplementedError

    def test_connect_clone_link_and_repeat(self):
        self.releases.publish('0.1.0')
        output = self.connect()
        self.assertIn('CONNECTED: ' + self.gitea.url('alice', 'skills'), output)
        self.assertEqual(self.git(self.path, 'rev-parse', 'HEAD'), self.git(self.gitea.bares / 'alice' / 'skills.git',
                                                                             'rev-parse', 'main'))
        self.assertEqual(Path(json.loads(self.config_file().read_text(encoding='utf-8'))['library']).resolve(),
                         self.path.resolve())
        linked = self.home / '.agents' / 'skills' / 'first-skill' / 'SKILL.md'
        self.assertIn('Shared text', linked.read_text(encoding='utf-8'))
        self.assertIn('alice-token', (self.home / '.git-credentials').read_text(encoding='utf-8'))
        self.assertNotIn('alice-token', output)
        self.assertIn('Existing clone kept', self.connect())

    def test_token_without_write_access_changes_nothing(self):
        self.releases.publish('0.1.0')
        output = self.connect(token='bob-token', expected=1)
        self.assertIn('cannot write to alice/skills', output)
        self.assertFalse(self.path.exists())
        self.assertFalse((self.home / '.git-credentials').exists())

    def test_folder_with_another_clone_is_refused(self):
        self.releases.publish('0.1.0')
        self.gitea.create_repo('alice', 'other')
        self.path.parent.mkdir(parents=True)
        self.git(self.root, 'clone', '-q', self.gitea.url('alice', 'other'), str(self.path))
        self.assertIn('not of', self.connect(expected=1))

    def test_app_with_a_wrong_checksum_is_not_installed(self):
        self.releases.publish('0.1.0', corrupt_app=True)
        output = self.connect(expected=1, app=True)
        self.assertIn('checksum mismatch', output.lower())


@unittest.skipUnless(WINDOWS and POWERSHELL, 'Windows PowerShell unavailable')
class PowerShellClientSetupTests(ClientSetup, unittest.TestCase):
    def config_file(self):
        return self.home / 'AppData' / 'Roaming' / 'Skillarium' / 'config.json'

    def connect(self, *extra, token='alice-token', expected=0, app=False):
        arguments = ['client', '-Library', self.gitea.url('alice', 'skills'), '-TokenFile', self.token(token),
                     '-NoSchedule', '-NoLaunch', *([] if app else ['-NoApp']), *extra]
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(ROOT / 'scripts' / 'skillarium.ps1'), *arguments], env=self.env,
                                capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux client tools unavailable')
class BashClientSetupTests(ClientSetup, unittest.TestCase):
    def config_file(self):
        return self.home / '.config' / 'skillarium' / 'config.json'

    def connect(self, *extra, token='alice-token', expected=0, app=False):
        arguments = ['client', '--library', self.gitea.url('alice', 'skills'), '--token-file', self.token(token),
                     '--no-timers', '--no-launch', *([] if app else ['--no-app']), *extra]
        result = subprocess.run(['bash', str(ROOT / 'scripts' / 'skillarium.sh'), *arguments], env=self.env,
                                capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output

    def test_app_image_is_installed_with_a_menu_entry(self):
        self.releases.publish('0.1.0', app=True)
        self.connect(app=True)
        app = self.home / '.local' / 'bin' / 'Skillarium.AppImage'
        self.assertEqual(app.read_bytes(), b'installer of 0.1.0')
        self.assertTrue(os.access(app, os.X_OK))
        self.assertIn(f'Exec="{app}"', (self.home / '.local' / 'share' / 'applications' / 'skillarium.desktop')
                      .read_text(encoding='utf-8'))


if __name__ == '__main__':
    unittest.main()
