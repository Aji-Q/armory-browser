#!/usr/bin/env python3
"""Real harvest CLI + real loopback HTTP fixtures; no mocks or external sites.

--root / ARMORY_ROOT selects the actual source. --stdlib-only adds Python -S
for a matching dependency-independent baseline/modified/rollback run; by default
installed dependencies remain available. Every command, stdout/stderr, exit code,
HTTP request log and saved artifact is retained under out/cli-live by default.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import unittest
import uuid

sys.dont_write_bytecode = True
HERE = Path(__file__).resolve().parent
ROOT = Path(os.environ.get('ARMORY_ROOT', HERE.parent)).resolve()
EVIDENCE = HERE.parent/'out/cli-live/evidence.json'
OUTPUT = HERE.parent/'out/cli-live/captures'
STDLIB_ONLY = False
BODY = 'Fixture body evidence: observed public research, methods, measurements, controls and reproducible results. ' * 40


def html(title, body=BODY, tail=''):
    return f'<!doctype html><html><head><title>{title}</title></head><body><article><h1>{title}</h1><p>{body}</p></article>{tail}</body></html>'.encode()


class FixtureServer(ThreadingHTTPServer):
    daemon_threads = True
    def __init__(self):
        super().__init__(('127.0.0.1', 0), FixtureHandler)
        self.log = []; self.lock = threading.Lock(); self.counts = {}


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path
        with self.server.lock:
            self.server.counts[path] = self.server.counts.get(path, 0) + 1
            attempt = self.server.counts[path]
            event = {'method':'GET', 'path':path, 'attempt':attempt, 'time':time.time()}
            self.server.log.append(event)
        if path == '/disconnect-once' and attempt == 1:
            event['outcome'] = 'connection_closed_without_response'
            self.close_connection = True
            try: self.connection.shutdown(socket.SHUT_RDWR)
            except OSError: pass
            self.connection.close()
            return
        status, mime = 200, 'text/html; charset=utf-8'
        if path == '/robots.txt':
            payload = b'User-agent: *\nDisallow: /blocked\n'; mime = 'text/plain; charset=utf-8'
        elif path == '/article': payload = html('Lowercase article', 'lowercase-only marker '+BODY)
        elif path == '/Article': payload = html('Uppercase Article', 'UPPERCASE-only marker '+BODY)
        elif path == '/blocked': payload = html('This route must never be requested with robots enabled')
        elif path == '/failure': status, payload = 500, html('Fixture server error')
        elif path == '/disconnect-once': payload = html('Recovered article', 'recovered-after-disconnect '+BODY)
        elif path.startswith('/chain/') and path.rsplit('/',1)[-1].isdigit():
            n = int(path.rsplit('/',1)[-1])
            # Finite eight-page chain makes an unfixed run safe to measure.
            tail = f'<a href="/chain/{n+1}">next</a>' if n < 7 else '<span>next</span>'
            payload = html('Repeated article', BODY, tail)
        else: status, payload = 404, html('Unknown fixture path')
        event.update({'status':status, 'outcome':'response', 'bytes':len(payload)})
        self.send_response(status)
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)
        self.close_connection = True


class RealCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = FixtureServer()
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.origin = f'http://127.0.0.1:{cls.server.server_port}'
        OUTPUT.mkdir(parents=True, exist_ok=True)
        cls.run_dir = OUTPUT/(ROOT.name+('-stdlib-' if STDLIB_ONLY else '-installed-')+uuid.uuid4().hex[:8])
        cls.run_dir.mkdir()
        cls.entries = []

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown(); cls.server.server_close(); cls.thread.join(timeout=3)

    def run_cli(self, paths, *, extra=(), retries=0):
        name = self._testMethodName
        folder = self.run_dir/name; folder.mkdir()
        home = folder/'home'; home.mkdir()
        temp = folder/'tmp'; temp.mkdir()
        urls = [self.origin+p for p in paths]
        if len(urls) == 1: target = urls[0]
        else:
            url_file = folder/'urls.txt'; url_file.write_text('\n'.join(urls)+'\n')
            target = str(url_file)
        out = folder/'out'
        command = [sys.executable]
        if STDLIB_ONLY: command.append('-S')
        command += [str(ROOT/'modules/harvest/harvest.py'), target, '--backend', 'static',
            '--profile', 'aggressive', '--no-impersonate', '--no-backend-memory',
            '--no-rotate-ua', '--no-keepalive', '--retries', str(retries), '--rate', '0',
            '--timeout', '4', '--concurrency', '1', '--json', '--out', str(out), *extra]
        # Deliberately do not inherit *_PROXY, tokens, Python path or user HOME.
        env = {'PATH':'/usr/bin:/bin', 'HOME':str(home), 'TMPDIR':str(temp),
            'PYTHONDONTWRITEBYTECODE':'1', 'PYTHONNOUSERSITE':'1', 'PYTHONUTF8':'1',
            'PYTHONIOENCODING':'utf-8', 'LANG':'en_US.UTF-8', 'NO_PROXY':'127.0.0.1,localhost',
            'no_proxy':'127.0.0.1,localhost'}
        with self.server.lock:
            self.server.log.clear(); self.server.counts.clear()
        started = time.time()
        entry = {'test':name, 'command':command, 'cwd':str(ROOT), 'environment':env,
            'input_urls':urls, 'output_dir':str(out), 'started_utc':datetime.now(timezone.utc).isoformat()}
        self.entries.append(entry)
        try:
            process = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                text=True, encoding='utf-8', timeout=25)
            entry.update({'stdout':process.stdout, 'stderr':process.stderr, 'exit_code':process.returncode})
        except subprocess.TimeoutExpired as exc:
            entry.update({'stdout':(exc.stdout or b'').decode() if isinstance(exc.stdout,bytes) else (exc.stdout or ''),
                'stderr':(exc.stderr or b'').decode() if isinstance(exc.stderr,bytes) else (exc.stderr or ''),
                'exit_code':None, 'execution_error':'TimeoutExpired after 25 seconds; subprocess.run killed/waited for the child'})
            raise
        finally:
            entry['elapsed_seconds'] = round(time.time()-started, 3)
            with self.server.lock: entry['http_requests'] = [dict(item) for item in self.server.log]
            entry['artifacts'] = [{'path':str(p), 'sha256':hashlib.sha256(p.read_bytes()).hexdigest(), 'bytes':p.stat().st_size}
                for p in sorted(out.glob('*')) if p.is_file()]
        index = json.loads((out/'index.json').read_text()) if (out/'index.json').is_file() else None
        records = [json.loads((out/page['file']).read_text()) for page in index['pages']] if index else []
        return process, entry, index, records

    def test_body_is_captured_to_real_index_json_and_markdown(self):
        process, entry, index, records = self.run_cli(['/article'])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(index['count'], 1)
        self.assertEqual(records[0]['url'], entry['input_urls'][0])
        self.assertEqual(records[0]['status'], 200)
        self.assertIn('lowercase-only marker', records[0]['markdown'])
        md = Path(entry['output_dir'])/Path(index['pages'][0]['file']).with_suffix('.md')
        self.assertEqual(md.read_text(), records[0]['markdown'])
        self.assertEqual([r['path'] for r in entry['http_requests']], ['/article'])

    def test_case_distinct_paths_keep_two_matching_saved_records(self):
        process, entry, index, records = self.run_cli(['/Article','/article'])
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(index['count'], 2)
        self.assertEqual(len({p['file'].casefold() for p in index['pages']}), 2)
        self.assertEqual({r['url'] for r in records}, set(entry['input_urls']))
        for page, rec in zip(index['pages'], records):
            self.assertEqual(page['url'], rec['url'])
            marker = 'UPPERCASE-only marker' if rec['url'].endswith('/Article') else 'lowercase-only marker'
            self.assertIn(marker, rec['markdown'])
        self.assertCountEqual([r['path'] for r in entry['http_requests']], ['/Article','/article'])

    def test_duplicate_chain_crawl_three_sends_only_three_page_requests(self):
        process, entry, index, records = self.run_cli(['/chain/0'], extra=('--crawl','3'))
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([r['path'] for r in entry['http_requests']], ['/chain/0','/chain/1','/chain/2'])
        self.assertEqual(index['count'], 1)
        self.assertEqual(len(records), 1)
        self.assertIn('预算耗尽', process.stderr)

    def test_robots_denial_never_requests_the_target(self):
        process, entry, index, records = self.run_cli(['/blocked'], extra=('--respect-robots',))
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([r['path'] for r in entry['http_requests']], ['/robots.txt'])
        self.assertEqual(index['count'], 1)
        self.assertIn('robots.txt', records[0].get('error',''))

    def test_http_500_is_a_nonzero_cli_result(self):
        process, entry, index, records = self.run_cli(['/failure'])
        self.assertNotEqual(process.returncode, 0)
        self.assertEqual([r['path'] for r in entry['http_requests']], ['/failure'])
        self.assertEqual(records[0]['status'], 500)

    def test_successful_retry_clears_prior_connection_error(self):
        process, entry, index, records = self.run_cli(['/disconnect-once'], retries=1)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual([r['outcome'] for r in entry['http_requests']], ['connection_closed_without_response','response'])
        self.assertEqual([r['path'] for r in entry['http_requests']], ['/disconnect-once','/disconnect-once'])
        self.assertEqual(records[0]['status'], 200)
        self.assertFalse(records[0].get('error'))
        self.assertEqual(records[0]['retries'], 1)
        self.assertIn('recovered-after-disconnect', records[0]['markdown'])


def main():
    global ROOT, EVIDENCE, OUTPUT, STDLIB_ONLY
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--evidence', type=Path, default=EVIDENCE)
    parser.add_argument('--output-root', type=Path, default=OUTPUT)
    parser.add_argument('--stdlib-only', action='store_true')
    args = parser.parse_args()
    ROOT, EVIDENCE, OUTPUT = args.root.resolve(), args.evidence.resolve(), args.output_root.resolve()
    STDLIB_ONLY = args.stdlib_only
    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(RealCLI))
    errors = {case._testMethodName: text for case,text in result.errors}
    failures = {case._testMethodName: text for case,text in result.failures}
    for entry in getattr(RealCLI,'entries',[]):
        entry['assertion_outcome'] = 'error' if entry['test'] in errors else 'failed' if entry['test'] in failures else 'passed'
        if entry['test'] in errors or entry['test'] in failures:
            entry['assertion_traceback'] = errors.get(entry['test'],failures.get(entry['test']))
    report = {'schema':'armory-cli-live-v1','runs':[]}
    if EVIDENCE.exists():
        report = json.loads(EVIDENCE.read_text())
        if report.get('schema') != 'armory-cli-live-v1': raise ValueError('Refusing to replace an unknown evidence file')
    report['runs'].append({'root':str(ROOT), 'stdlib_only':STDLIB_ONLY,
        'scope':'real harvest.py subprocess + real loopback HTTP; no browser, external site, or model/Agent API',
        'source_hashes':{name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ('modules/harvest/harvest.py','modules/harvest/engine.py')},
        'test_file_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'tests_run':result.testsRun, 'failures':len(result.failures), 'errors':len(result.errors),
        'passed':result.wasSuccessful(), 'output_dir':str(getattr(RealCLI,'run_dir','')),
        'commands':getattr(RealCLI,'entries',[])})
    EVIDENCE.parent.mkdir(parents=True,exist_ok=True)
    EVIDENCE.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print(f'Evidence: {EVIDENCE}')
    print(f'Captured files: {getattr(RealCLI,"run_dir","")}')
    return 0 if result.wasSuccessful() else 1

if __name__ == '__main__': sys.exit(main())
