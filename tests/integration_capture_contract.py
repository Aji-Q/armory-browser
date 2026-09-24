#!/usr/bin/env python3
"""Cross-language capture contract using real Node validation and relay HTTP.

Browser DOM extraction is a fixture, NOT an installed-extension/browser test.
All HTTP traffic is loopback; public-looking source/secondary URLs are never fetched.
Only stdlib plus Node.js is needed. Temporary token files and SQLite are removed.
"""
from __future__ import annotations
import argparse
import datetime
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

NODE_CHECK = """
import { readFileSync } from 'node:fs';
const { boundedResult } = await import(process.argv[1]);
const { result, job } = JSON.parse(readFileSync(0, 'utf8'));
try { process.stdout.write(JSON.stringify({accepted:true,result:boundedResult(result,job)})); }
catch (error) { process.stdout.write(JSON.stringify({accepted:false,error:String(error.message)})); }
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--node', default=shutil.which('node'))
    args = parser.parse_args()
    root = args.root.resolve()
    if not args.node:
        parser.error('Node.js is required')
    if not (root / 'extension/core.mjs').is_file() or not (root / 'bridge/relay.py').is_file():
        parser.error('--root must contain extension/core.mjs and bridge/relay.py')
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1')
    checks = []

    def check(name, passed, **observed):
        checks.append(bool(passed))
        print(json.dumps({'check': name, 'pass': bool(passed), **observed}, ensure_ascii=False), flush=True)

    def normalize(result, job):
        process = subprocess.run(
            [args.node, '--input-type=module', '-e', NODE_CHECK, (root / 'extension/core.mjs').as_uri()],
            input=json.dumps({'result': result, 'job': job}), capture_output=True, text=True,
            timeout=10, env=env, cwd=root,
        )
        if process.returncode:
            raise RuntimeError('Node validator process failed: ' + process.stderr[:500])
        return json.loads(process.stdout)

    with tempfile.TemporaryDirectory(prefix='armory-capture-contract-') as temporary:
        directory = Path(temporary)
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        base = f'http://127.0.0.1:{port}'
        init = subprocess.run(
            [sys.executable, '-m', 'bridge.relay', 'init', '--output-dir', str(directory), '--relay-url', base],
            cwd=root, env=env, capture_output=True, text=True, timeout=15,
        )
        if init.returncode:
            raise RuntimeError(f'Relay initialization failed (exit={init.returncode}); no credentials logged')
        agent_token = json.loads((directory / 'agent-client.json').read_text())['agent_token']
        browser_token = json.loads((directory / 'browser-client.json').read_text())['browser_token']
        relay = subprocess.Popen(
            [sys.executable, '-m', 'bridge.relay', 'serve', '--config', str(directory / 'server-config.json'),
             '--db', str(directory / 'jobs.sqlite3'), '--host', '127.0.0.1', '--port', str(port)],
            cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def http(method, path, body=None, browser=False, public=False):
            headers = {'Content-Type': 'application/json'}
            if not public:
                headers['Authorization'] = 'Bearer ' + (browser_token if browser else agent_token)
            request = urllib.request.Request(
                base + path, data=None if body is None else json.dumps(body).encode('utf-8'),
                method=method, headers=headers,
            )
            try:
                with opener.open(request, timeout=3) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                return error.code, json.load(error)

        def new_job(url):
            code, payload = http('POST', '/v1/jobs', {'url': url, 'purpose': 'Deterministic contract fixture; do not fetch URLs', 'max_chars': 12000})
            if code != 201:
                raise AssertionError(f'Create job HTTP {code}')
            job = payload['job']
            if job['state'] != 'queued':
                raise AssertionError('New job did not start queued')
            states = ['queued']
            for event, expected_state in [('approve', 'running'), ('preview_ready', 'awaiting_share')]:
                code, transition = http('POST', f"/v1/browser/jobs/{job['id']}/events", {'type': event}, browser=True)
                if code != 200 or transition.get('job', {}).get('state') != expected_state:
                    raise AssertionError(f'{event} HTTP {code} or unexpected state')
                states.append(transition['job']['state'])
            print('OBSERVED_RELAY_STATES=' + ' -> '.join(states), flush=True)
            return job

        try:
            for _ in range(80):
                try:
                    if http('GET', '/health', public=True)[0] == 200:
                        break
                except OSError:
                    pass
                time.sleep(.05)
            else:
                raise AssertionError('Relay startup timeout')
            print('TRANSPORT=real Node core.mjs -> real loopback HTTP -> real SQLite relay -> agent HTTP status', flush=True)
            print('BROWSER_EXTRACTION=simulated fixture; no external navigation, actual agent client or extension installed', flush=True)
            job = new_job('https://example.com/research/article')
            useful_text = 'Research finding: all 48 controlled observations preserve the requested evidence and source context.\n' * 12
            safe_links = [{'text': 'Public reference', 'url': 'https://example.org/reference'}, {'text': 'Search reference', 'url': 'https://example.com/search?q=research'}]
            unsafe_links = [
                {'text': 'Development example', 'url': 'http://localhost:3000/docs'},
                {'text': 'Private host example', 'url': 'http://192.168.1.1/docs'},
                {'text': 'Token example', 'url': 'https://example.com/logout?token=synthetic-fixture'},
                {'text': 'Cloud credential example', 'url': 'https://example.org/storage?X-Amz-Credential=synthetic-fixture'},
                {'text': 'Reserved address example', 'url': 'http://203.0.113.10/reference'},
            ]
            result = {'url': job['url'], 'title': 'Fixture research evidence', 'text': useful_text,
                      'markdown': '# Fixture research evidence\n\n' + useful_text,
                      'links': safe_links + unsafe_links,
                      'captured_at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      'truncated': False, 'degraded': False, 'quality': 'full'}
            normalized = normalize(result, job)
            check('Node accepts valid body and drops unsafe ancillary links',
                  normalized.get('accepted') and normalized['result']['links'] == safe_links,
                  node_accepted=normalized.get('accepted'),
                  link_count=len(normalized.get('result', {}).get('links', [])), expected_link_count=len(safe_links))
            if normalized.get('accepted'):
                code, _ = http('POST', f"/v1/browser/jobs/{job['id']}/events",
                               {'type': 'complete', 'result': normalized['result']}, browser=True)
                status_code, status = http('GET', f"/v1/jobs/{job['id']}")
                stored_job = status.get('job', {})
                stored_result = stored_job.get('result') or {}
                check('Real relay completes filtered body; agent reads unchanged useful text',
                      code == 200 and status_code == 200 and stored_job.get('state') == 'completed'
                      and stored_result.get('text') == useful_text
                      and stored_result.get('links') == safe_links,
                      complete_http=code, agent_status_http=status_code, observed_state=stored_job.get('state'),
                      body_preserved=stored_result.get('text') == useful_text)
            else:
                check('Real relay completes filtered body; agent reads unchanged useful text', False, skipped='Node rejected body')

            exotic = dict(result, links=safe_links + [
                {'text': 'Script example', 'url': 'javascript:alert(1)'},
                {'text': 'Userinfo example', 'url': 'https://fixture:placeholder@example.org/private'},
                {'text': 'Malformed example', 'url': 'not-a-url'},
            ])
            exotic_normalized = normalize(exotic, job)
            check('Non-web and credential-bearing ancillary links cannot poison the body',
                  exotic_normalized.get('accepted') and exotic_normalized['result']['links'] == safe_links,
                  node_accepted=exotic_normalized.get('accepted'),
                  link_count=len(exotic_normalized.get('result', {}).get('links', [])))

            wrong_job = new_job('https://example.com/research/requested')
            wrong_result = dict(result, url='https://example.com/account/home', links=[])
            wrong_normalized = normalize(wrong_result, wrong_job)
            check('Client rejects another page on the same origin', not wrong_normalized.get('accepted'),
                  node_accepted=wrong_normalized.get('accepted'))
            code, _ = http('POST', f"/v1/browser/jobs/{wrong_job['id']}/events",
                           {'type': 'complete', 'result': wrong_result}, browser=True)
            status_code, status = http('GET', f"/v1/jobs/{wrong_job['id']}")
            check('Bypassing client still yields HTTP 400 and leaves job uncompleted',
                  code == 400 and status_code == 200 and status.get('job', {}).get('state') == 'awaiting_share',
                  complete_http=code, observed_state=status.get('job', {}).get('state'))
            print(f'RESULT={sum(checks)}/{len(checks)} contract checks passed', flush=True)
        finally:
            relay.terminate()
            try:
                relay.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()
                relay.communicate()
    return 0 if checks and all(checks) else 1


if __name__ == '__main__':
    raise SystemExit(main())
