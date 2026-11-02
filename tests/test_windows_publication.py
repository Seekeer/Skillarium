"""Own-skill import and Windows path guards before anything is published."""
import os
import shutil
import subprocess
import unittest

from fixture import HAS_BASH_CLIENT, Library, POWERSHELL

CAN_SYNC = HAS_BASH_CLIENT or (os.name == 'nt' and bool(POWERSHELL))


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.lib = Library(self)
        self.lib.register()
        self.client = self.lib.clone('client', 'laptop')

    def folder(self, name='Папка с другим именем'):
        folder = self.lib.root / name
        folder.mkdir()
        (folder / 'SKILL.md').write_text('---\nname: my-local\ndescription: Our own script\n---\nRun scripts/run.sh', newline='\n')
        (folder / 'scripts').mkdir()
        (folder / 'scripts/run.sh').write_bytes(b'#!/bin/sh\necho local\n')
        (folder / 'binary.dat').write_bytes(b'\x00\xff\r\n')
        return folder

    def test_dry_run_is_read_only_and_works_offline(self):
        lib = self.lib
        source = self.folder()
        head = lib.git(self.client, 'rev-parse', 'HEAD')
        lib.remote.rename(lib.root / 'offline.git')
        output = lib.manage('import-local', str(source), '--dry-run', repo=self.client)
        self.assertIn('Проверка пройдена: my-local', output)
        self.assertIn('без изменений', output)
        self.assertFalse((self.client / 'skills/my-local').exists())
        self.assertEqual(lib.git(self.client, 'rev-parse', 'HEAD'), head)

    def test_invalid_folders_and_existing_names_are_refused_before_copying(self):
        lib = self.lib
        source = self.folder()
        for text, error in [('No frontmatter', 'Missing SKILL.md frontmatter'),
                            ('---\nname: ../unsafe\ndescription: Example\n---\n', 'Invalid skill name'),
                            ('---\nname: my-local\n---\n', 'Missing description'),
                            ('---\nname: first-skill\ndescription: Example\n---\n', 'already registered')]:
            with self.subTest(error=error):
                (source / 'SKILL.md').write_text(text)
                self.assertIn(error, lib.manage('import-local', str(source), repo=self.client, expected=1))
                self.assertFalse((self.client / 'skills/my-local').exists())
        self.assertEqual(lib.git(self.client, 'status', '--porcelain'), '')

    @unittest.skipIf(os.name == 'nt', 'Such names cannot be created on Windows file systems')
    def test_windows_incompatible_import_is_refused(self):
        lib = self.lib
        for names in [['scripts/x?.md'], ['Docs/a', 'docs/b'], ['AUX.txt'], ['trailing./x']]:
            with self.subTest(names=names):
                source = self.folder('incompatible')
                try:
                    for name in names:
                        path = source / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text('test')
                except OSError:
                    shutil.rmtree(source)
                    continue
                self.assertIn('Windows', lib.manage('import-local', str(source), repo=self.client, expected=1))
                self.assertFalse((self.client / 'skills/my-local').exists())
                shutil.rmtree(source)

    @unittest.skipUnless(CAN_SYNC, 'No native client for this platform')
    def test_import_copies_and_publishes_through_the_client(self):
        lib = self.lib
        source = self.folder()
        (source / '.gitignore').write_text('scratch.tmp\n')
        (source / 'scratch.tmp').write_text('local scratch file')
        output = lib.manage('import-local', str(source), repo=self.client)
        self.assertIn('Опубликован коммит', output)
        self.assertIn('scratch.tmp', output)
        files = lib.git(lib.remote, 'ls-tree', '-r', '--name-only', 'main')
        self.assertIn('skills/my-local/binary.dat', files)
        self.assertNotIn('scratch.tmp', files)
        self.assertEqual(subprocess.run(['git', '-C', str(lib.remote), 'cat-file', 'blob', 'main:skills/my-local/binary.dat'],
                                        capture_output=True).stdout, b'\x00\xff\r\n')
        self.assertTrue((source / 'SKILL.md').exists())
        lib.manage('maintain', '--apply-auto')
        self.assertEqual(lib.registry()['skills']['my-local']['source']['kind'], 'local')


class ServerPathTests(unittest.TestCase):
    def test_incompatible_external_update_never_reaches_main(self):
        lib = Library(self)
        lib.register()
        head = lib.main()
        target = lib.upstream / 'skills/first-skill'
        # Created in Git objects: such names cannot exist on Windows file systems.
        blob = subprocess.run(['git', '-C', str(lib.upstream), 'hash-object', '-w', '--stdin'], input=b'x',
                              capture_output=True, env=lib.env).stdout.decode().strip()
        lib.git(lib.upstream, '-c', 'core.protectNTFS=false', 'update-index', '--add', '--cacheinfo',
                f'100644,{blob},skills/first-skill/NUL.txt')
        lib.git(lib.upstream, 'commit', '-q', '-m', 'Incompatible name')
        self.assertTrue(target.exists())
        output = lib.manage('maintain', '--apply-auto', expected=1)
        if os.name != 'nt':
            self.assertIn('Windows', output)
        self.assertEqual(lib.main(), head)


if __name__ == '__main__':
    unittest.main()
