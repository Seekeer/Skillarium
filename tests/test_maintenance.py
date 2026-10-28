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
        folder.mkdir(parents=True, exist_ok=True)
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
        self.manage('maintain', '--apply-auto', '--publish')
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

    def test_import_local_infers_name_publishes_and_explains_client_installation(self):
        f = self.fixture
        source = self.local_folder(f.root / 'Папка с другим именем')
        (source / '.gitignore').write_text('scratch.tmp\n')
        (source / 'scratch.tmp').write_text('local scratch file')
        before = {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()}
        output = self.manage('import-local', str(source))
        self.assertIn('Проверка пройдена: my-local', output)
        self.assertIn(str(f.seed / 'skills/my-local'), output)
        self.assertIn('Опубликован коммит', output)
        self.assertIn('scratch.tmp', output)
        self.assertIn('bash install.sh --sync-only', output)
        self.assertIn('powershell -File .\\install.ps1 -Mode Sync', output)
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), f.git(f.remote, 'rev-parse', 'main'))
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')
        self.assertNotIn('scratch.tmp', f.git(f.remote, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertEqual(before, {p.relative_to(source): p.read_bytes() for p in source.rglob('*') if p.is_file()})
        f.sync()
        self.assertEqual((f.installed(name='my-local') / 'binary.dat').read_bytes(), b'\x00\xff\r\n')
        self.assertTrue((f.installed(name='my-local') / 'scripts/run.sh').stat().st_mode & 0o111)

    def test_import_local_dry_run_is_read_only_and_works_offline(self):
        f = self.fixture
        source = self.local_folder()
        registry = (f.seed / 'sources.json').read_bytes()
        head = f.git(f.seed, 'rev-parse', 'HEAD')
        index = f.git(f.seed, 'write-tree')
        f.remote.rename(f.root / 'offline.git')
        output = self.manage('import-local', str(source), '--dry-run')
        self.assertIn('без изменений', output)
        self.assertEqual((f.seed / 'sources.json').read_bytes(), registry)
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), head)
        self.assertEqual(f.git(f.seed, 'write-tree'), index)
        self.assertFalse((f.seed / 'skills/my-local').exists())
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')

    def test_import_local_validates_before_contacting_remote(self):
        f = self.fixture
        source = self.local_folder()
        f.remote.rename(f.root / 'offline.git')
        registry = (f.seed / 'sources.json').read_bytes()
        head = f.git(f.seed, 'rev-parse', 'HEAD')
        cases = [('No frontmatter', 'Missing SKILL.md frontmatter'),
                 ('---\nname: ../unsafe\ndescription: Example\n---\n', 'Invalid skill name'),
                 ('---\nname: my-local\n---\n', 'Missing description')]
        for text, error in cases:
            with self.subTest(error=error):
                (source / 'SKILL.md').write_text(text)
                output = self.manage('import-local', str(source), expected=1)
                self.assertIn(error, output)
                self.assertNotIn('Git command failed', output)
                self.assertFalse((f.seed / 'skills/my-local').exists())
                self.assertEqual((f.seed / 'sources.json').read_bytes(), registry)
                self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), head)
        (source / 'SKILL.md').unlink()
        self.assertIn('папку скилла с файлом SKILL.md', self.manage('import-local', str(source), expected=1))

    def test_import_local_adopts_folder_and_preserves_existing_registration(self):
        f = self.fixture
        source = self.local_folder(f.seed / 'skills/my-local')
        self.manage('import-local', str(source))
        registry = (f.seed / 'sources.json').read_bytes()
        original = (source / 'SKILL.md').read_bytes()
        other = self.local_folder(f.root / 'other source')
        (other / 'SKILL.md').write_text('---\nname: my-local\ndescription: Different\n---\nNew content')
        self.assertIn('already registered', self.manage('import-local', str(other), expected=1))
        self.assertEqual((source / 'SKILL.md').read_bytes(), original)
        self.assertEqual((f.seed / 'sources.json').read_bytes(), registry)

    def test_import_local_requires_catalog_selection_when_ambiguous(self):
        f = self.fixture
        source = self.local_folder()
        config = json.loads(f.config.read_text())
        config['catalogs'].append(dict(config['catalogs'][0], name='second'))
        f.config.write_text(json.dumps(config))
        self.assertIn('--catalog', self.manage('import-local', str(source), expected=1))
        self.assertFalse((f.seed / 'skills/my-local').exists())
        self.manage('--catalog', 'test', 'import-local', str(source))
        self.assertTrue((f.seed / 'skills/my-local/SKILL.md').is_file())

    def test_import_local_failed_push_keeps_import_and_explains_retry(self):
        f = self.fixture
        source = self.local_folder()
        f.git(f.seed, 'config', 'remote.origin.pushurl', str(f.root / 'offline.git'))
        output = self.manage('import-local', str(source), expected=1)
        self.assertIn('Публикация не завершена', output)
        self.assertIn('maintain --publish', output)
        self.assertNotIn('Опубликован коммит', output)
        self.assertTrue((f.seed / 'skills/my-local/SKILL.md').is_file())
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')
        self.assertNotEqual(f.git(f.seed, 'rev-parse', 'HEAD'), f.git(f.remote, 'rev-parse', 'main'))
        f.git(f.seed, 'config', '--unset', 'remote.origin.pushurl')
        self.manage('maintain', '--publish')
        self.assertEqual(f.git(f.seed, 'rev-parse', 'HEAD'), f.git(f.remote, 'rev-parse', 'main'))

    def test_import_local_can_explicitly_prepare_without_publication(self):
        f = self.fixture
        source = self.local_folder()
        remote_head = f.git(f.remote, 'rev-parse', 'main')
        output = self.manage('import-local', str(source), '--no-publish')
        self.assertIn('Публикация отключена', output)
        self.assertNotIn('Опубликован коммит', output)
        self.assertEqual(f.git(f.remote, 'rev-parse', 'main'), remote_head)
        self.assertIn('skills/my-local/SKILL.md', f.git(f.seed, 'diff', '--cached', '--name-only'))

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
        self.assertIn('version one', (f.seed / 'skills/first-skill/SKILL.md').read_text())
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
        shared.mkdir(parents=True, exist_ok=True)
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
        self.assertIn('version one', (f.seed / 'skills/first-skill/SKILL.md').read_text())

    def test_metadata_only_catalog_gets_all_pinned_folders_on_server(self):
        f = self.fixture
        shutil.rmtree(f.seed / 'skills/first-skill')
        f.commit(f.seed, 'Old catalog without payload')
        f.git(f.seed, 'push')
        f.write_skill('first-skill', 'unapproved upstream version')
        f.commit(f.upstream, 'Source moved')
        shutil.rmtree(f.home / '.cache')
        self.assertIn('populate skills/first-skill', self.manage('deploy-client', '--dry-run'))
        self.assertFalse((f.seed / 'skills/first-skill').exists())
        self.manage('deploy-client', '--publish')
        self.assertIn('version one', (f.seed / 'skills/first-skill/SKILL.md').read_text())
        self.assertIn('skills/first-skill/SKILL.md', f.git(f.seed, 'ls-tree', '-r', '--name-only', 'HEAD'))
        f.sync()
        self.assertEqual(f.installed().resolve(), f.consumer / 'skills/first-skill')

    def test_resolve_upstream_conflict_preserves_or_replaces_stable_folder(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        f.sync()
        path = f.installed() / 'SKILL.md'
        path.write_text(path.read_text() + '\nOur shared change')
        (f.installed() / 'local-only.md').write_text('ours')
        f.sync()
        f.write_skill('first-skill', 'upstream two')
        f.commit(f.upstream, 'Source update')
        self.manage('maintain', '--apply-auto', '--publish', expected=1)
        report = json.loads((f.seed / '.git/skill-updates.json').read_text())['first-skill']
        args = ['resolve', 'first-skill', '--revision', report['incoming']['revision'],
                '--shared-hash', report['shared']['content_sha256']]
        self.manage(*args, '--strategy', 'keep-shared', '--publish')
        self.assertIn('Our shared change', (f.seed / 'skills/first-skill/SKILL.md').read_text())
        self.manage('maintain', '--apply-auto', '--publish')
        f.write_skill('first-skill', 'upstream three')
        f.commit(f.upstream, 'Another source update')
        self.manage('maintain', '--apply-auto', '--publish', expected=1)
        report = json.loads((f.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.manage('resolve', 'first-skill', '--revision', report['incoming']['revision'],
            '--shared-hash', '0' * 64, '--strategy', 'take-upstream', '--publish', expected=1)
        self.assertTrue((f.seed / 'skills/first-skill/local-only.md').exists())
        self.manage('resolve', 'first-skill', '--revision', report['incoming']['revision'],
            '--shared-hash', report['shared']['content_sha256'], '--strategy', 'take-upstream', '--publish')
        self.assertIn('upstream three', (f.seed / 'skills/first-skill/SKILL.md').read_text())
        self.assertFalse((f.seed / 'skills/first-skill/local-only.md').exists())
        f.sync()
        self.assertIn('upstream three', (f.installed() / 'SKILL.md').read_text())
        self.assertNotIn('customization', json.loads((f.seed / 'sources.json').read_text())['skills']['first-skill'])

    def test_ignored_runtime_files_stay_private_during_server_updates(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        (f.seed / '.git/info/exclude').write_text('*.private\n')
        private = f.seed / 'skills/first-skill/runtime.private'
        private.write_text('local-runtime-data')
        before = (f.seed / 'sources.json').read_bytes()
        self.manage('maintain', '--apply-auto', '--publish')
        self.assertEqual((f.seed / 'sources.json').read_bytes(), before)
        f.write_skill('first-skill', 'upstream two')
        f.commit(f.upstream, 'Update')
        self.manage('maintain', '--apply-auto', '--publish')
        self.assertEqual(private.read_text(), 'local-runtime-data')
        self.assertNotIn('runtime.private', f.git(f.remote, 'ls-tree', '-r', '--name-only', 'HEAD'))
        backups = list((f.seed / '.git/skill-backups').glob('*/content/SKILL.md'))
        self.assertTrue(any('version one' in item.read_text() for item in backups))
        self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')

    def test_writes_to_open_old_file_survive_server_replacement(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        path = f.seed / 'skills/first-skill/SKILL.md'
        with path.open('a') as stream:
            f.write_skill('first-skill', 'upstream two')
            f.commit(f.upstream, 'Update')
            self.manage('maintain', '--apply-auto', '--publish')
            stream.write('\nLATE WRITE TO OPEN FILE')
            stream.flush()
        self.assertIn('upstream two', path.read_text())
        backups = list((f.seed / '.git/skill-backups').glob('*/content/SKILL.md'))
        self.assertTrue(any('LATE WRITE TO OPEN FILE' in item.read_text() for item in backups))

    def test_deleted_upstream_can_be_detached_without_network(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        path = f.seed / 'skills/first-skill/SKILL.md'
        path.write_text(path.read_text() + '\nOur customization')
        f.commit(f.seed, 'Customization')
        f.git(f.seed, 'push')
        shutil.rmtree(f.upstream / 'skills/first-skill')
        f.commit(f.upstream, 'Delete skill')
        self.manage('maintain', '--apply-auto', '--publish', expected=1)
        report = json.loads((f.seed / '.git/skill-updates.json').read_text())['first-skill']
        f.upstream.rename(f.root / 'upstream-offline')
        self.manage('resolve', 'first-skill', '--shared-hash', '0' * 64,
                    '--strategy', 'detach', '--publish', expected=1)
        self.manage('resolve', 'first-skill', '--shared-hash', report['shared']['content_sha256'],
                    '--strategy', 'detach', '--publish')
        self.manage('maintain', '--apply-auto', '--publish')
        entry = json.loads((f.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(entry['source'], dict(kind='local', path='skills/first-skill'))
        self.assertFalse(entry['auto_update'])
        self.assertNotIn('customization', entry)
        f.sync()
        self.assertIn('Our customization', (f.installed() / 'SKILL.md').read_text())

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

    def test_headless_launcher_is_added_to_old_manifest_and_edits_are_preserved(self):
        f = self.fixture
        self.manage('deploy-client', '--publish')
        relative = 'scripts/sync-hidden.vbs'
        launcher = f.seed / relative
        manifest_path = f.seed / '.agent-skills-client.json'
        manifest = json.loads(manifest_path.read_text())
        del manifest['files'][relative]
        manifest_path.write_text(json.dumps(manifest))
        launcher.unlink()
        f.commit(f.seed, 'Simulate previous client manifest')
        f.git(f.seed, 'push')
        self.manage('deploy-client', '--publish')
        self.assertEqual(launcher.read_bytes(), (self.templates / relative).read_bytes())
        self.assertIn(relative, json.loads(manifest_path.read_text())['files'])
        launcher.write_text("' User launcher customization\n")
        f.commit(f.seed, 'Customize launcher')
        f.git(f.seed, 'push')
        self.assertIn('unrecognized edits', self.manage('deploy-client', '--publish', expected=1))
        self.assertEqual(launcher.read_text(), "' User launcher customization\n")

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
