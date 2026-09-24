#!/usr/bin/env python3
"""Offline regressions for user-review F2/F5/F6/F8; ARMORY_ROOT selects a tree.

No external request, installed browser, user credentials, or real signer process
is used. The separate e2e_chain CLI is the genuine loopback integration gate.
"""
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get('ARMORY_ROOT', Path(__file__).resolve().parents[1])).resolve()

def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def invoke(call):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = call()
        except SystemExit as exc:
            code = exc.code
    return code, out.getvalue(), err.getvalue()


class AcceptanceFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sites = load('user_acceptance_sites', ROOT / 'tools/e2e_10sites.py')
        cls.chain = load('user_acceptance_chain', ROOT / 'tools/e2e_chain.py')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='armory-acceptance-unit-')
        self.addCleanup(self.temp.cleanup)
        self.tmp = Path(self.temp.name)
        self.network = patch.object(socket, 'socket', side_effect=AssertionError('Offline test attempted network'))
        self.network.start(); self.addCleanup(self.network.stop)

    def response(self, html, status=200):
        return SimpleNamespace(html=html, status=status, error='', backend='static', verdict='静态直连', headers={})

    def assess(self, html, minimum=800, markdown=None, status=200):
        return self.sites.assess(self.response(html, status), self.sites.ex.html_to_markdown(html) if markdown is None else markdown, minimum)

    def test_long_destination_urls_do_not_count_as_body(self):
        html = '<h1>Navigation</h1><p>' + ''.join('<a href="https://example.com/' + 'very-long-target-' * 90 + str(i) + '">x</a>' for i in range(3)) + '</p>'
        ok, issues, stats = self.assess(html)
        self.assertFalse(ok)
        self.assertTrue(any('正文过短' in issue for issue in issues))
        self.assertLess(stats['content_chars'], 30)
        self.assertLessEqual(stats['purity'], 1)

    def test_link_targets_reference_links_and_bare_urls_cannot_pad_text(self):
        html = '<h1>Links</h1><p>' + 'https://example.com/' + 'bare-address-' * 100 + '</p>'
        md = '# Links\n\n[x][r]\n\n[r]: https://example.com/' + 'ref-address-' * 100 + '\n\n' + 'https://example.com/' + 'bare-address-' * 100
        ok, _, stats = self.assess(html, markdown=md)
        self.assertFalse(ok)
        self.assertLess(stats['content_chars'], 30)

    def test_visible_navigation_labels_remain_useful(self):
        html = '<nav><ul>' + ''.join('<li><a href="https://example.com/' + 'target-' * 100 + str(i) + '">' + f'Research topic {i}: observed methodology and reproducible measurements for independent review.' + '</a></li>' for i in range(20)) + '</ul></nav>'
        ok, issues, stats = self.assess(html)
        self.assertTrue(ok, issues)
        self.assertGreater(stats['content_chars'], 800)
        self.assertGreaterEqual(stats['list_items'], 20)

    def test_ordered_lists_remain_structural_content(self):
        html = '<ol>' + ''.join('<li>' + f'Research observation {i}: a reproducible comparison of the observed data and limitations. ' + '</li>' for i in range(12)) + '</ol>'
        ok, issues, stats = self.assess(html)
        self.assertTrue(ok, issues)
        self.assertEqual(stats['list_items'], 12)

    def test_normal_article_and_table_pass(self):
        samples = ['<h1>Research</h1><p>' + 'Measured content supports a reproducible observation. ' * 30 + '</p>', '<table><tr><th>Research condition</th><th>Observation</th></tr>' + ''.join('<tr><td>Condition '+str(i)+'</td><td>'+'Verified measured results and limitations of this controlled experiment. '+'</td></tr>' for i in range(15)) + '</table>']
        for html in samples:
            with self.subTest(kind=html[:10]):
                ok, issues, _ = self.assess(html)
                self.assertTrue(ok, issues)

    def test_purity_is_enforced_for_mostly_unrepresented_text(self):
        html = '<h1>Research</h1><p>' + 'Visible source material. ' * 500 + '</p>'
        ok, issues, stats = self.assess(html, minimum=100, markdown='# Research\n\n'+'An isolated excerpt. '*10)
        self.assertFalse(ok)
        self.assertTrue(any('纯净' in issue or '覆盖' in issue for issue in issues))
        self.assertLess(stats['purity'], .2)

    def test_nonresponse_redirect_and_error_statuses_fail(self):
        html = '<h1>Research</h1><p>' + 'Observed text. '*100 + '</p>'
        for status in (0, 199, 302, 403, 500):
            with self.subTest(status=status): self.assertFalse(self.assess(html, status=status)[0])

    def sites_cli(self, *args, run_site=None):
        with patch.object(sys, 'argv', ['e2e_10sites.py', '--out', str(self.tmp/'reports'), *args]):
            with patch.object(self.sites, 'run_site', side_effect=run_site):
                return invoke(self.sites.main)

    def fake_site(self, target, timeout, outdir, min_chars=800):
        region, url, label, level = target
        return {'region':region,'url':url,'label':label,'level':level,'host':url.split('/')[2], 'ok':True,'issues':[],'backend':'offline fixture','stats':{'md_chars':1000,'content_chars':900,'purity':1,'headings':1,'paragraphs':1},'elapsed':0.0,'verdict':'static','markdown_file':None}

    def test_each_run_has_unique_report_and_complete_metadata(self):
        code, _, _ = self.sites_cli('--jobs','1',run_site=self.fake_site)
        self.assertEqual(code,0)
        existing = {p: p.read_bytes() for p in (self.tmp/'reports').rglob('report.json')}
        self.assertEqual(len(existing),1)
        code, _, _ = self.sites_cli('--site','books.toscrape.com','--jobs','1', '--min-chars','450','--timeout','7',run_site=self.fake_site)
        self.assertEqual(code,0)
        paths = list((self.tmp/'reports').rglob('report.json'))
        self.assertEqual(len(paths),2, 'A subset must not overwrite full-run evidence')
        self.assertTrue(all(p.read_bytes()==raw for p,raw in existing.items()))
        reports = [json.loads(p.read_text()) for p in paths]
        self.assertEqual(len({r['run_id'] for r in reports}),2)
        for report in reports:
            self.assertIn('started_at',report);self.assertIn('finished_at',report)
            self.assertLessEqual(report['started_at'],report['finished_at'])
            self.assertEqual(len(report['targets']),report['total'])
            self.assertEqual(report['source_hashes']['tools/e2e_10sites.py'],hashlib.sha256((ROOT/'tools/e2e_10sites.py').read_bytes()).hexdigest())
            self.assertIn('modules/harvest/engine.py',report['source_hashes'])
            self.assertIn('argv',report); self.assertIn('arguments',report)
        subset = next(r for r in reports if r['total']==1)
        self.assertEqual(subset['arguments']['min_chars'],450)
        self.assertEqual(subset['arguments']['timeout'],7)
        self.assertEqual(subset['scope']['kind'],'subset')
        self.assertEqual(next(r for r in reports if r['total']==10)['scope']['kind'],'full')

    def test_runner_exception_finishes_failed_report(self):
        code, _, _ = self.sites_cli('--site','books.toscrape.com','--jobs','1',run_site=RuntimeError('controlled runner failure'))
        self.assertEqual(code,1)
        reports = list((self.tmp/'reports').rglob('report.json'))
        self.assertEqual(len(reports),1)
        result=json.loads(reports[0].read_text());self.assertTrue(result['finished_at']);self.assertEqual(result['passed'],0)
        self.assertIn('controlled runner failure',result['run_error'])

    def test_empty_selection_stays_exit_two(self):
        code, _, _ = self.sites_cli('--site','missing-target.invalid',run_site=self.fake_site)
        self.assertEqual(code,2)

    def test_serial_parallel_and_fallback_keep_minimum(self):
        for jobs in (1,3):
            seen=[]
            def fake(*args): seen.append(args[3]);return self.fake_site(*args)
            code,_,_=self.sites_cli('--only','us','--jobs',str(jobs),'--min-chars','5432',run_site=fake)
            self.assertEqual(code,0);self.assertEqual(seen,[5432]*5)
        html='<h1>Research</h1><p>'+'Short measured data. '*70+'</p>'
        result=self.response(html)
        engine=SimpleNamespace(fetch=lambda *a,**kw:result,fetch_camoufox=lambda *a,**kw:result)
        with patch.object(self.sites.eng,'has_module',return_value=True):
            fetched=self.sites.fetch_one({'engine':engine},'https://example.com',min_chars=5000)
        self.assertFalse(fetched[2])

    def chain_probe(self, payload=None, returncode=0, process_error=None, fetch_error=None, thread_error=None):
        chain=self.chain; calls=[]; created=[]
        if payload is None: payload={'ok':True,'q':'hello','items':[{'id':i,'name':f'item-{i}'} for i in range(1,4)]}
        body = payload if isinstance(payload,str) else json.dumps(payload)
        class Server:
            server_address=('127.0.0.1',12345)
            def serve_forever(self): pass
            def shutdown(self): calls.append('shutdown')
            def server_close(self): calls.append('server_close')
        class Engine:
            def fetch_static(self,url):
                if fetch_error: raise fetch_error
                signed='&sign=' in url
                return SimpleNamespace(error='',status=200 if signed or url.endswith('/') else 403,html=body if signed else 'fixture',headers={})
        original_mkdtemp=tempfile.mkdtemp
        def mkdtemp(*args,**kwargs):
            if len(args) >= 3:
                args = (*args[:2], str(self.tmp), *args[3:])
            else:
                kwargs['dir'] = str(self.tmp)
            path=original_mkdtemp(*args,**kwargs);created.append(Path(path));return path
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(chain,'ThreadingHTTPServer',return_value=Server()))
            stack.enter_context(patch.object(chain.threading.Thread,'start',side_effect=thread_error))
            stack.enter_context(patch.object(chain.eng,'Engine',return_value=Engine()))
            stack.enter_context(patch.object(chain.scout_mod,'build_evidence',return_value={'api_hints':[]}))
            stack.enter_context(patch.object(chain.scout_mod,'decide',return_value=('疑似参数签名',[],{},None)))
            stack.enter_context(patch.object(chain.signer,'find_salts',return_value=[]))
            stack.enter_context(patch.object(chain.tempfile,'mkdtemp',side_effect=mkdtemp))
            stack.enter_context(patch.object(chain.subprocess,'run',side_effect=process_error,return_value=subprocess.CompletedProcess([],returncode,stdout=chain.py_sign('/api/data?q=hello'),stderr='controlled stderr')))
            code,out,err=invoke(chain.main)
        return code,calls,created,out,err

    def test_chain_valid_business_result_and_resources(self):
        code,calls,created,_,_=self.chain_probe();self.assertEqual(code,0)
        self.assertEqual(calls,['shutdown','server_close']);self.assertTrue(created);self.assertTrue(all(not p.exists() for p in created))

    def test_chain_rejects_wrong_business_schema_and_values(self):
        valid_items=[{'id':i,'name':f'item-{i}'} for i in range(1,4)]
        for payload in ({},{'ok':False,'q':'wrong','items':[]},{'ok':True,'q':'hello','items':'items'}, {'ok':True,'q':'wrong','items':valid_items},{'ok':True,'q':'hello','items':[{'id':True,'name':'item-1'},*valid_items[1:]]},{'ok':True,'q':'hello','items':[{'id':1,'name':'wrong'},*valid_items[1:]]},[1,2,3]):
            with self.subTest(payload=payload):self.assertEqual(self.chain_probe(payload)[0],1)

    def test_chain_signer_nonzero_and_non_json_still_fail(self):
        self.assertEqual(self.chain_probe(returncode=2)[0],1)
        self.assertEqual(self.chain_probe(payload='<h1>not JSON</h1>')[0],1)

    def test_chain_timeout_cleans_server_and_temporary_directory(self):
        code,calls,created,_,err=self.chain_probe(process_error=subprocess.TimeoutExpired('controlled signer',90))
        self.assertEqual(code,1);self.assertEqual(calls,['shutdown','server_close']);self.assertIn('TimeoutExpired',err)
        self.assertTrue(created);self.assertTrue(all(not p.exists() for p in created))

    def test_chain_fetch_failure_cleans_server(self):
        code,calls,_,_,_=self.chain_probe(fetch_error=RuntimeError('controlled fetch failure'))
        self.assertEqual(code,1);self.assertEqual(calls,['shutdown','server_close'])

    def test_chain_thread_start_failure_closes_without_shutdown_deadlock(self):
        code,calls,_,_,_=self.chain_probe(thread_error=RuntimeError('controlled thread start failure'))
        self.assertEqual(code,1);self.assertEqual(calls,['server_close'])

if __name__ == '__main__':unittest.main(verbosity=2)
