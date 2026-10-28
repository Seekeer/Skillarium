"""Exercise observable sync behavior against local Git repositories."""
import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


INSTALLER = Path(__file__).resolve().parents[1] / 'install.sh'


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent skills ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'user home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='Test',
                        GIT_AUTHOR_EMAIL='test@example.invalid', GIT_COMMITTER_NAME='Test',
                        GIT_COMMITTER_EMAIL='test@example.invalid')
        self.remote = self.root / 'remote.git'
        self.git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.seed = self.root / 'publisher'
        self.seed.mkdir()
        self.git(self.seed, 'init', '-b', 'main')
        shutil.copyfile(INSTALLER, self.seed / 'install.sh')
        self.skill = self.seed / 'skills' / 'first-skill'
        self.skill.mkdir(parents=True)
        (self.skill / 'SKILL.md').write_text('version one')
        self.publish('Initial')
        self.git(self.seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.seed, 'push', '-u', 'origin', 'main')
        self.consumer = self.root / 'consumer with spaces'
        self.git(self.root, 'clone', str(self.remote), str(self.consumer))

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env,
                              text=True, capture_output=True, check=True).stdout.strip()

    def publish(self, message):
        self.git(self.seed, 'add', '.')
        self.git(self.seed, 'commit', '-m', message)
        if (self.seed / '.git/refs/remotes/origin/main').exists():
            self.git(self.seed, 'push')

    def sync(self, mode='--sync-only', expected=0, env=None):
        result = subprocess.run(['bash', str(self.consumer / 'install.sh'), mode],
                                env=env or self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_new_skills_and_existing_content_update_in_both_catalogs(self):
        self.sync()
        (self.skill / 'SKILL.md').write_text('version two')
        new = self.seed / 'skills/new-skill'
        new.mkdir()
        (new / 'SKILL.md').write_text('new')
        self.publish('Update')
        self.sync()
        self.sync()
        for catalog in ['.agents', '.claude']:
            path = self.home / catalog / 'skills'
            self.assertEqual((path / 'first-skill/SKILL.md').read_text(), 'version two')
            self.assertEqual((path / 'new-skill/SKILL.md').read_text(), 'new')
            self.assertEqual((path / 'first-skill').resolve(), self.consumer / 'skills/first-skill')

    def test_conflict_preserves_user_files_and_installs_other_catalog(self):
        existing = self.home / '.agents/skills/first-skill'
        existing.mkdir(parents=True)
        (existing / 'private').write_text('keep')
        self.assertIn('CONFLICT', self.sync(expected=1))
        self.assertEqual((existing / 'private').read_text(), 'keep')
        self.assertTrue((self.home / '.claude/skills/first-skill').is_symlink())

    def test_dangling_link_is_preserved(self):
        existing = self.home / '.agents/skills/first-skill'
        existing.parent.mkdir(parents=True)
        existing.symlink_to(self.root / 'missing', target_is_directory=True)
        self.sync(expected=1)
        self.assertEqual(existing.readlink(), self.root / 'missing')

    def test_dirty_worktree_is_preserved_and_not_pulled(self):
        original_head = self.git(self.consumer, 'rev-parse', 'HEAD')
        local = self.consumer / 'skills/first-skill/SKILL.md'
        local.write_text('local edits')
        (self.skill / 'SKILL.md').write_text('upstream edits')
        self.publish('Upstream')
        self.sync(expected=1)
        self.assertEqual(local.read_text(), 'local edits')
        self.assertEqual(self.git(self.consumer, 'rev-parse', 'HEAD'), original_head)

    def test_diverged_history_is_preserved(self):
        (self.consumer / 'local.txt').write_text('local')
        self.git(self.consumer, 'add', '.')
        self.git(self.consumer, 'commit', '-m', 'Local')
        head = self.git(self.consumer, 'rev-parse', 'HEAD')
        (self.skill / 'SKILL.md').write_text('upstream')
        self.publish('Remote')
        self.sync(expected=1)
        self.assertEqual(self.git(self.consumer, 'rev-parse', 'HEAD'), head)

    def test_offline_sync_still_links_installed_version(self):
        self.remote.rename(self.root / 'offline.git')
        self.sync(expected=1)
        self.assertEqual((self.home / '.agents/skills/first-skill/SKILL.md').read_text(), 'version one')

    def test_lock_prevents_parallel_changes(self):
        with (self.consumer / '.git/agent-skills-sync.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Another sync', self.sync())
            self.assertFalse((self.home / '.agents').exists())

    def test_local_only_install_without_remote(self):
        self.git(self.consumer, 'remote', 'remove', 'origin')
        self.assertIn('No origin', self.sync())
        self.assertTrue((self.home / '.claude/skills/first-skill').is_symlink())

    def test_detached_head_and_missing_upstream_are_errors(self):
        self.git(self.consumer, 'branch', '--unset-upstream')
        self.assertIn('No upstream', self.sync(expected=1))
        self.git(self.consumer, 'checkout', '--detach')
        self.assertIn('Detached HEAD', self.sync(expected=1))

    def test_deleted_skills_leave_links_for_manual_removal(self):
        self.sync()
        shutil.rmtree(self.skill)
        self.publish('Remove skill')
        self.sync()
        path = self.home / '.agents/skills/first-skill'
        self.assertTrue(path.is_symlink())
        self.assertFalse(path.exists())


if __name__ == '__main__':
    unittest.main()
