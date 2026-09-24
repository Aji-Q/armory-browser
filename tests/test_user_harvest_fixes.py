#!/usr/bin/env python3
"""Offline regressions for user-source harvest fixes. No browser or network.
ARMORY_ROOT selects the source; fixture files stay beside this test file.
--crawl counts URL fetch attempts, not unique output records or transport retries.
"""
import contextlib
import copy
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
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get('ARMORY_ROOT', Path(__file__).resolve().parents[1]))
TEST_HOME = Path(__file__).resolve().parent

def forbidden(*a, **k):
    raise AssertionError('Unexpected network/browser process in offline harvest test')

def invoke(fn):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try: code = fn()
        except SystemExit as exc: code = exc.code
    return code, out.getvalue(), err.getvalue()

class UserHarvestFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('user_fixes_harvest', ROOT/'modules/harvest/harvest.py')
        cls.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.h)

    def setUp(self):
        self.stack = contextlib.ExitStack(); self.addCleanup(self.stack.close)
        for name in ('connect', 'connect_ex', 'bind'):
            self.stack.enter_context(patch.object(socket.socket, name, forbidden))
        self.stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        self.stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix='user-harvest-', dir=TEST_HOME)))

    def assert_index(self, out, urls):
        data = json.loads((out/'index.json').read_text())
        self.assertEqual(data['count'], len(urls))
        self.assertEqual([r['url'] for r in data['pages']], urls)
        self.assertEqual(len({r['file'].casefold() for r in data['pages']}), len(urls))
        for page in data['pages']:
            saved = json.loads((out/page['file']).read_text())
            self.assertEqual(saved['url'], page['url'])
            self.assertEqual((out/Path(page['file']).with_suffix('.md')).read_text(), page['url'])
        return data

    def records(self, urls):
        return [{'url': url, 'status': 200, 'markdown': url} for url in urls]

    def test_case_distinct_urls_cannot_overwrite_each_other(self):
        urls = ['https://fixture.invalid/Article', 'https://fixture.invalid/article']
        self.h.save(self.records(urls), self.tmp, None)
        self.assert_index(self.tmp, urls)

    def test_natural_slug_and_generated_suffix_collision_in_both_orders(self):
        urls = ['https://fixture.invalid/article', 'https://fixture.invalid/article?q=1']
        suffix = hashlib.sha1(urls[1].encode()).hexdigest()[:6]
        urls += ['https://fixture.invalid/article-'+suffix]
        for number, order in enumerate((urls, [urls[0], urls[2], urls[1]])):
            out = self.tmp/str(number); self.h.save(self.records(order), out, None)
            self.assert_index(out, order)

    def test_secondary_hash_collision_still_allocates_distinct_files(self):
        urls = ['https://fixture.invalid/a?q='+str(i) for i in range(4)]
        fake = SimpleNamespace(hexdigest=lambda: '123456'+'a'*34)
        with patch.object(self.h.hashlib, 'sha1', return_value=fake):
            self.h.save(self.records(urls), self.tmp, None)
        self.assert_index(self.tmp, urls)

    def test_unknown_existing_json_and_markdown_are_preserved(self):
        url = 'https://fixture.invalid/article'
        stem = self.h.slugify(url)
        old = {stem+'.json': b'user JSON, not Armory', stem+'.md': b'user note'}
        for name, data in old.items(): (self.tmp/name).write_bytes(data)
        self.h.save(self.records([url]), self.tmp, None)
        self.assert_index(self.tmp, [url])
        for name, data in old.items(): self.assertEqual((self.tmp/name).read_bytes(), data)

    def test_existing_markdown_only_is_not_replaced(self):
        url = 'https://fixture.invalid/article'
        md = self.tmp/(self.h.slugify(url)+'.md'); md.write_text('user-only markdown')
        self.h.save(self.records([url]), self.tmp, None)
        self.assertEqual(md.read_text(), 'user-only markdown')
        self.assert_index(self.tmp, [url])

    def test_unknown_index_is_rejected_before_any_page_write(self):
        before = b'{"personal":"do not overwrite"}'
        (self.tmp/'index.json').write_bytes(before)
        with self.assertRaises(FileExistsError):
            self.h.save(self.records(['https://fixture.invalid/a']), self.tmp, None)
        self.assertEqual((self.tmp/'index.json').read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ['index.json'])

    def test_repeated_save_updates_owned_index_without_replacing_old_page_bytes(self):
        url = 'https://fixture.invalid/article'
        self.h.save(self.records([url]), self.tmp, None)
        first = json.loads((self.tmp/'index.json').read_text())['pages'][0]['file']
        old = (self.tmp/first).read_bytes()
        self.h.save(self.records([url]), self.tmp, None)
        latest = self.assert_index(self.tmp, [url])['pages'][0]['file']
        self.assertNotEqual(first.casefold(), latest.casefold())
        self.assertEqual((self.tmp/first).read_bytes(), old)

    def test_index_symlink_cannot_overwrite_unrelated_file(self):
        target = self.tmp/'notes'; target.write_text('preserve')
        (self.tmp/'index.json').symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.h.save(self.records(['https://fixture.invalid/a']), self.tmp, None)
        self.assertEqual(target.read_text(), 'preserve')

    def transaction_fixture(self, old):
        out = self.tmp / ('old' if old else 'new')
        out.mkdir()
        if old:
            self.h.save(self.records(['https://fixture.invalid/original']), out, None)
        return out, {p.name: p.read_bytes() for p in out.iterdir()}

    def assert_transaction_rolled_back(self, out, before):
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, before,
                         'failed save must preserve every old byte and leave no new page/temp file')
        # An IO error must not make the directory unusable on the next attempt.
        retry = ['https://fixture.invalid/retry']
        self.h.save(self.records(retry), out, None)
        self.assert_index(out, retry)
        for name, data in before.items():
            if name != 'index.json': self.assertEqual((out/name).read_bytes(), data)

    def test_late_page_write_failure_removes_all_new_pages(self):
        real_open = Path.open
        class PartialWriter:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def write(self, data):
                self.stream.write(data[:20]); self.stream.flush()
                raise OSError('controlled disk-full in later page')
        @contextlib.contextmanager
        def broken_open(path, *a, **kw):
            with real_open(path, *a, **kw) as stream:
                yield PartialWriter(stream) if path.name.endswith('__later.md') else stream
        for old in (False, True):
            with self.subTest(old_index=old):
                out, before = self.transaction_fixture(old)
                with patch.object(Path, 'open', broken_open), self.assertRaisesRegex(OSError, 'controlled disk-full'):
                    self.h.save(self.records(['https://fixture.invalid/first', 'https://fixture.invalid/later']), out, None)
                self.assert_transaction_rolled_back(out, before)

    def test_partial_index_temporary_write_preserves_old_index_and_pages(self):
        real_fdopen = os.fdopen
        @contextlib.contextmanager
        def broken_fdopen(*a, **kw):
            with real_fdopen(*a, **kw) as stream:
                class PartialWriter:
                    def __getattr__(self, name): return getattr(stream, name)
                    def write(self, data):
                        stream.write(data[:20]); stream.flush()
                        raise OSError('controlled disk-full in temporary index')
                yield PartialWriter()
        for old in (False, True):
            with self.subTest(old_index=old):
                out, before = self.transaction_fixture(old)
                with patch.object(os, 'fdopen', broken_fdopen), self.assertRaisesRegex(OSError, 'controlled disk-full'):
                    self.h.save(self.records(['https://fixture.invalid/first', 'https://fixture.invalid/later']), out, None)
                self.assert_transaction_rolled_back(out, before)

    def test_index_atomic_commit_failure_rolls_back_pages_and_temp_file(self):
        for old in (False, True):
            with self.subTest(old_index=old):
                out, before = self.transaction_fixture(old)
                operation = 'replace' if old else 'link'
                with patch.object(os, operation, side_effect=OSError('controlled index commit failure')), \
                     self.assertRaisesRegex(OSError, 'controlled index commit failure'):
                    self.h.save(self.records(['https://fixture.invalid/first', 'https://fixture.invalid/later']), out, None)
                self.assert_transaction_rolled_back(out, before)

    def test_first_index_commit_does_not_replace_an_unknown_racing_file(self):
        out = self.tmp / 'race'; out.mkdir()
        real_link = os.link
        def raced_link(source, target, *a, **kw):
            Path(target).write_bytes(b'unknown concurrent index')
            return real_link(source, target, *a, **kw)
        with patch.object(os, 'link', raced_link), self.assertRaises(FileExistsError):
            self.h.save(self.records(['https://fixture.invalid/first']), out, None)
        self.assertEqual({p.name: p.read_bytes() for p in out.iterdir()}, {'index.json':b'unknown concurrent index'})

    def crawl(self, pages, count, limit=None):
        calls = []
        h = self.h
        class Engine:
            def fetch(self, url, **kw):
                calls.append(url)
                value = pages(url) if callable(pages) else pages[url]
                if isinstance(value, Exception): raise value
                return copy.deepcopy(value)
        args = SimpleNamespace(wait_human=None,backend='static',crawl=count,concurrency=2,
            links=False,crawl_same_host=True,max_crawl_attempts=limit)
        with patch.object(h, 'build_record', side_effect=lambda res, *a, **k: res):
            results, _, stderr = invoke(lambda: h.worker(args, ['https://fixture.invalid/'], Engine()))
        return results, calls, stderr

    @staticmethod
    def rec(path, fp, links=(), **extra):
        return {'url':'https://fixture.invalid/'+path, 'status':200, 'backend':'fixture',
            '_fp':fp, 'links':[{'href':'https://fixture.invalid/'+p} for p in links], **extra}

    def test_crawl_limit_counts_duplicate_fetches_not_only_unique_results(self):
        def page(url):
            n = int(url.rsplit('/',1)[1] or '0')
            # Finite input also makes the unfixed baseline safe to evaluate.
            return self.rec(str(n) if n else '', 'same', [str(n+1)] if n < 7 else [])
        results, calls, stderr = self.crawl(page, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(results), 1)
        self.assertIn('预算耗尽', stderr)
        self.assertTrue(any('3' in n for n in results[0].get('notes', [])))

    def test_legacy_attempt_limit_can_reduce_but_never_expand_crawl_limit(self):
        def page(url):
            n = int(url.rsplit('/',1)[1] or '0')
            return self.rec(str(n) if n else '', 'same', [str(n+1)] if n < 7 else [])
        for limit, expected in ((2,2),(8,3)):
            with self.subTest(limit=limit): self.assertEqual(len(self.crawl(page, 3, limit)[1]), expected)

    def test_duplicate_page_unique_link_is_followed_within_remaining_budget(self):
        rows = [self.rec('', 'same', ['a']), self.rec('a', 'same', ['b']), self.rec('b', 'unique')]
        results, calls, _ = self.crawl({r['url']:r for r in rows}, 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual([r['url'] for r in results], [rows[0]['url'], rows[2]['url']])
        self.assertTrue(all('links' not in r for r in results))

    def test_queued_frontier_is_not_lost_when_budget_allows_it(self):
        rows = [self.rec('', 'root', ['a','b','c']), self.rec('a', 'same'), self.rec('b', 'same'), self.rec('c', 'unique')]
        results, calls, _ = self.crawl({r['url']:r for r in rows}, 4)
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(results), 3)
        self.assertEqual(calls[-1], rows[-1]['url'])

    def test_failed_duplicate_is_retained_and_does_not_discover_links(self):
        rows = [self.rec('', 'root', ['a','b']), self.rec('a', 'same'),
            self.rec('b', 'same', ['never'], status=500, error='fixture failure')]
        results, calls, _ = self.crawl({r['url']:r for r in rows}, 4)
        self.assertEqual(len(results), 3)
        self.assertEqual(len(calls), 3)
        self.assertEqual(results[-1]['error'], 'fixture failure')

    def test_fetch_exception_returns_renderable_failure_record(self):
        records, calls, _ = self.crawl({'https://fixture.invalid/': RuntimeError('fixture failure')}, 2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(records[0]['status'], 0)
        rendered = self.h.render_human(records[0], SimpleNamespace(format='markdown'))
        self.assertIn('fixture failure', rendered)

    def cli(self, records, out=False):
        async def batch(*a): return copy.deepcopy(records)
        argv = ['harvest.py','https://fixture.invalid/','--json']
        if out: argv += ['-o',str(self.tmp/'cli')]
        with patch.object(sys,'argv',argv), patch.object(self.h.eng,'Engine',return_value=SimpleNamespace(_cffi=None)), \
             patch.object(self.h,'_batch',batch):
            return invoke(self.h.main)[0]

    def test_exit_status_is_independent_of_output_and_rejects_incomplete_results(self):
        cases = [([],1), ([{'url':'https://fixture.invalid/a','status':0}],1),
            ([{'url':'https://fixture.invalid/a','status':302}],1),
            ([{'url':'https://fixture.invalid/a','status':200,'degraded':True}],1),
            ([{'url':'https://fixture.invalid/a','status':200}],0),
            ([{'url':'https://fixture.invalid/a','status':200},{'url':'https://fixture.invalid/b','status':500,'error':'failed'}],1)]
        for records, expected in cases:
            for out in (False,True):
                with self.subTest(records=records,out=out): self.assertEqual(self.cli(records,out), expected)

    def test_negative_crawl_budget_is_rejected_before_engine_construction(self):
        with patch.object(sys,'argv',['harvest.py','https://fixture.invalid/','--crawl','-1']), \
             patch.object(self.h.eng,'Engine',side_effect=AssertionError('must validate CLI before engine')):
            code, _, _ = invoke(self.h.main)
        self.assertEqual(code, 2)

if __name__ == '__main__': unittest.main(verbosity=2)
