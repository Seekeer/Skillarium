"""An extracted skill keeps the source bytes whatever the machine's line-ending settings are."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import skills  # noqa: E402

TEXT = b'---\nname: lf-skill\ndescription: Test skill\n---\nline one\nline two\n'


class ExtractLineEndingTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        source = self.root / 'source'
        (source / 'skills' / 'lf-skill').mkdir(parents=True)
        (source / 'skills' / 'lf-skill' / 'SKILL.md').write_bytes(TEXT)
        # text=auto makes Git treat the file as text, so core.eol=crlf would also apply.
        (source / '.gitattributes').write_bytes(b'* text=auto\n')
        env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=str(self.root / 'none'),
                   GIT_AUTHOR_NAME='T', GIT_AUTHOR_EMAIL='t@example.invalid',
                   GIT_COMMITTER_NAME='T', GIT_COMMITTER_EMAIL='t@example.invalid')
        for args in (('init', '-q', '-b', 'main'), ('-c', 'core.autocrlf=false', 'add', '-A'),
                     ('commit', '-q', '-m', 'Skill')):
            subprocess.run(['git', '-C', str(source), *args], env=env, check=True, capture_output=True)
        self.revision = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], env=env, check=True,
                                       capture_output=True, text=True).stdout.strip()
        self.source = dict(kind='git', url=source.as_uri(), path='skills/lf-skill', ref='refs/heads/main')
        # A Windows machine: Git for Windows ships core.autocrlf=true in its system config.
        self.env = dict(env, LOCALAPPDATA=str(self.root / 'cache'), XDG_CACHE_HOME=str(self.root / 'cache'),
                        APPDATA=str(self.root / 'roaming'), XDG_CONFIG_HOME=str(self.root / 'config'),
                        GIT_CONFIG_COUNT='2', GIT_CONFIG_KEY_0='core.autocrlf', GIT_CONFIG_VALUE_0='true',
                        GIT_CONFIG_KEY_1='core.eol', GIT_CONFIG_VALUE_1='crlf')

    def test_windows_line_ending_settings_do_not_change_the_skill(self):
        destination = self.root / 'out'
        destination.mkdir()
        with mock.patch.dict(os.environ, self.env, clear=True):
            skills.extract_git(self.source, self.revision, destination)
        self.assertEqual((destination / 'SKILL.md').read_bytes(), TEXT)


if __name__ == '__main__':
    unittest.main()
