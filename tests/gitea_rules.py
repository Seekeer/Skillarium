#!/usr/bin/env python3
"""Live check of pull-request rules on a disposable Gitea test repository.

Never run it against the real library: it replaces main of the given
repository, configures its branch protection and opens test pull requests.
Credentials come from the Git credential helper (write:repository token).

    python tests/gitea_rules.py --clone-url https://host:port/owner/test.git --yes-replace-main

The report lists every hosting response that the clients and server rely on.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
import hosting  # noqa: E402

SERVER = 'gitea-test-server'
FILES = ('.gitea/workflows/policy.yml', 'server/policy.py', 'server/hosting.py', 'server/skills.py',
         '.gitattributes')


def git(repo, *args, env=None, check=True):
    result = hosting.run_git(repo, *args, env=env, check=False)
    if check and result.returncode:
        raise RuntimeError(f'git {args[0]} failed: ' + result.stderr.decode(errors='replace')[-300:])
    return result


class Check:
    def __init__(self, work, api):
        self.work = work
        subprocess.run(['git', '-C', str(work), 'config', 'agent-skills.api', api], check=True)
        self.host = hosting.Hosting(work)
        self.results = []

    def record(self, name, ok, detail=''):
        self.results.append(dict(check=name, ok=ok, detail=detail))
        print(('PASS ' if ok else 'FAIL ') + name + (f': {detail}' if detail else ''), flush=True)

    def branch(self, name, change, author):
        git(self.work, 'checkout', '-q', '-B', name, 'origin/main')
        change(self.work)
        git(self.work, 'add', '-A')
        git(self.work, 'commit', '-q', '-m', f'Test {name}', env=hosting.author_env(author))
        git(self.work, 'push', '-q', '-f', 'origin', f'{name}:refs/heads/{name}')
        head = git(self.work, 'rev-parse', 'HEAD').stdout.decode().strip()
        pull = self.host.create_pull(name, 'Rule check ' + name, 'Automated Gitea rule check; safe to close.')
        return pull['number'], head

    def merge(self, number, head):
        body = {'Do': 'merge', 'merge_when_checks_succeed': True, 'head_commit_id': head,
                'delete_branch_after_merge': True}
        return self.host.request('POST', f'/pulls/{number}/merge', body)

    def wait(self, number, seconds=600):
        deadline = time.time() + seconds
        while time.time() < deadline:
            pull = self.host.get_pull(number)
            if pull.get('merged'):
                return pull
            statuses = self.host.request('GET', f'/commits/{pull["head"]["sha"]}/status')[1] or {}
            if statuses.get('state') in ('failure', 'error'):
                return dict(pull, check_state=statuses.get('state'))
            time.sleep(10)
        return dict(self.host.get_pull(number), check_state='timeout')


def edit_skill(text):
    def change(work):
        path = work / 'skills/demo/SKILL.md'
        path.write_text(path.read_text() + '\n' + text + '\n', newline='\n')
    return change


def edit_policy(work):
    path = work / 'sources.json'
    data = json.loads(path.read_text())
    data['skills']['demo']['note'] = str(time.time())
    path.write_text(json.dumps(data, indent=2) + '\n', newline='\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--clone-url', required=True)
    parser.add_argument('--yes-replace-main', action='store_true', required=True)
    parser.add_argument('--report', type=Path, default=Path('gitea-rules-report.json'))
    args = parser.parse_args()
    temp = Path(tempfile.mkdtemp(prefix='agent-skills-gitea-'))
    try:
        work = temp / 'work'
        git(temp, 'clone', '-q', args.clone_url, str(work))
        api = hosting.api_base(work)
        check = Check(work, api)
        git(work, 'checkout', '-q', '--orphan', 'seed')
        git(work, 'rm', '-rq', '--cached', '--ignore-unmatch', '.')
        for relative in FILES:
            (work / relative).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, work / relative)
        (work / 'agent-skills.json').write_text(json.dumps(
            {'version': 1, 'server': {'machine': SERVER}, 'publication': {'branch': 'main'}}, indent=2) + '\n')
        (work / 'skills/demo').mkdir(parents=True, exist_ok=True)
        (work / 'skills/demo/SKILL.md').write_text('---\nname: demo\ndescription: Rule check\n---\nBase\n', newline='\n')
        digest = '0' * 64
        (work / 'sources.json').write_text(json.dumps({'version': 2, 'skills': {'demo': {
            'source': {'kind': 'local', 'path': 'skills/demo'}, 'auto_update': False,
            'revision': digest, 'content_sha256': digest}}}, indent=2) + '\n')
        git(work, 'add', '-A')
        git(work, 'commit', '-q', '-m', 'Seed rule check')
        protection = dict(rule_name='main', enable_push=False, enable_status_check=True,
                          status_check_contexts=['*policy*'], block_on_outdated_branch=True,
                          required_approvals=0)
        check.host.request('DELETE', '/branch_protections/main')
        git(work, 'push', '-q', '-f', 'origin', 'seed:main')
        git(work, 'fetch', '-q', 'origin')
        status, item = check.host.request('POST', '/branch_protections', protection)
        check.record('branch protection created', status in (200, 201), f'{status} {item}')
        git(work, 'commit', '-q', '--allow-empty', '-m', 'Direct push attempt')
        direct = git(work, 'push', 'origin', 'HEAD:main', check=False)
        check.record('direct push to main rejected', direct.returncode != 0)

        number, head = check.branch('rule-client-edit', edit_skill('client edit'), 'laptop')
        status, item = check.merge(number, head)
        check.record('auto-merge request accepted', status in (200, 201, 204), f'{status} {item}')
        again = check.merge(number, head)
        check.record('repeated auto-merge request response', True, f'{again[0]} {again[1]}')
        result = check.wait(number)
        check.record('client skill edit merged automatically after the check', bool(result.get('merged')),
                     result.get('check_state', ''))

        git(work, 'fetch', '-q', 'origin')
        number, head = check.branch('rule-foreign-control', edit_policy, 'laptop')
        check.merge(number, head)
        result = check.wait(number)
        check.record('control change by a non-server author not merged', not result.get('merged'),
                     result.get('check_state', ''))
        check.host.close_pull(number)

        git(work, 'fetch', '-q', 'origin')
        number_a, head_a = check.branch('rule-outdated', edit_skill('outdated edit'), 'laptop')
        number_b, head_b = check.branch('rule-server-control', edit_policy, SERVER)
        check.merge(number_b, head_b)
        result = check.wait(number_b)
        check.record('control change by the assigned server merged', bool(result.get('merged')),
                     result.get('check_state', ''))
        status, item = check.merge(number_a, head_a)
        time.sleep(60)
        pull = check.host.get_pull(number_a)
        check.record('outdated pull request not merged', not pull.get('merged'), f'{status} {item}')
        status, item = check.host.request('POST', f'/pulls/{number_a}/update', query=dict(style='merge'))
        check.record('update API merges main into the proposal', status in (200, 201, 204), f'{status}')
        time.sleep(5)
        pull = check.host.get_pull(number_a)
        status, item = check.merge(number_a, pull['head']['sha'])
        result = check.wait(number_a)
        check.record('updated proposal merged after a new check', bool(result.get('merged')),
                     f'{status} {result.get("check_state", "")}')
        stale = check.merge(number_a, head_a)
        check.record('response to a merge request for an old head (informational)', True, f'{stale[0]} {stale[1]}')
    finally:
        args.report.write_text(json.dumps(check.results if 'check' in locals() else [], indent=2, ensure_ascii=False))
        shutil.rmtree(temp, ignore_errors=True)
    print('Report:', args.report)
    return 0 if all(item['ok'] for item in check.results) else 1


if __name__ == '__main__':
    sys.exit(main())
