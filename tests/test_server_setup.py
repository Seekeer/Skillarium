"""`skillarium server` (server/setup.py) against a local Gitea stand-in: the three
repository cases, Gitea settings, the service bot, runners and repeat runs.

Timers are skipped (--no-timers); Git and Skillarium configuration live in a
temporary HOME.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from fake_gitea import FakeGitea

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / 'templates' / 'library'


class ServerSetupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='skillarium server ')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.gitea = FakeGitea(self.root / 'gitea')
        self.addCleanup(self.gitea.stop)
        self.gitea.add_user('alice', 'alice-token', admin=True)
        self.gitea.add_user('bob', 'bob-token')
        self.home = self.root / 'home'
        self.home.mkdir()
        self.gitconfig = self.root / 'gitconfig'
        self.gitea.git_config(self.gitconfig)
        self.env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home), GIT_CONFIG_GLOBAL=str(self.gitconfig),
                        GIT_CONFIG_NOSYSTEM='1', XDG_CONFIG_HOME=str(self.home / '.config'),
                        XDG_DATA_HOME=str(self.home / '.local' / 'share'),
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@localhost',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@localhost')
        self.env.pop('SKILLARIUM_LIBRARY', None)
        self.library = self.home / 'Skillarium' / 'library'

    def token(self, value):
        path = self.root / f'{value}.txt'
        path.write_text(value + '\n', encoding='utf-8')
        return str(path)

    def setup_server(self, *extra, token='alice-token', expected=0, repo=('alice', 'skills')):
        result = subprocess.run([sys.executable, str(ROOT / 'server' / 'setup.py'), '--repo', self.gitea.url(*repo),
                                 '--token-file', self.token(token), '--path', str(self.library), '--machine', 'i3',
                                 '--no-timers', *extra], env=self.env, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env, check=True, capture_output=True,
                              text=True, encoding='utf-8').stdout.strip()

    def bare(self, owner='alice', name='skills'):
        return self.gitea.bares / owner / f'{name}.git'

    def shown(self, path, owner='alice', name='skills'):
        return self.git(self.bare(owner, name), 'show', f'main:{path}')

    def seed(self, files, owner='alice', name='skills'):
        """Push a first commit with the given files to an existing repository."""
        work = self.root / 'seed'
        self.git(self.root, 'clone', '-q', self.gitea.url(owner, name), str(work))
        for relative, text in files.items():
            (work / relative).parent.mkdir(parents=True, exist_ok=True)
            (work / relative).write_text(text, encoding='utf-8', newline='\n')
        self.git(work, 'add', '-A')
        self.git(work, 'commit', '-q', '-m', 'Seed')
        self.git(work, 'push', '-q', 'origin', 'HEAD:main')

    def library_files(self, machine='i3', format_=1):
        config = json.loads((TEMPLATE / 'agent-skills.json').read_text(encoding='utf-8'))
        config['server']['machine'] = machine
        config['format'] = format_
        return {'agent-skills.json': json.dumps(config, indent=2) + '\n',
                'sources.json': (TEMPLATE / 'sources.json').read_text(encoding='utf-8')}

    def test_missing_repository_is_created_and_initialized(self):
        output = self.setup_server()
        self.assertIn('Repository created: alice/skills', output)
        config = json.loads(self.shown('agent-skills.json'))
        self.assertEqual(config['server']['machine'], 'i3')
        self.assertEqual(config['format'], 1)
        self.assertIn('.gitea/workflows/policy.yml', self.git(self.bare(), 'ls-tree', '-r', '--name-only', 'main'))
        readme = self.shown('README.md')
        self.assertIn(f'client --library {self.gitea.url("alice", "skills")}', readme)
        self.assertIn(f'client -Library {self.gitea.url("alice", "skills")}', readme)
        self.assertNotIn('alice-token', readme + output)
        repo = self.gitea.repos[('alice', 'skills')]
        self.assertTrue(repo['settings']['has_actions'])
        self.assertEqual(repo['protections']['main']['status_check_contexts'], ['*policy*'])
        self.assertTrue(repo['protections']['main']['block_on_outdated_branch'])
        self.assertEqual(json.loads((self.home / '.config' / 'skillarium' / 'config.json').read_text())['library'],
                         str(self.library.resolve()))
        self.assertEqual(self.git(self.library, 'config', 'agent-skills.machine'), 'i3')
        filled = subprocess.run(['git', 'credential', 'fill'], input=f'protocol=http\nhost={self.gitea.base[7:]}\n\n',
                                env=self.env, capture_output=True, text=True).stdout
        self.assertIn('password=alice-token', filled)
        self.assertIn('no Actions runner serves this repository', output)
        self.assertIn('Connect machines with:', output)
        # Repeating the setup changes nothing and duplicates nothing.
        head = self.git(self.bare(), 'rev-parse', 'main')
        again = self.setup_server()
        self.assertEqual(self.git(self.bare(), 'rev-parse', 'main'), head)
        self.assertIn('this machine (i3) is its server', again)
        self.assertEqual(list(repo['protections']), ['main'])
        self.assertTrue(any(call[0] == 'PATCH' and call[1].endswith('/branch_protections/main')
                            for call in self.gitea.calls))

    def test_empty_repository_of_an_organization_is_initialized(self):
        self.gitea.create_repo('team', 'skills')
        output = self.setup_server(repo=('team', 'skills'))
        self.assertNotIn('Repository created', output)
        self.assertEqual(json.loads(self.shown('agent-skills.json', 'team'))['server']['machine'], 'i3')

    def test_existing_library_is_connected_without_changes(self):
        self.gitea.create_repo('alice', 'skills')
        self.seed(self.library_files())
        head = self.git(self.bare(), 'rev-parse', 'main')
        output = self.setup_server()
        self.assertIn('Library connected; this machine (i3) is its server', output)
        self.assertEqual(self.git(self.bare(), 'rev-parse', 'main'), head)
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), head)

    def test_repository_that_is_not_a_library_is_refused(self):
        self.gitea.create_repo('alice', 'skills')
        self.seed({'README.md': 'Something else\n'})
        output = self.setup_server(expected=1)
        self.assertIn('not a skill library', output)
        self.assertEqual(self.gitea.repos[('alice', 'skills')]['protections'], {})

    def test_library_of_a_newer_format_is_refused(self):
        self.gitea.create_repo('alice', 'skills')
        self.seed(self.library_files(format_=2))
        output = self.setup_server(expected=1)
        self.assertIn('update Skillarium on this server first', output)

    def test_service_bot_is_created_once(self):
        output = self.setup_server('--bot', 'skills-bot')
        token_file = self.home / '.config' / 'skillarium' / 'bot-token'
        token = token_file.read_text(encoding='utf-8').strip()
        self.assertIn(token, output)
        self.assertIn(token, self.gitea.users['skills-bot']['tokens'])
        self.assertEqual(self.gitea.repos[('alice', 'skills')]['collaborators'], {'skills-bot': 'write'})
        if os.name != 'nt':
            self.assertEqual(token_file.stat().st_mode & 0o777, 0o600)
        again = self.setup_server('--bot', 'skills-bot')
        self.assertIn('exists; its token is kept in', again)
        self.assertNotIn(token, again)
        self.assertEqual(len(self.gitea.users['skills-bot']['tokens']), 1)

    def test_bot_needs_a_site_administrator(self):
        output = self.setup_server('--bot', 'skills-bot', token='bob-token', repo=('bob', 'skills'), expected=1)
        self.assertIn('site administrator', output)

    def test_rejected_token_stops_before_any_change(self):
        output = self.setup_server(token='wrong', expected=1)
        self.assertIn('Gitea rejected the token (401)', output)
        self.assertEqual(self.gitea.repos, {})

    def test_existing_runner_is_reported(self):
        self.gitea.runners.append(dict(name='i3-runner', status='online', labels=[dict(name='ubuntu-latest')]))
        self.assertIn('Actions runners: i3-runner', self.setup_server())


if __name__ == '__main__':
    unittest.main()
