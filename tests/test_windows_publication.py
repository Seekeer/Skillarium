"""Linux-side publication guards, using disposable catalogs and profiles."""
import os
from pathlib import Path
import shutil
import subprocess
import unittest

import test_sync

ROOT = Path(__file__).resolve().parents[1]


class WindowsPublicationTests(unittest.TestCase):
    def setUp(self):
        self.f = test_sync.RegistryTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def manage(self, *args):
        return subprocess.run(['python3', str(ROOT / 'server/manage.py'), '--config', str(self.f.config), *args],
                              env=self.f.env, capture_output=True, text=True)

    def test_local_import_rejects_windows_paths_before_changing_registry(self):
        f = self.f
        original = (f.seed / 'sources.json').read_bytes()
        for paths in [['AUX.txt'], ['scripts/COM1/run.ps1'], ['Helper.py', 'helper.py'],
                      ['Docs/a', 'docs/b'], ['unsafe?.md'], ['.Git/config']]:
            with self.subTest(paths=paths):
                source = f.root / 'local-import'
                source.mkdir()
                (source / 'SKILL.md').write_text('---\nname: my-local\ndescription: Test\n---\nLocal')
                for name in paths:
                    path = source / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('test')
                result = self.manage('register-local', 'my-local', '--directory', str(source), '--publish')
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn('Windows', result.stderr)
                self.assertEqual((f.seed / 'sources.json').read_bytes(), original)
                self.assertFalse((f.seed / 'skills/my-local').exists())
                self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')
                shutil.rmtree(source)

    def test_external_update_is_rejected_before_replacing_payload(self):
        f = self.f
        registry = (f.seed / 'sources.json').read_bytes()
        payload = (f.seed / 'skills/first-skill/SKILL.md').read_bytes()
        for names in [['NUL.txt'], ['Helper.py', 'helper.py'], ['Docs/a', 'docs/b']]:
            with self.subTest(names=names):
                for name in names:
                    path = f.upstream / 'skills/first-skill' / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('incompatible')
                f.commit(f.upstream, 'Windows-incompatible update')
                output = f.run_cli(f.seed, 'check', '--apply-auto', expected=1)
                self.assertIn('Windows', output)
                self.assertEqual((f.seed / 'sources.json').read_bytes(), registry)
                self.assertEqual((f.seed / 'skills/first-skill/SKILL.md').read_bytes(), payload)
                self.assertEqual(f.git(f.seed, 'status', '--porcelain'), '')
                for name in names:
                    (f.upstream / 'skills/first-skill' / name).unlink()
                f.commit(f.upstream, 'Remove incompatible files')

    def test_retry_cannot_publish_an_incompatible_existing_commit(self):
        f = self.f
        remote = f.git(f.seed, 'rev-parse', '@{upstream}')
        (f.seed / 'skills/first-skill/AUX.txt').write_text('unsafe')
        f.commit(f.seed, 'Automation commit\n\nAgent-Skills-Updater: 1')
        result = self.manage('deploy-client', '--publish')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Windows', result.stderr)
        self.assertEqual(f.git(f.seed, 'rev-parse', '@{upstream}'), remote)

    def test_linux_client_does_not_publish_incompatible_edits(self):
        f = self.f
        f.sync()
        remote = f.git(f.consumer, 'rev-parse', '@{upstream}')
        folder = f.consumer / 'skills/first-skill'
        for names in [['AUX.txt'], ['unsafe?.md'], ['Helper.py', 'helper.py'],
                      ['Docs/a', 'docs/b'], ['Справка/a', 'справка/b']]:
            with self.subTest(names=names):
                for name in names:
                    path = folder / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text('local edit')
                self.assertIn('Windows', f.sync(expected=1))
                self.assertEqual(f.git(f.consumer, 'rev-parse', 'HEAD'), remote)
                self.assertEqual(f.git(f.consumer, 'diff', '--cached', '--name-only'), '')
                for name in names:
                    (folder / name).unlink()

    @unittest.skipUnless(os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'), 'PowerShell unavailable')
    def test_powershell_client_checks_windows_paths_before_publication(self):
        f = self.f
        folder = f.consumer / 'skills/first-skill'
        (folder / 'Helper.py').write_text('one')
        (folder / 'helper.py').write_text('two')
        script = f.root / 'validate.ps1'
        script.write_text('''$ErrorActionPreference = 'Stop'
$repo = $env:TEST_REPO
function Invoke-Git {
    param([string[]]$GitArgs)
    $output = & git -C $repo @GitArgs
    if ($LASTEXITCODE -ne 0) { throw 'Git failed' }
    return $output
}
. $env:TEST_HELPER
$rejected = $false
try { Publish-LocalChanges } catch {
    if ($_.Exception.Message -notmatch 'Windows path collision') { throw }
    $rejected = $true
}
if (-not $rejected) { throw 'Incompatible paths were accepted' }
if (Invoke-Git -GitArgs @('diff', '--cached', '--name-only')) { throw 'Index changed' }
''')
        result = subprocess.run([os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'),
                                 '-NoProfile', '-NonInteractive', '-File', str(script)],
                                env=dict(f.env, TEST_REPO=str(f.consumer),
                                         TEST_HELPER=str(ROOT / 'client/scripts/client.ps1')),
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
