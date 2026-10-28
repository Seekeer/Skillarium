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
        self.sync()
        self.write_skill('first-skill', 'version two')
        self.write_skill('new-skill', 'new')
        self.commit(self.upstream, 'New upstream')
        self.sync()
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.register('new-skill', 'no')
        self.commit(self.seed, 'Register another source')
        self.git(self.seed, 'push')
        self.sync()
        self.sync()
        for catalog in ['.agents', '.claude']:
            self.assertIn('version two', (self.installed(catalog) / 'SKILL.md').read_text())
            self.assertTrue((self.installed(catalog, 'new-skill') / 'SKILL.md').is_file())
            self.assertTrue(self.installed(catalog).is_symlink())
            self.assertNotIn(str(self.consumer), str(self.installed(catalog).resolve()))

    def test_manual_updates_are_reported_until_approved(self):
        data = json.loads((self.seed / 'sources.json').read_text())
        data['skills']['first-skill']['auto_update'] = False
        (self.seed / 'sources.json').write_text(json.dumps(data))
        old = data['skills']['first-skill']['revision']
        self.write_skill('first-skill', 'new version')
        self.commit(self.upstream, 'Changed')
        self.assertIn('available', self.run_cli(self.seed, 'check', '--apply-auto'))
        self.assertEqual(json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']['revision'], old)
        self.assertIn('updated', self.run_cli(self.seed, 'check', '--approve', 'first-skill'))
        self.assertNotEqual(json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']['revision'], old)

    def test_controller_wrapper_checks_and_publishes(self):
        self.write_skill('first-skill', 'daily update')
        self.commit(self.upstream, 'Daily source update')
        result = subprocess.run(['python3', str(ROOT / 'server/manage.py'), '--config', str(self.config), 'maintain', '--apply-auto', '--publish'],
                                env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.sync()
        self.assertIn('daily update', (self.installed() / 'SKILL.md').read_text())

    def test_failed_publication_is_retried_without_new_source_changes(self):
        self.write_skill('first-skill', 'update')
        self.commit(self.upstream, 'Changed')
        self.git(self.seed, 'config', 'remote.origin.pushurl', str(self.root / 'offline.git'))
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish', expected=1)
        committed = self.git(self.seed, 'rev-parse', 'HEAD')
        self.git(self.seed, 'config', '--unset', 'remote.origin.pushurl')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.assertEqual(self.git(self.seed, 'rev-parse', '@{upstream}'), committed)

    def test_content_hash_mismatch_preserves_current_install(self):
        self.sync()
        old = self.installed().resolve()
        data = json.loads((self.consumer / 'sources.json').read_text())
        data['skills']['first-skill']['content_sha256'] = '0' * 64
        (self.consumer / 'sources.json').write_text(json.dumps(data))
        self.sync(expected=1, mode='--link-only')
        self.assertEqual(self.installed().resolve(), old)

    def test_linux_client_never_invokes_python(self):
        shims = self.root / 'no-python'
        shims.mkdir()
        for command in ('python', 'python3', 'py'):
            shim = shims / command
            shim.write_text('#!/bin/sh\necho "Python must not run" >&2\nexit 99\n')
            shim.chmod(0o755)
        self.env['PATH'] = str(shims) + os.pathsep + self.env['PATH']
        self.sync()
        self.assertTrue((self.installed() / 'SKILL.md').is_file())

    def test_nested_unicode_and_binary_content_matches_controller_hash(self):
        folder = self.upstream / 'skills/first-skill'
        for relative in ['a/z.md', 'a-b.md', 'a.txt', '.hidden', 'папка/имя файла.md', '😀/file']:
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\x00\xff\nfile content\r\n')
        self.commit(self.upstream, 'Nested content')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        # Remove controller cache to prove a fresh native download matches it.
        shutil.rmtree(self.home / '.cache')
        self.sync()
        self.assertEqual((self.installed() / 'папка/имя файла.md').read_bytes(), b'\x00\xff\nfile content\r\n')

    def test_native_single_file_download_and_hash_rejection(self):
        source = self.root / 'downloaded.md'
        payload = b'---\nname: file-skill\ndescription: Test\n---\nOriginal'
        source.write_bytes(payload)
        raw = hashlib.sha256(payload).digest()
        digest = hashlib.sha256(b'SKILL.md\0' + raw + b'\0').hexdigest()
        data = {'version': 2, 'skills': {'file-skill': {
            'source': {'kind': 'url', 'url': 'https://example.invalid/SKILL.md'},
            'revision': raw.hex(), 'content_sha256': digest, 'auto_update': False}}}
        registry = self.consumer / 'sources.json'
        registry.write_text(json.dumps(data))
        shims = self.root / 'curl-fixture'
        shims.mkdir()
        curl = shims / 'curl'
        curl.write_text('#!/bin/bash\nwhile (($#)); do\n'
                        'if [[ "$1" == --output ]]; then output=$2; shift 2; else shift; fi\n'
                        'done\ncp -- "$FIXTURE_FILE" "$output"\n')
        curl.chmod(0o755)
        self.env.update(PATH=str(shims) + os.pathsep + self.env['PATH'], FIXTURE_FILE=str(source))
        self.sync(mode='--link-only')
        installed = self.installed(name='file-skill')
        old = installed.resolve()
        self.assertEqual((installed / 'SKILL.md').read_bytes(), payload)
        data['skills']['file-skill']['revision'] = '0' * 64
        data['skills']['file-skill']['content_sha256'] = '0' * 64
        registry.write_text(json.dumps(data))
        self.sync(mode='--link-only', expected=1)
        self.assertEqual(installed.resolve(), old)

    @unittest.skipUnless(os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'), 'PowerShell runtime unavailable')
    def test_powershell_parser_hash_and_pinned_download(self):
        folder = self.upstream / 'skills/first-skill'
        for relative in ['a/z.md', 'a-b.md', 'a.txt', 'папка/имя.md', '😀/file']:
            path = folder / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\x00\xff\r\ncontent')
        self.commit(self.upstream, 'Unicode source')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.git(self.consumer, 'pull', '--ff-only')
        single_file = self.root / 'single.md'
        payload = b'---\nname: file-skill\ndescription: Test\n---\nSingle file'
        single_file.write_bytes(payload)
        file_revision = hashlib.sha256(payload).hexdigest()
        file_digest = hashlib.sha256(b'SKILL.md\0' + bytes.fromhex(file_revision) + b'\0').hexdigest()
        script = self.root / 'test-client.ps1'
        script.write_text('''$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
foreach ($file in @('install.ps1', 'scripts/client.ps1')) {
    $errors = $null; $tokens = $null
    $null = [Management.Automation.Language.Parser]::ParseFile((Join-Path $env:TEST_PROJECT $file), [ref]$tokens, [ref]$errors)
    if ($errors.Count) { throw ($errors -join "`n") }
}
$repo = $env:TEST_REGISTRY
function Invoke-Git {
    param([string[]]$GitArgs, [string]$Directory = $repo)
    $output = & git -C $Directory @GitArgs
    if ($LASTEXITCODE -ne 0) { throw 'Git operation failed.' }
    return $output
}
. (Join-Path $env:TEST_PROJECT 'scripts/client.ps1')
$cache = Join-Path $env:HOME '.powershell-cache'
$registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw | ConvertFrom-Json
$digest = Get-TreeHash -Directory $env:TEST_SKILL
if ($digest -cne $registry.skills.'first-skill'.content_sha256) { throw 'Controller/PowerShell hashes differ.' }
Set-Alias tar.exe tar
function Set-SkillLinks {
    param([string]$Name, [string]$VersionPath)
    $expected = $registry.skills.'first-skill'.content_sha256
    if ($Name -eq 'file-skill') { $expected = $env:TEST_FILE_HASH }
    Assert-Version -Directory $VersionPath -Expected $expected
    Write-Output 'VERIFIED'
}
Install-Registry
function curl.exe {
    $arguments = @($args)
    $index = [Array]::IndexOf($arguments, '--output')
    Copy-Item -LiteralPath $env:TEST_FILE -Destination $arguments[$index + 1]
    $global:LASTEXITCODE = 0
}
$entry = [PSCustomObject]@{
    source = [PSCustomObject]@{kind='url'; url='https://example.invalid/SKILL.md'}
    revision = $env:TEST_FILE_REVISION
    content_sha256 = $env:TEST_FILE_HASH
}
Install-SkillEntry -Name 'file-skill' -Entry $entry
$entry.revision = '0' * 64
$entry.content_sha256 = '0' * 64
$rejected = $false
try { Install-SkillEntry -Name 'file-skill' -Entry $entry }
catch { $rejected = $true }
if (-not $rejected) { throw 'Mismatched file revision was accepted.' }
# Exercise sharing with a real Git remote and a symbolic link on Linux.
$oldHash = [string]$registry.skills.'first-skill'.content_sha256
$oldPath = Join-Path $cache "versions/first-skill/$oldHash"
$discovery = Join-Path $HOME '.agents/skills'
[IO.Directory]::CreateDirectory($discovery) | Out-Null
New-Item -ItemType SymbolicLink -Path (Join-Path $discovery 'first-skill') -Target $oldPath | Out-Null
$file = Join-Path $oldPath 'SKILL.md'
[IO.File]::AppendAllText($file, "`nShared PowerShell edit", (New-Object Text.UTF8Encoding($false)))
[IO.File]::WriteAllBytes((Join-Path $oldPath 'helper.ps1'), [Text.Encoding]::UTF8.GetBytes("Write-Output local`r`n"))
Publish-LocalChanges
$registry = Get-Content -LiteralPath (Join-Path $repo 'sources.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$entry = $registry.skills.'first-skill'
if ($entry.customization.content_sha256 -cne (Get-TreeHash -Directory $oldPath)) { throw 'Shared hash differs.' }
function Set-SkillLinks {
    param([string]$Name, [string]$VersionPath)
    Assert-Version -Directory $VersionPath -Expected $entry.customization.content_sha256
}
Install-SkillEntry -Name 'first-skill' -Entry $entry
$shared = Join-Path $cache ('versions/first-skill/' + $entry.customization.content_sha256)
if ((Get-Content -LiteralPath (Join-Path $shared 'SKILL.md') -Raw) -notmatch 'Shared PowerShell edit') { throw 'Shared payload missing.' }
if ($entry.customization.path -cne 'skills/first-skill') { throw 'Expected stable shared path.' }
$firstCommit = Invoke-Git -GitArgs @('rev-parse', 'HEAD')
Remove-Item -LiteralPath (Join-Path $discovery 'first-skill') -Force
New-Item -ItemType SymbolicLink -Path (Join-Path $discovery 'first-skill') -Target $shared | Out-Null
[IO.File]::AppendAllText((Join-Path $shared 'SKILL.md'), "`nSecond PowerShell edit", (New-Object Text.UTF8Encoding($false)))
Remove-Item -LiteralPath (Join-Path $shared 'helper.ps1')
Publish-LocalChanges
$stable = Join-Path $repo 'skills/first-skill'
if (Test-Path -LiteralPath (Join-Path $stable 'helper.ps1')) { throw 'Deleted file survived.' }
if ((Get-Content -LiteralPath (Join-Path $stable 'SKILL.md') -Raw) -notmatch 'Second PowerShell edit') { throw 'Stable path not updated.' }
Invoke-Git -GitArgs @('show', ($firstCommit + ':skills/first-skill/helper.ps1')) | Out-Null
if (Test-Path -LiteralPath (Join-Path $repo 'customizations')) { throw 'Hash-named copies were created.' }
''')
        env = dict(self.env, TEST_PROJECT=str(ROOT / 'client'), TEST_REGISTRY=str(self.consumer), TEST_SKILL=str(folder),
                   TEST_FILE=str(single_file), TEST_FILE_HASH=file_digest, TEST_FILE_REVISION=file_revision)
        result = subprocess.run([os.environ.get('AGENT_SKILLS_PWSH') or shutil.which('pwsh'),
                                 '-NoProfile', '-NonInteractive', '-File', str(script)],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('VERIFIED', result.stdout)

    def test_unrelated_upstream_commit_does_not_churn_registry(self):
        before = (self.seed / 'sources.json').read_bytes()
        (self.upstream / 'unrelated.txt').write_text('irrelevant')
        self.commit(self.upstream, 'Unrelated change')
        self.assertIn('unchanged', self.run_cli(self.seed, 'check', '--apply-auto'))
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)

    def test_manual_review_cannot_approve_a_different_revision(self):
        old = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']['revision']
        before = (self.seed / 'sources.json').read_bytes()
        self.write_skill('first-skill', 'new unreviewed version')
        self.commit(self.upstream, 'Source moved')
        self.run_cli(self.seed, 'check', '--approve', 'first-skill', '--expected-revision', old, expected=1)
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)

    def test_bad_source_is_reported_without_losing_working_install(self):
        self.sync()
        (self.upstream / 'skills/first-skill/SKILL.md').write_text('invalid')
        self.commit(self.upstream, 'Broken upstream')
        before = (self.seed / 'sources.json').read_bytes()
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())

    def test_conflicting_installation_is_preserved(self):
        existing = self.installed()
        existing.mkdir(parents=True)
        (existing / 'private').write_text('keep')
        self.sync(expected=1)
        self.assertEqual((existing / 'private').read_text(), 'keep')
        self.assertTrue(self.installed('.claude').is_symlink())

    def test_dangling_user_link_is_preserved(self):
        existing = self.installed()
        existing.parent.mkdir(parents=True)
        existing.symlink_to(self.root / 'missing', target_is_directory=True)
        self.sync(expected=1)
        self.assertEqual(existing.readlink(), self.root / 'missing')

    def test_local_cache_edits_are_preserved_during_upgrade(self):
        self.sync()
        old_target = self.installed().resolve()
        (old_target / 'SKILL.md').write_text('local edits')
        self.write_skill('first-skill', 'upstream edits')
        self.commit(self.upstream, 'Update')
        # A different controller cannot see this client's local edits.
        with patch.dict(self.env, XDG_CACHE_HOME=str(self.root / 'separate-controller-cache')):
            self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.sync(expected=1)
        self.assertEqual(self.installed().resolve(), old_target)
        self.assertEqual((old_target / 'SKILL.md').read_text(), 'local edits')

    def test_server_preserves_local_changes_and_stages_incoming(self):
        self.sync()
        local = self.installed().resolve() / 'SKILL.md'
        local.write_text('local edits')
        before = (self.seed / 'sources.json').read_bytes()
        self.write_skill('first-skill', 'incoming upstream')
        self.commit(self.upstream, 'Update')
        for flags in [('--apply-auto',), ('--approve', 'first-skill')]:
            self.assertIn('local-conflict', self.run_cli(self.seed, 'check', *flags, expected=1))
            self.assertEqual((self.seed / 'sources.json').read_bytes(), before)
            self.assertEqual(local.read_text(), 'local edits')
            report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
            self.assertEqual(report['status'], 'local-conflict')
            incoming = Path(report['incoming']['path'])
            self.assertIn('incoming upstream', (incoming / 'SKILL.md').read_text())
        self.assertEqual(len(list(local.parent.parent.iterdir())), 1)

    def test_server_preserves_local_file_when_upstream_deletes_it(self):
        reference = self.upstream / 'skills/first-skill/reference.md'
        reference.write_text('original')
        self.commit(self.upstream, 'Reference')
        self.run_cli(self.seed, 'check', '--apply-auto', '--publish')
        self.sync()
        local = self.installed().resolve() / 'reference.md'
        local.write_text('custom reference')
        reference.unlink()
        self.commit(self.upstream, 'Delete reference')
        before = (self.seed / 'sources.json').read_bytes()
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertEqual(local.read_text(), 'custom reference')
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)

    def test_server_preserves_local_deletion_and_checks_other_skills(self):
        self.write_skill('second-skill', 'original')
        self.commit(self.upstream, 'Second skill')
        self.register('second-skill', 'yes')
        self.sync()
        local = self.installed().resolve() / 'SKILL.md'
        local.unlink()
        self.write_skill('first-skill', 'upstream edit')
        self.write_skill('second-skill', 'updated independently')
        self.commit(self.upstream, 'Update both')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        self.assertFalse(local.exists())
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())
        self.assertEqual(report['first-skill']['status'], 'local-conflict')
        self.assertEqual(report['second-skill']['status'], 'updated')

    def test_server_reports_local_conflict_when_upstream_skill_disappears(self):
        self.sync()
        local = self.installed().resolve() / 'SKILL.md'
        local.write_text('keep local')
        shutil.rmtree(self.upstream / 'skills/first-skill')
        self.commit(self.upstream, 'Remove skill')
        before = (self.seed / 'sources.json').read_bytes()
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.assertEqual(report['status'], 'local-conflict')
        self.assertIn('upstream_error', report)
        self.assertEqual(local.read_text(), 'keep local')
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)

    def test_local_edits_are_shared_with_another_station_and_upstream_conflicts(self):
        self.sync()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nOur shared customization')
        # Binary and CRLF payloads must survive Git round-trips unchanged.
        (self.installed() / 'helper.ps1').write_bytes(b'Write-Output "local"\r\n')
        (self.installed() / 'binary.dat').write_bytes(b'\x00\xff\x01')
        self.sync()
        self.git(self.seed, 'pull', '--ff-only')
        entry = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(entry['customization']['base_sha256'], entry['content_sha256'])
        self.assertEqual(self.installed().resolve().name, entry['customization']['content_sha256'])
        # Fresh station, separate HOME and cache, receiving only the catalog.
        peer = self.root / 'peer'
        self.git(self.root, 'clone', str(self.remote), str(peer))
        peer_home = self.root / 'peer-home'
        peer_home.mkdir()
        env = dict(self.env, HOME=str(peer_home), XDG_CACHE_HOME=str(peer_home / '.cache'))
        result = subprocess.run(['bash', str(peer / 'install.sh'), '--sync-only'], env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        shared = peer_home / '.agents/skills/first-skill'
        self.assertIn('Our shared customization', (shared / 'SKILL.md').read_text())
        self.assertEqual((shared / 'helper.ps1').read_bytes(), b'Write-Output "local"\r\n')
        self.assertEqual((shared / 'binary.dat').read_bytes(), b'\x00\xff\x01')
        # With no upstream change, the server sees and accepts the shared state.
        self.run_cli(self.seed, 'check', '--apply-auto')
        self.write_skill('first-skill', 'new upstream')
        self.commit(self.upstream, 'Upstream update')
        before = (self.seed / 'sources.json').read_bytes()
        self.assertIn('upstream-conflict', self.run_cli(self.seed, 'check', '--apply-auto', expected=1))
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.assertEqual(report['status'], 'upstream-conflict')
        self.assertIn('new upstream', (Path(report['incoming']['path']) / 'SKILL.md').read_text())
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)
        self.assertIn('Our shared customization', (shared / 'SKILL.md').read_text())
        shutil.rmtree(self.upstream / 'skills/first-skill')
        self.commit(self.upstream, 'Delete upstream skill')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        self.assertEqual(report['status'], 'upstream-conflict')
        self.assertIn('upstream_error', report)
        self.assertEqual((self.seed / 'sources.json').read_bytes(), before)

    def test_repeated_edits_use_one_stable_git_path_and_record_deletions(self):
        self.sync()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nFirst shared edit')
        (self.installed() / 'helper.sh').write_text('echo first\n')
        self.sync()
        first = self.git(self.consumer, 'rev-parse', 'HEAD')
        shared = self.consumer / 'skills/first-skill'
        self.assertTrue(shared.is_dir())
        self.assertFalse((self.consumer / 'customizations').exists())
        entry = json.loads((self.consumer / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(entry['customization']['path'], 'skills/first-skill')
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nSecond shared edit')
        (self.installed() / 'helper.sh').unlink()
        self.sync()
        self.assertEqual(list((self.consumer / 'skills').iterdir()), [shared])
        self.assertIn('Second shared edit', (shared / 'SKILL.md').read_text())
        self.assertFalse((shared / 'helper.sh').exists())
        self.assertEqual(self.git(self.consumer, 'show', first + ':skills/first-skill/helper.sh'), 'echo first')
        changes = self.git(self.consumer, 'diff', '--name-status', first, 'HEAD')
        self.assertIn('M\tskills/first-skill/SKILL.md', changes)
        self.assertIn('D\tskills/first-skill/helper.sh', changes)
        self.assertNotIn('customizations/', changes)

    def test_resolve_shared_conflict_and_return_to_original_upstream(self):
        self.sync()
        old = self.installed().resolve()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nShared edit')
        self.sync()
        self.assertFalse(old.exists())
        self.assertTrue(list((self.home / '.cache/agent-skills/local-history/first-skill').iterdir()))
        self.git(self.seed, 'pull', '--ff-only')
        entry = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        def resolve(strategy, revision, shared_hash, expected=0):
            result = subprocess.run(['python3', str(ROOT / 'server/manage.py'), '--config', str(self.config),
                'resolve', 'first-skill', '--revision', revision, '--shared-hash', shared_hash,
                '--strategy', strategy, '--publish'], env=self.env, capture_output=True, text=True)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        # Guard both sides of the review against concurrent changes.
        resolve('keep-shared', entry['revision'], '0' * 64, expected=1)
        self.assertIn('customization', json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill'])
        # Explicitly returning to the original pin must work on the editing station.
        resolve('take-upstream', entry['revision'], entry['customization']['content_sha256'])
        self.sync()
        self.assertEqual(self.installed().resolve(), old)
        self.assertNotIn('Shared edit', (self.installed() / 'SKILL.md').read_text())
        self.assertFalse((self.consumer / 'skills/first-skill').exists())

    def test_keep_shared_resolution_advances_base_without_losing_edits(self):
        self.sync()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nShared edit')
        self.sync()
        self.git(self.seed, 'pull', '--ff-only')
        self.write_skill('first-skill', 'new upstream')
        self.commit(self.upstream, 'New source')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)
        report = json.loads((self.seed / '.git/skill-updates.json').read_text())['first-skill']
        result = subprocess.run(['python3', str(ROOT / 'server/manage.py'), '--config', str(self.config),
            'resolve', 'first-skill', '--revision', report['incoming']['revision'],
            '--shared-hash', report['shared']['content_sha256'], '--strategy', 'keep-shared', '--publish'],
            env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.run_cli(self.seed, 'check', '--apply-auto')
        self.sync()
        self.assertIn('Shared edit', (self.installed() / 'SKILL.md').read_text())
        entry = json.loads((self.seed / 'sources.json').read_text())['skills']['first-skill']
        self.assertEqual(entry['revision'], report['incoming']['revision'])
        self.assertEqual(entry['customization']['base_sha256'], entry['content_sha256'])

    def test_failed_client_publication_is_retried(self):
        self.sync()
        local = self.installed() / 'SKILL.md'
        local.write_text(local.read_text() + '\nShared edit')
        self.git(self.consumer, 'config', 'remote.origin.pushurl', str(self.root / 'offline.git'))
        self.sync(expected=1)
        pending = self.git(self.consumer, 'rev-parse', 'HEAD')
        self.assertIn('Shared edit', (self.installed() / 'SKILL.md').read_text())
        self.git(self.consumer, 'config', '--unset', 'remote.origin.pushurl')
        self.sync()
        self.assertEqual(self.git(self.consumer, 'rev-parse', '@{upstream}'), pending)
        self.assertEqual(self.git(self.consumer, 'status', '--porcelain'), '')

    def test_concurrent_station_edit_does_not_overwrite_shared_version(self):
        self.sync()
        old = self.installed().resolve()
        local = old / 'SKILL.md'
        local.write_text(local.read_text() + '\nLocal pending edit')
        # Simulate a different station publishing another customization first.
        data = json.loads((self.seed / 'sources.json').read_text())
        entry = data['skills']['first-skill']
        incoming = self.root / 'incoming'
        incoming.mkdir()
        (incoming / 'SKILL.md').write_text('---\nname: first-skill\ndescription: Test\n---\nOther station')
        spec = importlib.util.spec_from_file_location('skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        digest = module.tree_hash(incoming)
        destination = self.seed / 'customizations/first-skill' / digest
        shutil.copytree(incoming, destination)
        entry['customization'] = dict(content_sha256=digest, base_sha256=entry['content_sha256'], base_revision=entry['revision'])
        (self.seed / 'sources.json').write_text(json.dumps(data))
        self.commit(self.seed, 'Other station customization')
        self.git(self.seed, 'push')
        self.assertIn('SHARED CONFLICT', self.sync(expected=1))
        self.assertEqual(self.installed().resolve(), old)
        self.assertIn('Local pending edit', local.read_text())
        self.assertEqual(json.loads((self.consumer / 'sources.json').read_text())['skills']['first-skill']['customization']['content_sha256'], digest)

    def test_cache_corruption_is_not_overwritten(self):
        self.sync()
        (self.installed() / 'SKILL.md').write_text('modified')
        self.sync(expected=1, mode='--link-only')
        self.assertEqual((self.installed() / 'SKILL.md').read_text(), 'modified')

    def test_cold_client_downloads_pinned_commit_after_upstream_moves(self):
        shutil.rmtree(self.home / '.cache')
        self.write_skill('first-skill', 'unapproved')
        self.commit(self.upstream, 'New source')
        self.sync()
        self.assertIn('version one', (self.installed() / 'SKILL.md').read_text())

    def test_dirty_worktree_and_divergence_are_preserved(self):
        head = self.git(self.consumer, 'rev-parse', 'HEAD')
        (self.consumer / 'local.txt').write_text('local')
        self.sync(expected=1)
        self.assertEqual(self.git(self.consumer, 'rev-parse', 'HEAD'), head)
        self.commit(self.consumer, 'Local')
        (self.seed / 'other.txt').write_text('remote')
        self.commit(self.seed, 'Other')
        self.git(self.seed, 'push')
        head = self.git(self.consumer, 'rev-parse', 'HEAD')
        self.sync(expected=1)
        self.assertEqual(self.git(self.consumer, 'rev-parse', 'HEAD'), head)

    def test_offline_client_uses_cached_version(self):
        self.sync()
        self.remote.rename(self.root / 'offline.git')
        self.upstream.rename(self.root / 'upstream-offline')
        self.sync(expected=1)
        self.assertTrue((self.installed() / 'SKILL.md').is_file())

    def test_lock_prevents_parallel_changes(self):
        with (self.consumer / '.git/agent-skills-sync.lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertIn('Another sync', self.sync())
            self.assertFalse(self.installed().exists())

    def test_source_path_traversal_and_symlinks_are_rejected(self):
        data = json.loads((self.seed / 'sources.json').read_text())
        data['skills']['first-skill']['source']['path'] = '../escape'
        (self.seed / 'sources.json').write_text(json.dumps(data))
        self.run_cli(self.seed, 'check', expected=1)
        data['skills']['first-skill']['source']['path'] = 'skills/first-skill'
        (self.seed / 'sources.json').write_text(json.dumps(data))
        (self.upstream / 'skills/first-skill/unsafe').symlink_to('/etc/passwd')
        self.commit(self.upstream, 'Unsafe source')
        self.run_cli(self.seed, 'check', '--apply-auto', expected=1)

    def test_single_file_must_match_approved_hash(self):
        spec = importlib.util.spec_from_file_location('skills', ROOT / 'server/skills.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        entry = {'source': {'kind': 'url', 'url': 'https://example.invalid/SKILL.md'}, 'auto_update': True}
        original = b'---\nname: file-skill\ndescription: Test\n---\nOriginal'
        with patch.dict(os.environ, self.env), patch.object(module, 'download', return_value=original):
            folder, revision, digest = module.materialize('file-skill', entry, approved=False)
            entry.update(revision=revision, content_sha256=digest)
            shutil.rmtree(folder)
            with patch.object(module, 'download', return_value=original + b'changed'):
                with self.assertRaisesRegex(ValueError, 'SHA256'):
                    module.materialize('file-skill', entry)


if __name__ == '__main__':
    unittest.main()
