"""Local stand-in for the Gitea pull-request protocol used by clients and server.

It serves the subset of the API the native clients and the server call,
protects main in a bare repository through a pre-receive hook and merges a
pull request only when server/policy.py accepts its current head, as the
required status check and the up-to-date rule of the real hosting would.
Fault injection reproduces lost responses, server errors and access errors.
"""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import policy  # noqa: E402

HOOK = '''#!/bin/sh
# Main is protected: only the hosting merge may update it.
[ "$AGENT_SKILLS_FAKE_HOST" = 1 ] && exit 0
while read old new ref; do
    if [ "$ref" = refs/heads/main ]; then
        echo "main is protected; publish through a pull request" >&2
        exit 1
    fi
done
exit 0
'''


def protect(bare):
    hook = Path(bare) / 'hooks' / 'pre-receive'
    hook.write_text(HOOK, newline='\n')
    hook.chmod(0o755)


class FakeHosting:
    def __init__(self, bare, env, owner='test/library', auto_process=True):
        self.bare = Path(bare)
        self.env = dict(env)
        self.prefix = f'/api/v1/repos/{owner}'
        self.pulls = {}
        self.next_number = 1
        self.auto_process = auto_process
        self.faults = {}
        self.calls = []
        self.lock = threading.RLock()
        self.checks = {}
        handler = self._handler()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api = f'http://127.0.0.1:{self.server.server_port}{self.prefix}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def git(self, *args, cwd=None, check=True, env=None):
        result = subprocess.run(['git', '-C', str(cwd or self.bare), *args], capture_output=True,
                                env=dict(self.env, **(env or {})))
        if check and result.returncode:
            raise RuntimeError(f'git {args} failed: {result.stderr.decode(errors="replace")}')
        return result.stdout.decode().strip()

    def head_of(self, branch):
        result = subprocess.run(['git', '-C', str(self.bare), 'rev-parse', '--verify', '-q', f'refs/heads/{branch}'],
                                capture_output=True, env=self.env)
        return result.stdout.decode().strip() if result.returncode == 0 else None

    def fault(self, name):
        with self.lock:
            count = self.faults.get(name, 0)
            if count:
                self.faults[name] = count - 1
                return True
        return False

    def view(self, pull):
        head = self.head_of(pull['head']) or pull.get('last_head')
        return dict(number=pull['number'], state=pull['state'], merged=pull['merged'],
                    title=pull['title'], body=pull['body'], mergeable=True,
                    html_url=f'http://fake/pulls/{pull["number"]}',
                    head=dict(ref=pull['head'], sha=head), base=dict(ref=pull['base']))

    def open_pulls(self):
        return [pull for pull in self.pulls.values() if pull['state'] == 'open']

    def evaluate(self, pull):
        head = self.head_of(pull['head'])
        base = self.head_of(pull['base'])
        errors, notes = policy.evaluate(self.bare, base, head)
        self.checks[(pull['number'], head)] = errors
        return head, errors

    def process(self):
        """Run checks of scheduled pull requests and merge accepted ones."""
        with self.lock:
            for pull in list(self.open_pulls()):
                scheduled = pull.get('scheduled')
                if not scheduled:
                    continue
                head = self.head_of(pull['head'])
                if head != scheduled:
                    # A new push needs a new merge request for its exact head.
                    pull['scheduled'] = None
                    continue
                head, errors = self.evaluate(pull)
                if errors:
                    pull['rejections'] = errors
                    continue
                self.merge(pull, head)

    def merge(self, pull, head):
        with tempfile.TemporaryDirectory(prefix='fake-host-merge-') as temporary:
            work = Path(temporary) / 'work'
            self.git('clone', '-q', '--branch', pull['base'], str(self.bare), str(work), cwd=temporary)
            env = dict(GIT_AUTHOR_NAME='Gitea', GIT_AUTHOR_EMAIL='gitea@localhost',
                       GIT_COMMITTER_NAME='Gitea', GIT_COMMITTER_EMAIL='gitea@localhost')
            self.git('fetch', '-q', 'origin', head, cwd=work)
            self.git('merge', '-q', '--no-ff', '-m', f'Merge pull request #{pull["number"]}', head, cwd=work, env=env)
            self.git('push', '-q', 'origin', 'HEAD:' + pull['base'], cwd=work, env=dict(AGENT_SKILLS_FAKE_HOST='1'))
        pull.update(state='closed', merged=True, last_head=head, scheduled=None)
        if pull.get('delete_branch'):
            self.git('update-ref', '-d', 'refs/heads/' + pull['head'])

    def update(self, pull):
        with tempfile.TemporaryDirectory(prefix='fake-host-update-') as temporary:
            work = Path(temporary) / 'work'
            self.git('clone', '-q', '--branch', pull['head'], str(self.bare), str(work), cwd=temporary)
            env = dict(GIT_AUTHOR_NAME='Gitea', GIT_AUTHOR_EMAIL='gitea@localhost',
                       GIT_COMMITTER_NAME='Gitea', GIT_COMMITTER_EMAIL='gitea@localhost')
            result = subprocess.run(['git', '-C', str(work), 'merge', '-q', '--no-ff', '-m',
                                     f"Merge branch '{pull['base']}' into {pull['head']}", 'origin/' + pull['base']],
                                    capture_output=True, env=dict(self.env, **env))
            if result.returncode:
                return False
            self.git('push', '-q', 'origin', 'HEAD:' + pull['head'], cwd=work)
        return True

    def _handler(self):
        host = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, status, data=None):
                payload = b'' if data is None else json.dumps(data).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def body(self):
                length = int(self.headers.get('Content-Length') or 0)
                return json.loads(self.rfile.read(length) or b'{}') if length else {}

            def handle_any(self, method):
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                query = dict(urllib.parse.parse_qsl(parsed.query))
                data = self.body() if method in ('POST', 'PATCH') else {}
                host.calls.append((method, path, data))
                auth = self.headers.get('Authorization', '')
                if host.fault('auth') or not auth.startswith('Basic ') or \
                        base64.b64decode(auth[6:]).decode() != 'test:secret':
                    return self.reply(401, dict(message='unauthorized'))
                if not path.startswith(host.prefix):
                    return self.reply(404, dict(message='not found'))
                path = path[len(host.prefix):]
                parts = path.strip('/').split('/')
                with host.lock:
                    if parts == ['pulls'] and method == 'GET':
                        if host.fault('list'):
                            return self.reply(500, dict(message='list failed'))
                        page, limit = int(query.get('page', 1)), int(query.get('limit', 30))
                        items = [host.view(p) for p in host.open_pulls()]
                        return self.reply(200, items[(page - 1) * limit:page * limit])
                    if parts == ['pulls'] and method == 'POST':
                        if not host.head_of(data['head']):
                            return self.reply(404, dict(message='head branch missing'))
                        if any(p['head'] == data['head'] and p['base'] == data['base'] for p in host.open_pulls()):
                            return self.reply(409, dict(message='pull request already exists'))
                        number = host.next_number
                        host.next_number += 1
                        host.pulls[number] = dict(number=number, head=data['head'], base=data['base'],
                                                  title=data.get('title', ''), body=data.get('body', ''),
                                                  state='open', merged=False, scheduled=None)
                        if host.fault('create_lost'):
                            return self.reply(500, dict(message='response lost'))
                        return self.reply(201, host.view(host.pulls[number]))
                    if len(parts) >= 2 and parts[0] == 'pulls' and parts[1].isdigit():
                        pull = host.pulls.get(int(parts[1]))
                        if not pull:
                            return self.reply(404, dict(message='no such pull request'))
                        if len(parts) == 2 and method == 'GET':
                            return self.reply(200, host.view(pull))
                        if len(parts) == 2 and method == 'PATCH':
                            if data.get('state') == 'closed' and pull['state'] == 'open':
                                pull.update(state='closed', scheduled=None)
                            return self.reply(201, host.view(pull))
                        if parts[2:] == ['merge'] and method == 'DELETE':
                            pull['scheduled'] = None
                            return self.reply(204)
                        if parts[2:] == ['merge'] and method == 'POST':
                            if pull['state'] != 'open':
                                return self.reply(405, dict(message='pull request is closed'))
                            if host.fault('merge'):
                                return self.reply(500, dict(message='merge request failed'))
                            current = host.head_of(pull['head'])
                            if data.get('head_commit_id') and data['head_commit_id'] != current:
                                return self.reply(409, dict(message='head out of date'))
                            if pull.get('scheduled') and pull['scheduled'] == current:
                                return self.reply(409, dict(message='pull request is already scheduled to auto merge'))
                            pull['scheduled'] = current
                            pull['delete_branch'] = data.get('delete_branch_after_merge', False)
                            if host.auto_process:
                                host.process()
                            return self.reply(201 if pull['state'] == 'open' else 200)
                        if parts[2:] == ['update'] and method == 'POST':
                            if not host.update(pull):
                                return self.reply(409, dict(message='merge conflict'))
                            return self.reply(200)
                return self.reply(404, dict(message='unsupported'))

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_PATCH(self):
                self.handle_any('PATCH')

            def do_DELETE(self):
                self.handle_any('DELETE')

        return Handler
