"""Server ownership, deployment and publication against disposable catalogs."""
import json
import importlib.util
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

    def local_folder(self, folder=None):
        f = self.fixture
        folder = folder or f.root / 'incoming-local'
        folder.mkdir(parents=True)
        (folder / 'SKILL.md').write_text('---\nname: my-local\ndescription: Our own script\n---\nRun scripts/run.sh')
        (folder / 'scripts').mkdir()
        script = folder / 'scripts/run.sh'
        script.write_bytes(b'#!/bin/sh\necho local\n')
        script.chmod(0o755)
        (folder / 'binary.dat').write_bytes(b'\x00\xff\r\n')
        return folder

    def test_local_skill_import_publication_and_two_station_updates(self):
        f = self.fixture
        source = self.local_folder()
        self.manage('register-local', 'my-local', '--directory', str(source), '--publish')
        entry = json.loads((f.seed / 'sources.json').read_text())['skills']['my-local']
        self.assertEqual(entry['source'], dict(kind='local', path='skills/my-local'))
        self.assertFalse(entry['auto_update'])
        self.assertEqual(entry['revision'], entry['content_sha256'])
        self.assertNotIn('customization', entry)
        self.assertTrue((f.seed / 'skills/my-local/scripts/run.sh').stat().st_mode & 0o111)
        shutil.rmtree(source)
        self.assertIn('my-local: local; no external source', self.manage('maintain', '--apply-auto', '--publish'))
        f.sync()
        self.assertEqual((f.installed(name='my-local') / 'binary.dat').read_bytes(), b'\x00\xff\r\n')
        self.assertTrue(f.installed('.claude', 'my-local').is_symlink())
        peer = f.root / 'peer'
        f.git(f.root, 'clone', str(f.remote), str(peer))
        home = f.root / 'peer-home'
        home.mkdir()
        env = dict(f.env, HOME=str(home), XDG_CACHE_HOME=str(home / '.cache'))
        def sync_peer():
            result = subprocess.run(['bash', str(peer / 'install.sh'), '--sync-only'], env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        sync_peer()
        installed = f.installed(name='my-local')
        (installed / 'scripts/run.sh').write_text('echo edited locally\n')
        (installed / 'binary.dat').unlink()
        f.sync()
        sync_peer()
        self.assertEqual((home / '.agents/skills/my-local/scripts/run.sh').read_text(), 'echo edited locally\n')
        self.assertFalse((home / '.agents/skills/my-local/binary.dat').exists())
        f.git(f.seed, 'pull', '--ff-only')
        updated = json.loads((f.seed / 'sources.json').read_text())['skills']['my-local']
        self.assertNotEqual(updated['revision'], entry['revision'])
        self.assertEqual(updated['revision'], updated['content_sha256'])
        self.assertNotIn('customization', updated)
        self.manage('maintain', '--apply-auto', '--publish')
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')

    def test_local_skill_can_adopt_explicit_untracked_catalog_folder(self):
        f = self.fixture
        source = self.local_folder(f.seed / 'skills/my-local')
        self.manage('register-local', 'my-local', '--directory', str(source), '--publish')
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')
        self.assertIn('skills/my-local/scripts/run.sh', f.git(f.seed, 'ls-tree', '-r', '--name-only', 'HEAD'))

    def test_local_registration_preserves_unrelated_changes_and_foreign_folders(self):
        f = self.fixture
        source = self.local_folder()
        note = f.seed / 'note.txt'
        note.write_text('private')
        before = (f.seed / 'sources.json').read_bytes()
        self.assertIn('Unrelated catalog changes preserved', self.manage('register-local', 'my-local', '--directory', str(source), '--publish', expected=1))
        self.assertEqual(note.read_text(), 'private')
        self.assertEqual((f.seed / 'sources.json').read_bytes(), before)
        note.unlink()
        target = f.seed / 'skills/my-local'
        target.mkdir(parents=True)
        (target / 'private').write_text('keep')
        self.assertIn('Existing skill folder preserved', self.manage('register-local', 'my-local', '--directory', str(source), '--publish', expected=1))
        self.assertEqual((target / 'private').read_text(), 'keep')
        self.assertEqual((f.seed / 'sources.json').read_bytes(), before)

    def test_local_import_cannot_recursively_copy_its_destination(self):
        f = self.fixture
        source = self.local_folder(f.seed / 'skills')
        f.commit(f.seed, 'Existing parent folder')
        f.git(f.seed, 'push')
        self.assertIn('cannot contain its destination', self.manage('register-local', 'my-local', '--directory', str(source), '--publish', expected=1))
        self.assertFalse((source / 'my-local').exists())

    def test_invalid_local_skill_is_not_registered_or_copied(self):
        f = self.fixture
        source = self.local_folder()
        (source / 'scripts/unsafe').symlink_to(source / 'SKILL.md')
        before = (f.seed / 'sources.json').read_bytes()
        self.assertIn('Unsupported local skill', self.manage('register-local', 'my-local', '--directory', str(source), '--publish', expected=1))
        self.assertFalse((f.seed / 'skills/my-local').exists())
        self.assertEqual((f.seed / 'sources.json').read_bytes(), before)

    def legacy_customizations(self):
        f = self.fixture
        spec = importlib.util.spec_from_file_location('skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        data = json.loads((f.seed / 'sources.json').read_text())
        entry = data['skills']['first-skill']
        versions = []
        for text in ['First customization', 'Current customization']:
            content = f.root / 'legacy-content'
            content.mkdir()
            shutil.copyfile(f.upstream / 'skills/first-skill/SKILL.md', content / 'SKILL.md')
            with (content / 'SKILL.md').open('a') as stream:
                stream.write('\n' + text)
            (content / 'helper.sh').write_bytes(b'#!/bin/sh\necho shared\n')
            (content / 'helper.sh').chmod(0o755)
            digest = module.tree_hash(content)
            destination = f.seed / 'customizations/first-skill' / digest
            shutil.copytree(content, destination)
            shutil.rmtree(content)
            versions.append(destination)
        entry['customization'] = dict(content_sha256=versions[-1].name,
            base_sha256=entry['content_sha256'], base_revision=entry['revision'])
        (f.seed / 'sources.json').write_text(json.dumps(data))
        f.commit(f.seed, 'Legacy customizations')
        f.git(f.seed, 'push')
        return versions

    def test_legacy_versions_migrate_to_stable_path_with_git_history(self):
        f = self.fixture
        versions = self.legacy_customizations()
        previous = f.git(f.seed, 'rev-parse', 'HEAD')
        self.assertIn('migrate shared skills', self.manage('deploy-client', '--dry-run'))
        self.assertFalse((f.seed / 'skills').exists())
        self.assertTrue(versions[-1].exists())
        self.manage('deploy-client', '--publish')
        shared = f.seed / 'skills/first-skill'
        self.assertIn('Current customization', (shared / 'SKILL.md').read_text())
        self.assertTrue((shared / 'helper.sh').stat().st_mode & 0o111)
        self.assertFalse((f.seed / 'customizations').exists())
        entry = json.loads((f.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(entry['customization']['path'], 'skills/first-skill')
        self.assertIn('First customization', f.git(f.seed, 'show', previous + ':' + versions[0].relative_to(f.seed).as_posix() + '/SKILL.md'))
        current = f.git(f.seed, 'rev-parse', 'HEAD')
        self.manage('deploy-client', '--publish')
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), current)
        f.sync()
        self.assertIn('Current customization', (f.installed() / 'SKILL.md').read_text())

    def test_migration_preserves_unrecognized_existing_stable_folder(self):
        f = self.fixture
        versions = self.legacy_customizations()
        shared = f.seed / 'skills/first-skill'
        shared.mkdir(parents=True)
        (shared / 'private.txt').write_text('keep')
        f.commit(f.seed, 'Foreign shared folder')
        f.git(f.seed, 'push')
        self.assertIn('differs; preserved', self.manage('deploy-client', '--publish', expected=1))
        self.assertEqual((shared / 'private.txt').read_text(), 'keep')
        self.assertTrue(versions[-1].exists())

    def test_migration_preserves_modified_legacy_history(self):
        f = self.fixture
        versions = self.legacy_customizations()
        local = versions[0] / 'helper.sh'
        local.write_text('unrecognized edits')
        f.commit(f.seed, 'Edited old legacy version')
        f.git(f.seed, 'push')
        before = (f.seed / 'sources.json').read_bytes()
        self.assertIn('unrecognized edits; preserved', self.manage('deploy-client', '--publish', expected=1))
        self.assertEqual(local.read_text(), 'unrecognized edits')
        self.assertEqual((f.seed / 'sources.json').read_bytes(), before)
        self.assertFalse((f.seed / 'skills').exists())

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
