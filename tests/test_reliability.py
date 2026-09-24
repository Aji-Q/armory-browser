#!/usr/bin/env python3
"""Offline reliability regression: --root selects the source tree under test.

No real browser, credentials, network, signer subprocess or external site is used.
Temporary files live below this test file's own optimized tree, not --root.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import io
import json
import socket
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stderr, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
TEST_HOME = Path(__file__).resolve().parent
ROOT = TEST_HOME.parent


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def forbidden(*args, **kwargs):
    raise AssertionError("Unexpected network or subprocess in offline reliability tests")


def invoke(function):
    stdout, stderr = io.StringIO(), io.StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        try:
            result = function()
        except SystemExit as exc:
            result = exc.code
    return result, stdout.getvalue(), stderr.getvalue()


class Reliability(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.harvest = load("reliability_harvest", ROOT / "modules/harvest/harvest.py")
        cls.sites = load("reliability_sites", ROOT / "tools/e2e_10sites.py")
        cls.chain = load("reliability_chain", ROOT / "tools/e2e_chain.py")
        cls.html = "<title>fixture</title><h1>heading</h1><p>" + "readable content " * 65 + "</p>"
        cls.md = cls.sites.ex.html_to_markdown(cls.html)
        cls.response = SimpleNamespace(error="", status=200, html=cls.html,
                                       backend="static", verdict="static")

    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("connect", "connect_ex", "bind"):
            self.stack.enter_context(patch.object(socket.socket, name, forbidden))
        self.stack.enter_context(patch.object(socket, "create_connection", forbidden))
        self.stack.enter_context(patch.object(subprocess, "Popen", forbidden))
        self.stack.enter_context(patch.object(subprocess, "run", forbidden))
        self.tmp = Path(self.stack.enter_context(tempfile.TemporaryDirectory(
            prefix="reliability-", dir=TEST_HOME)))

    def crawl(self, fixtures, count=3, attempt_limit=None):
        args = SimpleNamespace(wait_human=None, backend="static", crawl=count,
                               concurrency=1, links=False, crawl_same_host=True,
                               max_crawl_attempts=attempt_limit)
        fetched = []

        class FakeEngine:
            def fetch(self, url, **kwargs):
                fetched.append(url)
                value = fixtures(url) if callable(fixtures) else fixtures[url]
                if isinstance(value, Exception):
                    raise value
                return copy.deepcopy(value)

        with patch.object(self.harvest, "build_record", side_effect=lambda r, *a, **k: r):
            records, _, stderr = invoke(lambda: self.harvest.worker(
                args, ["https://fixture.invalid/"], FakeEngine()))
        return records, fetched, stderr

    @staticmethod
    def record(url, fingerprint, links=()):
        return {"url": "https://fixture.invalid/" + url, "_fp": fingerprint,
                "status": 200, "backend": "fixture",
                "links": [{"href": "https://fixture.invalid/" + link} for link in links]}

    def test_save_final_slug_collisions_preserve_each_record(self):
        urls = ["https://fixture.invalid/article", "https://fixture.invalid/article?q=1"]
        suffix = hashlib.sha1(urls[1].encode()).hexdigest()[:6]
        urls.append("https://fixture.invalid/article-" + suffix)
        for index, order in enumerate((urls, [urls[0], urls[2], urls[1]])):
            out = self.tmp / str(index)
            records = [{"url": url, "markdown": url} for url in order]
            self.harvest.save(records, out, SimpleNamespace())
            pages = json.loads((out / "index.json").read_text())["pages"]
            self.assertEqual(len({r["file"] for r in pages}), 3)
            for page in pages:
                saved = json.loads((out / page["file"]).read_text())
                self.assertEqual(saved["url"], page["url"])

    # --crawl is now a URL request budget, not a quota of distinct bodies.
    # These frontier fixtures need four fetches to reach three unique bodies.
    def test_crawl_preserves_frontier_after_duplicates(self):
        records = [self.record("", "root", ["a", "b", "c"]),
                   self.record("a", "same"), self.record("b", "same"),
                   self.record("c", "unique")]
        result, fetched, _ = self.crawl({r["url"]: r for r in records}, count=4)
        self.assertEqual(len(result), 3)
        self.assertIn("https://fixture.invalid/c", fetched)

    def test_duplicate_content_still_discovers_new_links(self):
        records = [self.record("", "root", ["a", "b"]),
                   self.record("a", "same"), self.record("b", "same", ["c"]),
                   self.record("c", "unique")]
        result, fetched, _ = self.crawl({r["url"]: r for r in records}, count=4)
        self.assertEqual(len(result), 3)
        self.assertIn("https://fixture.invalid/c", fetched)
        self.assertTrue(all("links" not in r for r in result))

    def test_crawl_repeated_content_respects_attempt_budget(self):
        def repeated(url):
            n = int(url.rsplit("/", 1)[1] or 0)
            return self.record(str(n) if n else "", "same", [str(n + 1)] if n < 30 else [])
        records, fetched, stderr = self.crawl(repeated, count=10, attempt_limit=5)
        self.assertEqual(len(fetched), 5)
        self.assertEqual(len(records), 1)
        self.assertIn("5", stderr)

    def test_crawl_worker_exception_becomes_failure_record(self):
        result, fetched, _ = self.crawl({"https://fixture.invalid/": RuntimeError("fixture failure")})
        self.assertEqual(len(result), 1)
        self.assertIn("fixture failure", result[0]["error"])

    def test_duplicate_error_pages_are_not_hidden(self):
        records = [self.record("", "root", ["a", "b"]),
                   self.record("a", "same"), self.record("b", "same")]
        records[-1]["error"] = "failed"
        result, _, _ = self.crawl({r["url"]: r for r in records})
        self.assertEqual(len(result), 3)
        self.assertTrue(any(r.get("error") == "failed" for r in result))

    def test_handoff_cli_preserves_success_and_failure(self):
        for success in (True, False):
            fake = {"ok": success, "url": "https://fixture.invalid/", "html": self.html,
                    "reason": "fixture"}
            with patch.object(sys, "argv", ["harvest.py", fake["url"], "--handoff", "--json"]), \
                    patch.object(self.harvest.eng, "Engine", return_value=SimpleNamespace(_cffi=None)), \
                    patch.object(self.harvest.ho, "handoff_fetch", return_value=fake), \
                    patch.object(self.harvest.ho, "default_state_path", return_value=self.tmp / "state.json"):
                self.assertEqual(invoke(self.harvest.main)[0], 0 if success else 1)

    def harvest_cli(self, records, output):
        async def fake_batch(*args):
            return copy.deepcopy(records)
        argv = ["harvest.py", "https://fixture.invalid/", "--json"]
        if output:
            argv += ["--out", str(self.tmp / "out")]
        with patch.object(sys, "argv", argv), \
                patch.object(self.harvest.eng, "Engine", return_value=SimpleNamespace(_cffi=None, proxy_pool=None)), \
                patch.object(self.harvest, "_batch", fake_batch), \
                patch.object(self.harvest, "render_human", side_effect=lambda r, a: json.dumps(r)):
            return invoke(self.harvest.main)[0]

    def test_small_cli_worker_failure_renders_without_crashing(self):
        class Engine:
            _cffi = None
            def fetch(self, *args, **kwargs):
                raise RuntimeError("fixture worker failure")
        with patch.object(sys, "argv", ["harvest.py", "https://fixture.invalid/", "--backend", "static"]), \
                patch.object(self.harvest.eng, "Engine", return_value=Engine()):
            code, stdout, _ = invoke(self.harvest.main)
        self.assertEqual(code, 1)
        self.assertIn("fixture worker failure", stdout)

    def test_crawl_cli_keeps_auto_policy_and_attempt_budget(self):
        records = [{"url": "https://fixture.invalid/", "status": 200, "backend": "fixture"}]
        for extra in ([], ["--max-crawl-attempts", "7"]):
            argv = ["harvest.py", records[0]["url"], "--crawl", "3", "--backend", "auto", "--json", *extra]
            engine = SimpleNamespace(_cffi=None)
            with patch.object(sys, "argv", argv), \
                    patch.object(self.harvest.eng, "Engine", return_value=engine), \
                    patch.object(self.harvest.eng, "has_module", return_value=True), \
                    patch.object(self.harvest.eng, "AsyncEngine", side_effect=AssertionError("CLI bypassed Engine policy")), \
                    patch.object(self.harvest, "worker", return_value=copy.deepcopy(records)) as worker, \
                    patch.object(self.harvest, "render_human", return_value="fixture"):
                self.assertEqual(invoke(self.harvest.main)[0], 0)
                worker.assert_called_once()
                args, urls, received_engine = worker.call_args.args
                self.assertEqual(args.backend, "auto")
                self.assertEqual(args.crawl, 3)
                self.assertEqual(args.max_crawl_attempts, 7 if extra else None)
                self.assertIs(received_engine, engine)

    def test_partial_failure_cli_exit_is_independent_of_output(self):
        records = [{"url": "https://fixture.invalid/good", "status": 200, "backend": "fixture"},
                   {"url": "https://fixture.invalid/bad", "status": 500, "backend": "fixture", "error": "failed"}]
        self.assertEqual(self.harvest_cli(records, False), 1)
        self.assertEqual(self.harvest_cli(records, True), 1)

    def test_successful_cli_keeps_exit_zero(self):
        records = [{"url": "https://fixture.invalid/good", "status": 200, "backend": "fixture"}]
        self.assertEqual(self.harvest_cli(records, False), 0)
        self.assertEqual(self.harvest_cli(records, True), 0)

    def test_cli_rejects_status_zero_even_without_error(self):
        records = [{"url": "https://fixture.invalid/bad", "status": 0, "backend": "fixture"}]
        self.assertEqual(self.harvest_cli(records, False), 1)
        self.assertEqual(self.harvest_cli(records, True), 1)

    def sites_cli(self, *argv):
        fake = SimpleNamespace(fetch=lambda *a, **k: self.response)
        with patch.object(sys, "argv", ["e2e_10sites.py", "--out", str(self.tmp / "sites"), *argv]), \
                patch.object(self.sites.eng, "Engine", return_value=fake), \
                patch.object(self.sites.eng, "has_module", return_value=False):
            return invoke(self.sites.main)[0]

    def test_min_chars_reaches_serial_and_parallel_assessment(self):
        for jobs in (1, 2):
            with self.subTest(jobs=jobs):
                self.assertEqual(self.sites_cli("--site", "books.toscrape.com", "--min-chars", "5000", "--jobs", str(jobs)), 1)

    def test_min_chars_reaches_fallback_assessment(self):
        fake = SimpleNamespace(fetch=lambda *a, **k: self.response,
                               fetch_camoufox=lambda *a, **k: self.response)
        with patch.object(self.sites.eng, "has_module", return_value=True):
            # Invoke via run_site so older signatures fail the same contract rather than
            # passing on a mocked implementation of assess itself.
            with patch.object(self.sites.eng, "Engine", return_value=fake):
                try:
                    result = self.sites.run_site(self.sites.SITES[0], 1, self.tmp, min_chars=5000)
                except TypeError:
                    self.fail("run_site does not accept/forward min_chars")
        self.assertFalse(result["ok"])

    def test_empty_site_selection_cannot_succeed(self):
        self.assertNotEqual(self.sites_cli("--site", "not-in-config.invalid"), 0)

    def test_long_link_destinations_are_not_body_content(self):
        html = '<h1>Navigation</h1><p>' + ''.join(
            '<a href="https://fixture.invalid/' + 'long-path-' * 70 + str(i) + '">x</a>'
            for i in range(3)) + '</p>'
        response = SimpleNamespace(error="", status=200, html=html)
        ok, issues, _ = self.sites.assess(response, self.sites.ex.html_to_markdown(html))
        self.assertFalse(ok)
        self.assertTrue(any("正文过短" in issue for issue in issues))

    def test_status_zero_redirect_and_errors_fail_assessment(self):
        for status in (0, 199, 302, 403, 500):
            with self.subTest(status=status):
                res = SimpleNamespace(**{**vars(self.response), "status": status})
                self.assertFalse(self.sites.assess(res, self.md)[0])

    def test_real_visible_content_passes(self):
        self.assertTrue(self.sites.assess(self.response, self.md)[0])
        self.assertEqual(self.sites_cli("--site", "books.toscrape.com", "--jobs", "1"), 0)

    def chain_probe(self, returncode=0, payload=None, process_error=None):
        chain = self.chain
        calls = []
        if payload is None:
            payload = json.dumps({"ok": True, "q": "hello", "items": [
                {"id": i, "name": f"item-{i}"} for i in range(1, 4)]})

        class Server:
            server_address = ("127.0.0.1", 12345)
            def serve_forever(self):
                pass
            def shutdown(self):
                calls.append("shutdown")
            def server_close(self):
                calls.append("server_close")

        class Engine:
            def fetch_static(self, url):
                signed = "&sign=" in url
                return SimpleNamespace(error="", status=200 if signed or url.endswith("/") else 403,
                                       html=payload if signed else "fixture", headers={})

        expected = chain.py_sign("/api/data?q=hello")
        original_tempdir = tempfile.TemporaryDirectory
        original_mkdtemp = tempfile.mkdtemp
        with ExitStack() as stack:
            stack.enter_context(patch.object(chain, "ThreadingHTTPServer", return_value=Server()))
            stack.enter_context(patch.object(chain.threading.Thread, "start"))
            stack.enter_context(patch.object(chain.eng, "Engine", return_value=Engine()))
            stack.enter_context(patch.object(chain.scout_mod, "build_evidence", return_value={"api_hints": []}))
            stack.enter_context(patch.object(chain.scout_mod, "decide", return_value=("疑似参数签名", [], {}, None)))
            stack.enter_context(patch.object(chain.signer, "find_salts", return_value=[]))
            stack.enter_context(patch.object(chain.tempfile, "mkdtemp", side_effect=lambda *a, **k: original_mkdtemp(dir=self.tmp)))
            stack.enter_context(patch.object(chain.tempfile, "TemporaryDirectory", side_effect=lambda *a, **k: original_tempdir(dir=self.tmp)))
            stack.enter_context(patch.object(chain.subprocess, "run", side_effect=process_error,
                                            return_value=subprocess.CompletedProcess([], returncode, stdout=expected, stderr="fixture stderr")))
            result, stdout, stderr = invoke(chain.main)
        return result, calls, stdout

    def test_chain_nonzero_signer_cannot_succeed(self):
        self.assertEqual(self.chain_probe(returncode=1)[0], 1)

    def test_chain_nonjson_cannot_succeed(self):
        self.assertEqual(self.chain_probe(payload="<h1>not JSON</h1>")[0], 1)

    def test_chain_wrong_payload_cannot_succeed(self):
        for payload in ("{}", "[]", '{"ok":true,"q":"wrong","items":[]}'):
            with self.subTest(payload=payload):
                self.assertEqual(self.chain_probe(payload=payload)[0], 1)

    def test_chain_valid_path_and_server_cleanup(self):
        result, calls, _ = self.chain_probe()
        self.assertEqual(result, 0)
        self.assertEqual(calls, ["shutdown", "server_close"])

    def test_chain_timeout_is_failure_and_cleans_server(self):
        result, calls, _ = self.chain_probe(process_error=subprocess.TimeoutExpired("fixture", 90))
        self.assertEqual(result, 1)
        self.assertEqual(calls, ["shutdown", "server_close"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args, remaining = parser.parse_known_args()
    ROOT = args.root.resolve()
    unittest.main(argv=[sys.argv[0], *remaining], verbosity=2)
