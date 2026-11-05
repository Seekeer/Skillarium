"""Engine apart from the library: an installed Skillarium runs a library clone
given by --library/-Library, SKILLARIUM_LIBRARY or the Skillarium
configuration, and a library of a newer format is used read-only.

The engine copy holds only the client files; it is not a library itself.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest

from fixture import CLIENT, HAS_BASH_CLIENT, POWERSHELL, ROOT, Library

ENGINE_FILES = [item for item in CLIENT if item.endswith(('.sh', '.ps1', '.vbs'))]
WINDOWS = os.name == 'nt'


class EngineLayout:
    """Shared scenarios; subclasses run them through one native client."""

    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.client = self.lib.clone('library clone', 'station')
        self.engine = self.lib.root / 'skillarium engine'
        for relative in ENGINE_FILES:
            (self.engine / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, self.engine / relative)
        self.config_home = self.lib.root / 'config home'

    def env(self, **extra):
        return dict(self.lib.env, XDG_CONFIG_HOME=str(self.config_home), APPDATA=str(self.config_home),
                    **extra)

    def configure(self, library):
        folder = self.config_home / ('Skillarium' if WINDOWS else 'skillarium')
        folder.mkdir(parents=True, exist_ok=True)
        (folder / 'config.json').write_text(json.dumps({'library': str(library)}), encoding='utf-8')

    def run_client(self, mode, library=None, env=None, expected=0):
        raise NotImplementedError

    def run_projects(self, *args, library=None, expected=0, raw=False):
        raise NotImplementedError

    def raise_format(self, local=False):
        """Publish (or only write locally) a library format newer than the engine."""
        target = self.client if local else self.lib.root / 'migration'
        if not local:
            self.lib.git(self.lib.root, 'clone', '-q', str(self.lib.remote), str(target))
        path = target / 'agent-skills.json'
        config = json.loads(path.read_text(encoding='utf-8'))
        config['format'] = 2
        path.write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        if not local:
            self.lib.commit(target, 'Migrate the library format')
            self.lib.git(target, 'push', '-q', 'origin', 'main', env={'AGENT_SKILLS_FAKE_HOST': '1'})

    def edit(self, text):
        path = self.client / 'skills' / 'first-skill' / 'SKILL.md'
        with path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write('\n' + text)

    def test_explicit_library_links_and_publishes(self):
        self.run_client('sync', library=self.client)
        installed = self.lib.installed()
        self.assertTrue(os.path.samefile(installed, self.client / 'skills' / 'first-skill'))
        self.edit('engine edit')
        self.assertIn('PUBLISHED: pull request', self.run_client('sync', library=self.client))
        self.assertIn('engine edit', self.lib.show('skills/first-skill/SKILL.md'))
        self.assertFalse((self.engine / 'skills').exists())

    def test_environment_and_configuration_find_the_library(self):
        self.run_client('sync', env={'SKILLARIUM_LIBRARY': str(self.client)})
        self.assertTrue(self.lib.installed().exists())
        self.configure(self.client)
        self.assertIn(f'format 1, engine supports 1', self.run_client('status'))

    def test_missing_library_is_reported(self):
        output = self.run_client('sync', expected=None)
        self.assertIn('Skill library not found', output)
        self.assertFalse(self.lib.installed().exists())

    def test_newer_upstream_format_is_read_only(self):
        self.run_client('sync', library=self.client)
        before = self.lib.main()
        self.raise_format()
        self.edit('kept local')
        output = self.run_client('sync', library=self.client, expected=None)
        self.assertIn('READ-ONLY: library format 2', output)
        # The edit may sit in a local automatic commit; it is neither merged nor published.
        self.assertNotIn('Migrate the library format', self.lib.git(self.client, 'log', '--format=%s', 'HEAD'))
        self.assertNotEqual(self.lib.main(), before)
        self.assertNotIn('kept local', self.lib.show('skills/first-skill/SKILL.md'))
        self.assertIn('kept local', (self.lib.installed() / 'SKILL.md').read_text(encoding='utf-8'))
        status = self.run_projects('status', library=self.client)
        self.assertEqual(status['format'], {'library': 2, 'supported': 1, 'readOnly': True})

    def test_newer_local_format_is_read_only(self):
        self.run_client('sync', library=self.client)
        self.raise_format(local=True)
        output = self.run_client('sync', library=self.client, expected=None)
        self.assertIn('READ-ONLY: library format 2', output)
        self.assertEqual(self.lib.git(self.client, 'rev-list', '--count', '@{upstream}..HEAD'), '0')

    def test_projects_status_reports_format(self):
        self.run_client('sync', library=self.client)
        status = self.run_projects('status', library=self.client)
        self.assertEqual(Path(status['library']).resolve(), self.client.resolve())
        self.assertEqual(status['format'], {'library': 1, 'supported': 1, 'readOnly': False})

    def test_status_without_origin_reads_the_working_tree(self):
        self.lib.git(self.client, 'remote', 'remove', 'origin')
        status = self.run_projects('status', library=self.client)
        self.assertEqual(status['format'], {'library': 1, 'supported': 1, 'readOnly': False})

    def test_conflict_resolution_is_refused_for_a_newer_format(self):
        self.run_client('sync', library=self.client)
        self.raise_format(local=True)
        head = self.lib.git(self.client, 'rev-parse', 'HEAD')
        request = self.lib.root / 'resolve.json'
        request.write_text(json.dumps({'upstream': head, 'files': []}), encoding='utf-8')
        output = self.run_projects('resolve', str(request), library=self.client, expected=None, raw=True)
        self.assertIn('READ-ONLY: library format 2', output)
        self.assertEqual(self.lib.git(self.client, 'rev-parse', 'HEAD'), head)


@unittest.skipUnless(WINDOWS and POWERSHELL, 'Windows PowerShell unavailable')
class PowerShellEngineLayoutTests(EngineLayout, unittest.TestCase):
    def powershell(self, script, arguments, env, expected):
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(self.engine / script), *arguments],
                                env=env, capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return result, output

    def run_client(self, mode, library=None, env=None, expected=0):
        arguments = ['-Mode', {'sync': 'Sync', 'status': 'Status'}[mode]]
        if library:
            arguments += ['-Library', str(library)]
        return self.powershell('install.ps1', arguments, self.env(**(env or {})), expected)[1]

    def run_projects(self, *args, library=None, expected=0, raw=False):
        arguments = (['-Library', str(library)] if library else []) + list(args)
        result, output = self.powershell('projects.ps1', arguments, self.env(), expected)
        if raw:
            return output
        return json.loads([line for line in result.stdout.splitlines() if line.startswith('{')][-1])


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux client tools unavailable')
class BashEngineLayoutTests(EngineLayout, unittest.TestCase):
    def bash(self, script, arguments, env, expected):
        result = subprocess.run(['bash', str(self.engine / script), *arguments], env=env,
                                capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return result, output

    def run_client(self, mode, library=None, env=None, expected=0):
        arguments = (['--library', str(library)] if library else []) + [{'sync': '--sync-only', 'status': '--status'}[mode]]
        return self.bash('install.sh', arguments, self.env(**(env or {})), expected)[1]

    def run_projects(self, *args, library=None, expected=0, raw=False):
        arguments = (['--library', str(library)] if library else []) + list(args)
        result, output = self.bash('projects.sh', arguments, self.env(), expected)
        if raw:
            return output
        return json.loads([line for line in result.stdout.splitlines() if line.startswith('{')][-1])


class ServerFormatTests(unittest.TestCase):
    def test_server_refuses_a_newer_library_format(self):
        lib = Library(self)
        migration = lib.root / 'migration'
        lib.git(lib.root, 'clone', '-q', str(lib.remote), str(migration))
        config = json.loads((migration / 'agent-skills.json').read_text(encoding='utf-8'))
        config['format'] = 2
        (migration / 'agent-skills.json').write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8')
        lib.commit(migration, 'Migrate the library format')
        lib.git(migration, 'push', '-q', 'origin', 'main', env={'AGENT_SKILLS_FAKE_HOST': '1'})
        lib.git(lib.server, 'fetch', '-q', 'origin')
        output = lib.manage('maintain', expected=1)
        self.assertIn('Library format 2 needs a newer Skillarium', output)

    def test_server_finds_the_library_through_the_environment(self):
        lib = Library(self)
        result = subprocess.run([sys.executable, str(ROOT / 'server/manage.py'), 'status'],
                                env=dict(lib.env, SKILLARIUM_LIBRARY=str(lib.server)),
                                capture_output=True, text=True, encoding='utf-8', cwd=str(lib.root))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
