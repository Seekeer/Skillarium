"""Native Linux client through install.sh: publication via pull requests.

Requires Bash, jq, flock and curl (Linux); skipped elsewhere.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import unittest

from fixture import HAS_BASH_CLIENT, Library


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux client tools (bash, jq, flock, curl) unavailable')
class LinuxClientTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.client = self.lib.clone('client with spaces', 'laptop')

    def peer(self, name='peer', machine='desk'):
        repo = self.lib.clone(name, machine)
        home = self.lib.root / (name + '-home')
        home.mkdir()
        return repo, home

    def edit(self, text, name='first-skill', home=None):
        path = self.lib.installed(name, home=home) / 'SKILL.md'
        path.write_text(path.read_text() + '\n' + text, newline='\n')
        return path

    def test_edits_publish_through_merged_pull_request_and_reach_second_station(self):
        lib = self.lib
        self.lib.sync(self.client)
        self.assertEqual(lib.installed().resolve(), self.client / 'skills/first-skill')
        self.assertTrue(lib.installed(catalog='.claude').is_symlink())
        self.edit('Laptop edit')
        (lib.installed() / 'helper.ps1').write_bytes(b'Write-Output ours\r\n')
        output = lib.sync(self.client)
        self.assertIn('PUBLISHED: pull request', output)
        self.assertIn('Laptop edit', lib.show('skills/first-skill/SKILL.md'))
        pull = [p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/sync/laptop-')][0]
        self.assertTrue(pull['merged'])
        self.assertEqual(lib.git(lib.remote, 'log', '-1', '--format=%an', 'main^2'), 'agent-skills/laptop')
        # Service identity is per command: no user.name was written to any Git config.
        self.assertEqual((self.lib.root / 'global.gitconfig').read_text(), '')
        peer, home = self.peer()
        lib.sync(peer, home=home)
        self.assertIn('Laptop edit', (lib.installed(home=home) / 'SKILL.md').read_text())
        self.assertEqual((lib.installed(home=home) / 'helper.ps1').read_bytes(), b'Write-Output ours\r\n')
        lib.sync(self.client)
        self.assertEqual(lib.git(self.client, 'rev-list', '--count', '@{upstream}..HEAD'), '0')
        self.assertIn('Publication: ', lib.sync(self.client, '--status'))

    def test_new_valid_own_skill_is_published_and_linked_before_registration(self):
        lib = self.lib
        lib.sync(self.client)
        folder = lib.write_skill('my-local', 'own skill', root=self.client / 'skills')
        script = folder / 'run.sh'
        script.write_text('#!/bin/sh\necho ok\n')
        script.chmod(0o755)
        lib.sync(self.client)
        self.assertIn('my-local/run.sh', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertTrue(lib.installed('my-local').is_symlink())
        peer, home = self.peer()
        lib.sync(peer, home=home)
        self.assertTrue(os.access(lib.installed('my-local', home=home) / 'run.sh', os.X_OK))
        lib.manage('maintain', '--apply-auto')
        self.assertEqual(lib.registry()['skills']['my-local']['source']['kind'], 'local')

    def test_invalid_skill_and_windows_paths_are_refused_with_files_kept(self):
        lib = self.lib
        lib.sync(self.client)
        head = lib.main()
        invalid = self.client / 'skills/private'
        invalid.mkdir()
        (invalid / 'SKILL.md').write_text('private')
        self.assertIn('frontmatter', lib.sync(self.client, expected=1))
        self.assertEqual((invalid / 'SKILL.md').read_text(), 'private')
        shutil.rmtree(invalid)
        for names in [['AUX.txt'], ['Helper.py', 'helper.py'], ['Справка/a', 'справка/b']]:
            with self.subTest(names=names):
                for name in names:
                    path = lib.installed() / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('local')
                self.assertIn('Windows', lib.sync(self.client, expected=1))
                self.assertEqual(lib.git(self.client, 'diff', '--cached', '--name-only'), '')
                for name in names:
                    (lib.installed() / name).unlink()
        self.assertEqual(lib.main(), head)

    def test_limits_of_automatic_collection(self):
        lib = self.lib
        lib.sync(self.client)
        head = lib.main()
        (self.client / '.git/info/exclude').write_text('*.private\n')
        private = lib.installed() / 'secret.private'
        private.write_text('local only')
        self.edit('Visible edit')
        lib.sync(self.client)
        self.assertNotIn('secret.private', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertEqual(private.read_text(), 'local only')
        head = lib.main()
        code = self.client / 'scripts/client.sh'
        code.write_text(code.read_text() + '\n# local developer change\n')
        self.assertIn('Unrelated local changes preserved: scripts/client.sh', lib.sync(self.client, expected=1))
        lib.git(self.client, 'checkout', '--', 'scripts/client.sh')
        self.edit('Staged edit')
        lib.git(self.client, 'add', 'skills')
        self.assertIn('Staged changes preserved', lib.sync(self.client, expected=1))
        lib.git(self.client, 'reset', '-q')
        lib.git(self.client, 'checkout', '--', 'skills')
        (self.client / 'notes.txt').write_text('manual')
        lib.commit(self.client, 'Manual commit')
        self.assertIn('Unpublished manual commits', lib.sync(self.client, expected=1))
        self.assertEqual(lib.main(), head)
        self.assertFalse(any(p['head'].startswith('agent-skills/sync/laptop') and p['state'] == 'open'
                             for p in lib.host.pulls.values()))

    def test_whole_skill_deletion_needs_management_change(self):
        lib = self.lib
        lib.sync(self.client)
        shutil.rmtree(self.client / 'skills/first-skill')
        self.assertIn('explicit management', lib.sync(self.client, expected=1))
        self.assertIn('first-skill/SKILL.md', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))

    def test_parallel_stations_merge_compatible_edits_and_keep_conflicts_out(self):
        lib = self.lib
        peer, home = self.peer()
        lib.sync(self.client)
        lib.sync(peer, home=home)
        (lib.installed() / 'local.md').write_text('laptop')
        (lib.installed(home=home) / 'remote.md').write_text('desk')
        lib.sync(peer, home=home)
        lib.sync(self.client)
        files = lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main')
        self.assertIn('local.md', files)
        self.assertIn('remote.md', files)
        lib.sync(peer, home=home)
        self.assertTrue((lib.installed(home=home) / 'local.md').exists())
        self.edit('Laptop line')
        self.edit('Desk line', home=home)
        lib.sync(peer, home=home)
        self.assertIn('CONFLICT', lib.sync(self.client, expected=1))
        text = (lib.installed() / 'SKILL.md').read_text()
        self.assertIn('Laptop line', text)
        self.assertNotIn('<<<<<<<', text)
        self.assertFalse((self.client / '.git/MERGE_HEAD').exists())
        self.assertEqual(len(lib.git(self.client, 'worktree', 'list').splitlines()), 1)

    def test_stale_proposal_is_checked_again_after_main_changes(self):
        lib = self.lib
        peer, home = self.peer()
        lib.sync(self.client)
        lib.sync(peer, home=home)
        lib.host.auto_process = False
        (lib.installed() / 'local.md').write_text('laptop')
        lib.sync(self.client)
        laptop = [p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/sync/laptop')][0]
        # The other station's proposal is accepted first, while this one waits for checks.
        waiting, laptop['scheduled'] = laptop['scheduled'], None
        (lib.installed(home=home) / 'remote.md').write_text('desk')
        lib.sync(peer, home=home)
        lib.host.process()
        laptop['scheduled'] = waiting
        lib.host.process()
        lib.host.auto_process = True
        self.assertFalse(laptop['merged'])
        self.assertTrue(any('outdated' in e for e in laptop.get('rejections', [])), laptop)
        lib.sync(self.client)
        self.assertTrue(laptop['merged'])
        self.assertIn('local.md', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))

    def test_publication_retries_lost_responses_and_reports_access_errors(self):
        lib = self.lib
        lib.sync(self.client)
        self.edit('Pending edit')
        lib.git(self.client, 'config', 'remote.origin.pushurl', str(lib.root / 'offline.git'))
        lib.sync(self.client, expected=1)
        self.assertIn('pending', lib.sync(self.client, '--status', expected=None))
        lib.git(self.client, 'config', '--unset', 'remote.origin.pushurl')
        lib.host.faults['auth'] = 10
        self.assertIn('rejected access', lib.sync(self.client, expected=1))
        lib.host.faults.clear()
        lib.host.faults.update(create_lost=1, merge=1)
        lib.sync(self.client, expected=None)
        lib.sync(self.client)
        pulls = [p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/sync/laptop')]
        self.assertEqual(len(pulls), 1)
        self.assertTrue(pulls[0]['merged'])
        self.assertIn('Pending edit', lib.show('skills/first-skill/SKILL.md'))
        self.assertIn('Pending edit', (lib.installed() / 'SKILL.md').read_text())

    def test_direct_push_to_main_is_rejected(self):
        lib = self.lib
        (self.client / 'skills/first-skill/x.md').write_text('direct')
        lib.commit(self.client, 'Direct')
        result = subprocess.run(['git', '-C', str(self.client), 'push', 'origin', 'HEAD:main'],
                                env=lib.env, capture_output=True, text=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('protected', result.stderr)

    def test_offline_station_keeps_links_and_edits(self):
        lib = self.lib
        lib.sync(self.client)
        lib.remote.rename(lib.root / 'offline.git')
        self.edit('Offline edit')
        lib.sync(self.client, expected=1)
        self.assertIn('Offline edit', (lib.installed() / 'SKILL.md').read_text())
        self.assertEqual(lib.git(self.client, 'status', '--porcelain'), '')

    def test_lock_prevents_parallel_operations(self):
        import fcntl
        with (self.client / '.git/agent-skills-sync.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Another sync', self.lib.sync(self.client))
            self.assertFalse(self.lib.installed().exists())

    def test_confirm_update_review_and_approval_on_client(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        lib.write_skill('first-skill', 'reviewed upstream')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto')
        lib.sync(self.client)
        self.assertIn('UPDATE first-skill: confirm', lib.sync(self.client, '--updates'))
        review = lib.sync(self.client, '--review', args=['first-skill'])
        self.assertIn('+reviewed upstream', review)
        commit = review.split('Proposal commit: ')[1].split()[0]
        self.assertIn('version one', (lib.installed() / 'SKILL.md').read_text())
        self.assertIn('changed since review', lib.sync(self.client, '--approve', args=['first-skill', '0' * 40], expected=1))
        lib.sync(self.client, '--approve', args=['first-skill', commit])
        self.assertIn('reviewed upstream', lib.show('skills/first-skill/SKILL.md'))
        lib.sync(self.client)
        self.assertIn('reviewed upstream', (lib.installed() / 'SKILL.md').read_text())

    def test_outdated_proposal_is_updated_but_changed_result_needs_new_review(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        lib.write_skill('first-skill', 'reviewed upstream')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto')
        lib.sync(self.client)
        commit = lib.sync(self.client, '--review', args=['first-skill']).split('Proposal commit: ')[1].split()[0]
        # Unrelated main change: the proposal is refreshed and accepted.
        lib.manage('set-policy', 'first-skill', 'confirm')
        other = lib.write_skill('other-skill', 'other', root=self.client / 'skills')
        lib.sync(self.client)
        lib.sync(self.client, '--approve', args=['first-skill', commit])
        self.assertIn('reviewed upstream', lib.show('skills/first-skill/SKILL.md'))
        self.assertTrue(other.exists())

    def test_shared_change_during_review_invalidates_approval(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        lib.write_skill('first-skill', 'reviewed upstream')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto')
        lib.sync(self.client)
        commit = lib.sync(self.client, '--review', args=['first-skill']).split('Proposal commit: ')[1].split()[0]
        (lib.installed() / 'extra.md').write_text('edited meanwhile')
        lib.sync(self.client)
        self.assertIn('Shared skill changed', lib.sync(self.client, '--approve', args=['first-skill', commit], expected=1))
        self.assertNotIn('reviewed upstream', lib.show('skills/first-skill/SKILL.md'))
        lib.manage('maintain', '--apply-auto')
        lib.sync(self.client)
        review = lib.sync(self.client, '--review', args=['first-skill'])
        self.assertNotIn(commit, review.split('Proposal commit: ')[1].split()[0])

    def test_conflict_resolution_with_merged_result_on_client(self):
        lib = self.lib
        lib.sync(self.client)
        self.edit('Our line')
        lib.sync(self.client)
        lib.write_skill('first-skill', 'Their line')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto', expected=1)
        lib.sync(self.client)
        review = lib.sync(self.client, '--review', args=['first-skill'])
        commit = review.split('Proposal commit: ')[1].split()[0]
        merged = self.client / '.git/agent-skills-review/first-skill/skills/first-skill/SKILL.md'
        self.assertIn('<<<<<<<', merged.read_text())
        self.assertIn('Conflict markers remain', lib.sync(self.client, '--resolve', args=['first-skill', commit, 'merged'], expected=1))
        merged.write_text('---\nname: first-skill\ndescription: Test skill\n---\nOur line\nTheir line\n')
        lib.sync(self.client, '--resolve', args=['first-skill', commit, 'merged'])
        text = lib.show('skills/first-skill/SKILL.md')
        self.assertIn('Our line', text)
        self.assertIn('Their line', text)
        self.assertNotIn('<<<<<<<', text)
        lib.sync(self.client)
        self.assertNotIn('<<<<<<<', (lib.installed() / 'SKILL.md').read_text())
        lib.manage('maintain', '--apply-auto')
        entry = lib.registry()['skills']['first-skill']
        self.assertEqual(entry['customization']['base_revision'], entry['revision'])

    def test_conflict_take_upstream_on_client(self):
        lib = self.lib
        lib.sync(self.client)
        self.edit('Our line')
        lib.sync(self.client)
        lib.write_skill('first-skill', 'Their line')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto', expected=1)
        lib.sync(self.client)
        commit = lib.sync(self.client, '--review', args=['first-skill']).split('Proposal commit: ')[1].split()[0]
        lib.sync(self.client, '--resolve', args=['first-skill', commit, 'take-upstream'])
        self.assertIn('Their line', lib.show('skills/first-skill/SKILL.md'))
        self.assertNotIn('Our line', lib.show('skills/first-skill/SKILL.md'))

    def test_own_edits_never_require_approval_under_confirm_policy(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        lib.sync(self.client)
        self.edit('Own edit')
        lib.sync(self.client)
        self.assertIn('Own edit', lib.show('skills/first-skill/SKILL.md'))

    def legacy_link(self, edited=False):
        entry = json.loads((self.client / 'sources.json').read_text())['skills']['first-skill']
        cache = self.lib.home / '.cache/agent-skills/versions/first-skill' / entry['content_sha256']
        shutil.copytree(self.client / 'skills/first-skill', cache)
        if edited:
            path = cache / 'SKILL.md'
            path.write_text(path.read_text() + '\nLegacy local edit')
        for catalog in ['.agents', '.claude']:
            link = self.lib.installed(catalog=catalog)
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(cache)
        return cache

    def test_legacy_cache_links_move_to_library_without_losing_edits(self):
        lib = self.lib
        old = self.legacy_link(edited=True)
        lib.sync(self.client)
        self.assertEqual(lib.installed().resolve(), self.client / 'skills/first-skill')
        self.assertIn('Legacy local edit', lib.show('skills/first-skill/SKILL.md'))
        self.assertIn('Legacy local edit', (old / 'SKILL.md').read_text())

    def test_foreign_installations_and_dangling_links_are_preserved(self):
        lib = self.lib
        foreign = lib.installed()
        foreign.mkdir(parents=True)
        (foreign / 'private').write_text('keep')
        lib.sync(self.client, expected=1)
        self.assertEqual((foreign / 'private').read_text(), 'keep')
        other = lib.installed(catalog='.claude')
        other.unlink()
        other.symlink_to(lib.root / 'missing')
        lib.sync(self.client, expected=1)
        self.assertEqual(os.readlink(other), str(lib.root / 'missing'))

    def test_client_never_contacts_sources(self):
        lib = self.lib
        lib.upstream.rename(lib.root / 'source-offline')
        shims = lib.root / 'disabled-tools'
        shims.mkdir()
        for name in ['tar', 'python', 'python3', 'wget']:
            tool = shims / name
            tool.write_text('#!/bin/sh\necho External tool invoked >&2\nexit 98\n')
            tool.chmod(0o755)
        lib.env['PATH'] = str(shims) + os.pathsep + lib.env['PATH']
        lib.sync(self.client)
        self.assertIn('version one', (lib.installed() / 'SKILL.md').read_text())


if __name__ == '__main__':
    unittest.main()
