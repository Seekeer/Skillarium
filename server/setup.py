#!/usr/bin/env python3
"""Set up the skill library server on this machine; run by `skillarium server`.

    skillarium server --repo https://gitea.example.org/owner/skills.git [--bot skills-bot] [--runner]

One command, safe to repeat ("repair or update"):
  * the Gitea token (prompted, or --token-file) goes to the Git credential helper;
  * the repository is created when missing; an empty one is initialized from
    templates/library; an existing library is connected (and, when another
    machine serves it, reassigned to this one through the administrative PR);
  * Actions, protection of main with the required policy check and merge
    settings are applied through the API; --bot creates the service user
    clients connect with; --runner registers act_runner on this machine;
  * timers: daily maintenance, hourly engine self-update and, with
    --with-client, the hourly client sync of this machine;
  * the commands that connect Windows and Linux clients are printed.
Tokens are never printed into commands or written into the library.
"""
import argparse
import base64
import getpass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

import hosting

# The path as invoked (…/current/…), not resolved: schedules written from here must keep
# following current, or they would stay on one version and break when it is pruned.
ENGINE = Path(os.path.abspath(__file__)).parents[1]
TEMPLATE = ENGINE / 'templates' / 'library'
RELEASES = os.environ.get('SKILLARIUM_RELEASES', 'https://github.com/Seekeer/Skillarium/releases')
CLIENTS_BEGIN = '<!-- skillarium:clients -->'
CLIENTS_END = '<!-- /skillarium:clients -->'
PROTECTION = dict(enable_push=False, enable_status_check=True, status_check_contexts=['*policy*'],
                  block_on_outdated_branch=True, required_approvals=0)
BOT_TOKEN_SCOPES = ['write:repository', 'read:user']


class SetupError(Exception):
    pass


def say(message):
    print(message, flush=True)


def git(*args, cwd=None, check=True, data=None, env=None):
    result = subprocess.run(['git', *(['-C', str(cwd)] if cwd else []), *args], input=data, capture_output=True,
                            env=dict(os.environ, GIT_TERMINAL_PROMPT='0', **(env or {})))
    if check and result.returncode:
        raise SetupError(f'git {args[0]} failed: ' + result.stderr.decode(errors='replace').strip()[-400:])
    return result.stdout.decode(errors='replace').strip()


def parse_repository(url):
    """(web base, owner, name) of https://host[/prefix]/owner/name[.git]."""
    parts = urllib.parse.urlsplit(url)
    path = parts.path.rstrip('/')
    if path.endswith('.git'):
        path = path[:-4]
    segments = [item for item in path.split('/') if item]
    if parts.scheme not in ('http', 'https') or not parts.netloc or len(segments) < 2 or parts.username:
        raise SetupError('Expected the library as https://host/owner/name.git (no credentials in the URL)')
    prefix = '/'.join(segments[:-2])
    base = f'{parts.scheme}://{parts.netloc}' + (f'/{prefix}' if prefix else '')
    return base, segments[-2], segments[-1]


class Gitea:
    """The few Gitea API calls the installation needs; messages never carry the token."""

    def __init__(self, base, token):
        self.api = base + '/api/v1'
        self.token = token

    def call(self, method, path, body=None, basic=None, expect=(200, 201, 204)):
        headers = {'Accept': 'application/json', 'Content-Type': 'application/json'}
        if basic:
            headers['Authorization'] = 'Basic ' + base64.b64encode(f'{basic[0]}:{basic[1]}'.encode()).decode()
        else:
            headers['Authorization'] = 'token ' + self.token
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.api + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            payload, status = error.read(), error.code
        except (urllib.error.URLError, OSError) as error:
            raise SetupError(f'Gitea is unavailable at {self.api}: {type(error).__name__}')
        try:
            result = json.loads(payload) if payload.strip() else None
        except ValueError:
            result = None
        if expect is not None and status not in expect:
            message = result.get('message') if isinstance(result, dict) else ''
            raise SetupError(f'Gitea {method} {path} answered {status}: {message}')
        return status, result


# --- Token ---------------------------------------------------------------------

def read_token(path):
    if path:
        token = Path(path).read_text(encoding='utf-8').strip()
    else:
        say('Gitea access token of the library owner (write:repository, write:user;')
        say('site admin for --bot). It is stored in the Git credential helper, never printed.')
        token = getpass.getpass('Token: ').strip()
    if not token:
        raise SetupError('No token given')
    return token


def store_credential(base, login, token):
    """Git credential helper of this user; `store` is configured when there is none."""
    if not git('config', '--global', '--get', 'credential.helper', check=False):
        # The only change to the user's global Git configuration, and only when no helper exists.
        git('config', '--global', 'credential.helper', 'store')
        say('Git credential helper was not set; now: store (~/.git-credentials, readable by this user only)')
    parts = urllib.parse.urlsplit(base)
    request = f'protocol={parts.scheme}\nhost={parts.netloc}\nusername={login}\npassword={token}\n\n'
    git('credential', 'approve', data=request.encode())


# --- Repository and library -----------------------------------------------------

def ensure_repository(gitea, owner, name, login):
    status, item = gitea.call('GET', f'/repos/{owner}/{name}', expect=None)
    if status == 200:
        return False
    if status != 404:
        raise SetupError(f'Cannot read repository {owner}/{name} ({status})')
    body = dict(name=name, private=True, auto_init=False, default_branch='main',
                description='Skill library (Skillarium)')
    path = '/user/repos' if owner == login else f'/orgs/{owner}/repos'
    gitea.call('POST', path, body)
    say(f'Repository created: {owner}/{name}')
    return True


def ensure_clone(url, path):
    if (path / '.git').exists():
        # The configured URL as written (get-url would apply insteadOf rewrites).
        origin = git('config', '--get', 'remote.origin.url', cwd=path, check=False)
        if not origin.startswith(('http://', 'https://')) or parse_repository(origin)[1:] != parse_repository(url)[1:]:
            raise SetupError(f'{path} is a clone of {origin}, not of {url}; choose another --path')
        git('-c', 'credential.interactive=false', 'fetch', '-q', '--prune', 'origin', cwd=path)
        return
    if path.exists() and any(path.iterdir()):
        raise SetupError(f'{path} exists and is not a clone; choose another --path')
    path.parent.mkdir(parents=True, exist_ok=True)
    git('clone', '-q', url, str(path))


def remote_main(path):
    return git('rev-parse', '--verify', '-q', 'refs/remotes/origin/main', cwd=path, check=False) or None


def engine_release():
    """The feed of the installed release: {version, engine: {linux: {sha512}}} or None."""
    release = ENGINE / 'release.json'
    return json.loads(release.read_text(encoding='utf-8')) if release.is_file() else None


def client_commands(url):
    return (f"Windows (PowerShell):\n"
            f"  & ([scriptblock]::Create((irm {RELEASES}/latest/download/skillarium.ps1))) client -Library {url}\n"
            f"Linux:\n"
            f"  curl -fsSL {RELEASES}/latest/download/skillarium.sh | bash -s -- client --library {url}\n")


def readme_with_commands(text, url):
    block = f'{CLIENTS_BEGIN}\n```\n{client_commands(url)}```\n{CLIENTS_END}'
    if CLIENTS_BEGIN in text and CLIENTS_END in text:
        start, end = text.index(CLIENTS_BEGIN), text.index(CLIENTS_END) + len(CLIENTS_END)
        return text[:start] + block + text[end:]
    return text.rstrip('\n') + '\n\n## Подключение машин\n\n' + block + '\n'


def initialize(path, url, machine, login):
    """First commit of an empty repository: the library template for this server."""
    for item in TEMPLATE.rglob('*'):
        if item.is_file():
            target = path / item.relative_to(TEMPLATE)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(item, target)
    config = json.loads((path / hosting.CONFIG).read_text(encoding='utf-8'))
    config['server']['machine'] = machine
    release = engine_release()
    if release:
        config['policy_engine'] = dict(version=release['version'], sha512=release['engine']['linux']['sha512'])
    else:
        say('WARNING: this engine was not installed from a release; the PR check stays unpinned '
            'until the server runs from an installed Skillarium.')
    (path / hosting.CONFIG).write_text(json.dumps(config, indent=2) + '\n', encoding='utf-8', newline='\n')
    readme = path / 'README.md'
    readme.write_text(readme_with_commands(readme.read_text(encoding='utf-8'), url), encoding='utf-8', newline='\n')
    (path / 'skills').mkdir(exist_ok=True)
    git('checkout', '-q', '-B', 'main', cwd=path)
    git('add', '-A', cwd=path)
    identity = ['-c', f'user.name={login}', '-c', f'user.email={login}@users.noreply.skillarium']
    git(*identity, 'commit', '-q', '-m', f'Create the skill library served by {machine}', cwd=path)
    git('-c', 'credential.interactive=false', 'push', '-q', '-u', 'origin', 'main', cwd=path)
    say(f'Library initialized from the Skillarium template; server: {machine}')


def connect_library(path, machine):
    """An existing library: check the format and take over the server role if needed."""
    if not git('rev-parse', '--verify', '-q', 'HEAD', cwd=path, check=False):
        git('checkout', '-q', '-B', 'main', 'refs/remotes/origin/main', cwd=path)
    try:
        config = hosting.shared_config(path, 'refs/remotes/origin/main')
    except ValueError as error:
        if 'newer Skillarium' in str(error):
            raise SetupError(str(error) + '; update Skillarium on this server first')
        raise SetupError(f'The repository is not a skill library: {error}')
    assigned = config['server'].get('machine')
    if assigned == machine:
        say(f'Library connected; this machine ({machine}) is its server')
        return
    say(f'Library served by {assigned or "nobody"}; assigning the server role to {machine} (administrative PR)')
    result = subprocess.run([sys.executable, str(ENGINE / 'server' / 'manage.py'), '--repo', str(path),
                             'assign-server', machine])
    if result.returncode:
        raise SetupError('Server assignment was not published; rerun the setup when Gitea is reachable')


# --- Gitea settings ---------------------------------------------------------------

def configure_repository(gitea, owner, name):
    gitea.call('PATCH', f'/repos/{owner}/{name}', dict(has_actions=True, allow_merge_commits=True,
                                                       default_delete_branch_after_merge=False))
    status, rules = gitea.call('GET', f'/repos/{owner}/{name}/branch_protections')
    existing = [rule for rule in rules or [] if rule.get('rule_name', rule.get('branch_name')) == 'main']
    if existing:
        gitea.call('PATCH', f'/repos/{owner}/{name}/branch_protections/main', PROTECTION)
    else:
        gitea.call('POST', f'/repos/{owner}/{name}/branch_protections', dict(rule_name='main', **PROTECTION))
    say('Gitea: Actions on, main protected (required check *policy*, up to date), merge commits allowed')


def config_home():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'skillarium'


def unit_quote(value):
    """A value for a systemd unit line: quoted, with \\, ", % and $ escaped."""
    escaped = value.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$')
    return f'"{escaped}"'


def write_private(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
        stream.write(text)


def ensure_bot(gitea, owner, name, bot):
    """The service user every client connects with; its token is shown once and kept 0600."""
    token_file = config_home() / 'bot-token'
    status, _ = gitea.call('GET', f'/users/{bot}', expect=None)
    if status == 404:
        password = secrets.token_urlsafe(32)
        status, item = gitea.call('POST', '/admin/users', dict(
            username=bot, email=f'{bot}@users.noreply.skillarium', password=password,
            must_change_password=False, visibility='private'), expect=None)
        if status == 403:
            raise SetupError('Creating the bot needs a token of a Gitea site administrator')
        if status not in (200, 201):
            raise SetupError(f'Cannot create the bot user ({status})')
        status, item = gitea.call('POST', f'/users/{bot}/tokens',
                                  dict(name='skillarium-clients', scopes=BOT_TOKEN_SCOPES), basic=(bot, password))
        write_private(token_file, item['sha1'] + '\n')
        say(f'Bot user {bot} created. Its token (shown once, also kept in {token_file}):')
        say('  ' + item['sha1'])
        say('Clients ask for it when they connect; it is never part of a command.')
    elif token_file.is_file():
        say(f'Bot user {bot} exists; its token is kept in {token_file}')
    else:
        say(f'Bot user {bot} exists, but this machine has no token for it: create one in Gitea '
            f'(write:repository) and give it to the clients.')
    gitea.call('PUT', f'/repos/{owner}/{name}/collaborators/{bot}', dict(permission='write'))


def check_runner(gitea, owner, name, machine, register):
    status, item = gitea.call('GET', f'/repos/{owner}/{name}/actions/runners', expect=None)
    runners = (item or {}).get('runners', []) if status == 200 else None
    if runners is None and status not in (200,):
        say('WARNING: cannot list Actions runners (Gitea 1.23+ API); make sure a runner with the '
            'label ubuntu-latest serves this repository, or the required policy check never runs.')
    elif not runners:
        say('WARNING: no Actions runner serves this repository; pull requests wait for the policy check.')
    else:
        say(f'Actions runners: {", ".join(runner.get("name", "?") for runner in runners)}')
    if not register:
        if not runners:
            say('  Register one on this machine: skillarium server --repo <url> --runner')
        return
    runner = shutil.which('act_runner')
    if not runner:
        raise SetupError('act_runner is not on PATH; download it from https://gitea.com/gitea/act_runner/releases')
    status, item = gitea.call('GET', f'/repos/{owner}/{name}/actions/runners/registration-token')
    folder = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'skillarium' / 'runner'
    folder.mkdir(parents=True, exist_ok=True)
    if not (folder / '.runner').is_file():
        # The registration token is single-use and expires; act_runner takes it only as an argument.
        result = subprocess.run([runner, 'register', '--no-interactive', '--instance', gitea.api[:-len('/api/v1')],
                                 '--token', item['token'], '--name', f'{machine}-skillarium',
                                 '--labels', 'ubuntu-latest:host'], cwd=folder)
        if result.returncode:
            raise SetupError('act_runner registration failed')
    unit_dir = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'systemd' / 'user'
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / 'skillarium-runner.service').write_text(
        '# Managed by Skillarium runner\n[Unit]\nDescription=Gitea Actions runner for the skill library\n'
        f'[Service]\nWorkingDirectory={unit_quote(str(folder))}\nExecStart={unit_quote(runner)} daemon\n'
        'Restart=always\nRestartSec=30\n'
        '[Install]\nWantedBy=default.target\n', encoding='utf-8')
    subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
    subprocess.run(['systemctl', '--user', 'enable', '--now', 'skillarium-runner.service'], check=True)
    say('act_runner registered on this machine (label ubuntu-latest:host)')


# --- This machine -------------------------------------------------------------------

def write_config(path):
    config = config_home() / 'config.json'
    data = json.loads(config.read_text(encoding='utf-8')) if config.is_file() else {}
    data['library'] = str(path)
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def install_timers(path, machine, with_client):
    bash = shutil.which('bash') or '/usr/bin/bash'
    steps = [[bash, str(ENGINE / 'server' / 'install.sh'), '--repo', str(path), '--install', machine]]
    if ENGINE.name == 'current' and (ENGINE.parent / 'versions').is_dir():
        steps.append([bash, str(ENGINE / 'scripts' / 'skillarium.sh'), 'enable-updates'])
    else:
        say('Engine not installed from a release (development copy): no self-update timer')
    if with_client:
        steps.append([bash, str(ENGINE / 'install.sh'), '--library', str(path), '--install'])
    for step in steps:
        if subprocess.run(step).returncode:
            raise SetupError('Timer installation failed: ' + ' '.join(step[1:3]))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--repo', required=True, help='Library repository URL: https://host/owner/name.git')
    parser.add_argument('--path', type=Path, default=Path.home() / 'Skillarium' / 'library',
                        help='Working copy of the library on this server (default ~/Skillarium/library)')
    parser.add_argument('--machine', default=socket.gethostname().split('.')[0],
                        help='Server name in agent-skills.json (default: short host name)')
    parser.add_argument('--token-file', help='Read the Gitea token from a file instead of a prompt')
    parser.add_argument('--bot', help='Create (or reuse) this service user for the clients')
    parser.add_argument('--runner', action='store_true', help='Register act_runner on this machine')
    parser.add_argument('--with-client', action='store_true', help='Also sync skills into this machine hourly')
    parser.add_argument('--no-timers', action='store_true', help='Skip systemd units (tests, containers)')
    args = parser.parse_args()
    try:
        if not hosting.MACHINE.fullmatch(args.machine):
            raise SetupError(f'Invalid machine name: {args.machine}; pass --machine')
        base, owner, name = parse_repository(args.repo)
        url = f'{base}/{owner}/{name}.git'
        path = args.path.expanduser().resolve()
        gitea = Gitea(base, read_token(args.token_file))
        status, user = gitea.call('GET', '/user', expect=None)
        if status != 200:
            raise SetupError(f'Gitea rejected the token ({status})')
        store_credential(base, user['login'], gitea.token)
        ensure_repository(gitea, owner, name, user['login'])
        ensure_clone(url, path)
        git('config', 'agent-skills.machine', args.machine, cwd=path)
        if remote_main(path) is None:
            initialize(path, url, args.machine, user['login'])
        else:
            connect_library(path, args.machine)
        configure_repository(gitea, owner, name)
        if args.bot:
            ensure_bot(gitea, owner, name, args.bot)
        check_runner(gitea, owner, name, args.machine, args.runner)
        write_config(path)
        if not args.no_timers:
            install_timers(path, args.machine, args.with_client)
        say('')
        say(f'Skill library server ready: {url} in {path}')
        say('Connect machines with:')
        say(client_commands(url))
        return 0
    except SetupError as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
