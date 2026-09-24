#!/usr/bin/env python3
"""User-source regressions: retry outcome and additive CA trust, no external network/browser.

ARMORY_ROOT selects the source under test; run this same file against B/M/R.
Browser checks verify adapter arguments only, not an installed browser session.
Leaf-certificate checks use disposable OpenSSL keys and a loopback HTTPS server.
"""
import asyncio
from contextlib import redirect_stderr
import gc
import importlib.util
import io
import os
from pathlib import Path
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

sys.dont_write_bytecode = True
ROOT = Path(os.environ.get('ARMORY_ROOT', Path(__file__).resolve().parents[1]))
URL = 'https://fixture.example/article'
BODY = b'<article>Offline regression fixture.</article>'


class UserEngineFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('user_engine_under_test', ROOT / 'modules/harvest/engine.py')
        cls.engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.engine
        spec.loader.exec_module(cls.engine)
        spec = importlib.util.spec_from_file_location('user_engine_harvest', ROOT / 'modules/harvest/harvest.py')
        cls.harvest = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {'engine': cls.engine}):
            spec.loader.exec_module(cls.harvest)
        cls.defaults = set(ssl.create_default_context().get_ca_certs(binary_form=True))
        if not cls.defaults:
            raise RuntimeError('Default CA roots are required for the additive-trust fixture')
        cls.temp = tempfile.TemporaryDirectory(prefix='armory-user-engine-')
        cls.ca = Path(cls.temp.name) / 'one-existing-root.pem'
        cls.ca.write_text(ssl.DER_cert_to_PEM_cert(next(iter(cls.defaults))))

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def client(self, **kwargs):
        return self.engine.Engine(prefer_cffi=False, backend_memory=False, timeout=1, **kwargs)

    def assert_merged_roots(self, pem):
        self.assertIsInstance(pem, str, 'curl needs a merged CA bundle, not just the private file')
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.load_verify_locations(cadata=pem)
        self.assertTrue(self.defaults <= set(context.get_ca_certs(binary_form=True)), 'default trusted roots were removed')

    def test_success_after_transport_retry_clears_only_final_error(self):
        client = self.client(retries=1)
        with patch.object(client, '_request_pooled', side_effect=[OSError('fixture reset'), (200, URL, {}, BODY)]), patch.object(self.engine.time, 'sleep'):
            result = client.fetch_static(URL)
        self.assertEqual(result.status, 200)
        self.assertEqual(result.error, '')
        self.assertEqual(result.retries, 1)
        self.assertEqual(client.stats['errors'], 1)
        self.assertIn('Offline regression fixture.', result.html)

    def test_exhausted_retry_still_reports_transport_failure(self):
        client = self.client(retries=1)
        with patch.object(client, '_request_pooled', side_effect=OSError('fixture reset')), patch.object(self.engine.time, 'sleep'):
            result = client.fetch_static(URL)
        self.assertEqual(result.status, 0)
        self.assertIn('fixture reset', result.error)
        self.assertEqual(result.retries, 1)

    def test_explicit_ca_adds_to_python_defaults_and_keeps_hostname_checks(self):
        client = self.client(ca_file=self.ca)
        self.assertTrue(self.defaults <= set(client.ssl_ctx.get_ca_certs(binary_form=True)), 'ca_file must add trust, not replace system roots')
        self.assertEqual(client.ssl_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(client.ssl_ctx.check_hostname)

    def test_explicit_ca_adds_to_curl_session_defaults(self):
        session = self.engine.make_cffi_session(verify=str(self.ca))
        self.assertIsNotNone(session)
        self.addCleanup(session.close)
        bundle = Path(session.verify)
        self.assert_merged_roots(bundle.read_text())
        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
        session.close()
        self.assertFalse(bundle.exists(), 'closing the session must remove its temporary trust bundle')

    def test_async_curl_uses_same_additive_trust(self):
        seen = {}
        class Session:
            def __init__(self, **kwargs):
                seen.update(kwargs)
                seen['pem'] = Path(kwargs['verify']).read_text()
            async def __aenter__(self): return self
            async def __aexit__(self, *args): return False
        with patch('curl_cffi.requests.AsyncSession', Session):
            result = asyncio.run(self.engine.AsyncEngine(ca_file=self.ca).fetch_many([]))
        self.assertEqual(result, [])
        self.assert_merged_roots(seen.get('pem'))
        self.assertFalse(Path(seen['verify']).exists(), 'the async batch must clean up its temporary trust bundle')

    def test_default_strict_and_insecure_only_when_explicit(self):
        strict, insecure = self.client(), self.client(insecure=True)
        self.assertIs(strict.tls_verify, True)
        self.assertEqual(strict.ssl_ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(strict.ssl_ctx.check_hostname)
        self.assertIs(insecure.tls_verify, False)
        self.assertEqual(insecure.ssl_ctx.verify_mode, ssl.CERT_NONE)
        self.assertFalse(insecure.ssl_ctx.check_hostname)
        self.assertIs(self.engine.AsyncEngine().tls_verify, True)
        self.assertIs(self.engine.AsyncEngine(insecure=True).tls_verify, False)

    def test_unsupported_browser_ca_is_explicit_before_starting_a_browser(self):
        client = self.client(ca_file=self.ca)
        with patch.object(self.engine, 'playwright_module', return_value=None), patch.object(self.engine, 'has_module', return_value=False):
            for method in (client.fetch_render, client.fetch_camoufox, client.fetch_scrapling):
                with self.subTest(method=method.__name__):
                    self.assertIn('ca_file', method(URL).error)
            result, error = client.fetch_sniff(URL)
        self.assertIsNone(result)
        self.assertIn('ca_file', error)

    def test_explicit_insecure_reaches_render_adapter(self):
        browser, page, manager = MagicMock(), MagicMock(), MagicMock()
        page.goto.return_value = SimpleNamespace(status=200, headers={})
        page.url, page.content.return_value = URL, BODY.decode()
        browser.new_context.return_value.new_page.return_value = page
        manager.__enter__.return_value.chromium.launch.return_value = browser
        module = SimpleNamespace(sync_playwright=lambda: manager)
        with patch.object(self.engine, 'playwright_module', return_value='playwright'), patch.object(self.engine.importlib, 'import_module', return_value=module):
            result = self.client(insecure=True).fetch_render(URL)
        self.assertFalse(result.error)
        self.assertIs(browser.new_context.call_args.kwargs.get('ignore_https_errors'), True)

    def test_explicit_insecure_reaches_camoufox_and_sniff_adapters(self):
        browser, page, manager = MagicMock(), MagicMock(), MagicMock()
        page.goto.return_value = SimpleNamespace(status=200, headers={})
        page.url, page.content.return_value = URL, BODY.decode()
        browser.new_page.return_value = page
        manager.__enter__.return_value = browser
        factory = MagicMock(return_value=manager)
        with patch.dict(sys.modules, {'camoufox.sync_api': SimpleNamespace(Camoufox=factory)}), patch.object(self.engine, 'has_module', return_value=True):
            for persistent in (None, '/unused-mock-profile'):
                result = self.client(insecure=True).fetch_camoufox(URL, persistent_dir=persistent)
                self.assertFalse(result.error)
                kwargs = factory.call_args.kwargs if persistent else browser.new_page.call_args.kwargs
                self.assertIs(kwargs.get('ignore_https_errors'), True)
            result, used = self.client(insecure=True).fetch_sniff(URL, wait_ms=0)
        self.assertEqual(used, 'camoufox')
        self.assertIs(browser.new_page.call_args.kwargs.get('ignore_https_errors'), True)

    def test_explicit_insecure_reaches_scrapling_and_binary_adapters(self):
        fetch = MagicMock(return_value=SimpleNamespace(html_content=BODY.decode(), status=200, url=URL))
        with patch.dict(sys.modules, {'scrapling.fetchers': SimpleNamespace(StealthyFetcher=SimpleNamespace(fetch=fetch))}), patch.object(self.engine, 'has_module', return_value=True):
            result = self.client(insecure=True).fetch_scrapling(URL)
        self.assertFalse(result.error)
        self.assertIs(fetch.call_args.kwargs.get('additional_args', {}).get('ignore_https_errors'), True)
        client = self.client(insecure=True)
        client._cffi = MagicMock()
        client._cffi.get.return_value = SimpleNamespace(status_code=200, content=BODY)
        self.assertEqual(client.fetch_binary(URL), (200, BODY))
        self.assertIs(client._cffi.get.call_args.kwargs.get('verify'), False)

    def test_ca_bundle_removed_even_when_session_close_raises(self):
        from curl_cffi.requests import Session
        close = Session.close
        with patch.object(Session, 'close', side_effect=RuntimeError('fixture close failure')):
            session = self.engine.make_cffi_session(verify=str(self.ca))
            self.assertIsNotNone(session)
            bundle = Path(session.verify)
            with self.assertRaisesRegex(RuntimeError, 'fixture close failure'):
                session.close()
            self.assertFalse(bundle.exists())
        close(session)

    def test_ca_bundle_has_garbage_collection_cleanup(self):
        session = self.engine.make_cffi_session(verify=str(self.ca))
        self.assertIsNotNone(session)
        bundle = Path(session.verify)
        with self.assertWarnsRegex(ResourceWarning, 'Implicitly cleaning up'):
            del session
            gc.collect()
        self.assertFalse(bundle.exists())

    def test_ca_bundle_removed_when_session_construction_fails(self):
        seen = []
        def fail(**kwargs):
            seen.append(Path(kwargs['verify']))
            raise OSError('fixture construction failure')
        with patch('curl_cffi.requests.Session', side_effect=fail):
            self.assertIsNone(self.engine.make_cffi_session(verify=str(self.ca)))
        with patch('curl_cffi.requests.AsyncSession', side_effect=fail):
            with self.assertRaisesRegex(OSError, 'fixture construction failure'):
                asyncio.run(self.engine.AsyncEngine(ca_file=self.ca).fetch_many([]))
        self.assertEqual(len(seen), 2)
        self.assertTrue(all(not path.exists() for path in seen))

    def human_rebuild(self, *, replacement=None, factory_error=None, close_error=None, better=False):
        h = self.harvest
        human_html = '<article>' + 'Human-authorized fixture content. ' * 30 + '</article>'
        first = h.eng.Result(url=URL, status=200, html='fixture wall')
        second = h.eng.Result(url=URL, status=200, html=human_html + 'more' if better else '<p>short</p>')
        old = MagicMock()
        client = SimpleNamespace(fetch=MagicMock(side_effect=[first, second]), cookies_dict={},
                                 _cffi=old, impersonate='chrome', proxies=[], timeout=1, tls_verify=True)
        def close():
            self.assertIs(client._cffi, replacement, 'replacement must be installed before old session is closed')
            if close_error:
                raise close_error
        old.close.side_effect = close
        reply = {'ok': True, 'html': human_html, 'url': URL, 'cookies': {'fixture': 'synthetic'}}
        args = SimpleNamespace(backend='auto', css=None, cdp=None, browser_profile=None, keep_browser=False)
        with patch.object(h.ho, 'need_human', side_effect=[(True, 'fixture wall'), (False, '')]), \
                patch.object(h.ho, 'handoff_fetch', return_value=reply), \
                patch.object(h.ho, 'default_state_path', return_value=Path(self.temp.name) / 'unused.json'), \
                patch.object(h.eng, 'make_cffi_session', return_value=replacement, side_effect=factory_error), \
                redirect_stderr(io.StringIO()):
            result = h.fetch_with_wait_human(client, URL, args, None, 1)
        self.assertEqual(result.human_status, 'completed')
        self.assertEqual(result.html, second.html if better else human_html)
        return client, old, result

    def test_human_rebuild_closes_old_session_after_installing_new_one(self):
        replacement = MagicMock()
        client, old, _ = self.human_rebuild(replacement=replacement)
        self.assertIs(client._cffi, replacement)
        old.close.assert_called_once_with()
        replacement.close.assert_not_called()

    def test_human_rebuild_close_failure_keeps_new_session_and_body(self):
        for better in (False, True):
            with self.subTest(better=better):
                replacement = MagicMock()
                client, old, result = self.human_rebuild(replacement=replacement, close_error=RuntimeError('fixture close'), better=better)
                self.assertIs(client._cffi, replacement)
                old.close.assert_called_once_with()
                self.assertTrue(any('旧 HTTP 会话清理失败' in note for note in result.notes))

    def test_human_rebuild_no_replacement_keeps_old_session_and_body(self):
        client, old, result = self.human_rebuild()
        self.assertIs(client._cffi, old)
        old.close.assert_not_called()
        self.assertTrue(any('新 HTTP 会话不可用' in note for note in result.notes))

    def test_human_rebuild_factory_exception_keeps_old_session_and_body(self):
        client, old, result = self.human_rebuild(factory_error=OSError('fixture session factory failure'))
        self.assertIs(client._cffi, old)
        old.close.assert_not_called()
        self.assertEqual(client.fetch.call_count, 1)
        self.assertTrue(any('沿用接管结果' in note for note in result.notes))


class LeafTrustVerification(unittest.TestCase):
    """Actual loopback TLS with an explicit non-CA certificate trust anchor."""
    @classmethod
    def setUpClass(cls):
        env = {key: value for key, value in os.environ.items() if key.upper() not in {
            'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'CURL_CA_BUNDLE',
            'REQUESTS_CA_BUNDLE', 'SSL_CERT_FILE', 'SSL_CERT_DIR'}}
        env.update(NO_PROXY='localhost,127.0.0.1', no_proxy='localhost,127.0.0.1')
        environment = patch.dict(os.environ, env, clear=True)
        environment.start()
        cls.addClassCleanup(environment.stop)
        spec = importlib.util.spec_from_file_location('leaf_trust_engine', ROOT / 'modules/harvest/engine.py')
        cls.engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.engine
        spec.loader.exec_module(cls.engine)
        cls.temp = tempfile.TemporaryDirectory(prefix='armory-leaf-tls-')
        cls.addClassCleanup(cls.temp.cleanup)
        directory = Path(cls.temp.name)
        cls.cert, key = directory / 'leaf.pem', directory / 'leaf.key'
        openssl = shutil.which('openssl')
        if not openssl:
            raise RuntimeError('OpenSSL is required for the disposable loopback TLS fixture')
        run = subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                              '-keyout', str(key), '-out', str(cls.cert), '-days', '1',
                              '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
                              '-addext', 'basicConstraints=critical,CA:FALSE'], capture_output=True, text=True)
        if run.returncode:
            raise RuntimeError(f'Leaf fixture generation failed: {run.stderr}')
        # A user may provide a combined PEM; generated curl trust files must never copy its key.
        cls.trust = directory / 'trust-with-key.pem'
        cls.trust.write_bytes(cls.cert.read_bytes() + key.read_bytes())
        cls.trust.chmod(0o600)
        store = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        store.load_verify_locations(cafile=cls.trust)
        if store.cert_store_stats()['x509'] != 1 or store.get_ca_certs():
            raise AssertionError('Fixture must be trusted explicitly but absent from CA-only enumeration')
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header('Content-Length', str(len(BODY)))
                self.end_headers()
                self.wfile.write(BODY)
            def log_message(self, *args): pass
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cls.cert, key)
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.socket = context.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        def stop():
            cls.server.shutdown()
            cls.server.server_close()
            cls.thread.join(2)
            if cls.thread.is_alive():
                raise AssertionError('Loopback fixture did not stop')
        cls.addClassCleanup(stop)
        cls.good = f'https://localhost:{cls.server.server_port}/article'
        cls.wrong_host = f'https://127.0.0.1:{cls.server.server_port}/article'

    def check_transports(self, url, *, trust=False, accepted=False):
        kwargs = {'ca_file': self.trust} if trust else {}
        for name, prefer_cffi, keepalive in [('pool', False, True), ('urllib', False, False), ('curl', True, True)]:
            with self.subTest(transport=name):
                client = self.engine.Engine(prefer_cffi=prefer_cffi, keepalive=keepalive,
                                            backend_memory=False, timeout=2, retries=0, **kwargs)
                bundle = None
                try:
                    if prefer_cffi:
                        self.assertIsNotNone(client._cffi, 'This check must use real curl, not silently fall back')
                    result = client.fetch_static(url)
                    self.assert_result(result, accepted)
                    if trust and prefer_cffi:
                        bundle = Path(client._cffi.verify)
                        text = bundle.read_text()
                        self.assertTrue(self.cert.read_text().strip() in text,
                                        'Explicit non-CA leaf certificate was dropped from the merged bundle')
                        self.assertFalse('PRIVATE KEY' in text,
                                         'The merged trust bundle must not contain any private key block')
                        self.assertEqual(bundle.stat().st_mode & 0o777, 0o600)
                finally:
                    client.conn_pool.close_all()
                    if client._cffi:
                        client._cffi.close()
                if bundle is not None:
                    self.assertFalse(bundle.exists())
        created = []
        real_directory = tempfile.TemporaryDirectory
        def tracked_directory(*args, **kwargs):
            directory = real_directory(*args, **kwargs)
            created.append(Path(directory.name))
            return directory
        with patch.object(tempfile, 'TemporaryDirectory', side_effect=tracked_directory):
            result = asyncio.run(self.engine.AsyncEngine(timeout=2, retries=0, **kwargs).fetch_many([url]))[0]
        self.assert_result(result, accepted)
        self.assertTrue(all(not directory.exists() for directory in created))
        if trust:
            self.assertTrue(created, 'Explicit curl trust must have a scoped merged bundle')

    def assert_result(self, result, accepted):
        if accepted:
            self.assertEqual(result.status, 200, result.error)
            self.assertEqual(result.error, '')
            self.assertIn('Offline regression fixture.', result.html)
        else:
            self.assertEqual(result.status, 0)
            self.assertTrue(result.error)
            self.assertIn('cert', result.error.lower())
            self.assertEqual(result.html, '')

    def test_leaf_is_rejected_by_default_on_all_http_transports(self):
        self.check_transports(self.good)

    def test_explicit_leaf_trust_works_without_copying_private_key(self):
        self.check_transports(self.good, trust=True, accepted=True)

    def test_explicit_leaf_trust_still_rejects_wrong_hostname(self):
        self.check_transports(self.wrong_host, trust=True)


if __name__ == '__main__':
    unittest.main(verbosity=2)
