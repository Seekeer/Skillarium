"""Engine release archives: content, executable bits, determinism and feed."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


class ReleaseTests(unittest.TestCase):
    def build(self, out):
        version = json.loads((ROOT / 'app' / 'package.json').read_text(encoding='utf-8'))['version']
        subprocess.run([sys.executable, str(ROOT / 'release' / 'build_engine.py'), '--version', version,
                        '--out', str(out)], check=True, capture_output=True)
        return version

    def test_archives_feed_and_determinism(self):
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            version = self.build(Path(first))
            self.build(Path(second))
            base = f'skillarium-engine-{version}'
            for name in (base + '.tar.gz', base + '.zip'):
                self.assertEqual((Path(first) / name).read_bytes(), (Path(second) / name).read_bytes(), name)
            with tarfile.open(Path(first) / (base + '.tar.gz')) as archive:
                names = archive.getnames()
                self.assertEqual(archive.extractfile('VERSION').read().decode().strip(), version)
                self.assertTrue(archive.getmember('install.sh').mode & 0o111)
            self.assertIn('scripts/locate.sh', names)
            self.assertIn('server/manage.py', names)
            self.assertIn('client/skills/review-skill-updates/SKILL.md', names)
            self.assertFalse([name for name in names if name.startswith(('app/', 'tests/', 'skills/'))], names)
            with zipfile.ZipFile(Path(first) / (base + '.zip')) as archive:
                self.assertEqual(sorted(archive.namelist()), sorted(names))
            feed = json.loads((Path(first) / 'latest.json').read_text(encoding='utf-8'))
            self.assertEqual(feed['version'], version)
            self.assertEqual(feed['format'], {'min': 1, 'max': 1})
            self.assertEqual((Path(first) / 'skillarium.sh').read_bytes(),
                             subprocess.run(['git', '-C', str(ROOT), 'show', 'HEAD:scripts/skillarium.sh'],
                                            check=True, capture_output=True).stdout)
            self.assertTrue((Path(first) / 'skillarium.ps1').is_file())
            for platform, file in (('linux', base + '.tar.gz'), ('windows', base + '.zip')):
                entry = feed['engine'][platform]
                self.assertEqual(entry['file'], file)
                self.assertTrue(entry['url'].endswith(f'/v{version}/{file}'))
                self.assertEqual(entry['sha512'], hashlib.sha512((Path(first) / file).read_bytes()).hexdigest())

    def test_version_must_match_the_app(self):
        with tempfile.TemporaryDirectory() as out:
            result = subprocess.run([sys.executable, str(ROOT / 'release' / 'build_engine.py'), '--version', '99.0.0',
                                     '--out', out], capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('app/package.json has version', result.stderr)


if __name__ == '__main__':
    unittest.main()
