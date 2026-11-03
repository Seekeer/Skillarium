"""Portable Windows filename policy; runs on both Linux and Windows."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('windows_path_skills', ROOT / 'server/skills.py')
skills = importlib.util.module_from_spec(spec)
spec.loader.exec_module(skills)


class WindowsPathTests(unittest.TestCase):
    def test_supported_paths(self):
        skills.validate_windows_paths([
            'SKILL.md', 'scripts/run.ps1', 'scripts/run.sh', '.gitignore',
            'docs/my reference.md', 'docs/Справка.md', 'docs/café.md',
            'docs/COM10.txt', 'docs/auxiliary.txt', 'docs/' + 'a' * 255,
        ])

    def test_device_names_with_extensions_and_spaces(self):
        for device in ['CON', 'PRN', 'AUX', 'NUL', 'CONIN$', 'CONOUT$',
                       'COM1', 'COM9', 'LPT1', 'LPT9', 'COM¹', 'LPT²', 'COM³']:
            for path in [device, device.lower() + '.txt', device + ' .txt', device + '/run.ps1']:
                with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Windows-incompatible'):
                    skills.validate_windows_paths([path])

    def test_forbidden_characters_and_aliases(self):
        for path in ['a<b', 'a>b', 'a:b', 'a"b', 'a|b', 'a?b', 'a*b',
                     'a\\b', 'a\x00b', 'a\x1fb', 'trailing.', 'trailing ',
                     '.GIT/config', '.GitAttributes', '../outside', '/absolute', 'a//b']:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Windows-incompatible'):
                skills.validate_windows_paths([path])

    def test_case_collisions_include_parents_and_file_directory_aliases(self):
        for paths in [['Helper.py', 'helper.py'], ['Docs/a.md', 'docs/b.md'],
                      ['Readme', 'README/nested.md'], ['Справка/a', 'справка/b'],
                      ['café.md', 'CAFÉ.md']]:
            with self.subTest(paths=paths), self.assertRaisesRegex(ValueError, 'Windows path collision'):
                skills.validate_windows_paths(paths)

    def test_component_limit_counts_utf16_units(self):
        for path in ['a' * 256, '😀' * 128]:
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, 'Windows-incompatible'):
                skills.validate_windows_paths([path])
        skills.validate_windows_paths(['😀' * 127])


if __name__ == '__main__':
    unittest.main()
