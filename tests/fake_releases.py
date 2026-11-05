"""Local stand-in for GitHub Releases: the download URLs and the releases API.

Serves  /releases/latest/download/<file>   (newest stable release)
        /releases/download/v<version>/<file>
        /api/releases                       (newest first, pre-releases included)
Releases are built from the working tree with release/build_engine.py, so the
tests run the engine as it is now. Faults cut a download short.
"""
import json
from pathlib import Path
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'release'))
import build_engine  # noqa: E402


def worktree_files():
    """Engine files as build_engine selects them, read from the working tree."""
    rows = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-s', '-z'], check=True,
                          capture_output=True).stdout.split(b'\0')
    tracked = {}
    for row in rows:
        if row:
            meta, name = row.split(b'\t', 1)
            tracked[name.decode()] = 0o755 if meta.split()[0] == b'100755' else 0o644
    # New files not yet committed belong to the engine as well.
    others = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '-o', '--exclude-standard', '-z'], check=True,
                            capture_output=True).stdout.split(b'\0')
    for name in others:
        if name:
            tracked.setdefault(name.decode(), 0o755 if name.endswith(b'.sh') else 0o644)
    files = []
    for name, mode in sorted(tracked.items()):
        if any(name == item or (item.endswith('/') and name.startswith(item)) for item in build_engine.ENGINE_PATHS):
            path = ROOT / name
            if path.is_file():
                data = path.read_bytes()
                # Text is stored with LF in the repository, as the release archives carry it.
                if b'\0' not in data:
                    data = data.replace(b'\r\n', b'\n')
                files.append((name, mode, data))
    return files


class FakeReleases:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.releases = []  # (version, prerelease), oldest first
        self.faults = {}
        self.requests = []
        self.files = worktree_files()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        self.releases_url = self.base + '/releases'
        self.api_url = self.base + '/api'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def env(self):
        return {'SKILLARIUM_RELEASES': self.releases_url, 'SKILLARIUM_API': self.api_url}

    def publish(self, version, prerelease=False, corrupt=False, app=False, corrupt_app=False):
        out = self.folder / ('v' + version)
        apps = []
        if app or corrupt_app:
            # Stand-ins for the installers; the tests check download and verification.
            out.mkdir(parents=True, exist_ok=True)
            for name in (f'Skillarium-Setup-{version}.exe', f'Skillarium-{version}.AppImage'):
                (out / name).write_bytes(b'installer of ' + version.encode())
                apps.append(out / name)
        feed = build_engine.build(version, out, self.releases_url + '/download', apps=apps, files=self.files)
        if corrupt_app:
            for path in apps:
                path.write_bytes(b'tampered installer')
        if corrupt:
            for item in feed['engine'].values():
                path = out / item['file']
                data = bytearray(path.read_bytes())
                data[len(data) // 2] ^= 0xFF
                path.write_bytes(bytes(data))
        self.releases.append((version, prerelease))
        return feed

    def latest_stable(self):
        stable = [version for version, prerelease in self.releases if not prerelease]
        return stable[-1] if stable else None

    def _handler(self):
        releases = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def send(self, status, data=b'', content_type='application/octet-stream', declared=None):
                self.send_response(status)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Length', str(len(data) if declared is None else declared))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                path = self.path.split('?', 1)[0]
                releases.requests.append(path)
                if path == '/api/releases':
                    items = [dict(tag_name='v' + version, prerelease=prerelease, draft=False)
                             for version, prerelease in reversed(releases.releases)]
                    return self.send(200, json.dumps(items).encode(), 'application/json')
                parts = path.strip('/').split('/')
                if parts[:3] == ['releases', 'latest', 'download'] and len(parts) == 4:
                    version, name = releases.latest_stable(), parts[3]
                elif parts[:2] == ['releases', 'download'] and len(parts) == 4 and parts[2].startswith('v'):
                    version, name = parts[2][1:], parts[3]
                else:
                    return self.send(404, b'not found')
                file = releases.folder / ('v' + str(version)) / name
                if version is None or not file.is_file():
                    return self.send(404, b'not found')
                data = file.read_bytes()
                if releases.faults.get('truncate') and name.startswith('skillarium-engine-'):
                    releases.faults['truncate'] -= 1
                    # The connection drops halfway through the archive.
                    return self.send(200, data[:len(data) // 2], declared=len(data))
                self.send(200, data)

        return Handler
