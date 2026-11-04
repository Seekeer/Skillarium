#!/usr/bin/env python3
"""Live check of pull-request rules on Gitea, isolated in a temporary base branch.

The real main branch is never touched: a base branch is created from --from,
protected like main, and test pull requests target it. Everything (pull
requests, branches, the protection rule) is closed or deleted at the end.
Credentials come from the Git credential helper (write:repository token).

    python tests/gitea_rules.py --clone-url https://host:port/owner/repo.git --from origin/<feature-branch>

The report lists every hosting response that the clients and server rely on.
"""
import argparse
import json
from pathlib import Path
import shutil
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import hosting  # noqa: E402

SERVER = 'gitea-test-server'
PREFIX = 'agent-skills-pr-test/'
SKILL = 'pr-rule-check'


def git(repo, *args, env=None, check=True):
    result = hosting.run_git(repo, *args, env=env, check=False)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: ' + result.stderr.decode(errors='replace')[-300:])
    return result


class Check:
    def __init__(self, work, api, base):
        self.work = work
        self.base = base
        git(work, 'config', 'agent-skills.api', api)
        self.host = hosting.Hosting(work, base)
        self.results = []
        self.pulls = []
        self.branches = []

    def record(self, name, ok, detail=''):
        self.results.append(dict(check=name, ok=ok, detail=detail))
        print(('PASS ' if ok else 'FAIL ') + name + (f': {detail}' if detail else ''), flush=True)

    def branch(self, name, change, author):
        name = PREFIX + name
        git(self.work, 'fetch', '-q', 'origin')
        git(self.work, 'checkout', '-q', '-B', 'local-test', 'origin/' + self.base)
        change(self.work)
        git(self.work, 'add', '-A')
        git(self.work, 'commit', '-q', '-m', f'Rule check {name}', env=hosting.author_env(author))
        git(self.work, 'push', '-q', 'origin', f'HEAD:refs/heads/{name}')
        self.branches.append(name)
        head = git(self.work, 'rev-parse', 'HEAD').stdout.decode().strip()
        pull = self.host.create_pull(name, 'Rule check (temporary): ' + name,
                                     'Automated Gitea rule check for #4 against a temporary base branch; closed automatically.')
        self.pulls.append(pull['number'])
        return pull['number'], head

    def merge(self, number, head):
        body = {'Do': 'merge', 'merge_when_checks_succeed': True, 'head_commit_id': head,
                'delete_branch_after_merge': True}
        return self.host.request('POST', f'/pulls/{number}/merge', body)

    def wait(self, number, seconds=900):
        deadline = time.time() + seconds
        while time.time() < deadline:
            pull = self.host.get_pull(number)
            if pull.get('merged'):
                return pull
            status = self.host.request('GET', f'/commits/{pull["head"]["sha"]}/status')[1] or {}
            policy = [s for s in status.get('statuses') or [] if 'policy' in s.get('context', '')]
            if policy and policy[0].get('status') in ('failure', 'error'):
                return dict(pull, check_state=policy[0]['status'], context=policy[0]['context'])
            time.sleep(10)
        return dict(self.host.get_pull(number), check_state='timeout')

    def cleanup(self):
        for number in self.pulls:
            try:
                if self.host.get_pull(number).get('state') == 'open':
                    self.host.close_pull(number)
            except Exception as error:
                print(f'cleanup #{number}: {error}')
        self.host.request('DELETE', '/branch_protections/' + self.base.replace('/', '%2F'))
        for branch in self.branches + [self.base]:
            git(self.work, 'push', '-q', 'origin', '--delete', branch, check=False)


def edit_skill(text):
    def change(work):
        path = work / 'skills' / SKILL / 'SKILL.md'
        path.write_text(path.read_text() + text + '\n', newline='\n')
    return change


def edit_registry(work):
    path = work / 'sources.json'
    data = json.loads(path.read_text(encoding='utf-8'))
    data['skills'][SKILL]['note'] = str(time.time())
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + '\n', encoding='utf-8', newline='\n')


def seed(work, source, base):
    git(work, 'checkout', '-q', '-B', 'local-seed', source)
    config = json.loads((work / 'agent-skills.json').read_text())
    config['server']['machine'] = SERVER
    config['publication']['branch'] = base
    (work / 'agent-skills.json').write_text(json.dumps(config, indent=2) + '\n', newline='\n')
    folder = work / 'skills' / SKILL
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'SKILL.md').write_text(f'---\nname: {SKILL}\ndescription: Temporary rule check\n---\nBase\n', newline='\n')
    registry = json.loads((work / 'sources.json').read_text(encoding='utf-8'))
    digest = '0' * 64
    registry['skills'][SKILL] = {'source': {'kind': 'local', 'path': 'skills/' + SKILL}, 'auto_update': False,
                                 'revision': digest, 'content_sha256': digest}
    (work / 'sources.json').write_text(json.dumps(registry, indent=2, ensure_ascii=False) + '\n',
                                       encoding='utf-8', newline='\n')
    git(work, 'add', '-A')
    git(work, 'commit', '-q', '-m', 'Temporary base for the Gitea rule check')
    git(work, 'push', '-q', 'origin', f'HEAD:refs/heads/{base}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clone-url', required=True)
    parser.add_argument('--from', dest='source', required=True, help='Ref containing the policy workflow')
    parser.add_argument('--report', type=Path, default=Path('gitea-rules-report.json'))
    args = parser.parse_args()
    base = PREFIX + 'base'
    temp = Path(tempfile.mkdtemp(prefix='agent-skills-gitea-'))
    check = None
    try:
        work = temp / 'work'
        git(temp, 'clone', '-q', args.clone_url, str(work))
        check = Check(work, hosting.api_base(work), base)
        if git(work, 'rev-parse', '--verify', '-q', 'origin/' + base, check=False).returncode == 0:
            raise RuntimeError('Temporary base branch already exists; inspect it first')
        seed(work, args.source, base)
        protection = dict(rule_name=base, enable_push=False, enable_status_check=True,
                          status_check_contexts=['*policy*'], block_on_outdated_branch=True,
                          required_approvals=0)
        status, item = check.host.request('POST', '/branch_protections', protection)
        check.record('branch protection created on the temporary base', status in (200, 201), f'{status}')
        git(work, 'commit', '-q', '--allow-empty', '-m', 'Direct push attempt')
        direct = git(work, 'push', 'origin', f'HEAD:refs/heads/{base}', check=False)
        check.record('direct push to the protected branch rejected', direct.returncode != 0,
                     direct.stderr.decode(errors='replace').strip().splitlines()[-1:][0] if direct.stderr else '')

        number, head = check.branch('client-edit', edit_skill('client edit'), 'laptop')
        status, item = check.merge(number, head)
        check.record('auto-merge request accepted', status in (200, 201, 204), f'{status} {item}')
        again = check.merge(number, head)
        check.record('repeated auto-merge request (informational)', True, f'{again[0]} {again[1]}')
        result = check.wait(number)
        check.record('client skill edit merged automatically after the check', bool(result.get('merged')),
                     result.get('check_state', ''))

        number, head = check.branch('foreign-control', edit_registry, 'laptop')
        check.merge(number, head)
        result = check.wait(number)
        check.record('control change by a non-server author not merged', not result.get('merged'),
                     f'{result.get("check_state", "")} {result.get("context", "")}')

        number_a, head_a = check.branch('outdated', edit_skill('outdated edit'), 'laptop')
        number_b, head_b = check.branch('server-control', edit_registry, SERVER)
        check.merge(number_b, head_b)
        result = check.wait(number_b)
        check.record('control change by the assigned server merged', bool(result.get('merged')),
                     result.get('check_state', ''))
        status, item = check.merge(number_a, head_a)
        time.sleep(90)
        pull = check.host.get_pull(number_a)
        check.record('outdated pull request not merged', not pull.get('merged'), f'{status} {item}')
        status, item = check.host.request('POST', f'/pulls/{number_a}/update', query=dict(style='merge'))
        check.record('update API merges the base into the proposal', status in (200, 201, 204), f'{status} {item}')
        time.sleep(5)
        pull = check.host.get_pull(number_a)
        stale = check.merge(number_a, head_a)
        check.record('merge request for an old head (informational)', True, f'{stale[0]} {stale[1]}')
        status, item = check.merge(number_a, pull['head']['sha'])
        result = check.wait(number_a)
        check.record('updated proposal merged after a new check', bool(result.get('merged')),
                     f'{status} {result.get("check_state", "")}')
    except Exception as error:
        if check:
            check.record('run completed', False, str(error))
        else:
            print('ERROR', error)
    finally:
        if check:
            check.cleanup()
            args.report.write_text(json.dumps(check.results, indent=2, ensure_ascii=False), encoding='utf-8')
            print('Report:', args.report)
        shutil.rmtree(temp, ignore_errors=True)
    return 0 if check and all(item['ok'] for item in check.results) else 1


if __name__ == '__main__':
    sys.exit(main())
