"""Migration to the unified repository: histories, server switch-over, clients."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from fixture import HAS_BASH_CLIENT, Library, POWERSHELL, ROOT

sys.path.insert(0, str(ROOT / 'server'))
import absorb_updater  # noqa: E402

CLIENT = absorb_updater.CLIENT_FILES


class AbsorbTests(unittest.TestCase):
    """Both histories, collisions and dirty copies, on disposable repositories."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent skills absorb ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        (self.root / 'global.gitconfig').write_text('')
        self.env = dict(os.environ, GIT_CONFIG_GLOBAL=str(self.root / 'global.gitconfig'), GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='t@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='t@example.invalid')
        self.library = self.repo('library')
        self.updater = self.repo('updater')
        for relative in CLIENT:
            for target in (self.library / relative, self.updater / 'client' / relative):
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, target)
        manifest = dict(version=1, generator='agent-skills-updater/client', files={
            relative: __import__('hashlib').sha256(absorb_updater.canonical((ROOT / relative).read_bytes())).hexdigest()
            for relative in CLIENT})
        (self.library / absorb_updater.MANIFEST).write_text(json.dumps(manifest))
        (self.library / 'sources.json').write_text('{"version": 2, "skills": {}}\n')
        (self.library / 'skills/own/SKILL.md').parent.mkdir(parents=True)
        (self.library / 'skills/own/SKILL.md').write_text('---\nname: own\ndescription: d\n---\n')
        (self.library / '.gitignore').write_text('.env\n*.key\n')
        self.commit(self.library, 'Library history')
        (self.updater / 'server').mkdir()
        (self.updater / 'server/manage.py').write_text('print("server")\n')
        (self.updater / 'README.md').write_text('Updater readme\n')
        (self.updater / '.gitignore').write_text('*.key\n__pycache__/\n')
        (self.updater / 'AGENTS.md').write_text('Agents\n')
        self.commit(self.updater, 'Updater history')

    def repo(self, name):
        path = self.root / name
        path.mkdir()
        self.git(path, 'init', '-q', '-b', 'main')
        return path

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env, capture_output=True,
                              text=True, check=True).stdout.strip()

    def commit(self, repo, message):
        self.git(repo, 'add', '-A', '.')
        self.git(repo, 'commit', '-q', '-m', message)

    def absorb(self, expected=0):
        result = subprocess.run([sys.executable, str(ROOT / 'server/manage.py'), '--repo', str(self.library),
                                 'absorb-updater', '--updater', str(self.updater)],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_both_histories_and_paths_are_kept_and_repeat_is_noop(self):
        library_head = self.git(self.library, 'rev-parse', 'HEAD')
        updater_head = self.git(self.updater, 'rev-parse', 'HEAD')
        self.absorb()
        head = self.git(self.library, 'rev-parse', 'HEAD')
        self.assertEqual(self.git(self.library, 'log', '-1', '--format=%P').split(), [library_head, updater_head])
        self.assertEqual(len(self.git(self.library, 'rev-list', '--max-parents=0', 'HEAD').split()), 2)
        self.assertEqual((self.library / 'server/README.md').read_text(), 'Updater readme\n')
        self.assertEqual((self.library / 'server/manage.py').read_text(), 'print("server")\n')
        self.assertEqual((self.library / '.gitignore').read_text(), '.env\n*.key\n__pycache__/\n')
        self.assertTrue((self.library / 'skills/own/SKILL.md').exists())
        self.assertFalse((self.library / 'client').exists())
        self.assertIn('already absorbed', self.absorb())
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), head)

    def test_dirty_copies_and_collisions_are_preserved_for_review(self):
        head = self.git(self.library, 'rev-parse', 'HEAD')
        (self.updater / 'uncommitted.txt').write_text('work in progress')
        self.assertIn('updater has local changes', self.absorb(expected=1))
        self.assertEqual((self.updater / 'uncommitted.txt').read_text(), 'work in progress')
        (self.updater / 'uncommitted.txt').unlink()
        (self.library / 'server').mkdir()
        (self.library / 'server/manage.py').write_text('different\n')
        self.commit(self.library, 'Colliding file')
        head = self.git(self.library, 'rev-parse', 'HEAD')
        self.assertIn('server/manage.py', self.absorb(expected=1))
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), head)
        self.git(self.library, 'rm', '-q', 'server/manage.py')
        self.git(self.library, 'commit', '-q', '-m', 'Remove collision')
        install = self.library / 'install.sh'
        install.write_text(install.read_text() + '\n# manual edit\n')
        self.commit(self.library, 'Manual client edit')
        self.assertIn('install.sh: library copy has unrecognized edits', self.absorb(expected=1))
        self.assertIn('# manual edit', install.read_text())

    def test_interrupted_run_leaves_branch_and_can_be_repeated(self):
        head = self.git(self.library, 'rev-parse', 'HEAD')
        original = absorb_updater.git

        def failing(repo, *args, **kwargs):
            if args[0] == 'merge':
                raise ValueError('interrupted')
            return original(repo, *args, **kwargs)
        with mock.patch.dict(os.environ, self.env), mock.patch.object(absorb_updater, 'git', failing):
            self.assertEqual(absorb_updater.absorb(self.library, self.updater), 1)
        self.assertEqual(self.git(self.library, 'rev-parse', 'HEAD'), head)
        self.assertEqual(self.git(self.library, 'status', '--porcelain'), '')
        self.absorb()
        self.assertEqual(self.git(self.library, 'log', '-1', '--format=%P').split()[0], head)

    def test_this_repository_contains_both_original_histories(self):
        if self.git(ROOT, 'rev-parse', '--is-shallow-repository') == 'true':
            self.skipTest('shallow checkout')
        for revision in ('469e62c', '66b5daa7e295b2fdb11269033dc676b29c239b21'):
            if subprocess.run(['git', '-C', str(ROOT), 'cat-file', '-e', revision + '^{commit}']).returncode:
                self.skipTest('original history not fetched')
            self.git(ROOT, 'merge-base', '--is-ancestor', revision, 'HEAD')


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux tools unavailable')
class ServerSwitchTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='agent skills switch ')
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.home = self.root / 'home'
        self.units = self.home / '.config/systemd/user'
        self.units.mkdir(parents=True)
        shims = self.root / 'bin'
        shims.mkdir()
        (shims / 'systemctl').write_text('#!/bin/sh\necho "$@" >> "$HOME/systemctl.log"\n')
        (shims / 'systemctl').chmod(0o755)
        self.library = self.root / 'agent skills'
        shutil.copytree(ROOT / 'server', self.library / 'server')
        subprocess.run(['git', 'init', '-q', str(self.library)], check=True)
        self.env = dict(os.environ, HOME=str(self.home), XDG_CONFIG_HOME=str(self.home / '.config'),
                        PATH=str(shims) + os.pathsep + os.environ['PATH'], GIT_CONFIG_GLOBAL=os.devnull)

    def install(self, *args, expected=0):
        result = subprocess.run(['bash', str(self.library / 'server/install.sh'), *args], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_legacy_updater_timer_is_switched_off_once(self):
        legacy = self.root / 'agent-skills-updater'
        legacy.mkdir()
        (legacy / 'uncommitted.py').write_text('user work')
        for unit in ('agent-skills-updater.service', 'agent-skills-updater.timer'):
            (self.units / unit).write_text('# Managed by agent-skills-updater\n[Unit]\n')
        self.install('--install', 'i3')
        log = (self.home / 'systemctl.log').read_text()
        self.assertIn('--user disable --now agent-skills-updater.timer', log)
        self.assertIn('--user enable --now agent-skills-server.timer', log)
        self.assertFalse((self.units / 'agent-skills-updater.timer').exists())
        self.assertIn('server/run.sh', (self.units / 'agent-skills-server.service').read_text())
        self.assertEqual((legacy / 'uncommitted.py').read_text(), 'user work')
        self.assertEqual(subprocess.run(['git', '-C', str(self.library), 'config', 'agent-skills.machine'],
                                        capture_output=True, text=True, env=self.env).stdout.strip(), 'i3')
        self.install('--install')
        self.assertEqual(log.count('agent-skills-updater.timer'), 1)

    def test_unrecognized_units_are_preserved(self):
        (self.units / 'agent-skills-updater.timer').write_text('[Unit]\n# user unit\n')
        self.assertIn('Unrecognized legacy unit', self.install(expected=1))
        self.assertTrue((self.units / 'agent-skills-updater.timer').exists())
        self.assertFalse((self.units / 'agent-skills-server.timer').exists())


class ClientMigrationTests(unittest.TestCase):
    """Existing clones keep their links; pending automatic commits move to PRs."""

    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.client = self.lib.clone('old client', 'oldbox')
        # A commit of the previous client that could not be pushed directly.
        path = self.client / 'skills/first-skill/SKILL.md'
        path.write_text(path.read_text() + '\nEdit made by the previous client')
        self.lib.git(self.client, 'add', '-A')
        self.lib.git(self.client, 'commit', '-q', '-m', 'Share local skill edits\n\nAgent-Skills-Client: 1',
                     env=dict(GIT_AUTHOR_NAME='agent-skills', GIT_AUTHOR_EMAIL='agent-skills@localhost'))

    def check(self):
        lib = self.lib
        self.assertIn('Edit made by the previous client', lib.show('skills/first-skill/SKILL.md'))
        self.assertEqual(lib.git(lib.remote, 'log', '-1', '--format=%an', 'main^2'), 'agent-skills')
        self.assertIn('Client manifest current', lib.manage('status', repo=self.client))

    @unittest.skipUnless(HAS_BASH_CLIENT, 'Linux tools unavailable')
    def test_linux_client_publishes_pending_commit_through_pull_request(self):
        self.lib.sync(self.client)
        self.check()

    @unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell unavailable')
    def test_windows_client_publishes_pending_commit_through_pull_request(self):
        self.lib.powershell(self.client)
        self.check()


if __name__ == '__main__':
    unittest.main()
