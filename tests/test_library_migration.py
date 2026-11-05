"""release/migrate_library.py: the engine leaves a legacy library in one commit
that keeps the library data and passes the PR policy."""
import json
from pathlib import Path
import subprocess
import sys
import unittest

from fixture import Library, ROOT

sys.path.insert(0, str(ROOT / 'server'))
import policy  # noqa: E402


class LibraryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.work = self.lib.clone('admin', 'admin-laptop')
        # Old workflows of the library and a file of its own.
        workflows = self.work / '.gitea' / 'workflows'
        workflows.mkdir(parents=True)
        (workflows / 'tests.yml').write_text('name: Engine tests\n')
        (workflows / 'policy.yml').write_text('name: Old policy\n')
        (self.work / 'notes').mkdir()
        (self.work / 'notes' / 'team.md').write_text('Our notes\n')
        self.lib.commit(self.work, 'Library extras')
        self.lib.git(self.work, 'push', '-q', 'origin', 'HEAD:main', env={'AGENT_SKILLS_FAKE_HOST': '1'})

    def migrate(self, expected=0):
        result = subprocess.run([sys.executable, str(ROOT / 'release' / 'migrate_library.py'), '--library', str(self.work),
                                 '--url', 'https://gitea.example.org/team/skills.git'],
                                env=self.lib.env, capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, expected, output)
        return output

    def pin(self):
        path = self.work / 'agent-skills.json'
        config = json.loads(path.read_text())
        config['policy_engine'] = {'version': '1.0.0', 'sha512': 'a' * 128}
        path.write_text(json.dumps(config, indent=2) + '\n')
        self.lib.commit(self.work, 'Pin the policy')
        self.lib.git(self.work, 'push', '-q', 'origin', 'HEAD:main', env={'AGENT_SKILLS_FAKE_HOST': '1'})

    def test_unpinned_library_is_refused(self):
        self.assertIn('not pinned yet', self.migrate(expected=1))

    def test_engine_files_leave_and_the_data_stays(self):
        self.pin()
        before = set(self.lib.git(self.work, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines())
        self.assertIn('install.sh', before)
        self.migrate()
        after = set(self.lib.git(self.work, 'ls-tree', '-r', '--name-only', 'HEAD').splitlines())
        for path in ('install.sh', 'install.ps1', 'scripts/client.sh', 'projects.ps1', '.agent-skills-client.json',
                     '.gitea/workflows/tests.yml'):
            self.assertNotIn(path, after)
        for path in ('skills/first-skill/SKILL.md', 'sources.json', 'agent-skills.json', 'notes/team.md',
                     '.gitea/workflows/policy.yml', 'README.md', '.gitattributes'):
            self.assertIn(path, after)
        workflow = (self.work / '.gitea' / 'workflows' / 'policy.yml').read_text(encoding='utf-8')
        self.assertIn('Download the pinned Skillarium policy', workflow)
        self.assertIn('client --library https://gitea.example.org/team/skills.git',
                      (self.work / 'README.md').read_text(encoding='utf-8'))
        self.lib.git(self.work, 'push', '-q', 'origin', 'HEAD:refs/heads/skillarium/remove-engine')
        errors, _ = policy.evaluate(self.lib.remote, self.lib.main(),
                                    self.lib.git(self.lib.remote, 'rev-parse', 'skillarium/remove-engine'))
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()
