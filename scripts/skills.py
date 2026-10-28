#!/usr/bin/env python3
"""Source registration/checking on i3; clients use native shell scripts."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'sources.json'
MAX_DOWNLOAD = 32 * 1024 * 1024


def cache_root():
    base = (os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))
            if os.name == 'nt' else os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
    return Path(base) / 'agent-skills'


def git(*args, cwd=ROOT):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
    env.setdefault('GIT_SSH_COMMAND', 'ssh -oBatchMode=yes -oConnectTimeout=15')
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            env=env, timeout=120)
    if result.returncode:
        # Avoid printing credential-bearing URLs from Git diagnostics.
        raise ValueError('Git command failed: ' + args[0])
    return result.stdout


def load_registry():
    data = json.loads(REGISTRY.read_text(encoding='utf-8'))
    if data.get('version') != 2 or not isinstance(data.get('skills'), dict):
        raise ValueError('Expected sources.json version 2 with a skills object')
    return data


def save_registry(data):
    atomic_json(REGISTRY, data)


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.agent-skills-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_entry(name, entry):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('Invalid skill name')
    if not isinstance(entry.get('auto_update'), bool):
        raise ValueError('auto_update must be true or false')
    source = entry['source']
    if source.get('kind') not in ('git', 'url'):
        raise ValueError('Supported sources: git and url')
    url = source['url']
    parsed = urllib.parse.urlsplit(url)
    if parsed.password or parsed.username and parsed.scheme in ('http', 'https'):
        raise ValueError('Keep credentials outside source URLs')
    if source['kind'] == 'url' and parsed.scheme != 'https':
        raise ValueError('Single-file sources require HTTPS')
    if source['kind'] == 'git':
        # Local paths/file URLs support offline tests and local trusted sources.
        if url.startswith('-') or parsed.scheme not in ('', 'https', 'ssh', 'file'):
            raise ValueError('Unsupported Git URL')
        path = PurePosixPath(source.get('path', '.'))
        if path.is_absolute() or '..' in path.parts or '\\' in str(path):
            raise ValueError('Source path must stay inside its repository')
        ref = source.get('ref', '')
        if not re.fullmatch(r'refs/(?:heads|tags)/[^\s~^:?*\[\\]+', ref):
            raise ValueError('Use a full ref, e.g. refs/heads/main')


def validate_skill(path, name):
    text = (path / 'SKILL.md').read_text(encoding='utf-8-sig')
    if not text.startswith('---\n') and not text.startswith('---\r\n'):
        raise ValueError('Missing SKILL.md frontmatter')
    sections = text.split('---', 2)
    if len(sections) < 3:
        raise ValueError('Unterminated frontmatter')
    match = re.search(r'^name:\s*([^\r\n]+)', sections[1], re.M)
    if not match or match[1].strip().strip('\"\'') != name:
        raise ValueError('SKILL.md name must match registry name')
    if not re.search(r'^description:\s*\S', sections[1], re.M):
        raise ValueError('Missing description')


def tree_hash(path):
    digest = hashlib.sha256()
    for item in sorted(path.rglob('*')):
        if item.is_symlink():
            raise ValueError('Symlinks in source skills are not supported')
        if item.is_file():
            relative = item.relative_to(path).as_posix()
            digest.update(relative.encode('utf-8') + b'\0')
            digest.update(hashlib.sha256(item.read_bytes()).digest() + b'\0')
    return digest.hexdigest()


def download(url):
    with urllib.request.urlopen(url, timeout=30) as response:
        if urllib.parse.urlsplit(response.url).scheme != 'https':
            raise ValueError('Download redirected outside HTTPS')
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError('Source exceeds 32 MiB limit')
    return data


def latest_revision(source):
    ref = source['ref']
    output = git('ls-remote', source['url'], ref, ref + '^{}').decode()
    refs = dict((line.split()[1], line.split()[0]) for line in output.splitlines())
    revision = refs.get(ref + '^{}', refs.get(ref))
    if not revision:
        raise ValueError('Source ref not found')
    return revision


def extract_git(source, revision, destination):
    if not re.fullmatch(r'[0-9a-f]{40}|[0-9a-f]{64}', revision):
        raise ValueError('Expected a complete Git object ID')
    mirror = cache_root() / 'repositories' / hashlib.sha256(source['url'].encode()).hexdigest()
    if not mirror.exists():
        mirror.mkdir(parents=True)
        git('init', '--bare', cwd=mirror)
    try:
        git('cat-file', '-e', revision + '^{commit}', cwd=mirror)
    except ValueError:
        git('fetch', '--no-tags', '--depth=1', source['url'], revision, cwd=mirror)
    relative = str(PurePosixPath(source.get('path', '.')))
    tree = revision if relative == '.' else revision + ':' + relative
    archive = git('archive', '--format=tar', tree, cwd=mirror)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        for member in bundle:
            path = PurePosixPath(member.name)
            if (path.is_absolute() or '..' in path.parts or '\\' in member.name
                    or ':' in member.name or any(p.endswith((' ', '.')) for p in path.parts)):
                raise ValueError('Unsafe source archive path')
            target = destination.joinpath(*path.parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                total += member.size
                if total > MAX_DOWNLOAD:
                    raise ValueError('Skill exceeds 32 MiB limit')
                target.parent.mkdir(parents=True, exist_ok=True)
                with bundle.extractfile(member) as stream:
                    target.write_bytes(stream.read())
                if member.mode & 0o111:
                    target.chmod(0o755)
            else:
                raise ValueError('Only regular files and directories may be imported')


def materialize(name, entry, approved=True):
    validate_entry(name, entry)
    source = entry['source']
    revision = entry.get('revision') if approved else None
    expected = entry.get('content_sha256') if approved else None
    if approved and not re.fullmatch(r'[0-9a-f]{64}', expected or ''):
        raise ValueError('Missing approved content_sha256')
    version_dir = cache_root() / 'versions' / name
    version_dir.mkdir(parents=True, exist_ok=True)
    if approved:
        cached = version_dir / expected
        if cached.exists():
            if tree_hash(cached) != expected:
                raise ValueError('Cached skill has local edits; preserved')
            validate_skill(cached, name)
            return cached, revision, expected
    with tempfile.TemporaryDirectory(dir=version_dir) as temporary:
        folder = Path(temporary) / 'content'
        folder.mkdir()
        if source['kind'] == 'git':
            revision = revision if approved else latest_revision(source)
            extract_git(source, revision, folder)
        else:
            payload = download(source['url'])
            revision = hashlib.sha256(payload).hexdigest()
            if approved and revision != entry.get('revision'):
                raise ValueError('File no longer matches approved SHA256')
            (folder / 'SKILL.md').write_bytes(payload)
        validate_skill(folder, name)
        content_hash = tree_hash(folder)
        if approved and content_hash != expected:
            raise ValueError('Downloaded content differs from approved version')
        final = version_dir / content_hash
        if final.exists():
            if tree_hash(final) != content_hash:
                raise ValueError('Cached skill has local edits; preserved')
        else:
            folder.rename(final)
        return final, revision, content_hash


def check(args):
    if args.publish and git('status', '--porcelain').strip():
        raise ValueError('Publish requires a clean repository')
    data = load_registry()
    unknown = set(args.approve) - set(data['skills'])
    if unknown:
        raise ValueError('Unknown approval names: ' + ', '.join(sorted(unknown)))
    if args.expected_revision and len(args.approve) != 1:
        raise ValueError('--expected-revision requires exactly one --approve')
    reports = {}
    changed = False
    failed = False
    for name, entry in data['skills'].items():
        try:
            _, revision, digest = materialize(name, entry, approved=False)
            if name in args.approve and args.expected_revision and revision != args.expected_revision:
                raise ValueError('Source moved since review; expected revision differs')
            status = 'unchanged'
            if digest != entry.get('content_sha256'):
                status = 'available'
                if name in args.approve or (args.apply_auto and entry['auto_update']):
                    entry.update(revision=revision, content_sha256=digest)
                    changed = True
                    status = 'updated'
            reports[name] = dict(status=status, revision=revision, content_sha256=digest)
            print(f'{name}: {status}')
        except Exception as error:
            reports[name] = dict(status='error', error=str(error))
            print(f'ERROR {name}: {error}', file=sys.stderr)
            failed = True
    if changed:
        save_registry(data)
    report_path = Path(git('rev-parse', '--git-path', 'skill-updates.json').decode().strip())
    if not report_path.is_absolute():
        report_path = ROOT / report_path
    atomic_json(report_path, reports)
    print(f'Checked {len(reports)} source(s); report: {report_path}')
    if args.publish:
        if changed:
            git('add', '--', 'sources.json')
            git('commit', '-m', 'Update approved skill versions', '--', 'sources.json')
        # Retry an earlier failed push even when no sources changed this run.
        git('push')
    return 1 if failed else 0


def register(args):
    data = load_registry()
    if args.name in data['skills']:
        raise ValueError('Skill already registered; edit its entry explicitly')
    source = (dict(kind='git', url=args.git_url, path=str(PurePosixPath(args.path)), ref=args.ref)
              if args.git_url else dict(kind='url', url=args.url))
    entry = dict(source=source, auto_update=args.auto_update == 'yes')
    _, revision, digest = materialize(args.name, entry, approved=False)
    entry.update(revision=revision, content_sha256=digest)
    data['skills'][args.name] = entry
    save_registry(data)
    print('Registered:', args.name)
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    checker = commands.add_parser('check', help='Check upstream versions; read-only by default')
    checker.add_argument('--apply-auto', action='store_true')
    checker.add_argument('--approve', action='append', default=[], metavar='NAME')
    checker.add_argument('--expected-revision', help='Require this source revision for one manual approval')
    checker.add_argument('--publish', action='store_true')
    registration = commands.add_parser('register', help='Validate a source and pin its initial version')
    registration.add_argument('name')
    source = registration.add_mutually_exclusive_group(required=True)
    source.add_argument('--git-url')
    source.add_argument('--url', help='HTTPS URL of a single SKILL.md file')
    registration.add_argument('--path', default='.')
    registration.add_argument('--ref', default='refs/heads/main')
    registration.add_argument('--auto-update', choices=('yes', 'no'), required=True)
    args = parser.parse_args()
    try:
        return {'check': check, 'register': register}[args.command](args)
    except Exception as error:
        print(f'ERROR: {error}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
