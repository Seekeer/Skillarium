#!/usr/bin/env python3
"""Source registration/checking on i3; clients use native shell scripts."""
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / 'sources.json'


def set_catalog(path):
    global ROOT, REGISTRY
    ROOT = Path(path).resolve()
    REGISTRY = ROOT / 'sources.json'
MAX_DOWNLOAD = 32 * 1024 * 1024


def cache_root():
    base = (os.environ.get('LOCALAPPDATA', str(Path.home() / 'AppData/Local'))
            if os.name == 'nt' else os.environ.get('XDG_CACHE_HOME', str(Path.home() / '.cache')))
    return Path(base) / 'agent-skills'


def git(*args, cwd=None):
    cwd = ROOT if cwd is None else cwd
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


def validate_entry_name(name):
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name) or len(name) > 64:
        raise ValueError('Invalid skill name')
    validate_windows_paths([name])


def policy_name(entry):
    """auto: apply compatible updates, ask on conflict; confirm: ask every time."""
    return 'auto' if entry.get('auto_update') else 'confirm'


def validate_entry(name, entry):
    validate_entry_name(name)
    if not isinstance(entry.get('auto_update'), bool):
        raise ValueError('auto_update must be true or false')
    for record in (entry, entry.get('customization') or {}):
        if not isinstance(record, dict):
            raise ValueError('Invalid customization')
        if 'executable_sha256' in record and (not isinstance(record['executable_sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', record['executable_sha256'])):
            raise ValueError('Invalid executable hash')
    custom = entry.get('customization')
    if custom is not None:
        if not isinstance(custom, dict) or any(not re.fullmatch(r'[0-9a-f]{64}', custom.get(key, ''))
                                               for key in ('content_sha256', 'base_sha256')):
            raise ValueError('Invalid customization hashes')
        if custom.get('path') not in (None, 'skills/' + name):
            raise ValueError('Invalid shared skill path')
        if custom.get('base_sha256') != entry.get('content_sha256') or custom.get('base_revision') != entry.get('revision'):
            raise ValueError('Customization base differs from approved source pin')
    source = entry['source']
    if source.get('kind') == 'local':
        if source.get('path') != 'skills/' + name or entry['auto_update'] or custom is not None:
            raise ValueError('Local skills require their catalog path, auto_update=false and no customization')
        if entry.get('revision') != entry.get('content_sha256'):
            raise ValueError('Local skill revision must equal its content hash')
        return
    if source.get('kind') not in ('git', 'url'):
        raise ValueError('Supported sources: git, url and local')
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


def validate_skill(path, name=None):
    return validate_skill_text((path / 'SKILL.md').read_text(encoding='utf-8-sig'), name)


def validate_skill_text(text, name=None):
    if not text.startswith('---\n') and not text.startswith('---\r\n'):
        raise ValueError('Missing SKILL.md frontmatter')
    sections = text.split('---', 2)
    if len(sections) < 3:
        raise ValueError('Unterminated frontmatter')
    match = re.search(r'^name:\s*([^\r\n]+)', sections[1], re.M)
    if not match:
        raise ValueError('Missing SKILL.md name')
    actual_name = match[1].strip().strip('\"\'')
    if name is not None and actual_name != name:
        raise ValueError('SKILL.md name must match registry name')
    if not re.search(r'^description:\s*\S', sections[1], re.M):
        raise ValueError('Missing description')
    return actual_name


def validate_windows_paths(paths):
    """Reject Win32 device names and case aliases, including parent directories."""
    seen = {}
    devices = re.compile(r'(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|(?:COM|LPT)[1-9\u00b9\u00b2\u00b3])', re.I)
    for path in paths:
        parts = str(path).split('/')
        for index, part in enumerate(parts):
            if (not part or part in ('.', '..') or part.endswith((' ', '.'))
                    or re.search(r'[<>:"\\|?*\x00-\x1f]', part)
                    or len(part.encode('utf-16-le')) // 2 > 255
                    or devices.fullmatch(part.split('.')[0].rstrip(' '))
                    or part.lower() in ('.git', '.gitattributes')):
                raise ValueError('Windows-incompatible skill path: ' + str(path))
            prefix = '/'.join(parts[:index + 1])
            # Native case comparison does not expand sharp-s into two letters.
            key = ''.join(c.upper() if len(c.upper()) == 1 else c for c in prefix)
            previous = seen.setdefault(key, prefix)
            if previous != prefix:
                raise ValueError(f'Windows path collision: {previous} / {prefix}')


def content_paths(path, catalog=False):
    if not catalog:
        return sorted(path.rglob('*'))
    relative = path.relative_to(ROOT).as_posix()
    names = git('ls-files', '-z', '--cached', '--others', '--exclude-standard', '--', relative)
    paths = {ROOT / os.fsdecode(name) for name in names.split(b'\0') if name}
    return sorted(item for item in paths if item.exists() or item.is_symlink())


def validate_catalog_windows_paths():
    """Check the proposed Git payload before committing or retrying a push."""
    for name in load_registry()['skills']:
        validate_windows_paths([name])
        paths = git('ls-files', '-z', '--cached', '--', 'skills/' + name)
        validate_windows_paths(os.fsdecode(path) for path in paths.split(b'\0') if path)


def executable_hash(path, catalog=False):
    digest = hashlib.sha256()
    for item in content_paths(path, catalog):
        if item.is_file():
            digest.update(item.relative_to(path).as_posix().encode() + b'\0')
            digest.update(b'x' if item.stat().st_mode & 0o111 else b'-')
    return digest.hexdigest()


def tree_hash(path, catalog=False):
    digest = hashlib.sha256()
    for item in content_paths(path, catalog):
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


def git_mirror(source, revision):
    """Cached bare copy of the source holding this revision."""
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
    return mirror


def git_skill_files(source, revision):
    """Paths of the SKILL.md files under the source folder, relative to it."""
    mirror = git_mirror(source, revision)
    relative = str(PurePosixPath(source.get('path', '.')))
    names = git('ls-tree', '-r', '--name-only', revision, '--', '.' if relative == '.' else relative + '/',
                cwd=mirror).decode('utf-8').splitlines()
    prefix = '' if relative == '.' else relative + '/'
    return sorted(name[len(prefix):] for name in names
                  if name.startswith(prefix) and PurePosixPath(name).name == 'SKILL.md')


def git_skill_text(source, revision):
    mirror = git_mirror(source, revision)
    relative = str(PurePosixPath(source.get('path', '.')))
    return git('show', revision + ':' + ('SKILL.md' if relative == '.' else relative + '/SKILL.md'),
               cwd=mirror).decode('utf-8-sig')


def extract_git(source, revision, destination):
    mirror = git_mirror(source, revision)
    relative = str(PurePosixPath(source.get('path', '.')))
    tree = revision if relative == '.' else revision + ':' + relative
    # git archive converts line endings like a checkout would; Git for Windows defaults to
    # core.autocrlf=true, which would make content hashes differ between Windows and Linux.
    archive = git('-c', 'core.autocrlf=false', '-c', 'core.eol=lf', 'archive', '--format=tar', tree, cwd=mirror)
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        members = bundle.getmembers()
        validate_windows_paths(member.name.rstrip('/') for member in members)
        for member in members:
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
    if source['kind'] == 'local':
        return local_skill(name, entry), entry['revision'], entry['content_sha256']
    revision = entry.get('revision') if approved else None
    custom = entry.get('customization') if approved else None
    expected = (custom or entry).get('content_sha256') if approved else None
    if approved and not re.fullmatch(r'[0-9a-f]{64}', expected or ''):
        raise ValueError('Missing approved content_sha256')
    version_dir = cache_root() / 'upstream-versions' / name
    version_dir.mkdir(parents=True, exist_ok=True)
    expected_modes = (custom or entry).get('executable_sha256') if approved else None
    if approved and expected_modes:
        cached = version_dir / (expected + '-' + expected_modes)
        if cached.exists():
            if tree_hash(cached) != expected or executable_hash(cached) != expected_modes:
                raise ValueError('Cached skill has local edits; preserved')
            validate_skill(cached, name)
            return cached, revision, expected
    with tempfile.TemporaryDirectory(dir=version_dir) as temporary:
        folder = Path(temporary) / 'content'
        folder.mkdir()
        if custom:
            shared = shared_skill(name, entry)
            shutil.copytree(shared, folder, dirs_exist_ok=True)
        elif source['kind'] == 'git':
            revision = revision if approved else latest_revision(source)
            extract_git(source, revision, folder)
        else:
            payload = download(source['url'])
            revision = hashlib.sha256(payload).hexdigest()
            if approved and revision != entry.get('revision'):
                raise ValueError('File no longer matches approved SHA256')
            (folder / 'SKILL.md').write_bytes(payload)
        validate_local_content(folder, name)
        content_hash = tree_hash(folder)
        if approved and content_hash != expected:
            raise ValueError('Downloaded content differs from approved version')
        modes = executable_hash(folder)
        if approved and expected_modes and modes != expected_modes:
            raise ValueError('Downloaded executable modes differ from approved version')
        final = version_dir / (content_hash + '-' + modes)
        if final.exists():
            if tree_hash(final) != content_hash or executable_hash(final) != modes:
                raise ValueError('Cached skill has local edits; preserved')
        else:
            folder.rename(final)
        return final, revision, content_hash


def validate_local_content(folder, name, catalog=False):
    """Validate native-client portability before a local folder enters Git."""
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError('Local skill must be a regular directory')
    total = 0
    items = content_paths(folder, catalog)
    validate_windows_paths(item.relative_to(folder).as_posix() for item in items)
    for item in items:
        parts = item.relative_to(folder).parts
        if (any(parent.is_symlink() for parent in (item, *item.parents) if parent != ROOT and ROOT in parent.parents)
                or item.is_symlink() or not (item.is_file() or item.is_dir())
                or any(part in ('.git', '.gitattributes') or part.endswith((' ', '.'))
                       or any(ord(char) < 32 or char in '\\:' for char in part) for part in parts)):
            raise ValueError('Unsupported local skill file or path')
        if item.is_file():
            total += item.stat().st_size
            if total > MAX_DOWNLOAD:
                raise ValueError('Skill exceeds 32 MiB limit')
    if catalog and folder / 'SKILL.md' not in content_paths(folder, catalog=True):
        raise ValueError('SKILL.md is ignored by Git; registration stopped')
    validate_skill(folder, name)
    return tree_hash(folder, catalog)


def local_skill(name, entry):
    validate_entry(name, entry)
    folder = ROOT / entry['source']['path']
    if (ROOT / 'skills').is_symlink():
        raise ValueError('Local skill path contains a symlink')
    if validate_local_content(folder, name, catalog=True) != entry['content_sha256']:
        raise ValueError('Local skill content differs from its registered hash')
    return folder


def inspect_local_import(directory, name=None):
    """Validate an import without copying files, staging, or changing metadata."""
    supplied = Path(directory)
    if supplied.is_symlink():
        raise ValueError('Local skill must be a regular directory')
    source = supplied.resolve()
    if not source.is_dir() or not (source / 'SKILL.md').is_file():
        raise ValueError('Укажите папку скилла с файлом SKILL.md')
    if (source / 'SKILL.md').is_symlink():
        raise ValueError('Unsupported local skill file or path')
    name = validate_skill(source, name)
    entry = dict(source=dict(kind='local', path='skills/' + name), auto_update=False)
    validate_entry(name, entry)
    if name in load_registry()['skills']:
        raise ValueError('Skill already registered; existing installation preserved')
    content_hash = validate_local_content(source, name)
    destination = ROOT / 'skills' / name
    if (ROOT / 'skills').is_symlink() or destination.is_symlink():
        raise ValueError('Local skill path contains a symlink')
    if source != destination:
        if source in destination.parents:
            raise ValueError('Local skill source cannot contain its destination')
        if destination.exists():
            raise ValueError('Existing skill folder preserved; choose another name')
    return dict(name=name, source=source, destination=destination, content_sha256=content_hash)


def register_local(args):
    plan = inspect_local_import(args.directory, args.name)
    source, destination = plan['source'], plan['destination']
    content_hash = plan['content_sha256']
    data = load_registry()
    entry = dict(source=dict(kind='local', path='skills/' + args.name), auto_update=False)
    if source != destination:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=destination.parent, prefix='.import-') as temporary:
            staged = Path(temporary) / 'content'
            shutil.copytree(source, staged)
            if validate_local_content(staged, args.name) != content_hash:
                raise ValueError('Local skill changed during import; retry')
            staged.rename(destination)
    git('add', '-A', '--', 'skills/' + args.name)
    content_hash = validate_local_content(destination, args.name, catalog=True)
    entry.update(revision=content_hash, content_sha256=content_hash, executable_sha256=executable_hash(destination, catalog=True))
    data['skills'][args.name] = entry
    save_registry(data)
    print('Registered local skill:', args.name)
    return 0


def shared_skill(name, entry):
    validate_entry(name, entry)
    expected = entry['customization']['content_sha256']
    relative = entry['customization'].get('path') or f'customizations/{name}/{expected}'
    folder = ROOT / relative
    for path in (folder, *folder.parents):
        if path == ROOT:
            break
        if path.is_symlink():
            raise ValueError('Shared skill path contains a symlink')
    if not folder.is_dir() or tree_hash(folder, catalog=relative == 'skills/' + name) != expected:
        raise ValueError('Shared skill content differs from its hash')
    validate_skill(folder, name)
    return folder


def catalog_folder(name):
    folder = ROOT / 'skills' / name
    if (ROOT / 'skills').is_symlink() or folder.is_symlink():
        raise ValueError('Catalog skill path contains a symlink')
    return folder


def folder_state(folder):
    if not folder.exists():
        return None
    return tree_hash(folder), executable_hash(folder)


def stage_verified_payload(name, source):
    """Stage only imported bytes, never runtime files from the editable tree."""
    prefix = 'skills/' + name
    tracked = git('ls-files', '-z', '--', prefix).split(b'\0')
    records = [b'0 ' + b'0' * 40 + b'\t' + path + b'\0' for path in tracked if path]
    for item in content_paths(source):
        if not item.is_file():
            continue
        path = prefix + '/' + item.relative_to(source).as_posix()
        blob = git('hash-object', '-w', '--no-filters', '--', str(item)).strip()
        mode = b'100755' if item.stat().st_mode & 0o111 else b'100644'
        records.append(mode + b' ' + blob + b'\t' + os.fsencode(path) + b'\0')
    result = subprocess.run(['git', '-C', str(ROOT), 'update-index', '-z', '--index-info'],
                            input=b''.join(records), capture_output=True, timeout=120)
    if result.returncode:
        raise ValueError('Could not stage verified skill payload')


def store_catalog_skill(name, source, expected_state=None):
    """Preserve the previous tree, including open-file writes and ignored files."""
    validate_local_content(source, name)
    target = catalog_folder(name)
    if source == target:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent, prefix='.skill-') as temporary:
        staged = Path(temporary) / 'content'
        shutil.copytree(source, staged)
        if target.exists():
            ignored = git('ls-files', '-z', '--others', '--ignored', '--exclude-standard',
                          '--', 'skills/' + name)
            for value in ignored.split(b'\0'):
                if not value:
                    continue
                item = ROOT / os.fsdecode(value)
                destination = staged / item.relative_to(target)
                if destination.exists() or destination.is_symlink():
                    raise ValueError('Upstream overlaps ignored local file; preserved: ' + str(item))
                if item.is_symlink() or not item.is_file():
                    raise ValueError('Unsupported ignored file preserved: ' + str(item))
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, destination)
        validate_local_content(staged, name)
        if folder_state(target) != expected_state:
            raise ValueError('Skill changed during update; local edits preserved')
        backup = None
        if target.exists():
            backup_root = Path(git('rev-parse', '--absolute-git-dir').decode().strip()) / 'skill-backups'
            backup_root.mkdir(exist_ok=True)
            backup = Path(tempfile.mkdtemp(prefix=name + '-', dir=backup_root)) / 'content'
            target.rename(backup)
            if folder_state(backup) != expected_state:
                backup.rename(target)
                raise ValueError('Skill changed during replacement; local edits preserved')
        try:
            staged.rename(target)
        except BaseException:
            if backup is not None and not target.exists():
                backup.rename(target)
            raise
        stage_verified_payload(name, source)
        if backup is not None:
            print(f'{name}: previous folder preserved at {backup}')
        # Retain backup: an editor may still have an open file in the old tree.


def prepare_catalog_skill(name, entry, write=False):
    """Observe committed client edits, or populate a missing approved folder."""
    validate_entry(name, entry)
    before = json.dumps(entry, sort_keys=True)
    folder = catalog_folder(name)
    copied = False
    if not folder.exists():
        if entry['source']['kind'] == 'local':
            raise ValueError('Registered local skill folder is missing')
        folder = materialize(name, entry)[0]
        if write:
            store_catalog_skill(name, folder)
            folder = catalog_folder(name)
            copied = True
    in_catalog = folder == catalog_folder(name)
    state = folder_state(folder)
    actual = validate_local_content(folder, name, catalog=in_catalog)
    modes = executable_hash(folder, catalog=in_catalog)
    if entry['source']['kind'] != 'local' and 'executable_sha256' not in entry:
        if entry['source']['kind'] == 'url':
            # Single-file HTTPS imports always start as a non-executable SKILL.md.
            entry['executable_sha256'] = hashlib.sha256(b'SKILL.md\0-').hexdigest()
        else:
            base_entry = dict(entry)
            base_entry.pop('customization', None)
            baseline = materialize(name, base_entry)[0]
            entry['executable_sha256'] = executable_hash(baseline)
    if folder_state(folder) != state:
        raise ValueError('Skill changed during inspection; local edits preserved')
    if entry['source']['kind'] == 'local':
        entry.update(revision=actual, content_sha256=actual, executable_sha256=modes)
    elif actual == entry['content_sha256'] and modes == entry['executable_sha256']:
        entry.pop('customization', None)
    else:
        entry['customization'] = dict(path='skills/' + name, content_sha256=actual,
            base_revision=entry['revision'], base_sha256=entry['content_sha256'], executable_sha256=modes)
    return folder, before != json.dumps(entry, sort_keys=True), copied, state


def populate_catalog(write=False):
    data = load_registry()
    changed = False
    for name, entry in data['skills'].items():
        _, observed, copied, _ = prepare_catalog_skill(name, entry, write)
        changed |= observed
        if copied or not catalog_folder(name).exists():
            print(f'{name}: populate skills/{name}')
    if write and changed:
        save_registry(data)


