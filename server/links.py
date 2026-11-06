"""A link to a skill as people copy it from a browser, turned into a registry source.

Understood: a repository (https://host/owner/repo[.git], also file:// in tests),
the web view of a folder or of its SKILL.md on GitHub (tree/blob), GitLab (-/tree,
-/blob) and Gitea/Forgejo (src/branch, src/tag), raw.githubusercontent.com, and
any other HTTPS address of a single SKILL.md file.
"""
from pathlib import PurePosixPath
import re
import subprocess
import urllib.parse

# Path segments that start the ref part of a web view, by hosting.
VIEWS = (('tree',), ('blob',), ('-', 'tree'), ('-', 'blob'),
         ('src', 'branch'), ('src', 'tag'), ('src', 'commit'))


def remote_refs(url):
    """Branches and tags of a repository and the branch HEAD points to."""
    try:
        output = subprocess.run(['git', 'ls-remote', '--symref', url], check=True, capture_output=True,
                                text=True, encoding='utf-8', timeout=60).stdout
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
        raise ValueError(f'Репозиторий {url} недоступен: {(getattr(error, "stderr", "") or "").strip()}') from error
    refs, head = set(), None
    for line in output.splitlines():
        if line.startswith('ref: ') and line.endswith('\tHEAD'):
            head = line[len('ref: '):-len('\tHEAD')]
        elif '\t' in line:
            name = line.split('\t', 1)[1]
            if name.startswith(('refs/heads/', 'refs/tags/')) and not name.endswith('^{}'):
                refs.add(name)
    return refs, head


def find_view(parts):
    """Index where the web view starts and its marker, or (None, None) for a plain repository."""
    for index in range(2, len(parts)):
        for marker in VIEWS:
            if tuple(parts[index:index + len(marker)]) == marker:
                return index, marker
    return None, None


def split_ref(rest, marker, refs):
    """The longest leading segments that name a branch or tag; the rest is the folder."""
    if marker == ('src', 'commit'):
        raise ValueError('Это ссылка на коммит. Дайте ссылку на ветку или тег: по ним скилл получает обновления.')
    kinds = {('src', 'branch'): ('refs/heads/',), ('src', 'tag'): ('refs/tags/',)}.get(marker,
                                                                                          ('refs/heads/', 'refs/tags/'))
    for count in range(len(rest), 0, -1):
        for prefix in kinds:
            ref = prefix + '/'.join(rest[:count])
            if ref in refs:
                return ref, rest[count:]
    raise ValueError('Ветка или тег из ссылки в репозитории не найдены.')


def parse(link, refs_of=remote_refs):
    """{'kind': 'git', url, path, ref} or {'kind': 'url', url} for a single file."""
    link = link.strip()
    split = urllib.parse.urlsplit(link)
    if split.scheme not in ('https', 'http', 'file') or (split.scheme != 'file' and not split.netloc):
        raise ValueError('Нужна ссылка https://… на репозиторий, папку скилла или его SKILL.md.')
    if split.scheme == 'http':
        raise ValueError('Ссылка должна быть https://.')
    if split.query or split.fragment:
        split = split._replace(query='', fragment='')
    if split.scheme == 'file':
        url = urllib.parse.urlunsplit(split).rstrip('/')
        refs, head = refs_of(url)
        return dict(kind='git', url=url, path='.', ref=head or 'refs/heads/main')
    parts = [urllib.parse.unquote(part) for part in split.path.split('/') if part]
    if split.netloc == 'raw.githubusercontent.com' and len(parts) >= 4:
        url = f'https://github.com/{parts[0]}/{parts[1]}.git'
        rest, marker = parts[2:], ('blob',)
        if rest[:2] == ['refs', 'heads'] or rest[:2] == ['refs', 'tags']:
            rest = rest[2:]
    else:
        index, marker = find_view(parts)
        if index is None:
            if len(parts) >= 2 and parts[-1].lower().endswith('.md'):
                return dict(kind='url', url=urllib.parse.urlunsplit(split))
            if len(parts) < 2:
                raise ValueError('В ссылке нет владельца и репозитория.')
            base = '/'.join(parts)
            url = f'https://{split.netloc}/{base if base.endswith(".git") else base + ".git"}'
            refs, head = refs_of(url)
            if not head:
                raise ValueError('Не удалось определить основную ветку репозитория; дайте ссылку на ветку.')
            return dict(kind='git', url=url, path='.', ref=head)
        base = '/'.join(parts[:index])
        if base.endswith('/-'):
            base = base[:-2]
        url = f'https://{split.netloc}/{base if base.endswith(".git") else base + ".git"}'
        rest = parts[index + len(marker):]
    if not rest:
        raise ValueError('В ссылке нет ветки или тега.')
    refs, _ = refs_of(url)
    ref, folder = split_ref(rest, marker, refs)
    if folder and (marker[-1] == 'blob' or folder[-1].lower() == 'skill.md'):
        if folder[-1].lower() != 'skill.md':
            raise ValueError('Ссылка ведёт на файл. Дайте ссылку на папку скилла или на его SKILL.md.')
        folder = folder[:-1]
    path = str(PurePosixPath(*folder)) if folder else '.'
    if any(part in ('..', '.') for part in folder) or re.search(r'[\\:]', path):
        raise ValueError('Недопустимый путь в ссылке.')
    return dict(kind='git', url=url, path=path, ref=ref)
