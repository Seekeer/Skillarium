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
from fixture import HAS_BASH_CLIENT, POWERSHELL, ROOT, TEST_TASK

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

    def test_update_commands_run_the_installed_engine(self):
        self.releases.publish('0.1.0')
        self.assertIn('not installed', self.run_skillarium('updates', expected=1))
        self.run_skillarium('install')
        library, remote = self.root / 'library', self.root / 'library.git'
        subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
        subprocess.run(['git', 'init', '-q', '-b', 'main', str(library)], check=True)
        shutil.copy2(ROOT / 'templates' / 'library' / 'agent-skills.json', library / 'agent-skills.json')
        (library / 'sources.json').write_text('{"version": 2, "skills": {}}\n', encoding='utf-8')
        for command in (['add', '-A'], ['-c', 'user.name=Test', '-c', 'user.email=test@localhost', 'commit', '-q', '-m', 'Library'],
                        ['remote', 'add', 'origin', str(remote)], ['push', '-q', 'origin', 'main']):
            subprocess.run(['git', '-C', str(library), *command], check=True)
        env = {'SKILLARIUM_LIBRARY': str(library)}
        self.assertIn('No pending external updates', self.run_skillarium('updates', env=env))
        self.assertIn('no-such-skill', self.run_skillarium('review', 'no-such-skill', env=env, expected=1))
        wrong = self.run_skillarium('approve', 'only-a-name', env=env, expected=None)
        self.assertIn('approve', wrong.lower())
        self.assertNotIn('only-a-name', wrong)

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

    def run_skillarium(self, *args, expected=0, env=None, script=None):
        arguments = [{'--channel': '-Channel', '--version': '-Version'}.get(item, item) for item in args]
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), APPDATA=str(self.root / 'appdata'),
                           SKILLARIUM_TASK_NAME=TEST_TASK, SKILLARIUM_UPDATE_TASK=TEST_TASK + '-Update',
                           **self.releases.env(), **(env or {}))
        if not env:
            environment.pop('SKILLARIUM_LIBRARY', None)
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(script or ROOT / 'scripts' / 'skillarium.ps1'), *arguments],
                                env=environment, capture_output=True, text=True, encoding='utf-8', errors='replace')
        output = result.stdout + result.stderr
        if expected is not None:
            self.assertEqual(result.returncode, expected, output)
        return output

    def run_installed(self, *args):
        """The installed engine, as its scheduled task runs it (current\\scripts\\skillarium.ps1)."""
        return self.run_skillarium(*args, script=self.home / 'current' / 'scripts' / 'skillarium.ps1')

    def scheduled_task(self, task):
        """Action and first trigger start of a task; empty when there is none."""
        script = (f"$t = Get-ScheduledTask -TaskName '{task}' -ErrorAction SilentlyContinue; "
                  "if ($t) { $t.Actions[0].Execute + '|' + $t.Actions[0].Arguments + '|' + $t.Triggers[0].StartBoundary }")
        return subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', script],
                              capture_output=True, text=True).stdout.strip()

    def assert_task(self, task, hidden, start):
        execute, arguments, boundary = self.scheduled_task(task).split('|', 2)
        if hidden:
            self.assertTrue(execute.lower().endswith('\\system32\\wscript.exe'), execute)
            self.assertIn(str(self.home / 'current' / 'scripts' / 'update-hidden.vbs'), arguments)
        else:
            self.assertTrue(execute.lower().endswith('\\powershell.exe'), execute)
            self.assertIn(str(self.home / 'current' / 'scripts' / 'skillarium.ps1'), arguments)
        # The schedule is kept whatever the action becomes.
        self.assertEqual(boundary, start)

    def test_self_update_task_moves_to_the_hidden_launcher_from_a_real_older_engine(self):
        task = TEST_TASK + '-Update'
        remove = f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue"
        self.addCleanup(subprocess.run, [POWERSHELL, '-NoProfile', '-NonInteractive', '-Command', remove], capture_output=True)
        # 0.1.0 is Skillarium 0.1.4 as released: powershell.exe -WindowStyle Hidden runs the task (it
        # flashes a console), there is no launcher and nothing moves the task.
        old_script = (ROOT / 'tests' / 'fixtures' / 'skillarium-0.1.4.ps1').read_bytes().replace(b'\r\n', b'\n')
        files = self.releases.files
        self.releases.files = [(name, mode, old_script if name == 'scripts/skillarium.ps1' else data)
                               for name, mode, data in files if name != 'scripts/update-hidden.vbs']
        self.releases.publish('0.1.0')
        self.releases.files = files
        self.run_skillarium('install')
        self.run_installed('enable-updates')
        start = self.scheduled_task(task).split('|', 2)[2]
        self.assert_task(task, hidden=False, start=start)
        # The old engine installs the new one but keeps running its own code: the task moves on
        # the next run, which is the new engine's.
        self.releases.publish('0.1.1')
        output = self.run_installed('self-update')
        self.assertIn('UPDATED: Skillarium 0.1.0 -> 0.1.1', output)
        self.assertNotIn('now runs hidden', output)
        self.assert_task(task, hidden=False, start=start)
        self.assertIn('now runs hidden', self.run_installed('self-update'))
        self.assert_task(task, hidden=True, start=start)
        self.assertNotIn('now runs hidden', self.run_installed('self-update'))
        # Rolling back to the engine without the launcher keeps the task able to run.
        self.assertIn('runs PowerShell directly', self.run_installed('rollback'))
        self.assertEqual(self.version(), '0.1.0')
        self.assert_task(task, hidden=False, start=start)
        # Back on a newer release, the same two-step move happens again.
        self.releases.publish('0.1.2')
        self.assertIn('UPDATED: Skillarium 0.1.0 -> 0.1.2', self.run_installed('self-update'))
        self.assert_task(task, hidden=False, start=start)
        self.assertIn('now runs hidden', self.run_installed('self-update'))
        self.assert_task(task, hidden=True, start=start)
        # A new registration runs hidden from the start.
        self.run_installed('disable-updates')
        self.assertEqual(self.scheduled_task(task), '')
        self.run_installed('enable-updates')
        self.assertIn('wscript.exe|//B //NoLogo', self.scheduled_task(task))
        # The launcher itself runs the installed engine's self-update with its exit code.
        launcher = self.home / 'current' / 'scripts' / 'update-hidden.vbs'
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), APPDATA=str(self.root / 'appdata'),
                           SKILLARIUM_UPDATE_TASK=task, **self.releases.env())
        wscript = os.path.join(os.environ['SystemRoot'], 'System32', 'wscript.exe')
        self.releases.publish('0.2.0')
        result = subprocess.run([wscript, '//B', '//NoLogo', str(launcher)], env=environment, timeout=180)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.version(), '0.2.0')
        # A failing run reaches Task Scheduler as a failure.
        self.releases.publish('0.3.0', corrupt=True)
        failed = subprocess.run([wscript, '//B', '//NoLogo', str(launcher)], env=environment, timeout=180)
        self.assertNotEqual(failed.returncode, 0)
        self.assertEqual(self.version(), '0.2.0')

    def test_bootstrap_scriptblock_passes_role_arguments(self):
        self.releases.publish('0.1.0')
        # The published bootstrap runs from memory, as irm | scriptblock does; neither success nor
        # failure may close the user's PowerShell window.
        block = f"([scriptblock]::Create([IO.File]::ReadAllText('{ROOT / 'scripts' / 'skillarium.ps1'}')))"
        script = (f"& {block} install; \"after install $LASTEXITCODE\"; "
                  f"& {block} rollback; \"after failure $LASTEXITCODE\"")
        environment = dict(os.environ, SKILLARIUM_HOME=str(self.home), APPDATA=str(self.root / 'appdata'),
                           **self.releases.env())
        result = subprocess.run([POWERSHELL, '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-Command', script], env=environment, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        self.assertIn('after install 0', result.stdout, result.stdout + result.stderr)
        self.assertIn('after failure 1', result.stdout, result.stdout + result.stderr)
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
