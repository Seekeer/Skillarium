"""Machine identity and pull-request publication through the Git hosting API.

Every automatic publication pushes a proposal branch and opens (or reuses) a
pull request. The hosting service runs the acceptance checks and merges it;
nothing here pushes to the protected main branch.
"""
import base64
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

MACHINE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,62}')
CONFIG = 'agent-skills.json'
BRANCH_PREFIX = 'agent-skills/'


class HostingError(ValueError):
    def __init__(self, kind, message, status=None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def run_git(repo, *args, env=None, check=True, data=None):
    full_env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never', **(env or {}))
    full_env.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes -oConnectTimeout=15')
    result = subprocess.run(['git', '-C', str(repo), *args], capture_output=True, env=full_env,
                            input=data, timeout=300)
    if check and result.returncode:
        # Avoid printing credential-bearing URLs from Git diagnostics.
        raise ValueError('Git command failed: ' + args[0])
    return result


def git_config(repo, key):
    result = run_git(repo, 'config', '--get', key, check=False)
    return result.stdout.decode().strip() if result.returncode == 0 else ''


def machine_name(repo):
    """The service name of this installation; stored per clone, never globally."""
    name = git_config(repo, 'agent-skills.machine') or socket.gethostname().split('.')[0]
    if not MACHINE.fullmatch(name):
        raise ValueError('Invalid machine name; set: git config agent-skills.machine <name>')
    return name


def author_env(machine):
    """Commit identity of a service; the user's own Git settings stay untouched."""
    name = 'agent-skills/' + machine
    email = f'agent-skills+{machine}@localhost'
    return dict(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email,
                GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)


def service_author(machine):
    return 'agent-skills/' + machine


def shared_config(repo, revision):
    """Read the shared configuration of an exact revision (normally origin/main)."""
    result = run_git(repo, 'show', f'{revision}:{CONFIG}', check=False)
    if result.returncode:
        raise ValueError('Shared configuration is unavailable at ' + revision)
    data = json.loads(result.stdout.decode('utf-8'))
    if data.get('version') != 1 or not isinstance(data.get('server'), dict):
        raise ValueError('Expected agent-skills.json version 1 with a server section')
    machine = data['server'].get('machine')
    if machine is not None and (not isinstance(machine, str) or not MACHINE.fullmatch(machine)):
        raise ValueError('Invalid assigned server machine name')
    return data


def main_branch(config):
    return (config.get('publication') or {}).get('branch', 'main')


def api_base(repo):
    configured = git_config(repo, 'agent-skills.api')
    if configured:
        return configured.rstrip('/')
    url = run_git(repo, 'remote', 'get-url', 'origin').stdout.decode().strip()
    match = re.fullmatch(r'(https?)://(?:[^@/]*@)?([^/]+)/(.+?)(?:\.git)?/?', url)
    if not match:
        match = re.fullmatch(r'(?:ssh://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/](.+?)(?:\.git)?/?', url)
        if not match:
            raise ValueError('Cannot derive hosting API from origin; set git config agent-skills.api')
        return f'https://{match[1]}/api/v1/repos/{match[2]}'
    return f'{match[1]}://{match[2]}/api/v1/repos/{match[3]}'


def credentials(repo, api):
    parts = urllib.parse.urlsplit(api)
    request = f'protocol={parts.scheme}\nhost={parts.netloc}\n\n'.encode()
    result = run_git(repo, 'credential', 'fill', data=request, check=False)
    if result.returncode:
        return None
    values = dict(line.split('=', 1) for line in result.stdout.decode().splitlines() if '=' in line)
    if not values.get('password'):
        return None
    return values.get('username', ''), values['password']


class Hosting:
    """Minimal Gitea pull-request API; errors never include credentials."""

    def __init__(self, repo, base_branch='main', timeout=30):
        self.repo = Path(repo)
        self.api = api_base(repo)
        self.base_branch = base_branch
        self.timeout = timeout
        self._auth = None

    def request(self, method, path, body=None, query=None):
        if self._auth is None:
            pair = credentials(self.repo, self.api)
            if pair is None:
                raise HostingError('auth', 'Hosting credentials are unavailable; publication pending')
            self._auth = 'Basic ' + base64.b64encode(f'{pair[0]}:{pair[1]}'.encode()).decode()
        url = self.api + path + ('?' + urllib.parse.urlencode(query) if query else '')
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(url, data=data, method=method, headers={
            'Authorization': self._auth, 'Accept': 'application/json',
            'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = response.read()
                return response.status, json.loads(payload) if payload.strip() else None
        except urllib.error.HTTPError as error:
            payload = error.read()
            try:
                message = json.loads(payload).get('message', '')
            except Exception:
                message = payload[:200].decode(errors='replace')
            if error.code in (401, 403):
                raise HostingError('auth', f'Hosting rejected access ({error.code}); publication pending', error.code)
            return error.code, dict(message=message)
        except (urllib.error.URLError, OSError, TimeoutError) as error:
            raise HostingError('network', 'Hosting unavailable; publication pending: ' + type(error).__name__)

    def open_pull(self, branch):
        page = 1
        while True:
            status, items = self.request('GET', '/pulls', query=dict(state='open', page=page, limit=50))
            if status != 200:
                raise HostingError('http', f'Cannot list pull requests ({status})', status)
            for item in items or []:
                if item['head']['ref'] == branch and item['base']['ref'] == self.base_branch:
                    return item
            if not items or len(items) < 50:
                return None
            page += 1

    def pulls_with_prefix(self, prefix):
        found, page = [], 1
        while True:
            status, items = self.request('GET', '/pulls', query=dict(state='open', page=page, limit=50))
            if status != 200:
                raise HostingError('http', f'Cannot list pull requests ({status})', status)
            found += [item for item in items or [] if item['head']['ref'].startswith(prefix)]
            if not items or len(items) < 50:
                return found
            page += 1

    def create_pull(self, branch, title, body):
        status, item = self.request('POST', '/pulls', dict(head=branch, base=self.base_branch,
                                                           title=title, body=body))
        if status in (200, 201):
            return item
        # A previous attempt may have succeeded although its response was lost.
        existing = self.open_pull(branch)
        if existing:
            return existing
        raise HostingError('http', f'Cannot create pull request ({status}): {item.get("message", "")}', status)

    def get_pull(self, number):
        status, item = self.request('GET', f'/pulls/{number}')
        if status != 200:
            raise HostingError('http', f'Cannot read pull request #{number} ({status})', status)
        return item

    def schedule_merge(self, number, head):
        """Merge when checks succeed, only for this exact head commit."""
        body = {'Do': 'merge', 'merge_when_checks_succeed': True, 'head_commit_id': head,
                'delete_branch_after_merge': True}
        status, item = self.request('POST', f'/pulls/{number}/merge', body)
        if status in (200, 201, 204):
            return 'scheduled'
        message = (item or {}).get('message', '')
        if status == 409 and 'schedul' in message.lower():
            # The hosting keeps one schedule; renew it for the current head.
            self.request('DELETE', f'/pulls/{number}/merge')
            status, item = self.request('POST', f'/pulls/{number}/merge', body)
            if status in (200, 201, 204):
                return 'scheduled'
            message = (item or {}).get('message', '')
        if self.get_pull(number).get('merged'):
            return 'merged'
        raise HostingError('http', f'Automatic merge was not accepted ({status}): {message}', status)

    def update_pull(self, number):
        status, item = self.request('POST', f'/pulls/{number}/update', query=dict(style='merge'))
        if status not in (200, 201, 204):
            raise HostingError('http', f'Cannot update pull request #{number} ({status})', status)

    def close_pull(self, number):
        self.request('PATCH', f'/pulls/{number}', dict(state='closed'))


def is_ancestor(repo, older, newer):
    return run_git(repo, 'merge-base', '--is-ancestor', older, newer, check=False).returncode == 0


def push_branch(repo, head, branch):
    """Fast-forward only: proposal branches are never rewritten."""
    result = run_git(repo, '-c', 'credential.interactive=false', 'push', 'origin',
                     f'{head}:refs/heads/{branch}', check=False)
    if result.returncode:
        raise HostingError('push', 'Proposal branch push failed; local commits preserved for retry')


def publish(repo, head, branch, title, body, automerge=True, attempts=2, hosting=None):
    """Push a proposal branch and ensure one pull request for it.

    Repeating the call after a lost response or a crash finds the existing
    branch and pull request; a head already merged into main is reported.
    """
    hosting = hosting or Hosting(repo)
    last = None
    for attempt in range(attempts):
        try:
            run_git(repo, '-c', 'credential.interactive=false', 'fetch', 'origin')
            if is_ancestor(repo, head, 'origin/' + hosting.base_branch):
                return dict(state='merged', branch=branch, head=head)
            push_branch(repo, head, branch)
            pull = hosting.open_pull(branch) or hosting.create_pull(branch, title, body)
            state = hosting.schedule_merge(pull['number'], head) if automerge else 'open'
            return dict(state=state, branch=branch, head=head, number=pull['number'],
                        url=pull.get('html_url', ''))
        except HostingError as error:
            last = error
            if error.kind == 'auth':
                break
            time.sleep(min(2 ** attempt, 5) if attempt + 1 < attempts else 0)
        except ValueError as error:
            last = HostingError('network', str(error))
    raise last
