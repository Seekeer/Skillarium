"""Native Windows client through install.ps1 with real NTFS junctions.

Uses Windows PowerShell 5.1, a bare remote and the fake hosting; the profile
of child processes is a temporary directory. Skipped outside Windows.
"""
import os
import unittest

from fixture import POWERSHELL, Library


@unittest.skipUnless(os.name == 'nt' and POWERSHELL, 'Windows PowerShell unavailable')
class WindowsClientTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.client = self.lib.clone('client with spaces', 'winbox')

    def junction(self, path):
        import ctypes
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != -1 and bool(attributes & 0x400)

    def edit(self, text, home=None):
        path = self.lib.installed(home=home) / 'SKILL.md'
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write('\n' + text)

    def review(self):
        output = self.lib.powershell(self.client, 'Review', extra=['-Skill', 'first-skill'])
        return output, output.split('Proposal commit: ')[1].split()[0]

    def test_junctions_publication_and_second_station(self):
        lib = self.lib
        lib.powershell(self.client)
        for catalog in ('.agents', '.claude'):
            self.assertTrue(self.junction(lib.installed(catalog=catalog)))
            self.assertTrue(os.path.samefile(lib.installed(catalog=catalog), self.client / 'skills' / 'first-skill'))
        self.edit('Windows edit')
        (lib.installed() / 'helper.ps1').write_bytes(b'Write-Output ours\r\n')
        self.assertIn('PUBLISHED: pull request', lib.powershell(self.client))
        self.assertIn('Windows edit', lib.show('skills/first-skill/SKILL.md'))
        self.assertEqual(lib.git(lib.remote, 'log', '-1', '--format=%an', 'main^2'), 'agent-skills/winbox')
        self.assertEqual((lib.root / 'global.gitconfig').read_text(), '')
        peer = lib.clone('peer', 'desk')
        home = lib.root / 'peer home'
        home.mkdir()
        lib.powershell(peer, home=home)
        self.assertIn('Windows edit', (lib.installed(home=home) / 'SKILL.md').read_text(encoding='utf-8'))
        self.assertEqual((lib.installed(home=home) / 'helper.ps1').read_bytes(), b'Write-Output ours\r\n')
        lib.powershell(self.client)
        self.assertEqual(lib.git(self.client, 'rev-list', '--count', '@{upstream}..HEAD'), '0')
        self.assertIn('Publication:', lib.powershell(self.client, 'Status'))

    def test_new_skill_invalid_skill_and_unrelated_changes(self):
        lib = self.lib
        lib.powershell(self.client)
        lib.write_skill('win-local', 'own', root=self.client / 'skills')
        lib.powershell(self.client)
        self.assertIn('skills/win-local/SKILL.md', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertTrue(self.junction(lib.installed('win-local')))
        head = lib.main()
        bad = self.client / 'skills' / 'bad-skill'
        bad.mkdir()
        (bad / 'SKILL.md').write_text('no frontmatter')
        self.assertIn('frontmatter', lib.powershell(self.client, expected=1))
        self.assertEqual((bad / 'SKILL.md').read_text(), 'no frontmatter')
        (bad / 'SKILL.md').unlink()
        bad.rmdir()
        (self.client / 'notes.txt').write_text('private')
        self.assertIn('Unrelated local changes preserved', lib.powershell(self.client, expected=1))
        (self.client / 'notes.txt').unlink()
        self.edit('Staged')
        lib.git(self.client, 'add', 'skills')
        self.assertIn('Staged changes preserved', lib.powershell(self.client, expected=1))
        self.assertEqual(lib.main(), head)

    def test_retries_and_access_errors_keep_local_commit(self):
        lib = self.lib
        lib.powershell(self.client)
        self.edit('Pending edit')
        lib.host.faults['auth'] = 10
        self.assertIn('rejected access', lib.powershell(self.client, expected=1))
        self.assertIn('pending', lib.powershell(self.client, 'Status'))
        lib.host.faults.clear()
        lib.host.faults.update(create_lost=1, merge=1)
        lib.powershell(self.client, expected=None)
        lib.powershell(self.client)
        pulls = [p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/sync/winbox')]
        self.assertEqual(len(pulls), 1)
        self.assertTrue(pulls[0]['merged'])
        self.assertIn('Pending edit', lib.show('skills/first-skill/SKILL.md'))

    def test_confirm_update_review_and_exact_approval(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        lib.write_skill('first-skill', 'reviewed upstream')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto')
        lib.powershell(self.client)
        self.assertIn('UPDATE first-skill: confirm', lib.powershell(self.client, 'Updates'))
        output, commit = self.review()
        self.assertIn('+reviewed upstream', output)
        self.assertIn('changed since review', lib.powershell(self.client, 'Approve', expected=1,
                                                             extra=['-Skill', 'first-skill', '-Commit', '0' * 40]))
        lib.powershell(self.client, 'Approve', extra=['-Skill', 'first-skill', '-Commit', commit])
        self.assertIn('reviewed upstream', lib.show('skills/first-skill/SKILL.md'))
        lib.powershell(self.client)
        self.assertIn('reviewed upstream', (lib.installed() / 'SKILL.md').read_text(encoding='utf-8'))

    def test_conflict_resolution_with_merged_result(self):
        lib = self.lib
        lib.powershell(self.client)
        self.edit('Our line')
        lib.powershell(self.client)
        lib.write_skill('first-skill', 'Their line')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto', expected=1)
        lib.powershell(self.client)
        _, commit = self.review()
        merged = self.client / '.git/agent-skills-review/first-skill/skills/first-skill/SKILL.md'
        self.assertIn('<<<<<<<', merged.read_text(encoding='utf-8'))
        self.assertIn('Conflict markers remain', lib.powershell(self.client, 'Resolve', expected=1,
            extra=['-Skill', 'first-skill', '-Commit', commit, '-Strategy', 'merged']))
        merged.write_bytes(b'---\nname: first-skill\ndescription: Test skill\n---\nOur line\nTheir line\n')
        lib.powershell(self.client, 'Resolve', extra=['-Skill', 'first-skill', '-Commit', commit, '-Strategy', 'merged'])
        text = lib.show('skills/first-skill/SKILL.md')
        self.assertIn('Our line', text)
        self.assertIn('Their line', text)
        lib.powershell(self.client)
        self.assertNotIn('<<<<<<<', (lib.installed() / 'SKILL.md').read_text(encoding='utf-8'))

    def test_conflict_keep_ours_on_windows(self):
        lib = self.lib
        lib.powershell(self.client)
        self.edit('Our line')
        lib.powershell(self.client)
        lib.write_skill('first-skill', 'Their line')
        lib.commit(lib.upstream, 'Upstream update')
        lib.manage('maintain', '--apply-auto', expected=1)
        lib.powershell(self.client)
        _, commit = self.review()
        lib.powershell(self.client, 'Resolve', extra=['-Skill', 'first-skill', '-Commit', commit, '-Strategy', 'keep-ours'])
        self.assertIn('Our line', lib.show('skills/first-skill/SKILL.md'))
        entry = lib.registry()['skills']['first-skill']
        self.assertEqual(entry['revision'], lib.git(lib.upstream, 'rev-parse', 'HEAD'))

    def test_foreign_directory_is_preserved(self):
        lib = self.lib
        foreign = lib.installed(catalog='.claude')
        foreign.mkdir(parents=True)
        (foreign / 'private.txt').write_text('keep')
        lib.powershell(self.client, 'Link', expected=1)
        self.assertEqual((foreign / 'private.txt').read_text(), 'keep')
        self.assertTrue(self.junction(lib.installed()))


if __name__ == '__main__':
    unittest.main()
