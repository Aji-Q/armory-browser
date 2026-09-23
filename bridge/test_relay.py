"""Offline protocol/security tests, including real loopback HTTP interactions."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
from pathlib import Path
import stat
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest

from bridge.relay import (
    MAX_BODY_BYTES, RelayError, RelayServer, Store, init_config,
    load_config, token_hash, validate_public_url,
)


def sample_result(url="https://example.com/article"):
    return {"url": url, "title": "Article", "text": "Visible article text",
            "markdown": "# Article\n\nVisible article text", "links": [{"text": "More", "url": "https://example.org/more"}],
            "captured_at": "2026-09-23T12:30:00Z", "truncated": False}


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.now = 1_800_000_000.0
        self.store = Store(":memory:", clock=lambda: self.now)

    def tearDown(self):
        self.store.close()

    def create(self, **kwargs):
        payload = {"url": "https://example.com/article", "purpose": "Read article", **kwargs}
        return self.store.create(payload)[0]

    def assert_error(self, status, function, *args):
        with self.assertRaises(RelayError) as raised:
            function(*args)
        self.assertEqual(status, raised.exception.status)

    def test_full_human_and_share_state_machine(self):
        job = self.create()
        self.assertEqual("queued", job["state"])
        for event, state in [("approve", "running"), ("human_required", "awaiting_human"),
                             ("resume", "running"), ("preview_ready", "awaiting_share")]:
            job = self.store.event(job["id"], {"type": event, "reason": "Login required" if event == "human_required" else ""})
            self.assertEqual(state, job["state"])
            self.assertIsNone(job["result"])
        job = self.store.event(job["id"], {"type": "complete", "result": sample_result()})
        self.assertEqual("completed", job["state"])
        self.assertEqual("Visible article text", job["result"]["text"])
        self.assertEqual([], self.store.pending())

    def test_content_upload_cannot_bypass_preview(self):
        job = self.create()
        self.assert_error(409, self.store.event, job["id"], {"type": "complete", "result": sample_result()})
        self.store.event(job["id"], {"type": "approve"})
        self.assert_error(400, self.store.event, job["id"], {"type": "preview_ready", "result": sample_result()})
        self.assertEqual("running", self.store.get(job["id"])["state"])
        self.assertIsNone(self.store.get(job["id"])["result"])

    def test_cancel_is_terminal_from_each_nonterminal_state(self):
        for transitions in [[], ["approve"], ["approve", "human_required"], ["approve", "preview_ready"]]:
            job = self.create()
            for event in transitions:
                self.store.event(job["id"], {"type": event})
            cancelled = self.store.cancel(job["id"])
            self.assertEqual("cancelled", cancelled["state"])
            for event in ["approve", "resume", "complete", "cancel", "fail"]:
                self.assert_error(409, self.store.event, job["id"], {"type": event})

    def test_failed_and_completed_are_terminal(self):
        job = self.create()
        self.assertEqual("failed", self.store.event(job["id"], {"type": "fail", "reason": "Tab closed"})["state"])
        self.assert_error(409, self.store.cancel, job["id"])
        job = self.create()
        for event in ["approve", "preview_ready"]:
            self.store.event(job["id"], {"type": event})
        self.store.event(job["id"], {"type": "complete", "result": sample_result()})
        self.assert_error(409, self.store.event, job["id"], {"type": "complete", "result": sample_result()})

    def test_invalid_transitions_and_unknown_event(self):
        job = self.create()
        for event in ["resume", "human_required", "preview_ready"]:
            self.assert_error(409, self.store.event, job["id"], {"type": event})
        self.assert_error(400, self.store.event, job["id"], {"type": "execute_js"})
        self.assert_error(400, self.store.event, job["id"], {"type": "approve", "script": "alert(1)"})
        self.assert_error(400, self.store.event, job["id"], {"type": "fail", "reason": "<html><form>secret</form></html>"})

    def test_idempotency_replay_conflict_and_concurrent_creation(self):
        job = self.create(idempotency_key="task-key")
        payload = {"url": job["url"], "purpose": job["purpose"], "idempotency_key": "task-key"}
        replay, created = self.store.create(payload)
        self.assertFalse(created)
        self.assertEqual(job, replay)
        self.assert_error(409, self.store.create, {**payload, "purpose": "Other request"})
        with ThreadPoolExecutor(max_workers=8) as pool:
            outcomes = list(pool.map(lambda _: self.store.create({**payload, "idempotency_key": "parallel"}), range(24)))
        self.assertEqual(1, sum(created for _, created in outcomes))
        self.assertEqual(1, len({value["id"] for value, _ in outcomes}))

    def test_ttl_removes_results_and_idempotency(self):
        job = self.create(idempotency_key="expires")
        for event in ["approve", "preview_ready"]:
            self.store.event(job["id"], {"type": event})
        self.store.event(job["id"], {"type": "complete", "result": sample_result()})
        self.now += 7 * 86400
        self.assert_error(404, self.store.get, job["id"])
        self.assertEqual(0, self.store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        new = self.create(idempotency_key="expires")
        self.assertNotEqual(job["id"], new["id"])

    def test_queue_limit_and_listing_limit_order(self):
        self.store.queue_max = 105
        jobs = [self.create() for _ in range(105)]
        self.assertEqual([job["id"] for job in jobs[:100]], [job["id"] for job in self.store.pending()])
        self.assert_error(429, self.store.create, {"url": "https://example.com/", "purpose": "full"})
        self.store.cancel(jobs[0]["id"])
        self.create()

    def test_job_field_validation(self):
        base = {"url": "https://example.com/", "purpose": "Read"}
        for change in [{"url": "http://127.0.0.1/"}, {"purpose": ""}, {"purpose": "a" * 501},
                       {"max_chars": True}, {"max_chars": 99}, {"max_chars": 100001},
                       {"idempotency_key": None}, {"idempotency_key": ""}, {"cookies": "secret"}]:
            with self.subTest(change=change):
                self.assert_error(400, self.store.create, {**base, **change})

    def test_reject_private_ambiguous_and_credential_urls(self):
        bad = ["http://localhost/", "https://x.localhost/", "http://127.0.0.1/", "http://10.0.0.3/",
               "http://169.254.169.254/", "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/",
               "http://192.168.1.1/", "http://0.0.0.0/", "http://127.1/", "http://2130706433/",
               "http://0x7f000001/", "http://0177.0.0.1/", "http://alice:password@example.com/",
               "http://example.com/?access_token=secret", "file:///etc/passwd", "javascript:alert(1)",
               "https://example.com\\@localhost/", "https://example.com:bad/", "https://exam ple.com/",
               "http://foo.local/", "https://example.com:\n80/", "http://example.com:0/"]
        for value in bad:
            with self.subTest(url=value):
                self.assert_error(400, validate_public_url, value)
        for value in ["https://example.com/", "https://8.8.8.8/", "https://[2606:4700:4700::1111]/", "https://EXAMPLE.com:443/page"]:
            self.assertEqual(value, validate_public_url(value)[0])

    def test_result_strict_whitelist_origin_and_limits(self):
        job = self.create(max_chars=100)
        for event in ["approve", "preview_ready"]:
            self.store.event(job["id"], {"type": event})
        bad_results = [
            {**sample_result(), "cookies": [{"value": "secret"}]},
            {**sample_result(), "headers": {"Authorization": "secret"}},
            {**sample_result(), "url": "https://idp.example.com/login"},
            {**sample_result(), "url": "http://example.com/article"},
            {**sample_result(), "url": "https://example.com:444/article"},
            {**sample_result(), "url": "https://user:secret@example.com/article"},
            {**sample_result(), "text": "a" * 101},
            {**sample_result(), "markdown": "a" * 201},
            {**sample_result(), "title": "a" * 1001},
            {**sample_result(), "truncated": 1},
            {**sample_result(), "captured_at": "2026-09-23"},
            {**sample_result(), "links": [{"text": "x", "url": "https://example.com", "cookie": "x"}]},
            {**sample_result(), "links": [{"text": "x", "url": "javascript:alert(1)"}]},
            {**sample_result(), "links": [{"text": "x", "url": "https://example.com?token=secret"}]},
            {**sample_result(), "links": [{"text": "x", "url": "https://example.com"}] * 201},
            {key: value for key, value in sample_result().items() if key != "text"},
        ]
        for result in bad_results:
            with self.subTest(keys=list(result)):
                self.assert_error(400, self.store.event, job["id"], {"type": "complete", "result": result})
                self.assertEqual("awaiting_share", self.store.get(job["id"])["state"])
                self.assertIsNone(self.store.get(job["id"])["result"])
        result = sample_result("https://EXAMPLE.com:443/other")
        self.assertEqual("completed", self.store.event(job["id"], {"type": "complete", "result": result})["state"])

    def test_server_deadline_timeout_and_late_resume(self):
        job = self.create(human_timeout_seconds=5)
        self.assertEqual(5, job["human_timeout_seconds"])
        self.assertIsNone(job["human_deadline_at"])
        self.assertFalse(job["degraded"])
        self.store.event(job["id"], {"type": "approve"})
        waiting = self.store.event(job["id"], {"type": "human_required", "reason": "Login wall"})
        self.assertIsNotNone(waiting["human_deadline_at"])
        self.assert_error(409, self.store.event, job["id"], {"type": "timeout"})
        self.now += 5
        self.assert_error(409, self.store.event, job["id"], {"type": "resume"})
        fallback = self.store.event(job["id"], {"type": "timeout"})
        self.assertEqual("running", fallback["state"])
        self.assertTrue(fallback["degraded"])
        self.assertIn("public-content fallback", fallback["reason"])
        self.assertIsNone(fallback["human_deadline_at"])
        self.assert_error(409, self.store.event, job["id"], {"type": "timeout"})
        self.store.event(job["id"], {"type": "preview_ready"})
        self.assert_error(400, self.store.event, job["id"], {"type": "complete", "result": sample_result()})
        result = {**sample_result(), "degraded": True, "quality": "partial"}
        completed = self.store.event(job["id"], {"type": "complete", "result": result})
        self.assertEqual("completed", completed["state"])
        self.assertTrue(completed["degraded"])
        self.assertEqual("partial", completed["result"]["quality"])

    def test_early_human_resume_clears_deadline_without_degrading(self):
        job = self.create(human_timeout_seconds=5)
        self.store.event(job["id"], {"type": "approve"})
        self.store.event(job["id"], {"type": "human_required"})
        self.now += 4.9
        resumed = self.store.event(job["id"], {"type": "resume"})
        self.assertEqual("running", resumed["state"])
        self.assertFalse(resumed["degraded"])
        self.assertIsNone(resumed["human_deadline_at"])

    def test_timeout_parameter_and_result_quality_validation(self):
        for value in [True, None, "5", 4, 901, 5.5]:
            self.assert_error(400, self.store.create, {"url": "https://example.com", "purpose": "Read", "human_timeout_seconds": value})
        job = self.create(idempotency_key="timeout-key")
        self.assertEqual(300, job["human_timeout_seconds"])
        self.assert_error(409, self.store.create, {"url": job["url"], "purpose": job["purpose"],
                                                "idempotency_key": "timeout-key", "human_timeout_seconds": 5})
        for event in ["approve", "preview_ready"]:
            self.store.event(job["id"], {"type": event})
        for updates in [{"text": ""}, {"text": " \n\t"}, {"degraded": "true"}, {"quality": []},
                        {"quality": "unknown"}, {"degraded": True}, {"quality": "partial"},
                        {"degraded": True, "quality": "full"}, {"degraded": False, "quality": "partial"}]:
            self.assert_error(400, self.store.event, job["id"], {"type": "complete", "result": {**sample_result(), **updates}})
        failed = self.store.event(job["id"], {"type": "fail", "reason": "No public content available"})
        self.assertEqual("failed", failed["state"])
        self.assertIsNone(failed["result"])

    def test_migration_preserves_existing_identity_and_replay(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "legacy.sqlite"
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE jobs (id TEXT PRIMARY KEY, url TEXT NOT NULL, purpose TEXT NOT NULL, max_chars INTEGER NOT NULL, state TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL, reason TEXT NOT NULL DEFAULT '', result_json TEXT, idempotency_key TEXT UNIQUE, fingerprint TEXT NOT NULL)")
            identity = json.dumps(["https://example.com/", "Read", 20000], ensure_ascii=False, separators=(",", ":"))
            conn.execute("INSERT INTO jobs (id,url,purpose,max_chars,state,created,updated,idempotency_key,fingerprint) VALUES (?,?,?,?,?,?,?,?,?)",
                         ("existing-id", "https://example.com/", "Read", 20000, "queued", self.now, self.now, "existing-key", token_hash(identity)))
            conn.commit()
            conn.close()
            migrated = Store(path, clock=lambda: self.now)
            try:
                job, created = migrated.create({"url": "https://example.com/", "purpose": "Read", "idempotency_key": "existing-key"})
                self.assertFalse(created)
                self.assertEqual("existing-id", job["id"])
                self.assertEqual(300, job["human_timeout_seconds"])
                self.assertFalse(job["degraded"])
            finally:
                migrated.close()

    def test_persistent_database_and_private_permissions(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "jobs.sqlite"
            store = Store(path)
            job, _ = store.create({"url": "https://example.com/", "purpose": "Persist"})
            store.close()
            reopened = Store(path)
            try:
                self.assertEqual(job, reopened.get(job["id"]))
                self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            finally:
                reopened.close()


class InitTests(unittest.TestCase):
    def test_init_outputs_separate_tokens_hashes_permissions_without_leak(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run([sys.executable, "-m", "bridge.relay", "init", "--output-dir", temporary],
                                    capture_output=True, text=True, check=True)
            directory = Path(temporary)
            server = load_config(directory / "server-config.json")
            agent = json.loads((directory / "agent-client.json").read_text())
            browser = json.loads((directory / "browser-client.json").read_text())
            self.assertNotEqual(agent["agent_token"], browser["browser_token"])
            self.assertEqual(token_hash(agent["agent_token"]), server["agent_token_hash"])
            self.assertEqual(token_hash(browser["browser_token"]), server["browser_token_hash"])
            self.assertEqual({"relay_url", "agent_token"}, set(agent))
            self.assertEqual({"relay_url", "browser_token"}, set(browser))
            for name in ["server-config.json", "agent-client.json", "browser-client.json"]:
                self.assertEqual(0o600, stat.S_IMODE((directory / name).stat().st_mode))
            for token in [agent["agent_token"], browser["browser_token"]]:
                self.assertNotIn(token, result.stdout + result.stderr + (directory / "server-config.json").read_text())
            original = (directory / "server-config.json").read_bytes()
            with self.assertRaises(FileExistsError):
                init_config(temporary)
            self.assertEqual(original, (directory / "server-config.json").read_bytes())

    def test_init_https_or_loopback_only(self):
        for url in ["http://example.com", "https://user:secret@example.com", "https://example.com/?token=secret", "file:///tmp"]:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(url=url), self.assertRaises(ValueError):
                init_config(temporary, url)
        for url in ["https://relay.example.com", "http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765"]:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(url=url):
                self.assertEqual(3, len(init_config(temporary, url)))


class HTTPTests(unittest.TestCase):
    AGENT = "unit-test-agent-token"
    BROWSER = "unit-test-browser-token"
    ORIGIN = "chrome-extension://" + "a" * 32

    def setUp(self):
        self.now = 1_800_000_000.0
        self.store = Store(":memory:", clock=lambda: self.now)
        self.server = RelayServer(("127.0.0.1", 0), self.store,
                                  {"agent_token_hash": token_hash(self.AGENT), "browser_token_hash": token_hash(self.BROWSER)})
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.close()

    def request(self, method, path, payload=None, token=None, headers=None, raw=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        combined = {"Content-Type": "application/json"}
        if token:
            combined["Authorization"] = "Bearer " + token
        if headers:
            combined.update(headers)
        body = raw if raw is not None else (json.dumps(payload).encode() if payload is not None else None)
        try:
            connection.request(method, path, body, combined)
            response = connection.getresponse()
            data = response.read()
            return response.status, json.loads(data) if data else None, dict(response.getheaders())
        finally:
            connection.close()

    def create(self, **kwargs):
        status, body, _ = self.request("POST", "/v1/jobs", {"url": "https://example.com/article", "purpose": "Read", **kwargs}, self.AGENT)
        self.assertEqual(201, status)
        return body["job"]

    def test_health_public_no_secret_status(self):
        status, body, headers = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual({"status": "ok", "version": 1, "single_user": True}, body)
        self.assertEqual("no-store", headers["Cache-Control"])

    def test_all_v1_routes_require_auth(self):
        job = self.create()
        for method, path in [("POST", "/v1/jobs"), ("GET", "/v1/jobs/" + job["id"]),
                             ("POST", "/v1/jobs/" + job["id"] + "/cancel"), ("GET", "/v1/browser/jobs"),
                             ("POST", "/v1/browser/jobs/" + job["id"] + "/events"), ("GET", "/v1/unknown")]:
            with self.subTest(path=path):
                for token in [None, "wrong"]:
                    self.assertEqual(401, self.request(method, path, {}, token)[0])

    def test_agent_cannot_approve_share_or_poll_browser_queue(self):
        job = self.create()
        path = "/v1/browser/jobs/" + job["id"] + "/events"
        self.assertEqual(403, self.request("POST", path, {"type": "approve"}, self.AGENT)[0])
        self.assertEqual(403, self.request("POST", path, {"type": "complete", "result": sample_result()}, self.AGENT)[0])
        self.assertEqual(403, self.request("GET", "/v1/browser/jobs", token=self.AGENT)[0])
        self.assertEqual("queued", self.store.get(job["id"])["state"])

    def test_browser_cannot_submit_or_agent_cancel(self):
        job = self.create()
        self.assertEqual(403, self.request("POST", "/v1/jobs", {"url": "https://example.com", "purpose": "x"}, self.BROWSER)[0])
        self.assertEqual(403, self.request("POST", "/v1/jobs/" + job["id"] + "/cancel", {}, self.BROWSER)[0])
        self.assertEqual(200, self.request("GET", "/v1/jobs/" + job["id"], token=self.BROWSER)[0])

    def test_http_login_handoff_preview_share_and_result(self):
        job = self.create(idempotency_key="http-flow")
        status, body, headers = self.request("GET", "/v1/browser/jobs", token=self.BROWSER, headers={"Origin": self.ORIGIN})
        self.assertEqual(200, status)
        self.assertEqual(self.ORIGIN, headers["Access-Control-Allow-Origin"])
        self.assertEqual(job["id"], body["jobs"][0]["id"])
        path = "/v1/browser/jobs/" + job["id"] + "/events"
        expected = [("approve", "running"), ("human_required", "awaiting_human"), ("resume", "running"), ("preview_ready", "awaiting_share")]
        for event, state in expected:
            status, body, _ = self.request("POST", path, {"type": event}, self.BROWSER)
            self.assertEqual(200, status)
            self.assertEqual(state, body["job"]["state"])
            status, polled, _ = self.request("GET", "/v1/jobs/" + job["id"], token=self.AGENT)
            self.assertEqual(state, polled["job"]["state"])
            self.assertIsNone(polled["job"]["result"])
        status, body, _ = self.request("POST", path, {"type": "complete", "result": sample_result()}, self.BROWSER)
        self.assertEqual(200, status)
        self.assertEqual("completed", body["job"]["state"])
        self.assertEqual(sample_result(), self.request("GET", "/v1/jobs/" + job["id"], token=self.AGENT)[1]["job"]["result"])
        self.assertEqual([], self.request("GET", "/v1/browser/jobs", token=self.BROWSER)[1]["jobs"])
        self.assertEqual(409, self.request("POST", path, {"type": "complete", "result": sample_result()}, self.BROWSER)[0])

    def test_http_replay_conflict_cancel_and_ttl(self):
        job = self.create(idempotency_key="reuse")
        payload = {"url": job["url"], "purpose": job["purpose"], "idempotency_key": "reuse"}
        replay = self.request("POST", "/v1/jobs", payload, self.AGENT)
        self.assertEqual(200, replay[0])
        self.assertEqual(job, replay[1]["job"])
        self.assertEqual(409, self.request("POST", "/v1/jobs", {**payload, "purpose": "different"}, self.AGENT)[0])
        self.assertEqual("cancelled", self.request("POST", "/v1/jobs/" + job["id"] + "/cancel", {}, self.AGENT)[1]["job"]["state"])
        self.assertEqual(409, self.request("POST", "/v1/browser/jobs/" + job["id"] + "/events", {"type": "approve"}, self.BROWSER)[0])
        self.now += 7 * 86400 + 1
        self.assertEqual(404, self.request("GET", "/v1/jobs/" + job["id"], token=self.AGENT)[0])

    def test_http_timeout_reconnect_and_partial_result(self):
        job = self.create(human_timeout_seconds=5)
        path = "/v1/browser/jobs/" + job["id"] + "/events"
        for event in ["approve", "human_required"]:
            self.assertEqual(200, self.request("POST", path, {"type": event}, self.BROWSER)[0])
        self.assertEqual(409, self.request("POST", path, {"type": "timeout"}, self.BROWSER)[0])
        self.now += 6
        # Closed browsers do not magically run: next poll still exposes waiting job.
        pending = self.request("GET", "/v1/browser/jobs", token=self.BROWSER)[1]["jobs"][0]
        self.assertEqual("awaiting_human", pending["state"])
        self.assertIsNotNone(pending["human_deadline_at"])
        self.assertEqual(409, self.request("POST", path, {"type": "resume"}, self.BROWSER)[0])
        self.assertEqual(403, self.request("POST", path, {"type": "timeout"}, self.AGENT)[0])
        status, body, _ = self.request("POST", path, {"type": "timeout"}, self.BROWSER)
        self.assertEqual(200, status)
        self.assertEqual("running", body["job"]["state"])
        self.assertTrue(body["job"]["degraded"])
        self.assertEqual(200, self.request("POST", path, {"type": "preview_ready"}, self.BROWSER)[0])
        self.assertEqual(400, self.request("POST", path, {"type": "complete", "result": sample_result()}, self.BROWSER)[0])
        partial = {**sample_result(), "quality": "partial", "degraded": True}
        self.assertEqual(200, self.request("POST", path, {"type": "complete", "result": partial}, self.BROWSER)[0])
        completed = self.request("GET", "/v1/jobs/" + job["id"], token=self.AGENT)[1]["job"]
        self.assertEqual("completed", completed["state"])
        self.assertEqual("partial", completed["result"]["quality"])

    def test_cors_denies_web_origins_and_invalid_extension_origins(self):
        for origin in ["https://example.com", "http://localhost:9000", "null", "chrome-extension://" + "z" * 32,
                       self.ORIGIN + "/", "chrome-extension://short"]:
            with self.subTest(origin=origin):
                status, _, headers = self.request("GET", "/v1/browser/jobs", token=self.BROWSER, headers={"Origin": origin})
                self.assertEqual(403, status)
                self.assertNotIn("Access-Control-Allow-Origin", headers)
        self.assertEqual(200, self.request("GET", "/v1/browser/jobs", token=self.BROWSER)[0])
        self.server.extension_id = "b" * 32
        self.assertEqual(403, self.request("GET", "/v1/browser/jobs", token=self.BROWSER, headers={"Origin": self.ORIGIN})[0])
        self.assertEqual(200, self.request("GET", "/v1/browser/jobs", token=self.BROWSER,
                                         headers={"Origin": "chrome-extension://" + "b" * 32})[0])

    def test_extension_preflight_without_bearer_but_no_data(self):
        headers = {"Origin": self.ORIGIN, "Access-Control-Request-Method": "POST",
                   "Access-Control-Request-Headers": "authorization,content-type"}
        status, body, returned = self.request("OPTIONS", "/v1/browser/jobs", headers=headers)
        self.assertEqual(204, status)
        self.assertIsNone(body)
        self.assertEqual("Authorization, Content-Type", returned["Access-Control-Allow-Headers"])
        for change in [{"Origin": "https://bad.example"}, {"Access-Control-Request-Method": "DELETE"},
                       {"Access-Control-Request-Headers": "X-Arbitrary"}]:
            self.assertEqual(403, self.request("OPTIONS", "/v1/browser/jobs", headers={**headers, **change})[0])

    def test_body_limit_json_schema_and_utf8(self):
        path = "/v1/jobs"
        self.assertEqual(413, self.request("POST", path, token=self.AGENT, raw=b"{}", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})[0])
        for raw in [b"not-json", b'{"url":"https://example.com", "url":"https://example.org", "purpose":"x"}',
                    b'{"url":"https://example.com","purpose":"x","max_chars":NaN}', b"\xff", b"[]",
                    b'{"url":"https://example.com", "purpose":"\\ud800"}']:
            with self.subTest(raw=raw):
                self.assertEqual(400, self.request("POST", path, token=self.AGENT, raw=raw)[0])
        self.assertEqual(415, self.request("POST", path, token=self.AGENT, raw=b"{}", headers={"Content-Type": "text/plain"})[0])
        self.assertEqual(400, self.request("POST", path, token=self.AGENT, raw=b"{}", headers={"Transfer-Encoding": "chunked"})[0])

    def test_unknown_url_query_routes_and_credential_fields(self):
        self.assertEqual(404, self.request("GET", "/v1/jobs?token=secret", token=self.AGENT)[0])
        self.assertEqual(404, self.request("GET", "/not-here")[0])
        self.assertEqual(400, self.request("POST", "/v1/jobs", {"url": "http://127.0.0.1", "purpose": "x"}, self.AGENT)[0])
        self.assertEqual(400, self.request("POST", "/v1/jobs", {"url": "https://example.com", "purpose": "x", "cookies": "secret"}, self.AGENT)[0])

    def test_simultaneous_http_idempotency_and_cancel_share_race(self):
        payload = {"url": "https://example.com/article", "purpose": "Read", "idempotency_key": "parallel-http"}
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(lambda _: self.request("POST", "/v1/jobs", payload, self.AGENT), range(12)))
        self.assertEqual(1, sum(status == 201 for status, _, _ in outcomes))
        self.assertEqual(11, sum(status == 200 for status, _, _ in outcomes))
        job_id = outcomes[0][1]["job"]["id"]
        for event in ["approve", "preview_ready"]:
            self.store.event(job_id, {"type": event})
        with ThreadPoolExecutor(max_workers=2) as pool:
            cancel = pool.submit(self.request, "POST", "/v1/jobs/" + job_id + "/cancel", {}, self.AGENT)
            share = pool.submit(self.request, "POST", "/v1/browser/jobs/" + job_id + "/events", {"type": "complete", "result": sample_result()}, self.BROWSER)
            self.assertEqual([200, 409], sorted([cancel.result()[0], share.result()[0]]))
        job = self.store.get(job_id)
        self.assertIn(job["state"], {"completed", "cancelled"})
        self.assertEqual(job["state"] == "completed", job["result"] is not None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
