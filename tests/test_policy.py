"""Acceptance rule of pull requests, evaluated on actual commits of a bare remote."""
import json
import subprocess
import sys
import unittest

from fixture import Library, ROOT

sys.path.insert(0, str(ROOT / 'server'))
import hosting  # noqa: E402
import policy  # noqa: E402


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.work = self.lib.clone('work', 'laptop')

    def commit_as(self, machine, message='Change', trailer=None):
        env = hosting.author_env(machine) if machine else {}
        self.lib.git(self.work, 'add', '-A', '.')
        text = message + ('\n\n' + trailer if trailer else '')
        self.lib.git(self.work, 'commit', '-q', '-m', text, env=env)
        return self.lib.git(self.work, 'rev-parse', 'HEAD')

    def evaluate(self, head=None):
        self.lib.git(self.work, 'push', '-q', 'origin', f'+{head or "HEAD"}:refs/heads/proposal')
        return policy.evaluate(self.lib.remote, self.lib.main(), self.lib.git(self.lib.remote, 'rev-parse', 'proposal'))[0]

    def edit_registry(self, change):
        path = self.work / 'sources.json'
        data = json.loads(path.read_text())
        change(data['skills']['first-skill'])
        path.write_text(json.dumps(data, indent=2) + '\n')

    def test_skill_content_is_accepted_from_any_machine_after_technical_checks(self):
        skill = self.work / 'skills/first-skill/SKILL.md'
        skill.write_text(skill.read_text() + '\nLaptop edit')
        self.commit_as('laptop')
        self.assertEqual(self.evaluate(), [])
        self.lib.write_skill('new-skill', 'own', root=self.work / 'skills')
        self.commit_as(None, 'Manual commit by a person')
        self.assertEqual(self.evaluate(), [])

    def test_invalid_skill_and_windows_paths_are_rejected(self):
        (self.work / 'skills/bad-skill').mkdir()
        (self.work / 'skills/bad-skill/SKILL.md').write_text('no frontmatter')
        self.commit_as('laptop')
        self.assertTrue(any('frontmatter' in e for e in self.evaluate()))
        self.lib.git(self.work, 'rm', '-rq', 'skills/bad-skill')
        # Created through the index: the name cannot exist on a Windows file system.
        blob = subprocess.run(['git', '-C', str(self.work), 'hash-object', '-w', '--stdin'], input=b'x',
                              capture_output=True, env=self.lib.env).stdout.decode().strip()
        self.lib.git(self.work, '-c', 'core.protectNTFS=false', 'update-index', '--add', '--cacheinfo', f'100644,{blob},skills/first-skill/AUX.txt')
        self.lib.git(self.work, 'commit', '-q', '-m', 'AUX', env=hosting.author_env('laptop'))
        self.assertTrue(any('Windows' in e for e in self.evaluate()))

    def test_pins_require_the_assigned_server_author(self):
        self.edit_registry(lambda entry: entry.update(auto_update=False))
        self.commit_as('laptop')
        errors = self.evaluate()
        self.assertTrue(any('assigned server agent-skills/i3' in e for e in errors), errors)
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        self.edit_registry(lambda entry: entry.update(auto_update=False))
        self.commit_as('i3')
        self.assertEqual(self.evaluate(), [])

    def pin(self, version, sha='a' * 128, **extra):
        path = self.work / 'agent-skills.json'
        config = json.loads(path.read_text())
        config['policy_engine'] = dict(version=version, sha512=sha)
        config.update(extra)
        path.write_text(json.dumps(config, indent=2) + '\n')

    def test_policy_engine_moves_forward_only_by_the_assigned_server(self):
        self.pin('1.2.0')
        self.commit_as('laptop', 'Pin by a client')
        self.assertTrue(any('pinned only by the assigned server' in e for e in self.evaluate()))
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        self.pin('1.2.0')
        self.commit_as('i3', 'Pin the policy')
        self.assertEqual(self.evaluate(), [])
        self.lib.git(self.lib.remote, 'update-ref', 'refs/heads/main', self.lib.git(self.work, 'rev-parse', 'HEAD'),
                     env={'AGENT_SKILLS_FAKE_HOST': '1'})
        self.lib.git(self.work, 'fetch', '-q', 'origin')
        self.pin('1.1.9')
        self.commit_as('i3', 'Downgrade')
        self.assertTrue(any('only moves forward' in e for e in self.evaluate()))
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        self.pin('1.3.0-beta.1', sha='bad')
        self.commit_as('i3', 'Broken pin')
        self.assertTrue(any('sha512' in e for e in self.evaluate()))
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        # Any other configuration change stays an administrative operation.
        self.pin('1.3.0', server={'machine': 'laptop'})
        self.commit_as('i3', 'Pin and reassign')
        self.assertTrue(any('assign-server' in e for e in self.evaluate()))

    def test_workflows_change_only_by_the_assigned_server(self):
        workflow = self.work / '.gitea' / 'workflows' / 'policy.yml'
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text('name: Always green\n')
        self.commit_as('laptop', 'Replace the required check')
        self.assertTrue(any('.gitea/workflows/policy.yml' in e and 'assigned server' in e for e in self.evaluate()))
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        workflow.parent.mkdir(parents=True, exist_ok=True)
        workflow.write_text('name: Library policy\n')
        self.commit_as('i3', 'Server updates the check')
        self.assertEqual(self.evaluate(), [])

    def test_version_precedence(self):
        order = ['0.9.0', '1.0.0-alpha', '1.0.0-alpha.1', '1.0.0-alpha.beta', '1.0.0-beta.2', '1.0.0-beta.11',
                 '1.0.0-rc.1', '1.0.0', '1.0.1', '1.10.0']
        self.assertEqual(sorted(order, key=hosting.version_key), order)

    def test_mixed_pull_request_checks_each_control_change(self):
        skill = self.work / 'skills/first-skill/SKILL.md'
        skill.write_text(skill.read_text() + '\nContent')
        self.commit_as('laptop')
        self.edit_registry(lambda entry: entry.update(auto_update=False))
        self.commit_as('i3', 'Server pin')
        self.assertEqual(self.evaluate(), [])
        upstream = self.work / 'upstream/first-skill/SKILL.md'
        upstream.write_text('forged base')
        self.commit_as('laptop', 'Forged base')
        self.assertTrue(any('upstream/first-skill' in e for e in self.evaluate()))

    def test_assignment_inside_a_proposal_does_not_authorize_it(self):
        config = json.loads((self.work / 'agent-skills.json').read_text())
        config['server']['machine'] = 'laptop'
        (self.work / 'agent-skills.json').write_text(json.dumps(config))
        self.edit_registry(lambda entry: entry.update(auto_update=False))
        self.commit_as('laptop')
        errors = self.evaluate()
        self.assertTrue(any('separate administrative' in e for e in errors), errors)

    def test_assignment_is_an_explicit_separate_operation(self):
        config = json.loads((self.work / 'agent-skills.json').read_text())
        config['server']['machine'] = 'laptop'
        (self.work / 'agent-skills.json').write_text(json.dumps(config))
        self.commit_as(None, 'Assign without marker')
        self.assertTrue(any('assign-server' in e for e in self.evaluate()))
        self.lib.git(self.work, 'commit', '-q', '--amend', '-m', 'Assign\n\n' + policy.ADMIN_TRAILER)
        self.assertEqual(self.evaluate(), [])

    def test_outdated_proposal_and_whole_skill_removal_are_rejected(self):
        skill = self.work / 'skills/first-skill/SKILL.md'
        skill.write_text(skill.read_text() + '\nEdit')
        head = self.commit_as('laptop')
        self.lib.manage('set-policy', 'first-skill', 'confirm')
        self.assertTrue(any('outdated' in e for e in self.evaluate(head)))
        self.lib.git(self.work, 'fetch', '-q')
        self.lib.git(self.work, 'reset', '-q', '--hard', 'origin/main')
        self.lib.git(self.work, 'rm', '-rq', 'skills/first-skill')
        self.commit_as('laptop')
        self.assertTrue(any('first-skill' in e for e in self.evaluate()))

    def test_hosting_update_merge_keeps_server_endorsement(self):
        self.edit_registry(lambda entry: entry.update(auto_update=False))
        self.commit_as('i3', 'Server pin')
        self.lib.git(self.work, 'push', '-q', 'origin', 'HEAD:refs/heads/proposal')
        other = self.lib.clone('other', 'desk')
        skill = other / 'skills/first-skill/note.md'
        skill.write_text('other station')
        self.lib.commit(other, 'Other edit')
        self.lib.git(other, 'push', '-q', 'origin', 'HEAD:refs/heads/other')
        host = self.lib.host
        pull = dict(number=99, head='other', base='main', title='', body='', state='open', merged=False, scheduled=None)
        host.pulls[99] = pull
        pull['scheduled'] = host.head_of('other')
        host.process()
        self.assertTrue(pull['merged'])
        proposal = dict(number=100, head='proposal', base='main', title='', body='', state='open', merged=False)
        self.assertTrue(host.update(proposal))
        self.assertEqual(policy.evaluate(self.lib.remote, self.lib.main(), host.head_of('proposal'))[0], [])


if __name__ == '__main__':
    unittest.main()
