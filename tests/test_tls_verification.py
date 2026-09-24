#!/usr/bin/env python3
"""TLS regression against --root, using only ephemeral loopback HTTPS servers.

OpenSSL creates a temporary CA and a server certificate valid for localhost,
not 127.0.0.1. No certificate is installed in the user's trust store. Browser
adapters are argument-contract tests; pool/urllib/curl/async use real TLS.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
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
ROOT = Path(__file__).resolve().parents[1]
BODY = b'<html><body><article>Verified local TLS content.</article></body></html>'


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(BODY)))
        self.end_headers()
        self.wfile.write(BODY)

    def log_message(self, *args):
        pass


class TLSVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location('tls_engine_under_test', ROOT / 'modules/harvest/engine.py')
        cls.engine = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = cls.engine
        spec.loader.exec_module(cls.engine)
        cls.temp = tempfile.TemporaryDirectory(prefix='armory-local-tls-')
        d = Path(cls.temp.name)
        cls.ca = str(d / 'ca.pem')
        openssl = shutil.which('openssl')
        if not openssl:
            raise RuntimeError('OpenSSL required to create disposable TLS fixtures')
        commands = [
            [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(d/'ca.key'), '-out', cls.ca, '-days', '1', '-subj', '/CN=Armory test CA', '-addext', 'basicConstraints=critical,CA:TRUE'],
            [openssl, 'req', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(d/'server.key'), '-out', str(d/'server.csr'), '-subj', '/CN=localhost'],
        ]
        (d/'server.ext').write_text('subjectAltName=DNS:localhost\nbasicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
        commands.append([openssl, 'x509', '-req', '-in', str(d/'server.csr'), '-CA', cls.ca, '-CAkey', str(d/'ca.key'), '-CAcreateserial', '-out', str(d/'server.pem'), '-days', '1', '-extfile', str(d/'server.ext')])
        commands.append([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(d/'self.key'), '-out', str(d/'self.pem'), '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'])
        for command in commands:
            run = subprocess.run(command, capture_output=True, text=True)
            if run.returncode:
                raise RuntimeError(f'Fixture setup failed: {command!r}\n{run.stdout}\n{run.stderr}')
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(d/'server.pem'), str(d/'server.key'))
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.server.daemon_threads = True
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.good = f'https://localhost:{cls.server.server_port}/article'
        cls.mismatch = f'https://127.0.0.1:{cls.server.server_port}/article'
        self_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self_ctx.load_cert_chain(str(d/'self.pem'), str(d/'self.key'))
        cls.self_server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        cls.self_server.daemon_threads = True
        cls.self_server.socket = self_ctx.wrap_socket(cls.self_server.socket, server_side=True)
        cls.self_thread = threading.Thread(target=cls.self_server.serve_forever, daemon=True)
        cls.self_thread.start()
        cls.self_signed = f'https://localhost:{cls.self_server.server_port}/article'
        # Do not let a machine's proxy or optional CA bundle affect this test.
        env = {k: v for k, v in os.environ.items() if k.upper() not in {
            'HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','CURL_CA_BUNDLE','REQUESTS_CA_BUNDLE',
            'SSL_CERT_FILE','SSL_CERT_DIR'}}
        env.update({'NO_PROXY': 'localhost,127.0.0.1', 'no_proxy': 'localhost,127.0.0.1'})
        cls.environment = patch.dict(os.environ, env, clear=True)
        cls.environment.start()
        cls.has_cffi = cls.engine.has_module('curl_cffi')
        print('FIXTURE=ephemeral loopback HTTPS; CA trusted only when passed as ca_file')

    @classmethod
    def tearDownClass(cls):
        cls.environment.stop()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.self_server.shutdown()
        cls.self_server.server_close()
        cls.self_thread.join(timeout=2)
        cls.temp.cleanup()

    def client(self, **kwargs):
        client = self.engine.Engine(prefer_cffi=False, backend_memory=False, timeout=2, retries=0, **kwargs)
        self.addCleanup(client.conn_pool.close_all)
        return client

    def check_result(self, result, accepted):
        if accepted:
            self.assertEqual(result.status, 200, result.error)
            self.assertFalse(result.error)
            self.assertIn('Verified local TLS content.', result.html)
        else:
            self.assertEqual(result.status, 0)
            self.assertTrue(result.error, 'Invalid TLS must not become a successful response')
            self.assertIn('cert', result.error.lower())
            self.assertEqual(result.html, '')

    def test_default_ssl_context_requires_certificate_and_hostname(self):
        ctx = self.client().ssl_ctx
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(ctx.check_hostname)

    def test_pool_rejects_untrusted_self_signed_ca(self):
        self.check_result(self.client().fetch_static(self.good), False)

    def test_urllib_rejects_untrusted_self_signed_ca(self):
        self.check_result(self.client(keepalive=False).fetch_static(self.good), False)

    def test_all_http_clients_reject_untrusted_self_signed_leaf(self):
        clients = [self.client(), self.client(keepalive=False)]
        if self.has_cffi:
            clients.append(self.cffi_client())
        for index, client in enumerate(clients):
            with self.subTest(transport=index):
                self.check_result(client.fetch_static(self.self_signed), False)
        if self.has_cffi:
            self.check_result(self.engine.AsyncEngine(timeout=2, retries=0).run([self.self_signed])[0], False)

    def test_pool_accepts_explicit_trusted_ca(self):
        self.check_result(self.client(ca_file=self.ca).fetch_static(self.good), True)

    def test_urllib_accepts_explicit_trusted_ca(self):
        self.check_result(self.client(keepalive=False, ca_file=self.ca).fetch_static(self.good), True)

    def test_pool_rejects_hostname_mismatch_despite_trusted_ca(self):
        self.check_result(self.client(ca_file=self.ca).fetch_static(self.mismatch), False)

    def test_urllib_rejects_hostname_mismatch_despite_trusted_ca(self):
        self.check_result(self.client(keepalive=False, ca_file=self.ca).fetch_static(self.mismatch), False)

    def test_binary_builtin_rejects_untrusted_ca(self):
        with self.assertRaises((ssl.SSLError, self.engine.urllib.error.URLError)):
            self.client().fetch_binary(self.good)

    def test_binary_builtin_accepts_trusted_ca_and_rejects_mismatch(self):
        client = self.client(ca_file=self.ca)
        self.assertEqual(client.fetch_binary(self.good), (200, BODY))
        with self.assertRaises((ssl.SSLError, self.engine.urllib.error.URLError)):
            client.fetch_binary(self.mismatch)

    def cffi_client(self, **kwargs):
        if not self.has_cffi:
            self.skipTest('curl_cffi not installed: live optional transport test unavailable')
        client = self.engine.Engine(prefer_cffi=True, backend_memory=False, timeout=2, retries=0, **kwargs)
        self.assertIsNotNone(client._cffi, 'Do not accidentally test builtin fallback')
        self.addCleanup(client._cffi.close)
        self.addCleanup(client.conn_pool.close_all)
        return client

    def test_cffi_rejects_untrusted_ca(self):
        self.check_result(self.cffi_client().fetch_static(self.good), False)

    def test_cffi_accepts_trusted_ca_and_rejects_mismatch(self):
        client = self.cffi_client(ca_file=self.ca)
        self.check_result(client.fetch_static(self.good), True)
        self.check_result(client.fetch_static(self.mismatch), False)

    def test_binary_cffi_accepts_trusted_ca_and_rejects_mismatch(self):
        client = self.cffi_client(ca_file=self.ca)
        self.assertEqual(client.fetch_binary(self.good), (200, BODY))
        with self.assertRaises(OSError):
            client.fetch_binary(self.mismatch)

    def test_binary_cffi_rejects_untrusted_ca(self):
        with self.assertRaises(OSError):
            self.cffi_client().fetch_binary(self.good)

    def test_async_rejects_untrusted_ca(self):
        if not self.has_cffi:
            self.skipTest('curl_cffi not installed')
        client = self.engine.AsyncEngine(timeout=2, retries=0)
        self.check_result(client.run([self.good])[0], False)

    def test_async_accepts_trusted_ca_and_rejects_mismatch(self):
        if not self.has_cffi:
            self.skipTest('curl_cffi not installed')
        client = self.engine.AsyncEngine(timeout=2, retries=0, ca_file=self.ca)
        results = client.run([self.good, self.mismatch])
        self.check_result(results[0], True)
        self.check_result(results[1], False)

    def test_cffi_request_explicitly_keeps_verification_on(self):
        client = self.client()
        fake = MagicMock()
        fake.request.return_value = SimpleNamespace(status_code=200, url=self.good, headers={}, content=BODY)
        client._cffi = fake
        client._request_cffi(self.good)
        self.assertIs(fake.request.call_args.kwargs.get('verify'), True)

    def test_async_request_explicitly_keeps_verification_on(self):
        client = self.engine.AsyncEngine(timeout=2, retries=0)
        seen = {}
        async def get(url, **kwargs):
            seen.update(kwargs)
            return SimpleNamespace(status_code=200, url=url, headers={}, content=BODY)
        asyncio.run(client._one(SimpleNamespace(get=get), self.good, asyncio.Semaphore(1)))
        self.assertIs(seen.get('verify'), True)

    def test_scrapling_overrides_unsafe_library_default(self):
        fetch = MagicMock(return_value=SimpleNamespace(html_content=BODY.decode(), status=200, url=self.good))
        with patch.dict(sys.modules, {'scrapling': SimpleNamespace(), 'scrapling.fetchers': SimpleNamespace(StealthyFetcher=SimpleNamespace(fetch=fetch))}), patch.object(self.engine, 'has_module', return_value=True):
            result = self.client().fetch_scrapling(self.good)
        self.assertEqual(result.status, 200, result.error)
        self.assertIs(fetch.call_args.kwargs.get('additional_args', {}).get('ignore_https_errors'), False)

    def test_render_context_explicitly_verifies_https(self):
        page = MagicMock()
        page.goto.return_value = SimpleNamespace(status=200, headers={})
        page.url = self.good
        page.content.return_value = BODY.decode()
        context, browser = MagicMock(), MagicMock()
        context.new_page.return_value = page
        browser.new_context.return_value = context
        manager = MagicMock()
        manager.__enter__.return_value.chromium.launch.return_value = browser
        module = SimpleNamespace(sync_playwright=lambda: manager)
        with patch.object(self.engine, 'playwright_module', return_value='playwright'), patch.object(self.engine.importlib, 'import_module', return_value=module):
            self.check_result(self.client().fetch_render(self.good), True)
        self.assertIs(browser.new_context.call_args.kwargs.get('ignore_https_errors'), False)

    def test_camoufox_context_explicitly_verifies_https(self):
        for persistent in (None, '/unused/mock-profile'):
            with self.subTest(persistent=bool(persistent)):
                page, browser, manager = MagicMock(), MagicMock(), MagicMock()
                page.goto.return_value = SimpleNamespace(status=200, headers={})
                page.url = self.good
                page.content.return_value = BODY.decode()
                browser.new_page.return_value = page
                manager.__enter__.return_value = browser
                factory = MagicMock(return_value=manager)
                with patch.dict(sys.modules, {'camoufox.sync_api': SimpleNamespace(Camoufox=factory)}), patch.object(self.engine, 'has_module', return_value=True):
                    result = self.client().fetch_camoufox(self.good, persistent_dir=persistent)
                self.check_result(result, True)
                kwargs = factory.call_args.kwargs if persistent else browser.new_page.call_args.kwargs
                self.assertIs(kwargs.get('ignore_https_errors'), False)

    def test_sniff_contexts_explicitly_verify_https(self):
        for use in ('camoufox', 'playwright'):
            with self.subTest(use=use):
                browser, context, manager = MagicMock(), MagicMock(), MagicMock()
                browser.new_context.return_value = context
                if use == 'camoufox':
                    manager.__enter__.return_value = browser
                else:
                    manager.__enter__.return_value.chromium.launch.return_value = browser
                module = SimpleNamespace(sync_playwright=lambda: manager)
                with patch.dict(sys.modules, {'camoufox.sync_api': SimpleNamespace(Camoufox=lambda **kw: manager)}), patch.object(self.engine, 'has_module', return_value=True), patch.object(self.engine, 'playwright_module', return_value='playwright'), patch.object(self.engine.importlib, 'import_module', return_value=module):
                    _, used = self.client().fetch_sniff(self.good, use=use, wait_ms=0)
                self.assertEqual(used, use)
                kwargs = browser.new_page.call_args.kwargs if use == 'camoufox' else browser.new_context.call_args.kwargs
                self.assertIs(kwargs.get('ignore_https_errors'), False)

    def test_sniff_reports_certificate_failure_instead_of_empty_success(self):
        browser, page, manager = MagicMock(), MagicMock(), MagicMock()
        page.goto.side_effect = RuntimeError('net::ERR_CERT_AUTHORITY_INVALID')
        browser.new_page.return_value = page
        manager.__enter__.return_value = browser
        with patch.dict(sys.modules, {'camoufox.sync_api': SimpleNamespace(Camoufox=lambda **kw: manager)}), patch.object(self.engine, 'has_module', return_value=True):
            result, error = self.client().fetch_sniff(self.good, use='camoufox', wait_ms=0)
        self.assertIsNone(result)
        self.assertIn('ERR_CERT_AUTHORITY_INVALID', error)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=ROOT)
    args, rest = parser.parse_known_args()
    ROOT = args.root.resolve()
    unittest.main(argv=[sys.argv[0], *rest], verbosity=2)
