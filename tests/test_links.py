"""Links copied from a browser become registry sources; the network is replaced by a ref list."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import links  # noqa: E402

REFS = {'refs/heads/main', 'refs/heads/release/2.x', 'refs/tags/v1.0'}


def refs_of(url):
    refs_of.urls.append(url)
    return REFS, 'refs/heads/main'


class LinkTests(unittest.TestCase):
    def setUp(self):
        refs_of.urls = []

    def parse(self, link):
        return links.parse(link, refs_of)

    def test_github_folder_and_skill_file(self):
        expected = dict(kind='git', url='https://github.com/o/r.git', path='skills/pdf', ref='refs/heads/main')
        self.assertEqual(self.parse('https://github.com/o/r/tree/main/skills/pdf'), expected)
        self.assertEqual(self.parse('https://github.com/o/r/blob/main/skills/pdf/SKILL.md'), expected)
        self.assertEqual(self.parse('https://raw.githubusercontent.com/o/r/main/skills/pdf/SKILL.md'), expected)
        self.assertEqual(self.parse('https://raw.githubusercontent.com/o/r/refs/heads/main/skills/pdf/SKILL.md'),
                         expected)

    def test_branch_with_a_slash_and_tags(self):
        self.assertEqual(self.parse('https://github.com/o/r/tree/release/2.x/pdf'),
                         dict(kind='git', url='https://github.com/o/r.git', path='pdf', ref='refs/heads/release/2.x'))
        self.assertEqual(self.parse('https://github.com/o/r/tree/v1.0')['ref'], 'refs/tags/v1.0')

    def test_gitea_and_gitlab_views(self):
        self.assertEqual(self.parse('https://git.example.org/team/skills/src/branch/main/pdf'),
                         dict(kind='git', url='https://git.example.org/team/skills.git', path='pdf', ref='refs/heads/main'))
        self.assertEqual(self.parse('https://git.example.org/team/skills/src/tag/v1.0/pdf/SKILL.md')['ref'],
                         'refs/tags/v1.0')
        self.assertEqual(self.parse('https://gitlab.com/group/sub/r/-/tree/main/pdf'),
                         dict(kind='git', url='https://gitlab.com/group/sub/r.git', path='pdf', ref='refs/heads/main'))

    def test_repository_uses_its_default_branch(self):
        self.assertEqual(self.parse('https://github.com/o/r'),
                         dict(kind='git', url='https://github.com/o/r.git', path='.', ref='refs/heads/main'))
        self.assertEqual(self.parse('https://github.com/o/r.git?tab=readme')['url'], 'https://github.com/o/r.git')

    def test_single_file_elsewhere(self):
        self.assertEqual(self.parse('https://example.org/skills/pdf/SKILL.md'),
                         dict(kind='url', url='https://example.org/skills/pdf/SKILL.md'))
        self.assertEqual(refs_of.urls, [])

    def test_rejected_links_say_why(self):
        for link, words in (('https://github.com/o/r/tree/nope/pdf', 'не найдены'),
                            ('https://git.example.org/o/r/src/commit/0123abcd/pdf', 'коммит'),
                            ('https://github.com/o/r/blob/main/pdf/README.md', 'на файл'),
                            ('http://github.com/o/r', 'https'),
                            ('git@github.com:o/r.git', 'https'),
                            ('https://github.com/o', 'владельца')):
            with self.subTest(link=link):
                with self.assertRaises(ValueError) as caught:
                    self.parse(link)
                self.assertIn(words, str(caught.exception))


if __name__ == '__main__':
    unittest.main()
