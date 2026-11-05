"""Local stand-in for the Gitea administration API used by `skillarium server`.

Users with tokens, repositories backed by bare Git folders, repository
settings, branch protection rules, collaborators and Actions runners. Git
URLs http://127.0.0.1:<port>/<owner>/<name>.git reach the bare folders through
an url.insteadOf rule in the test Git configuration (see git_config()).
"""
import base64
import json
from pathlib import Path
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeGitea:
    def __init__(self, folder):
        self.folder = Path(folder)
        self.bares = self.folder / 'bares'
        self.bares.mkdir(parents=True, exist_ok=True)
        self.users = {}       # login -> {password, admin, tokens: {token: scopes}}
        self.repos = {}       # (owner, name) -> {settings, protections, collaborators}
        self.runners = []
        self.calls = []
        self.lock = threading.RLock()
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), self._handler())
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def add_user(self, login, token, admin=False, password='secret-password'):
        self.users[login] = dict(password=password, admin=admin, tokens={token: ['all']})

    def create_repo(self, owner, name):
        bare = self.bares / owner / f'{name}.git'
        bare.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', str(bare)], check=True)
        self.repos[(owner, name)] = dict(settings=dict(has_actions=False, allow_merge_commits=False),
                                         protections={}, collaborators={})
        return bare

    def url(self, owner, name):
        return f'{self.base}/{owner}/{name}.git'

    def git_config(self, path):
        """A global Git configuration that maps the fake URLs to the bare folders."""
        bares = self.bares.resolve().as_posix()
        prefix = f'file:///{bares.lstrip("/")}/'
        path.write_text(f'[url "{prefix}"]\n\tinsteadOf = {self.base}/\n'
                        '[init]\n\tdefaultBranch = main\n', encoding='utf-8')

    def login_of(self, header):
        if header.startswith('token '):
            token = header[6:]
            return next((login for login, user in self.users.items() if token in user['tokens']), None)
        if header.startswith('Basic '):
            login, _, secret = base64.b64decode(header[6:]).decode().partition(':')
            user = self.users.get(login)
            if user and (secret == user['password'] or secret in user['tokens']):
                return login
        return None

    def _handler(self):
        gitea = self

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

            def handle_any(self, method):
                path = self.path.split('?', 1)[0]
                length = int(self.headers.get('Content-Length') or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                gitea.calls.append((method, path, body))
                login = gitea.login_of(self.headers.get('Authorization', ''))
                if login is None:
                    return self.reply(401, dict(message='unauthorized'))
                if not path.startswith('/api/v1/'):
                    return self.reply(404, dict(message='not found'))
                parts = path[len('/api/v1/'):].strip('/').split('/')
                with gitea.lock:
                    return self.route(method, parts, body, login)

            def route(self, method, parts, body, login):
                user = gitea.users[login]
                if parts == ['user'] and method == 'GET':
                    return self.reply(200, dict(login=login, is_admin=user['admin']))
                if parts == ['user', 'repos'] and method == 'POST' or \
                        len(parts) == 3 and parts[0] == 'orgs' and parts[2] == 'repos' and method == 'POST':
                    owner = login if parts[0] == 'user' else parts[1]
                    if (owner, body['name']) in gitea.repos:
                        return self.reply(409, dict(message='repository exists'))
                    gitea.create_repo(owner, body['name'])
                    return self.reply(201, dict(full_name=f'{owner}/{body["name"]}'))
                if parts == ['admin', 'users'] and method == 'POST':
                    if not user['admin']:
                        return self.reply(403, dict(message='admin only'))
                    gitea.users[body['username']] = dict(password=body['password'], admin=False, tokens={})
                    return self.reply(201, dict(login=body['username']))
                if len(parts) == 2 and parts[0] == 'users' and method == 'GET':
                    return self.reply(200, dict(login=parts[1])) if parts[1] in gitea.users else \
                        self.reply(404, dict(message='no such user'))
                if len(parts) == 3 and parts[0] == 'users' and parts[2] == 'tokens' and method == 'POST':
                    if login != parts[1] or not self.headers.get('Authorization', '').startswith('Basic '):
                        return self.reply(403, dict(message='basic auth of the user required'))
                    token = f'token-of-{login}-{len(user["tokens"]) + 1}'
                    user['tokens'][token] = body.get('scopes', [])
                    return self.reply(201, dict(name=body['name'], sha1=token))
                if len(parts) >= 3 and parts[0] == 'repos':
                    repo = gitea.repos.get((parts[1], parts[2]))
                    if repo is None:
                        return self.reply(404, dict(message='repository not found'))
                    rest = parts[3:]
                    if not rest and method == 'GET':
                        push = login == parts[1] or user['admin'] or repo['collaborators'].get(login) in ('write', 'admin')
                        return self.reply(200, dict(full_name=f'{parts[1]}/{parts[2]}', **repo['settings'],
                                                    permissions=dict(pull=True, push=push, admin=login == parts[1])))
                    if not rest and method == 'PATCH':
                        repo['settings'].update(body)
                        return self.reply(200, dict(full_name=f'{parts[1]}/{parts[2]}', **repo['settings']))
                    if rest == ['branch_protections'] and method == 'GET':
                        return self.reply(200, list(repo['protections'].values()))
                    if rest == ['branch_protections'] and method == 'POST':
                        if body['rule_name'] in repo['protections']:
                            return self.reply(403, dict(message='branch protection exists'))
                        repo['protections'][body['rule_name']] = dict(body)
                        return self.reply(201, body)
                    if len(rest) == 2 and rest[0] == 'branch_protections' and method == 'PATCH':
                        if rest[1] not in repo['protections']:
                            return self.reply(404, dict(message='no such rule'))
                        repo['protections'][rest[1]].update(body)
                        return self.reply(200, repo['protections'][rest[1]])
                    if len(rest) == 2 and rest[0] == 'collaborators' and method == 'PUT':
                        repo['collaborators'][rest[1]] = body.get('permission', 'write')
                        return self.reply(204)
                    if rest == ['actions', 'runners'] and method == 'GET':
                        return self.reply(200, dict(runners=gitea.runners, total_count=len(gitea.runners)))
                    if rest == ['actions', 'runners', 'registration-token'] and method == 'GET':
                        return self.reply(200, dict(token='runner-registration-token'))
                return self.reply(404, dict(message='unsupported'))

            def do_GET(self):
                self.handle_any('GET')

            def do_POST(self):
                self.handle_any('POST')

            def do_PATCH(self):
                self.handle_any('PATCH')

            def do_PUT(self):
                self.handle_any('PUT')

        return Handler
