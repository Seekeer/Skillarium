"""Engine installation from releases: versions/<v> + current, self-update,
checksum and download failures, rollback, channels and library format.

Releases come from tests/fake_releases.py; every machine path is a temporary
HOME, so the real profile and PATH are never touched.
"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from fake_releases import FakeReleases
from fixture import HAS_BASH_CLIENT, POWERSHELL, ROOT

WINDOWS = os.name == 'nt'


class EngineInstall:
    """Shared scenarios; subclasses run them through one platform's skillarium."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='skillarium install ')
        self.root = Path(self.temporary.name)
        self.releases = FakeReleases(self.root / 'releases')
        self.home = self.root / 'engine home'
        self.addCleanup(self.temporary.cleanup)
        self.addCleanup(self.releases.stop)

    def run_skillarium(self, *args, expected=0, env=None):
        raise NotImplementedError

    def version(self):
        return (self.home / 'current' / 'VERSION').read_text(encoding='utf-8').strip()

    def installed(self):
        return sorted(item.name for item in (self.home / 'versions').iterdir() if not item.name.startswith('.'))

    def configure_library(self, format_):
        library = self.root / 'library'
        library.mkdir(exist_ok=True)
        (library / 'agent-skills.json').write_text(json.dumps({'version': 1, 'format': format_}), encoding='utf-8')
        (library / 'sources.json').write_text('{"version": 2, "skills": {}}', encoding='utf-8')
        return {'SKILLARIUM_LIBRARY': str(library)}

    def test_install_update_and_status(self):
        self.releases.publish('0.1.0')
        output = self.run_skillarium('install')
        self.assertIn('INSTALLED: Skillarium 0.1.0', output)
        self.assertEqual(self.version(), '0.1.0')
        self.assertTrue((self.home / 'current' / 'scripts' / 'client.ps1').is_file())
        self.assertTrue(self.command.is_file())
        self.assertIn('Up to date: Skillarium 0.1.0', self.run_skillarium('self-update'))
        self.releases.publish('0.2.0')
        self.assertIn('UPDATED: Skillarium 0.1.0 -> 0.2.0', self.run_skillarium('self-update'))
        self.assertEqual(self.version(), '0.2.0')
        self.assertEqual(self.installed(), ['0.1.0', '0.2.0'])
        status = self.run_skillarium('status', env=self.configure_library(1))
        self.assertIn('Skillarium: 0.2.0 (stable)', status)
        self.assertIn('format 1, engine supports 1; mode: normal', status)

    def test_broken_checksum_and_interrupted_download_change_nothing(self):
        self.releases.publish('0.1.0')
        self.run_skillarium('install')
        self.releases.publish('0.2.0', corrupt=True)
        output = self.run_skillarium('self-update', expected=1)
        self.assertIn('checksum mismatch', output.lower())
        self.assertEqual(self.version(), '0.1.0')
        self.releases.publish('0.2.1')
        self.releases.faults['truncate'] = 1
        self.run_skillarium('self-update', expected=1)
        self.assertEqual(self.version(), '0.1.0')
        self.assertEqual(self.installed(), ['0.1.0'])
        self.assertIn('UPDATED: Skillarium 0.1.0 -> 0.2.1', self.run_skillarium('self-update'))

    def test_rollback_skips_the_bad_version_and_old_versions_are_pruned(self):
        for version in ('0.1.0', '0.2.0'):
            self.releases.publish(version)
            self.run_skillarium('install' if version == '0.1.0' else 'self-update')
        self.assertIn('ROLLED BACK: Skillarium 0.2.0 -> 0.1.0', self.run_skillarium('rollback'))
        self.assertEqual(self.version(), '0.1.0')
        self.assertIn('rolled back', self.run_skillarium('self-update').lower())
        self.assertEqual(self.version(), '0.1.0')
        for version in ('0.3.0', '0.4.0', '0.5.0'):
            self.releases.publish(version)
            self.run_skillarium('self-update')
        self.assertEqual(self.version(), '0.5.0')
        # The current version and the two most recently active ones stay.
        self.assertEqual(self.installed(), ['0.3.0', '0.4.0', '0.5.0'])

    def test_beta_channel_takes_pre_releases(self):
        self.releases.publish('0.1.0')
        self.run_skillarium('install')
        self.releases.publish('0.2.0-beta.1', prerelease=True)
        self.assertIn('Up to date', self.run_skillarium('self-update'))
        self.assertIn('UPDATED: Skillarium 0.1.0 -> 0.2.0-beta.1', self.run_skillarium('self-update', '--channel', 'beta'))
        self.assertIn('(beta)', self.run_skillarium('status'))
        self.releases.publish('0.2.0')
        # 0.2.0 is newer than its own pre-release.
        self.assertIn('UPDATED: Skillarium 0.2.0-beta.1 -> 0.2.0', self.run_skillarium('self-update'))

    def test_release_that_cannot_read_the_library_is_skipped(self):
        self.releases.publish('0.1.0')
        env = self.configure_library(2)
        output = self.run_skillarium('install', expected=1, env=env)
        self.assertIn('supports library format up to 1; the library has 2', output)
        self.assertFalse((self.home / 'current').exists())
        status = self.run_skillarium('install')
        self.assertIn('INSTALLED', status)
        self.assertIn('mode: read-only', self.run_skillarium('status', env=env))


@unittest.skipUnless(WINDOWS and POWERSHELL, 'Windows PowerShell unavailable')
class PowerShellEngineInstallTests(EngineInstall, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.command = self.home / 'bin' / 'skillarium.cmd'

    def run_skillarium(self, *args, expected=0, env=None):
        arguments = [{'--channel': '-Channel', '--version': '-Version'}.get(item, item) for item in args]
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), APPDATA=str(self.root / 'appdata'),
                           **self.releases.env(), **(env or {}))
        if not env:
            environment.pop('SKILLARIUM_LIBRARY', None)
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(ROOT / 'scripts' / 'skillarium.ps1'), *arguments],
                                env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output

    def test_bootstrap_scriptblock_passes_role_arguments(self):
        self.releases.publish('0.1.0')
        # The published bootstrap runs from memory, as irm | scriptblock does.
        script = (f"& ([scriptblock]::Create([IO.File]::ReadAllText('{ROOT / 'scripts' / 'skillarium.ps1'}'))) install")
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), APPDATA=str(self.root / 'appdata'),
                           **self.releases.env())
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-Command', script], env=environment, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.version(), '0.1.0')


@unittest.skipUnless(HAS_BASH_CLIENT, 'Linux client tools unavailable')
class BashEngineInstallTests(EngineInstall, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.command = self.root / 'bin' / 'skillarium'

    def run_skillarium(self, *args, expected=0, env=None):
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), SKILLARIUM_BIN=str(self.root / 'bin'),
                           XDG_CONFIG_HOME=str(self.root / 'config'), **self.releases.env(), **(env or {}))
        if not env:
            environment.pop('SKILLARIUM_LIBRARY', None)
        result = subprocess.run(['bash', str(ROOT / 'scripts' / 'skillarium.sh'), *args], env=environment,
                                capture_output=True, text=True, encoding='utf-8')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output

    def test_bootstrap_from_a_pipe(self):
        self.releases.publish('0.1.0')
        script = (ROOT / 'scripts' / 'skillarium.sh').read_bytes()
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), SKILLARIUM_BIN=str(self.root / 'bin'),
                           **self.releases.env())
        result = subprocess.run(['bash', '-s', '--', 'install'], input=script, env=environment, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
        self.assertTrue((self.home / 'current').is_symlink())
        installed = subprocess.run([str(self.command), 'status'], env=environment, capture_output=True, text=True)
        self.assertIn('Skillarium: 0.1.0', installed.stdout)


if __name__ == '__main__':
    unittest.main()
