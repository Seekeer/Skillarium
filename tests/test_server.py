"""Server role through the user commands: maintenance, policies and proposals.

Runs on Linux and Windows: only Git, Python and the fake hosting are used.
"""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from fixture import Library, ROOT


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()

    def update_upstream(self, text, name='first-skill', extra=None):
        lib = self.lib
        lib.write_skill(name, text)
        for path, data in (extra or {}).items():
            target = lib.upstream / 'skills' / name / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        lib.commit(lib.upstream, 'Upstream ' + text)
        return lib.git(lib.upstream, 'rev-parse', 'HEAD')

    def edit_shared(self, path, data, name='first-skill', message='Client edit'):
        """A client edit that already reached main through its own pull request."""
        lib = self.lib
        work = lib.root / 'editor'
        if not work.exists():
            lib.clone('editor', 'editor-machine')
        lib.pull(work)
        target = work / 'skills' / name / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        lib.commit(work, message)
        lib.git(work, 'push', '-q', 'origin', 'HEAD:refs/heads/editor-change')
        number = self.open_pull('editor-change')
        self.merge(number, lib.git(work, 'rev-parse', 'HEAD'))
        return work

    def open_pull(self, branch):
        status = self.lib.host
        with status.lock:
            number = status.next_number
            status.next_number += 1
            status.pulls[number] = dict(number=number, head=branch, base='main', title='', body='',
                                        state='open', merged=False, scheduled=None)
        return number

    def merge(self, number, head):
        pull = self.lib.host.pulls[number]
        pull['scheduled'] = head
        self.lib.host.process()
        self.assertTrue(pull['merged'], pull.get('rejections'))

    def proposal(self, name='first-skill'):
        refs = self.lib.git(self.lib.remote, 'for-each-ref', '--format=%(refname:short)', f'refs/heads/agent-skills/update/{name}/')
        self.assertTrue(refs, 'proposal branch missing')
        branch = refs.splitlines()[0]
        message = self.lib.git(self.lib.remote, 'log', '-1', '--format=%B', branch)
        values = dict(line[len('Agent-Skills-'):].split(': ', 1) for line in message.splitlines() if line.startswith('Agent-Skills-'))
        values['head'] = self.lib.git(self.lib.remote, 'rev-parse', branch)
        return branch, values

    def report(self):
        return json.loads((self.lib.server / '.git/skill-updates.json').read_text(encoding='utf-8'))

    def test_registration_and_auto_update_are_server_pull_requests(self):
        lib = self.lib
        entry = lib.registry()['skills']['first-skill']
        self.assertTrue(entry['auto_update'])
        self.assertEqual(lib.main('upstream/first-skill'), lib.main('skills/first-skill'))
        self.assertIn('agent-skills/i3', lib.git(lib.remote, 'log', '--format=%an', 'main'))
        revision = self.update_upstream('version two')
        lib.pull(lib.server)
        before = (lib.server / 'skills/first-skill/SKILL.md').read_bytes()
        self.assertIn('first-skill: updated', lib.manage('maintain', '--apply-auto'))
        self.assertIn('version two', lib.show('skills/first-skill/SKILL.md'))
        self.assertEqual(lib.registry()['skills']['first-skill']['revision'], revision)
        # The active copy of the server is updated only by its own sync.
        self.assertEqual((lib.server / 'skills/first-skill/SKILL.md').read_bytes(), before)
        self.assertEqual(lib.git(lib.server, 'status', '--porcelain'), '')
        head = lib.main()
        self.assertIn('first-skill: unchanged', lib.manage('maintain', '--apply-auto'))
        self.assertEqual(lib.main(), head)

    def test_confirm_policy_proposes_and_approval_merges_exact_result(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        self.assertFalse(lib.registry()['skills']['first-skill']['auto_update'])
        revision = self.update_upstream('reviewed version')
        lib.manage('maintain', '--apply-auto')
        self.assertIn('version one', lib.show('skills/first-skill/SKILL.md'))
        branch, values = self.proposal()
        self.assertEqual(values['Status'], 'confirm')
        self.assertEqual(values['Policy'], 'confirm')
        self.assertEqual(values['Incoming-Revision'], revision)
        self.assertEqual(self.report()['first-skill']['status'], 'awaiting-confirmation')
        pulls = [p for p in lib.host.pulls.values() if p['head'] == branch]
        self.assertEqual(len(pulls), 1)
        self.assertIsNone(pulls[0]['scheduled'])
        # Repeated checks of an unchanged proposal create no duplicates.
        lib.manage('maintain', '--apply-auto')
        self.assertEqual(len([p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/update/')]), 1)
        self.assertIn('source moved', lib.manage('approve', 'first-skill', '--commit', values['head'], '--revision', '0' * 40, expected=1))
        lib.manage('approve', 'first-skill', '--commit', values['head'], '--revision', revision)
        self.assertIn('reviewed version', lib.show('skills/first-skill/SKILL.md'))
        self.assertEqual(lib.registry()['skills']['first-skill']['revision'], revision)

    def test_regenerated_result_requires_a_new_exact_approval(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        revision = self.update_upstream('reviewed update')
        lib.manage('maintain')
        _, old = self.proposal()
        self.edit_shared('local.md', b'changed shared result\n')
        lib.manage('maintain')
        _, new = self.proposal()
        self.assertEqual(old['Incoming-Revision'], new['Incoming-Revision'])
        self.assertNotEqual(old['Result-Tree'], new['Result-Tree'])
        self.assertIn('changed since review', lib.manage('approve', 'first-skill',
                      '--revision', revision, '--commit', old['head'], expected=1))
        lib.manage('approve', 'first-skill', '--revision', revision, '--commit', new['head'])

    def test_refreshed_proposal_can_be_reviewed_and_approved_after_schedule_failure(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        revision = self.update_upstream('reviewed update')
        lib.manage('maintain')
        branch, values = self.proposal()
        # Move main without changing this proposal's skill, policy or inputs.
        lib.write_skill('another-skill', 'unrelated', root=lib.upstream / 'skills')
        lib.commit(lib.upstream, 'Unrelated skill')
        lib.register('another-skill')
        lib.host.faults['merge'] = 1
        lib.manage('approve', 'first-skill', '--revision', revision,
                   '--commit', values['head'], expected=1)
        refreshed = lib.git(lib.remote, 'rev-parse', branch)
        self.assertNotEqual(refreshed, values['head'])
        self.assertIn('Proposal first-skill: confirm', lib.manage('status'))
        self.assertIn('changed since review', lib.manage('approve', 'first-skill',
                      '--revision', revision, '--commit', values['head'], expected=1))
        lib.manage('approve', 'first-skill', '--revision', revision, '--commit', refreshed)
        self.assertIn('reviewed update', lib.show('skills/first-skill/SKILL.md'))

    def test_changed_incoming_version_replaces_proposal_and_old_approval_fails(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        old = self.update_upstream('first proposal')
        lib.manage('maintain')
        old_branch, old_values = self.proposal()
        new = self.update_upstream('second proposal')
        lib.manage('maintain')
        branch, values = self.proposal()
        self.assertNotEqual(branch, old_branch)
        self.assertEqual(values['Incoming-Revision'], new)
        self.assertFalse(any(p['state'] == 'open' for p in lib.host.pulls.values() if p['head'] == old_branch))
        lib.manage('approve', 'first-skill', '--commit', old_values['head'], '--revision', old, expected=1)
        self.assertIn('version one', lib.show('skills/first-skill/SKILL.md'))

    def test_client_edit_is_merged_with_upstream_and_mode_respected(self):
        lib = self.lib
        self.edit_shared('local.md', b'our addition\n')
        self.update_upstream('version two')
        lib.manage('maintain', '--apply-auto')
        files = lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main', 'skills/first-skill')
        self.assertIn('local.md', files)
        self.assertIn('version two', lib.show('skills/first-skill/SKILL.md'))
        entry = lib.registry()['skills']['first-skill']
        self.assertEqual(entry['customization']['base_sha256'], entry['content_sha256'])
        # The same clean merge waits for approval under the confirm policy.
        lib.manage('set-policy', 'first-skill', 'confirm')
        self.update_upstream('version three')
        lib.manage('maintain', '--apply-auto')
        _, values = self.proposal()
        self.assertEqual(values['Status'], 'confirm')
        result = lib.git(lib.remote, 'ls-tree', '-r', '--name-only', values['Result-Tree'])
        self.assertIn('local.md', result)

    def test_overlapping_edits_become_conflict_with_materials_and_preserve_installation(self):
        lib = self.lib
        self.edit_shared('SKILL.md', b'---\nname: first-skill\ndescription: Test skill\n---\nour line\n')
        self.update_upstream('their line')
        output = lib.manage('maintain', '--apply-auto', expected=1)
        self.assertIn('upstream-conflict', output)
        self.assertIn('our line', lib.show('skills/first-skill/SKILL.md'))
        branch, values = self.proposal()
        self.assertEqual(values['Status'], 'conflict')
        self.assertEqual(lib.git(lib.remote, 'rev-parse', branch + ':skills/first-skill'), values['Shared-Tree'])
        materials = f'agent-skills/materials/first-skill/{values["Key"]}'
        self.assertIn('<<<<<<<', lib.show('skills/first-skill/SKILL.md', materials))
        self.assertIn('their line', lib.show('upstream/first-skill/SKILL.md', branch))
        report = self.report()['first-skill']
        self.assertEqual(report['status'], 'upstream-conflict')
        shared = report['shared']['content_sha256']
        lib.manage('approve', 'first-skill', '--commit', values['head'], '--revision', values['Incoming-Revision'], expected=1)
        lib.manage('resolve', 'first-skill', '--commit', values['head'], '--revision', values['Incoming-Revision'], '--shared-hash', '0' * 64,
                   '--strategy', 'take-upstream', expected=1)
        lib.manage('resolve', 'first-skill', '--commit', values['head'], '--revision', values['Incoming-Revision'], '--shared-hash', shared,
                   '--strategy', 'keep-shared')
        self.assertIn('our line', lib.show('skills/first-skill/SKILL.md'))
        entry = lib.registry()['skills']['first-skill']
        self.assertEqual(entry['revision'], values['Incoming-Revision'])
        self.assertIn('customization', entry)
        self.assertIn('first-skill: customized', lib.manage('maintain', '--apply-auto'))

    def test_take_upstream_replaces_content_and_keeps_history(self):
        lib = self.lib
        self.edit_shared('SKILL.md', b'---\nname: first-skill\ndescription: Test skill\n---\nour line\n')
        self.update_upstream('their line')
        lib.manage('maintain', '--apply-auto', expected=1)
        _, values = self.proposal()
        shared = self.report()['first-skill']['shared']['content_sha256']
        lib.manage('resolve', 'first-skill', '--commit', values['head'], '--revision', values['Incoming-Revision'], '--shared-hash', shared,
                   '--strategy', 'take-upstream')
        self.assertIn('their line', lib.show('skills/first-skill/SKILL.md'))
        self.assertIn('our line', lib.git(lib.remote, 'log', '-p', 'main', '--', 'skills/first-skill/SKILL.md'))
        lib.manage('maintain', '--apply-auto')
        self.assertNotIn('customization', lib.registry()['skills']['first-skill'])

    def test_delete_versus_modify_binary_and_mode_conflicts_stay_unresolved(self):
        lib = self.lib
        self.update_upstream('base', extra={'data.bin': b'\x00\x01base', 'tool.sh': b'echo base\n'})
        lib.manage('maintain', '--apply-auto')
        self.edit_shared('data.bin', b'\x00\x01ours')
        lib.write_skill('first-skill', 'base')
        (lib.upstream / 'skills/first-skill/data.bin').write_bytes(b'\x00\x01theirs')
        lib.commit(lib.upstream, 'Binary change')
        lib.manage('maintain', '--apply-auto', expected=1)
        _, values = self.proposal()
        self.assertEqual(values['Status'], 'conflict')
        self.assertEqual(lib.git(lib.remote, 'cat-file', '-p', 'main:skills/first-skill/data.bin'), '\x00\x01ours')

    def test_upstream_removal_or_outage_keeps_installed_skill_and_detach_works_offline(self):
        lib = self.lib
        self.edit_shared('SKILL.md', b'---\nname: first-skill\ndescription: Test skill\n---\nours\n')
        lib.manage('maintain', '--apply-auto')
        lib.git(lib.upstream, 'rm', '-rq', 'skills/first-skill')
        lib.git(lib.upstream, 'commit', '-q', '-m', 'Remove skill')
        lib.manage('maintain', '--apply-auto', expected=1)
        self.assertEqual(self.report()['first-skill']['status'], 'source-unavailable')
        lib.upstream.rename(lib.root / 'offline-upstream')
        lib.manage('maintain', '--apply-auto', expected=1)
        self.assertIn('ours', lib.show('skills/first-skill/SKILL.md'))
        shared = self.report()['first-skill']['shared']['content_sha256']
        lib.manage('resolve', 'first-skill', '--shared-hash', shared, '--strategy', 'detach')
        entry = lib.registry()['skills']['first-skill']
        self.assertEqual(entry['source'], dict(kind='local', path='skills/first-skill'))
        self.assertNotIn('upstream/first-skill', lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main'))
        self.assertIn('first-skill: local', lib.manage('maintain', '--apply-auto'))

    def test_published_new_local_folder_is_registered_without_external_source(self):
        lib = self.lib
        work = lib.clone('editor', 'laptop')
        folder = lib.write_skill('my-local', 'own skill', root=work / 'skills')
        (folder / 'run.ps1').write_bytes(b'Write-Output ok\r\n')
        lib.commit(work, 'New own skill')
        lib.git(work, 'push', '-q', 'origin', 'HEAD:refs/heads/new-skill')
        self.merge(self.open_pull('new-skill'), lib.git(work, 'rev-parse', 'HEAD'))
        self.assertNotIn('my-local', lib.registry()['skills'])
        lib.manage('maintain', '--apply-auto')
        entry = lib.registry()['skills']['my-local']
        self.assertEqual(entry['source'], dict(kind='local', path='skills/my-local'))
        self.assertFalse(entry['auto_update'])
        self.assertEqual(subprocess.run(['git', '-C', str(lib.remote), 'cat-file', 'blob', 'main:skills/my-local/run.ps1'],
                                        capture_output=True).stdout, b'Write-Output ok\r\n')

    def test_server_pins_the_policy_engine_to_its_release(self):
        lib = self.lib
        release = lib.root / 'release.json'
        def write(version):
            release.write_text(json.dumps({'version': version, 'engine': {'linux': {'sha512': version[0] * 128}}}))
        self.assertIn('not installed from a release', lib.manage('pin-policy'))
        write('1.0.0')
        lib.manage('pin-policy', '--release', str(release))
        pinned = json.loads(lib.show('agent-skills.json'))['policy_engine']
        self.assertEqual(pinned, {'version': '1.0.0', 'sha512': '1' * 128})
        lib.pull(lib.server)
        self.assertIn('already pinned', lib.manage('pin-policy', '--release', str(release)))
        write('0.9.0')
        self.assertIn('not older', lib.manage('pin-policy', '--release', str(release)))
        self.assertEqual(json.loads(lib.show('agent-skills.json'))['policy_engine']['version'], '1.0.0')
        laptop = lib.clone('laptop', 'laptop')
        write('2.0.0')
        self.assertIn('not the assigned server', lib.manage('pin-policy', '--release', str(release), repo=laptop,
                                                            expected=1))

    def test_unassigned_machine_cannot_publish_server_changes(self):
        lib = self.lib
        other = lib.clone('other', 'laptop')
        self.update_upstream('version two')
        head = lib.main()
        self.assertIn('not the assigned server', lib.manage('maintain', '--apply-auto', repo=other, expected=1))
        self.assertIn('not the assigned server', lib.manage('set-policy', 'first-skill', 'confirm', repo=other, expected=1))
        self.assertEqual(lib.main(), head)
        # A read-only check stays available everywhere.
        self.assertIn('first-skill: updated', lib.manage('check', repo=other) + lib.manage('check', '--apply-auto', repo=other))
        self.assertEqual(lib.main(), head)

    def test_server_role_transfer_without_old_server_closes_its_pending_pull_requests(self):
        lib = self.lib
        lib.host.auto_process = False
        self.update_upstream('version two')
        lib.manage('maintain', '--apply-auto')
        old = [p for p in lib.host.pulls.values() if p['state'] == 'open']
        self.assertEqual(len(old), 1)
        # Its checks are still pending when the role moves.
        old[0]['scheduled'] = None
        old_head = lib.host.head_of(old[0]['head'])
        lib.host.auto_process = True
        laptop = lib.clone('laptop', 'laptop')
        lib.manage('assign-server', 'laptop', repo=laptop)
        self.assertEqual(json.loads(lib.show('agent-skills.json'))['server']['machine'], 'laptop')
        self.assertEqual(old[0]['state'], 'closed')
        self.assertFalse(old[0]['merged'])
        # Even if restored and reopened, the former server's change no longer passes.
        lib.git(lib.remote, 'update-ref', 'refs/heads/' + old[0]['head'], old_head)
        old[0]['state'] = 'open'
        lib.host.update(old[0])
        old[0]['scheduled'] = lib.host.head_of(old[0]['head'])
        lib.host.process()
        self.assertFalse(old[0]['merged'])
        self.assertTrue(any('assigned server agent-skills/laptop' in e for e in old[0]['rejections']))
        self.assertIn('not the assigned server', lib.manage('maintain', '--apply-auto', expected=1))
        lib.manage('maintain', '--apply-auto', repo=laptop)
        self.assertIn('version two', lib.show('skills/first-skill/SKILL.md'))

    def test_offline_hosting_leaves_publication_pending_without_local_changes(self):
        lib = self.lib
        self.update_upstream('version two')
        lib.host.faults['auth'] = 10
        output = lib.manage('maintain', '--apply-auto', expected=1)
        self.assertIn('Publication pending', output)
        self.assertIn('version one', lib.show('skills/first-skill/SKILL.md'))
        self.assertEqual(lib.git(lib.server, 'status', '--porcelain'), '')
        lib.host.faults.clear()
        lib.manage('maintain', '--apply-auto')
        self.assertIn('version two', lib.show('skills/first-skill/SKILL.md'))

    def test_lost_create_response_and_failed_merge_request_do_not_duplicate(self):
        lib = self.lib
        self.update_upstream('version two')
        lib.host.faults.update(create_lost=1, merge=1)
        lib.manage('maintain', '--apply-auto')
        servers = [p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/server/i3/') and 'register' not in p['head']]
        self.assertEqual(len(servers), 1)
        self.assertTrue(servers[0]['merged'])
        lib.manage('maintain', '--apply-auto')
        self.assertEqual(len([p for p in lib.host.pulls.values() if p['head'].startswith('agent-skills/server/i3/')]), 2)

    def test_two_local_operations_do_not_change_one_copy_simultaneously(self):
        lib = self.lib
        lock = Path(lib.git(lib.server, 'rev-parse', '--absolute-git-dir')) / 'agent-skills-sync.lock'
        code = ('import sys,time; sys.path.insert(0, sys.argv[1]); import maintenance\n'
                'with maintenance.library_lock(sys.argv[2]):\n    print("locked", flush=True); time.sleep(5)\n')
        holder = subprocess.Popen([sys.executable, '-c', code, str(ROOT / 'server'), str(lib.server)],
                                  stdout=subprocess.PIPE, text=True, env=lib.env)
        self.assertEqual(holder.stdout.readline().strip(), 'locked')
        try:
            self.assertIn('Another client/server operation', lib.manage('maintain', '--apply-auto', expected=1))
        finally:
            holder.wait()
        self.assertTrue(lock.exists())

    def test_registry_records_and_policies_survive_client_code_updates(self):
        lib = self.lib
        lib.manage('set-policy', 'first-skill', 'confirm')
        before = lib.registry()
        work = lib.clone('developer', 'devbox')
        (work / 'install.sh').write_text((work / 'install.sh').read_text() + '\n# client change\n', newline='\n')
        lib.manage('client-manifest', '--write', repo=work)
        lib.commit(work, 'Client code change')
        lib.git(work, 'push', '-q', 'origin', 'HEAD:refs/heads/developer')
        self.merge(self.open_pull('developer'), lib.git(work, 'rev-parse', 'HEAD'))
        lib.manage('maintain', '--apply-auto')
        self.assertEqual(lib.registry(), before)
        lib.pull(lib.server)
        self.assertIn('Client manifest current', lib.manage('status'))


if __name__ == '__main__':
    unittest.main()
