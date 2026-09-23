#!/usr/bin/env python3
"""Offline tests for auto -> human -> timeout fallback. No real browser/state.
Run with --root PATH to load another Armory tree; defaults to this tree.
"""
from __future__ import annotations

import argparse
import importlib.util
import io
import socket
import subprocess
import sys
import threading
import time
import unittest
from contextlib import ExitStack, redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[1]
URL = 'https://fixture.invalid/article'
GOOD = '<title>Article</title><h1>正文</h1><p>' + '授权正文 content ' * 50 + '</p>'
WALL = '<title>请先登录</title><body>请先登录后查看</body>'


def forbidden(*a, **k):
    raise AssertionError('Unexpected network/browser process in offline test')


class HumanFallback(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('human_fallback_harvest', ROOT / 'modules/harvest/harvest.py')
        cls.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.h)

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('connect', 'connect_ex', 'bind'):
            self.stack.enter_context(patch.object(socket.socket, name, forbidden))
        self.stack.enter_context(patch.object(socket, 'create_connection', forbidden))
        self.stack.enter_context(patch.object(subprocess, 'Popen', forbidden))
        self.stack.enter_context(patch.object(subprocess, 'run', forbidden))
        self.stack.enter_context(patch.object(self.h.ho, 'default_state_path', return_value=Path('/fixture-only/state.json')))
        self.stack.enter_context(patch.object(self.h.eng, 'make_cffi_session', return_value=None))
        self.args = SimpleNamespace(backend='auto', css=None, regex=None, regex_on_text=False,
                                    cdp=None, browser_profile=None, keep_browser=False,
                                    links=False, tables=False, format='json', max_chars=6000,
                                    wait_human=10, concurrency=32, crawl=0,
                                    crawl_same_host=True, max_crawl_attempts=None)

    def result(self, html=WALL, status=200, **kwargs):
        return self.h.eng.Result(url=URL, final_url=URL, html=html, status=status,
                                 backend='static', bytes=len(html.encode()), **kwargs)

    def engine(self, *responses):
        return SimpleNamespace(fetch=Mock(side_effect=responses), cookies_dict={},
                               impersonate='chrome', proxies=[], timeout=5, _cffi=None)

    def fetch(self, engine, reply):
        with patch.object(self.h.ho, 'handoff_fetch', return_value=reply) as handoff, redirect_stderr(io.StringIO()):
            result = self.h.fetch_with_wait_human(engine, URL, self.args, self.h.scout_mod, 10)
        return result, handoff

    def test_normal_body_never_calls_handoff(self):
        original = self.result(GOOD)
        result, handoff = self.fetch(self.engine(original), {'ok': False})
        self.assertIs(result, original)
        handoff.assert_not_called()

    def test_timeout_preserves_original_and_has_explicit_state(self):
        original = self.result(WALL)
        result, handoff = self.fetch(self.engine(original), {'ok': True, 'degraded': True, 'html': 'do not replace original'})
        self.assertIs(result, original)
        self.assertEqual(result.html, WALL)
        self.assertTrue(getattr(result, 'degraded', False))
        self.assertEqual(getattr(result, 'human_status', None), 'timeout')
        self.assertTrue(any('降级为自动结果' in note for note in result.notes))
        self.assertEqual(handoff.call_args.kwargs['timeout'], 10)

    def test_timeout_record_is_partial_not_complete_success(self):
        result, _ = self.fetch(self.engine(self.result(WALL)), {'degraded': True})
        record = self.h.build_record(result, self.args)
        self.assertEqual(record['status'], 200)  # Preserve observed HTTP, not invented success/failure.
        self.assertTrue(hasattr(self.h, 'record_ok'))
        self.assertFalse(self.h.record_ok(record))

    def test_handoff_start_exception_preserves_auto(self):
        original = self.result()
        with patch.object(self.h.ho, 'handoff_fetch', side_effect=RuntimeError('fixture browser unavailable')), redirect_stderr(io.StringIO()):
            result = self.h.fetch_with_wait_human(self.engine(original), URL, self.args, self.h.scout_mod, 10)
        self.assertIs(result, original)
        self.assertEqual(getattr(result, 'human_status', None), 'failed')
        self.assertTrue(getattr(result, 'degraded', False))

    def test_timeout_metadata_survives_record_and_empty_body(self):
        for html in (WALL, ''):
            with self.subTest(html_empty=not html):
                original = self.result(html, status=403)
                result, _ = self.fetch(self.engine(original), {'degraded': True})
                record = self.h.build_record(result, self.args)
                self.assertIs(record.get('degraded'), True)
                self.assertEqual(record.get('human_status'), 'timeout')
                self.assertTrue(record.get('human_reason'))

    def test_human_success_preserves_url_body_and_state(self):
        result, _ = self.fetch(self.engine(self.result()), {'ok': True, 'html': GOOD, 'url': URL + '?ready=1'})
        self.assertEqual(result.html, GOOD)
        self.assertEqual(result.final_url, URL + '?ready=1')
        self.assertEqual(result.bytes, len(GOOD.encode('utf-8')))
        record = self.h.build_record(result, self.args)
        self.assertIs(record.get('degraded'), False)
        self.assertEqual(record.get('human_status'), 'completed')

    def assert_refetch_preserves_human(self, response):
        result, _ = self.fetch(self.engine(self.result(), response),
                               {'ok': True, 'html': GOOD, 'url': URL, 'cookies': {'fixture': 'not-a-real-secret'}})
        self.assertEqual(result.html, GOOD)
        self.assertEqual(result.backend, 'handoff')
        self.assertTrue(any('沿用接管结果' in note for note in result.notes))
        return result

    def test_error_refetch_cannot_replace_human_body(self):
        self.assert_refetch_preserves_human(self.result('', status=0, error='fixture fetch error'))

    def test_exception_refetch_cannot_replace_human_body(self):
        self.assert_refetch_preserves_human(RuntimeError('fixture exception'))

    def test_forbidden_refetch_cannot_replace_human_body(self):
        self.assert_refetch_preserves_human(self.result(WALL * 2, status=403))

    def test_login_wall_200_refetch_cannot_replace_human_body(self):
        self.assert_refetch_preserves_human(self.result(WALL * 2))

    def test_short_refetch_cannot_replace_better_human_body(self):
        self.assert_refetch_preserves_human(self.result('<title>Article</title><p>short response</p>'))

    def test_good_refetch_can_resume_automatic_backend(self):
        refreshed = self.result(GOOD + '<p>more complete content</p>')
        result, _ = self.fetch(self.engine(self.result(), refreshed),
                               {'ok': True, 'html': GOOD, 'cookies': {'fixture': 'not-a-real-secret'}})
        self.assertIs(result, refreshed)
        self.assertEqual(self.h.build_record(result, self.args).get('human_status'), 'completed')

    def test_handoff_failure_preserves_auto_as_degraded(self):
        original = self.result()
        result, _ = self.fetch(self.engine(original), {'ok': False, 'reason': 'fixture browser unavailable'})
        self.assertIs(result, original)
        self.assertIs(self.h.build_record(result, self.args).get('degraded'), True)
        self.assertEqual(getattr(result, 'human_status', None), 'failed')

    def run_concurrency(self, wait_human, crawl):
        self.args.wait_human = 10 if wait_human else None
        self.args.crawl = 3 if crawl else 0
        root = 'https://fixture.invalid/'
        lock = threading.Lock()
        automatic_barrier = threading.Barrier(2)
        active, peak = 0, 0

        def measured():
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            if not wait_human:
                automatic_barrier.wait(timeout=2)
            else:
                time.sleep(0.015)
            with lock:
                active -= 1

        def fetch(url, **kwargs):
            if crawl and url == root:
                html = GOOD + '<a href="/a">A</a><a href="/b">B</a>'
            else:
                if not wait_human:
                    measured()
                html = WALL if wait_human else GOOD + url
            return self.h.eng.Result(url=url, final_url=url, html=html, status=200, backend='static')

        def human(url, **kwargs):
            measured()
            return {'ok': True, 'html': GOOD + url, 'url': url}

        engine = SimpleNamespace(fetch=fetch)
        urls = [root] if crawl else [root + 'a', root + 'b']
        with patch.object(self.h.ho, 'handoff_fetch', side_effect=human), redirect_stderr(io.StringIO()):
            records = self.h.worker(self.args, urls, engine)
        self.assertTrue(all(not rec.get('error') for rec in records), records)
        self.assertEqual(len(records), 3 if crawl else 2)
        return peak

    def test_wait_human_crawl_has_one_human_at_a_time(self):
        self.assertEqual(self.run_concurrency(wait_human=True, crawl=True), 1)

    def test_wait_human_batch_has_one_human_at_a_time(self):
        self.assertEqual(self.run_concurrency(wait_human=True, crawl=False), 1)

    def test_automatic_batch_keeps_concurrency(self):
        self.assertGreater(self.run_concurrency(wait_human=False, crawl=False), 1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    args, remaining = parser.parse_known_args()
    ROOT = args.root.resolve()
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
