"""Integration tests: upstream -> controller registry -> client cache/links."""
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='agent skills ')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'user home'
        self.home.mkdir()
        self.env = dict(os.environ, HOME=str(self.home), XDG_CACHE_HOME=str(self.home / '.cache'),
                        GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1',
                        GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
                        GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        self.upstream = self.root / 'upstream'
        self.upstream.mkdir()
        self.git(self.upstream, 'init', '-b', 'main')
        self.write_skill('first-skill', 'version one')
        self.commit(self.upstream, 'Initial source')
        self.remote = self.root / 'registry.git'
        self.git(self.root, 'init', '--bare', '--initial-branch=main', str(self.remote))
        self.seed = self.root / 'controller'
        self.seed.mkdir()
        self.git(self.seed, 'init', '-b', 'main')
        (self.seed / 'scripts').mkdir()
        shutil.copyfile(ROOT / 'client/install.sh', self.seed / 'install.sh')
        shutil.copyfile(ROOT / 'client/.gitattributes', self.seed / '.gitattributes')
        shutil.copyfile(ROOT / 'client/scripts/client.sh', self.seed / 'scripts/client.sh')
        (self.seed / 'sources.json').write_text('{"version":2,"skills":{}}')
        self.register('first-skill', auto='yes')
        self.commit(self.seed, 'Initial registry')
        self.git(self.seed, 'remote', 'add', 'origin', str(self.remote))
        self.git(self.seed, 'push', '-u', 'origin', 'main')
        self.consumer = self.root / 'client with spaces'
        self.git(self.root, 'clone', str(self.remote), str(self.consumer))
        self.config = self.root / 'catalogs.json'
        self.config.write_text(json.dumps({'version': 1, 'catalogs': [{'name': 'test', 'path': str(self.seed), 'remote': str(self.remote), 'branch': 'main'}]}))

    def git(self, cwd, *args):
        return subprocess.run(['git', '-C', str(cwd), *args], env=self.env,
                              text=True, capture_output=True, check=True).stdout.strip()

    def commit(self, repo, message):
        self.git(repo, 'add', '.')
        self.git(repo, 'commit', '-m', message)

    def write_skill(self, name, content):
        folder = self.upstream / 'skills' / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: Test skill\n---\n{content}')
        return folder

    def run_cli(self, repo, *args, expected=0):
        result = subprocess.run(['python3', str(ROOT / 'server/skills.py'), '--catalog', str(repo), *args],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def register(self, name, auto):
        return self.run_cli(self.seed, 'register', name, '--git-url', str(self.upstream),
                            '--path', 'skills/' + name, '--auto-update', auto)

    def sync(self, expected=0, mode='--sync-only'):
        result = subprocess.run(['bash', str(self.consumer / 'install.sh'), mode],
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def installed(self, catalog='.agents', name='first-skill'):
        return self.home / catalog / 'skills' / name

    def test_clients_only_follow_approved_versions_and_install_new_entries(self):
        self.assertTrue((self.seed / 'skills/first-skill/SKILL.md').is_file())
        self.sync()
        self.assertEqual(self.installed().resolve(), self.consumer / 'skills/first-skill')
        self.write_skill('first-skill', 'version two')
        self.write_skill('new-skill', 'new')
        self.commit(self.upstream, 'New sources')
        self.sync()
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.register('new-skill', 'no')
        self.commit(self.seed, 'Register new source')
        self.git(self.seed, 'push')
        self.sync()
        self.assertIn('version two', (self.installed() / 'SKILL.md').read_text())
        self.assertTrue(self.installed('.claude', 'new-skill').is_symlink())
        self.assertFalse((self.home / '.cache/agent-skills/versions').exists())

    def test_client_uses_only_catalog_even_with_sources_offline_and_tools_missing(self):
        self.upstream.rename(self.root / 'source-offline')
        shutil.rmtree(self.home / '.cache')
        shims = self.root / 'disabled-tools'
        shims.mkdir()
        for name in ['curl', 'tar', 'python', 'python3']:
            tool = shims / name
            tool.write_text('#!/bin/sh\necho External tool invoked >&2\nexit 98\n')
            tool.chmod(0o755)
        self.env['PATH'] = str(shims) + os.pathsep + self.env['PATH']
        self.sync()
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())
        self.assertEqual(self.installed().resolve(), self.consumer / 'skills/first-skill')
        self.assertFalse((self.home / '.cache').exists())

    def test_manual_updates_only_change_payload_after_approval(self):
        data = json.loads((self.seed / 'sources.json').read_text())
        data['skills']['first-skill']['auto_update'] = False
        (self.seed / 'sources.json').write_text(json.dumps(data))
        before = (self.seed / 'skills/first-skill/SKILL.md').read_bytes()
        self.write_skill('first-skill', 'manual version')
        self.commit(self.upstream, 'Update source')
        self.assertIn('available', self.run_cli(self.seed, 'check', '--apply-auto'))
        self.assertEqual((self.seed / 'skills/first-skill/SKILL.md').read_bytes(), before)
        revision = self.git(self.upstream, 'rev-parse', 'HEAD')
        self.run_cli(self.seed, 'check', '--approve', 'first-skill', '--expected-revision', revision)
        self.assertIn('manual version', (self.seed / 'skills/first-skill/SKILL.md').read_text())

    def test_local_edits_are_git_commits_and_server_detects_upstream_conflict(self):
        self.sync()
        before_registry = (self.consumer / 'sources.json').read_bytes()
        skill = self.installed() / 'SKILL.md'
        skill.write_text(skill.read_text() + '\nOur change')
        (self.installed() / 'helper.ps1').write_bytes(b'Write-Output ours\r\n')
        self.sync()
        self.assertEqual((self.consumer / 'sources.json').read_bytes(), before_registry)
        self.assertEqual(self.git(self.consumer, 'status', '--porcelain'), '')
        self.git(self.seed, 'pull', '--ff-only')
        self.assertIn('Our change', (self.seed / 'skills/first-skill/SKILL.md').read_text())
        self.write_skill('first-skill', 'upstream changed')
        self.commit(self.upstream, 'New upstream')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish', expected=1)
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.assertEqual(report['status'], 'upstream-conflict')
        self.assertIn('upstream changed', (Path(report['incoming']['path']) / 'SKILL.md').read_text())
        self.assertIn('Our change', (self.seed / 'skills/first-skill/SKILL.md').read_text())
        entry = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertIn('customization', entry)
        self.assertEqual(entry['customization']['base_sha256'], entry['content_sha256'])

    def test_read_only_check_reports_unrecorded_edits_without_changing_files(self):
        path = self.seed / 'skills/first-skill/SKILL.md'
        path.write_text(path.read_text() + '\nLocal Git customization')
        self.commit(self.seed, 'Our committed skill edit')
        before = (self.seed / 'sources.json').read_bytes()
        self.assertIn('customized', self.run_cli(self.seed, 'check'))
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)
        self.assertIn('Local Git customization', path.read_text())

    def test_upstream_deletion_preserves_customized_files(self):
        source = self.upstream / 'skills/first-skill/reference.md'
        source.write_text('base')
        self.commit(self.upstream, 'Reference')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.sync()
        (self.installed() / 'reference.md').write_text('ours')
        self.sync()
        self.git(self.seed, 'pull', '--ff-only')
        source.unlink()
        self.commit(self.upstream, 'Delete reference')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertEqual((self.seed / 'skills/first-skill/reference.md').read_text(), 'ours')
        shutil.rmtree(self.upstream / 'skills/first-skill')
        self.commit(self.upstream, 'Delete entire skill')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.assertEqual(report['status'], 'upstream-conflict')
        self.assertIn('upstream_error', report)
        self.assertEqual((self.seed / 'skills/first-skill/reference.md').read_text(), 'ours')

    def test_clean_server_upgrade_removes_obsolete_files(self):
        source = self.upstream / 'skills/first-skill/reference.md'
        source.write_text('base')
        self.commit(self.upstream, 'Reference')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        source.unlink()
        self.commit(self.upstream, 'Remove reference')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.assertFalse((self.seed / 'skills/first-skill/reference.md').exists())
        self.sync()
        self.assertFalse((self.installed() / 'reference.md').exists())

    def test_ignored_files_are_not_published_with_visible_edits(self):
        self.sync()
        (self.consumer / '.git/info/exclude').write_text('*.private\n')
        private = self.installed() / 'credentials.private'
        private.write_text('dummy-local-private-data')
        skill = self.installed() / 'SKILL.md'
        skill.write_text(skill.read_text() + '\nOur normal edit')
        self.sync()
        tracked = self.git(self.remote, 'ls-tree', '-r', '--name-only', 'HEAD')
        self.assertNotIn('credentials.private', tracked)
        self.assertEqual(private.read_text(), 'dummy-local-private-data')
        self.assertEqual(self.git(self.consumer, 'status', '--porcelain'), '')

    def test_independent_station_changes_merge_without_rewriting_history(self):
        self.sync()
        local = self.installed() / 'local.sh'
        local.write_text('echo local\n')
        remote = self.seed / 'skills/first-skill/remote.sh'
        remote.write_text('echo remote\n')
        self.commit(self.seed, 'Other station independent edit')
        remote_revision = self.git(self.seed, 'rev-parse', 'HEAD')
        self.git(self.seed, 'push')
        self.sync()
        self.assertEqual((self.installed() / 'remote.sh').read_text(), 'echo remote\n')
        self.assertEqual(local.read_text(), 'echo local\n')
        self.git(self.consumer, 'merge-base', '--is-ancestor', remote_revision, 'HEAD')
        self.assertEqual(len(self.git(self.consumer, 'show', '-s', '--format=%P', 'HEAD').split()), 2)
        self.assertIn('local.sh', self.git(self.remote, 'ls-tree', '-r', '--name-only', 'HEAD'))
        self.sync()
        self.assertEqual(self.git(self.consumer, 'status', '--porcelain'), '')
        self.assertEqual(len(self.git(self.consumer, 'worktree', 'list').splitlines()), 1)

    def test_upstream_executable_only_update_is_published(self):
        script = self.upstream / 'skills/first-skill/run.sh'
        script.write_text('#!/bin/sh\necho works\n')
        script.chmod(0o644)
        self.commit(self.upstream, 'Add script')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        previous = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        # Old catalogs have no executable hash; derive the baseline from the pin.
        registry = json.loads((self.seed / 'sources.json').read_text())
        registry['skills']['first-skill'].pop('executable_sha256')
        (self.seed / 'sources.json').write_text(json.dumps(registry))
        self.commit(self.seed, 'Legacy metadata without mode hash')
        self.git(self.seed, 'push')
        script.chmod(0o755)
        self.commit(self.upstream, 'Make script executable')
        self.assertIn('updated', self.run_cli(self.seed, 'check', '--apply-auto', '--publish'))
        current = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(current['content_sha256'], previous['content_sha256'])
        self.assertNotEqual(current['executable_sha256'], previous['executable_sha256'])
        self.assertEqual(current['revision'], self.git(self.upstream, 'rev-parse', 'HEAD'))
        self.sync()
        self.assertTrue((self.installed() / 'run.sh').stat().st_mode & 0o111)

    def test_upstream_tracked_files_are_imported_even_when_source_ignores_them(self):
        folder = self.upstream / 'skills/first-skill'
        (folder / '.gitignore').write_text('generated.txt\n*.private\n')
        (folder / 'generated.txt').write_text('tracked upstream payload')
        self.git(self.upstream, 'add', '-f', '--', 'skills/first-skill/generated.txt')
        self.commit(self.upstream, 'Tracked file with matching ignore rule')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.assertIn('generated.txt', self.git(self.remote, 'ls-tree', '-r', '--name-only', 'HEAD'))
        self.sync()
        self.assertEqual((self.installed() / 'generated.txt').read_text(), 'tracked upstream payload')
        private = self.installed() / 'runtime.private'
        private.write_text('keep local')
        path = self.installed() / 'SKILL.md'
        path.write_text(path.read_text() + '\nVisible edit')
        self.sync()
        self.assertNotIn('runtime.private', self.git(self.remote, 'ls-tree', '-r', '--name-only', 'HEAD'))

    def test_mode_only_local_edit_blocks_upstream_changes(self):
        script = self.upstream / 'skills/first-skill/run.sh'
        script.write_text('echo base\n')
        script.chmod(0o644)
        self.commit(self.upstream, 'Script')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.sync()
        (self.installed() / 'run.sh').chmod(0o755)
        self.sync()
        self.git(self.seed, 'pull', '--ff-only')
        self.write_skill('first-skill', 'new upstream')
        self.commit(self.upstream, 'Upstream update')
        self.assertIn('upstream-conflict', self.run_cli(self.seed, 'check', '--apply-auto', expected=1))
        self.assertTrue((self.seed / 'skills/first-skill/run.sh').stat().st_mode & 0o111)

    def test_edit_during_server_fetch_is_preserved(self):
        spec = importlib.util.spec_from_file_location('review_skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_catalog(self.seed)
        self.write_skill('first-skill', 'upstream two')
        self.commit(self.upstream, 'Update upstream')
        path = self.seed / 'skills/first-skill/SKILL.md'
        original = module.materialize
        def fetch_and_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('approved') is False:
                path.write_text(path.read_text() + '\nEDIT DURING FETCH')
            return result
        from types import SimpleNamespace
        with patch.dict(os.environ, self.env), patch.object(module, 'materialize', side_effect=fetch_and_edit):
            result = module.check(SimpleNamespace(approve=[], expected_revision=None,
                apply_auto=True, apply_catalog=True, publish=False))
        self.assertEqual(result, 1)
        self.assertIn('EDIT DURING FETCH', path.read_text())
        self.assertIn('version one', path.read_text())
        self.assertNotIn('upstream two', path.read_text())

    def test_edit_during_legacy_mode_baseline_fetch_is_preserved(self):
        registry = json.loads((self.seed / 'sources.json').read_text())
        registry['skills']['first-skill'].pop('executable_sha256')
        (self.seed / 'sources.json').write_text(json.dumps(registry))
        self.commit(self.seed, 'Legacy registry')
        spec = importlib.util.spec_from_file_location('legacy_skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.set_catalog(self.seed)
        self.write_skill('first-skill', 'upstream two')
        self.commit(self.upstream, 'Update upstream')
        path = self.seed / 'skills/first-skill/SKILL.md'
        original = module.materialize
        def fetch_and_edit(*args, **kwargs):
            result = original(*args, **kwargs)
            if kwargs.get('approved', True):
                path.write_text(path.read_text() + '\nEDIT DURING BASELINE FETCH')
            return result
        from types import SimpleNamespace
        with patch.dict(os.environ, self.env), patch.object(module, 'materialize', side_effect=fetch_and_edit):
            result = module.check(SimpleNamespace(approve=[], expected_revision=None,
                apply_auto=True, apply_catalog=True, publish=False))
        self.assertEqual(result, 1)
        self.assertIn('EDIT DURING BASELINE FETCH', path.read_text())
        self.assertIn('version one', path.read_text())

    def test_client_preserves_unrelated_and_staged_changes(self):
        self.sync()
        private = self.consumer / 'private.txt'
        private.write_text('private')
        self.assertIn('Unrelated local changes', self.sync(expected=1))
        self.assertEqual(private.read_text(), 'private')
        private.unlink()
        script = self.installed() / 'SKILL.md'
        script.write_text(script.read_text() + '\nManual staged edit')
        self.git(self.consumer, 'add', 'skills')
        self.assertIn('Staged changes preserved', self.sync(expected=1))
        self.assertIn('Manual staged edit', script.read_text())

    def test_client_does_not_publish_unregistered_folders(self):
        folder = self.consumer / 'skills/private'
        folder.mkdir()
        (folder / 'SKILL.md').write_text('private')
        self.sync(expected=1)
        self.assertEqual((folder / 'SKILL.md').read_text(), 'private')
        self.assertNotIn('skills/private', self.git(self.seed, 'ls-tree', '-r', '--name-only', 'HEAD'))

    def test_missing_skill_never_triggers_client_source_download(self):
        shutil.rmtree(self.consumer / 'skills/first-skill')
        self.sync(expected=1, mode='--link-only')
        self.assertFalse(self.installed().exists())
        self.assertFalse((self.home / '.cache/agent-skills/versions').exists())

    def test_foreign_installations_and_dangling_links_are_preserved(self):
        foreign = self.installed()
        foreign.mkdir(parents=True)
        (foreign / 'private').write_text('keep')
        self.sync(expected=1)
        self.assertEqual((foreign / 'private').read_text(), 'keep')
        other = self.installed('.claude')
        other.unlink()
        other.symlink_to(self.root / 'missing')
        self.sync(expected=1)
        self.assertEqual(other.readlink(), self.root / 'missing')

    def legacy_link(self, edited=False):
        entry = json.loads((self.consumer / 'sources.json').read_text())['skills']['first-skill']
        cache = self.home / '.cache/agent-skills/versions/first-skill' / entry['content_sha256']
        shutil.copytree(self.consumer / 'skills/first-skill', cache)
        if edited:
            path = cache / 'SKILL.md'
            path.write_text(path.read_text() + '\nLegacy local edit')
        for catalog in ['.agents', '.claude']:
            link = self.installed(catalog)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(cache)
        return cache

    def test_legacy_cache_links_move_to_catalog_without_losing_edits(self):
        old = self.legacy_link(edited=True)
        self.sync()
        self.assertEqual(self.installed().resolve(), self.consumer / 'skills/first-skill')
        self.assertIn('Legacy local edit', (self.installed() / 'SKILL.md').read_text())
        self.assertIn('Legacy local edit', (old / 'SKILL.md').read_text())
        self.git(self.seed, 'pull', '--ff-only')
        self.assertIn('Legacy local edit', (self.seed / 'skills/first-skill/SKILL.md').read_text())

    def test_legacy_cache_conflict_with_new_catalog_version_is_preserved(self):
        old = self.legacy_link(edited=True)
        self.write_skill('first-skill', 'new server version')
        self.commit(self.upstream, 'Update')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        # Fetch catalog first, as on a station upgrading its old client.
        self.git(self.consumer, 'pull', '--ff-only')
        self.sync(expected=1)
        self.assertEqual(self.installed().resolve(), old)
        self.assertIn('Legacy local edit', (old / 'SKILL.md').read_text())
        self.assertIn('new server version', (self.consumer / 'skills/first-skill/SKILL.md').read_text())

    def test_failed_push_is_retried_and_does_not_lose_local_changes(self):
        self.sync()
        path = self.installed() / 'SKILL.md'
        path.write_text(path.read_text() + '\nPending edit')
        self.git(self.consumer, 'config', 'remote.origin.pushurl', str(self.root / 'offline.git'))
        self.sync(expected=1)
        pending = self.git(self.consumer, 'rev-parse', 'HEAD')
        self.assertEqual(self.git(self.consumer, 'status', '--porcelain'), '')
        self.git(self.consumer, 'config', '--unset', 'remote.origin.pushurl')
        self.sync()
        self.assertEqual(self.git(self.consumer, 'rev-parse', '@{upstream}'), pending)
        self.assertIn('Pending edit', path.read_text())

    def test_concurrent_git_edits_require_manual_resolution(self):
        self.sync()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nThis station')
        remote = self.seed / 'skills/first-skill/SKILL.md'
        remote.write_text(remote.read_text() + '\nOther station')
        self.commit(self.seed, 'Other station edit')
        self.git(self.seed, 'push')
        self.sync(expected=1)
        self.assertIn('This station', local.read_text())
        self.assertIn('Other station', remote.read_text())
        self.assertNotEqual(self.git(self.consumer, 'rev-parse', 'HEAD'), self.git(self.seed, 'rev-parse', 'HEAD'))
        self.assertNotIn('<<<<<<<', local.read_text())
        self.assertFalse((self.consumer / '.git/MERGE_HEAD').exists())
        self.assertEqual(len(self.git(self.consumer, 'worktree', 'list').splitlines()), 1)
        self.sync(expected=1)
        self.assertNotIn('<<<<<<<', local.read_text())

    def test_unpublished_manual_commits_are_never_sent_automatically(self):
        (self.consumer / 'private.txt').write_text('private')
        self.commit(self.consumer, 'Manual commit')
        before = self.git(self.seed, 'rev-parse', 'HEAD')
        self.assertIn('Unpublished manual commits', self.sync(expected=1))
        self.assertEqual(self.git(self.seed, 'rev-parse', 'HEAD'), before)

    def test_offline_catalog_keeps_working_local_links(self):
        self.sync()
        self.remote.rename(self.root / 'offline.git')
        self.sync(expected=1)
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())

    def test_lock_prevents_parallel_operations(self):
        with (self.consumer / '.git/agent-skills-sync.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Another sync', self.sync())
            self.assertFalse(self.installed().exists())

    def test_source_revision_cannot_change_after_review(self):
        old = self.git(self.upstream, 'rev-parse', 'HEAD')
        before = (self.seed / 'sources.json').read_bytes()
        payload = (self.seed / 'skills/first-skill/SKILL.md').read_bytes()
        self.write_skill('first-skill', 'unreviewed')
        self.commit(self.upstream, 'Moved source')
        self.run_cli(self.seed, 'check', '--approve', 'first-skill', '--expected-revision', old, expected=1)
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)
        self.assertEqual((self.seed / 'skills/first-skill/SKILL.md').read_bytes(), payload)

    def test_invalid_external_content_and_symlinks_do_not_replace_catalog(self):
        before = (self.seed / 'skills/first-skill/SKILL.md').read_bytes()
        (self.upstream / 'skills/first-skill/SKILL.md').write_text('invalid')
        self.commit(self.upstream, 'Invalid content')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertEqual((self.seed / 'skills/first-skill/SKILL.md').read_bytes(), before)
        self.write_skill('first-skill', 'valid')
        (self.upstream / 'skills/first-skill/unsafe').symlink_to('/etc/passwd')
        self.commit(self.upstream, 'Invalid symlink')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertEqual((self.seed / 'skills/first-skill/SKILL.md').read_bytes(), before)

    def test_https_file_materialization_is_server_only_and_hash_checked(self):
        spec = importlib.util.spec_from_file_location('skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = dict(source=dict(kind='url', url='https://example.invalid/SKILL.md'), auto_update=False)
        original = b'---\nname: file-skill\ndescription: Test\n---\nFile'
        with patch.dict(os.environ, self.env), patch.object(module, 'download', return_value=original):
            folder, revision, digest = module.materialize('file-skill', entry, approved=False)
            entry.update(revision=revision, content_sha256=digest)
            shutil.rmtree(folder)
            with patch.object(module, 'download', return_value=original + b'changed'):
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    module.materialize('file-skill', entry)

    @unittest.skipUnless(os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'), 'PowerShell unavailable')
    def test_powershell_git_sync_links_and_legacy_migration(self):
        old = self.legacy_link(edited=True)
        (self.seed / 'skills/first-skill/remote.sh').write_text('echo remote\n')
        self.commit(self.seed, 'Independent remote edit before PowerShell sync')
        self.git(self.seed, 'push')
        (self.consumer / '.git/info/exclude').write_text('*.private\n')
        script = self.root / 'powershell-test.ps1'
        script.write_text('''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$repo = $env:TEST_REPO
function Invoke-Git {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $output = & git -C $Directory @GitArgs
    if ($LASTEXITCODE -ne 0) { throw 'Git operation failed.' }
    return $output
}
foreach ($file in @('install.ps1', 'scripts/client.ps1')) {
    $errors = $null; $tokens = $null
    $null = [Management.Automation.Language.Parser]::ParseFile((Join-Path $env:TEST_CLIENT $file), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors -join "`n") }
}
. (Join-Path $env:TEST_CLIENT 'scripts/client.ps1')
$cache = Join-Path $HOME '.cache/agent-skills'
function New-SkillLink {
    param([string]$Path, [string]$Target)
    New-Item -ItemType SymbolicLink -Path $Path -Target $Target | Out-Null
}
# Fail if a client invokes any external-source tooling.
function curl.exe { throw 'Client must not download sources.' }
function tar.exe { throw 'Client must not extract sources.' }
Sync-Repository
Install-Registry
$skill = Join-Path $HOME '.agents/skills/first-skill'
$path = Join-Path $skill 'SKILL.md'
if ((Get-Content -LiteralPath $path -Raw) -notmatch 'Legacy local edit') { throw 'Lost legacy edit.' }
if (-not (Test-Path -LiteralPath (Join-Path $skill 'remote.sh'))) { throw 'Independent remote edit missing.' }
if ((Invoke-Git -GitArgs @('ls-tree', '-r', '--name-only', 'HEAD')) -match 'runtime.private') { throw 'Private file was published.' }
[IO.File]::WriteAllText((Join-Path $skill 'runtime.private'), 'private-runtime')
[IO.File]::AppendAllText($path, "`nPowerShell edit", (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllBytes((Join-Path $skill 'helper.ps1'), [Text.Encoding]::UTF8.GetBytes("Write-Output ours`r`n"))
Sync-Repository
Install-Registry
if ((Invoke-Git -GitArgs @('status', '--porcelain'))) { throw 'Git tree not clean.' }
if ((Invoke-Git -GitArgs @('ls-tree', '-r', '--name-only', 'HEAD')) -match 'runtime.private') { throw 'Private file was published.' }
if ([IO.File]::ReadAllText((Join-Path $skill 'runtime.private')) -ne 'private-runtime') { throw 'Private file was lost.' }
if ([IO.File]::ReadAllText((Join-Path $repo 'skills/first-skill/helper.ps1')) -cne "Write-Output ours`r`n") { throw 'CRLF payload changed.' }
$target = (Get-Item -LiteralPath $skill).Target
if ($target -ne (Join-Path $repo 'skills/first-skill')) { throw 'Link does not target catalog.' }
# Genuine conflicts stay out of installed files and leave no merge in progress.
Invoke-Git -Directory $env:TEST_PEER -GitArgs @('pull', '--ff-only') | Out-Null
$peerPath = Join-Path $env:TEST_PEER 'skills/first-skill/SKILL.md'
[IO.File]::AppendAllText($peerPath, "`nOther station conflict")
Invoke-Git -Directory $env:TEST_PEER -GitArgs @('add', '--', 'skills') | Out-Null
Invoke-Git -Directory $env:TEST_PEER -GitArgs @('commit', '-m', 'Other station conflict') | Out-Null
Invoke-Git -Directory $env:TEST_PEER -GitArgs @('push') | Out-Null
[IO.File]::AppendAllText($path, "`nThis station conflict")
$conflicted = $false
try { Sync-Repository } catch { $conflicted = $true }
if (-not $conflicted) { throw 'Expected a real conflict.' }
if ([IO.File]::ReadAllText($path) -match '<<<<<<<') { throw 'Conflict markers reached installed skill.' }
if (Test-Path -LiteralPath (Join-Path $repo '.git/MERGE_HEAD')) { throw 'Merge left in progress.' }
if (@(Invoke-Git -GitArgs @('worktree', 'list')).Count -ne 1) { throw 'Merge worktree leaked.' }
Write-Output 'VERIFIED'
''')
        result = subprocess.run([os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'), '-NoProfile', '-NonInteractive', '-File', str(script)],
            env=dict(self.env, TEST_REPO=str(self.consumer), TEST_CLIENT=str(ROOT / 'client'), TEST_PEER=str(self.seed)), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('VERIFIED', result.stdout)
        self.assertIn('Legacy local edit', (old / 'SKILL.md').read_text())
        self.git(self.seed, 'pull', '--ff-only')
        self.assertIn('PowerShell edit', (self.seed / 'skills/first-skill/SKILL.md').read_text())
        self.assertNotIn('customization', json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill'])


if __name__ == '__main__':
    unittest.main()
