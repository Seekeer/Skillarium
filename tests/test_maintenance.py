"""Server ownership, deployment and publication against disposable catalogs."""
import json
from pathlib import Path
import shutil
import subprocess
import unittest

import test_sync

ROOT = Path(__file__).resolve().parents[1]


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_sync.RegistryTests('test_clients_only_follow_approved_versions_and_install_new_entries')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.templates = self.fixture.root / 'templates'
        shutil.copytree(ROOT / 'client', self.templates)

    def manage(self, *args, expected=0):
        f = self.fixture
        result = subprocess.run(['python3', str(ROOT / 'server/manage.py'),
                                 '--config', str(f.config), '--client-dir', str(self.templates), *args],
                                env=f.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def test_client_deployment_upgrade_and_repeat_preserve_registry(self):
        f = self.fixture
        registry = (f.seed / 'sources.json').read_bytes()
        self.manage('deploy-client', '--publish')
        manifest = f.seed / '.agent-skills-client.json'
        self.assertTrue(manifest.is_file())
        script = self.templates / 'install.sh'
        script.write_text(script.read_text() + '\n# New template version\n')
        self.manage('maintain', '--publish')
        self.assertEqual((f.seed / 'install.sh').read_bytes(), script.read_bytes())
        self.assertEqual((f.seed / 'sources.json').read_bytes(), registry)
        head = f.git(f.seed, 'rev-parse', 'HEAD')
        self.manage('maintain', '--publish')
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), head)

    def test_committed_client_edits_are_preserved(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        installer = f.seed / 'install.sh'
        installer.write_text(installer.read_text() + '\n# Local customization\n')
        f.commit(f.seed, 'Manual customization')
        f.git(f.seed, 'push')
        before = installer.read_bytes()
        self.assertIn('unrecognized edits', self.manage('deploy-client', '--publish', expected=1))
        self.assertEqual(installer.read_bytes(), before)

    def test_dirty_catalog_is_preserved(self):
        f = self.fixture
        user_file = f.seed / 'notes.txt'
        user_file.write_text('user work')
        self.assertIn('local changes', self.manage('maintain', '--publish', expected=1))
        self.assertEqual(user_file.read_text(), 'user work')
        self.assertFalse((f.seed / '.agent-skills-client.json').exists())

    def test_unpublished_manual_commits_are_not_pushed(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        remote_head = f.git(f.seed, 'rev-parse', '@{upstream}')
        (f.seed / 'notes.txt').write_text('manual')
        f.commit(f.seed, 'Manual unpublished work')
        self.assertIn('manual commits', self.manage('maintain', '--publish', expected=1))
        self.assertEqual(f.git(f.seed, 'rev-parse', '@{upstream}'), remote_head)

    def test_failed_deployment_push_is_retried(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        script = self.templates / 'install.sh'
        script.write_text(script.read_text() + '\n# Updated\n')
        f.git(f.seed, 'config', 'remote.origin.pushurl', str(f.root / 'offline.git'))
        self.manage('deploy-client', '--publish', expected=1)
        head = f.git(f.seed, 'rev-parse', 'HEAD')
        f.git(f.seed, 'config', '--unset', 'remote.origin.pushurl')
        self.manage('deploy-client', '--publish')
        self.assertEqual(f.git(f.seed, 'rev-parse', '@{upstream}'), head)

    def test_dry_run_and_crlf_do_not_churn_deployment(self):
        f = self.fixture
        self.manage('maintain', '--dry-run')
        self.assertFalse((f.seed / '.agent-skills-client.json').exists())
        self.manage('deploy-client', '--publish')
        head = f.git(f.seed, 'rev-parse', 'HEAD')
        for script in self.templates.rglob('*.ps1'):
            script.write_bytes(script.read_bytes().replace(b'\n', b'\r\n'))
        self.manage('deploy-client', '--publish')
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), head)

    def test_server_registers_skill_and_deploys_client_together(self):
        f = self.fixture
        f.write_skill('second-skill', 'new')
        f.commit(f.upstream, 'New source')
        self.manage('register', 'second-skill', '--git-url', str(f.upstream),
                    '--path', 'skills/second-skill', '--auto-update', 'no', '--publish')
        registry = json.loads((f.seed / 'sources.json').read_text())
        self.assertFalse(registry['skills']['second-skill']['auto_update'])
        self.assertTrue((f.seed / '.agent-skills-client.json').is_file())
        f.sync()
        self.assertTrue((f.installed(name='second-skill') / 'SKILL.md').is_file())

    def test_bad_catalog_does_not_block_other_catalogs(self):
        f = self.fixture
        f.config.write_text(json.dumps({'version': 1, 'catalogs': [
            {'name': 'bad', 'path': str(f.seed), 'remote': 'wrong', 'branch': 'main'},
            {'name': 'good', 'path': str(f.consumer), 'remote': str(f.remote), 'branch': 'main'}]}))
        output = self.manage('deploy-client', '--publish', expected=1)
        self.assertIn('ERROR bad', output)
        self.assertIn('good: published', output)
        self.assertTrue((f.consumer / '.agent-skills-client.json').exists())


if __name__ == '__main__':
    unittest.main()
