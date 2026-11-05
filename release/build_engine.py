#!/usr/bin/env python3
"""Build the engine archives and the update feed of one Skillarium release.

The engine is the tracked files under ENGINE_PATHS at HEAD. Output (in --out):
  skillarium-engine-<version>.tar.gz  (Linux; keeps executable bits)
  skillarium-engine-<version>.zip     (Windows)
  skillarium.sh, skillarium.ps1       (bootstrap: releases/latest/download/...)
  <file>.sha512                       (sha512sum format)
  latest.json                         (feed read by `skillarium self-update`)
App installers built before (--app) are listed in latest.json as well.

Archives have no top-level folder: the installer unpacks them into
versions/<version>/. A VERSION file is added at the root.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[1]
ENGINE_PATHS = ('install.sh', 'install.ps1', 'projects.sh', 'projects.ps1', 'scripts/', 'server/',
                'templates/', '.codex/skills/', 'README.md', '.gitattributes')
# Oldest library format this engine still reads (and could migrate from).
MIN_FORMAT = 1
DEFAULT_BASE_URL = 'https://github.com/Seekeer/Skillarium/releases/download'
BOOTSTRAP = ('scripts/skillarium.sh', 'scripts/skillarium.ps1')
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?')
# Fixed timestamp so the same commit always gives the same archives.
EPOCH = 315532800  # 1980-01-01, the earliest zip timestamp


def git(*args):
    return subprocess.run(['git', '-C', str(ROOT), *args], check=True, capture_output=True).stdout


def engine_files():
    """(path, mode, blob bytes) of the engine files at HEAD, sorted."""
    rows = git('ls-tree', '-r', '-z', 'HEAD').split(b'\0')
    files = []
    for row in rows:
        if not row:
            continue
        meta, path = row.split(b'\t', 1)
        mode, kind, obj = meta.split()
        name = path.decode('utf-8')
        if kind != b'blob' or not any(name == item or (item.endswith('/') and name.startswith(item))
                                      for item in ENGINE_PATHS):
            continue
        if mode == b'120000':
            raise SystemExit(f'Symlinks are not supported in the engine: {name}')
        files.append((name, 0o755 if mode == b'100755' else 0o644, git('cat-file', 'blob', obj.decode())))
    return sorted(files)


def supported_format(files):
    """The library format of the archived files; all three engine copies must agree."""
    text = {name: data.decode('utf-8') for name, _, data in files}
    value = int(re.search(r'^SUPPORTED_FORMAT = (\d+)$', text['server/hosting.py'], re.M).group(1))
    for script, pattern in (('scripts/locate.sh', r'^supported_format=(\d+)$'),
                            ('scripts/locate.ps1', r'^\$supportedFormat = (\d+)\s*$')):
        found = int(re.search(pattern, text[script], re.M).group(1))
        if found != value:
            raise SystemExit(f'Supported format differs: server/hosting.py {value}, {script} {found}')
    return value


def build_tar(files, target):
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode='w', format=tarfile.PAX_FORMAT) as archive:
        for name, mode, data in files:
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.mtime = len(data), mode, EPOCH
            info.uid = info.gid = 0
            info.uname = info.gname = ''
            archive.addfile(info, io.BytesIO(data))
    with target.open('wb') as stream, gzip.GzipFile(fileobj=stream, mode='wb', mtime=EPOCH, filename='') as packed:
        packed.write(raw.getvalue())


def build_zip(files, target):
    with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
        for name, mode, data in files:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.external_attr = (0o100000 | mode) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)


def sha512(path):
    return hashlib.sha512(path.read_bytes()).hexdigest()


def entry(path, url):
    digest = sha512(path)
    (path.parent / (path.name + '.sha512')).write_text(f'{digest}  {path.name}\n', encoding='utf-8')
    return {'file': path.name, 'url': f'{url}/{path.name}', 'sha512': digest, 'size': path.stat().st_size}


def app_platform(path):
    if path.suffix == '.exe':
        return 'windows'
    if path.suffix == '.AppImage':
        return 'linux'
    raise SystemExit(f'Unknown app installer: {path.name}')


def build(version, out, base_url=DEFAULT_BASE_URL, apps=(), files=None):
    """Write the release files of one version into out; returns the feed.

    files (path, mode, bytes) default to the engine at HEAD; apps are app
    installers already in out (Skillarium-Setup-<v>.exe, Skillarium-<v>.AppImage).
    """
    files = sorted((engine_files() if files is None else list(files)) +
                   [('VERSION', 0o644, (version + '\n').encode())])
    out.mkdir(parents=True, exist_ok=True)
    base = f'skillarium-engine-{version}'
    archives = {'linux': out / f'{base}.tar.gz', 'windows': out / f'{base}.zip'}
    build_tar(files, archives['linux'])
    build_zip(files, archives['windows'])
    # The bootstrap scripts are published on their own: releases/latest/download/skillarium.sh.
    blobs = {name: data for name, _, data in files}
    for script in BOOTSTRAP:
        (out / Path(script).name).write_bytes(blobs[script])

    url = f'{base_url.rstrip("/")}/v{version}'
    feed = {
        'version': version,
        'channel': 'beta' if '-' in version else 'stable',
        'format': {'min': MIN_FORMAT, 'max': supported_format(files)},
        'engine': {platform: entry(path, url) for platform, path in archives.items()},
        'app': {},
    }
    for app in apps:
        app = out / Path(app).name
        feed['app'][app_platform(app)] = entry(app, url)
    (out / 'latest.json').write_text(json.dumps(feed, indent=2) + '\n', encoding='utf-8')
    return feed


def stage(version, folder):
    """The engine as plain files (the app bundles it as resources/engine)."""
    for name, mode, data in sorted(engine_files() + [('VERSION', 0o644, (version + '\n').encode())]):
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--version', required=True, help='Release version without the leading v, e.g. 1.2.0')
    parser.add_argument('--out', type=Path)
    parser.add_argument('--stage', type=Path, help='Write the engine files into this folder instead (app bundle)')
    parser.add_argument('--base-url', default=DEFAULT_BASE_URL,
                        help='Download prefix; the tag folder v<version> is appended')
    parser.add_argument('--app', action='append', default=[], type=Path,
                        help='App installer in --out to list in latest.json (repeatable)')
    args = parser.parse_args()
    if not VERSION.fullmatch(args.version):
        parser.error('Version must look like 1.2.3 or 1.2.3-beta.1')
    package = json.loads((ROOT / 'app' / 'package.json').read_text(encoding='utf-8'))
    if package['version'] != args.version:
        parser.error(f'app/package.json has version {package["version"]}, not {args.version}')
    if args.stage:
        stage(args.version, args.stage)
        print(f'Engine {args.version} staged in {args.stage}')
        return 0
    if not args.out:
        parser.error('--out or --stage is required')
    feed = build(args.version, args.out, args.base_url, args.app)
    print(f'Skillarium {args.version}: library formats {feed["format"]["min"]}-{feed["format"]["max"]}')
    for item in list(feed['engine'].values()) + list(feed['app'].values()):
        print(f'  {item["file"]}  {item["size"]} bytes')
    return 0


if __name__ == '__main__':
    sys.exit(main())
