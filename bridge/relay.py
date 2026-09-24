"""Single-user browser capture relay; never fetches jobs or executes their content.

Run ``python -m bridge.relay init --output-dir PRIVATE_DIR`` then ``serve``.
The relay is a development prototype, not a multi-tenant production service.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlsplit

MAX_BODY_BYTES = 600_000
QUEUE_MAX = 1000
RETENTION_DAYS = 7
TERMINAL = frozenset({"completed", "failed", "cancelled"})
EVENT_STATES = {
    "approve": ("queued", "running"),
    "human_required": ("running", "awaiting_human"),
    "resume": ("awaiting_human", "running"),
    "preview_ready": ("running", "awaiting_share"),
    "complete": ("awaiting_share", "completed"),
}
EXTENSION_ORIGIN = re.compile(r"chrome-extension://([a-p]{32})\Z")
SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "refresh_token", "id_token", "token", "auth_token", "api_key",
    "apikey", "password", "passwd", "secret", "client_secret", "session_token",
    "authorization", "cookie", "set-cookie", "auth", "session", "sessionid", "sid",
    "jwt", "bearer", "signature", "x-amz-signature", "x-amz-credential",
    "x-goog-signature", "x-goog-credential",
})
RESULT_REQUIRED = frozenset({"url", "title", "text", "markdown", "links", "captured_at", "truncated"})
RESULT_KEYS = RESULT_REQUIRED | {"degraded", "quality"}


class RelayError(Exception):
    def __init__(self, status: int, message: str):
        self.status, self.message = status, message
        super().__init__(message)


def utc_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _string(value, name: str, minimum: int = 0, maximum: int = 1000) -> str:
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise RelayError(400, f"Invalid {name}")
    # Surrogate escapes cannot be encoded as UTF-8 and are not valid capture text.
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise RelayError(400, f"Invalid {name}")
    return value


def _fields(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise RelayError(400, "Invalid or unexpected fields")


def validate_public_url(value, name="url"):
    """Validate syntax and literal hosts only; no DNS lookup or network request."""
    value = _string(value, name, 1, 8192)
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in value) or "\\" in value:
        raise RelayError(400, f"Invalid {name}")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if parsed.scheme not in {"http", "https"} or not host or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        host = host.rstrip(".").lower().encode("idna").decode("ascii")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")) or "%" in host:
            raise ValueError()
        try:
            literal = ipaddress.ip_address(host)
        except ValueError:
            # Reject short/numeric host spellings interpreted as IPs by browsers.
            labels = host.split(".")
            if len(labels) < 2 or all(re.fullmatch(r"(?:[0-9]+|0x[0-9a-f]+)", label) for label in labels):
                raise ValueError()
            if not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
                raise ValueError()
            if len(host) > 253:
                raise ValueError()
        else:
            if not literal.is_global or (getattr(literal, "ipv4_mapped", None) is not None and not literal.ipv4_mapped.is_global):
                raise ValueError()
            host = literal.compressed
        if any(key.lower() in SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query, keep_blank_values=True)):
            raise ValueError()
    except (ValueError, UnicodeError):
        raise RelayError(400, f"Invalid or non-public {name}") from None
    return value, (parsed.scheme, host, port or (443 if parsed.scheme == "https" else 80))


def validate_result(result, job):
    _fields(result, RESULT_KEYS, RESULT_REQUIRED)
    _, origin = validate_public_url(result["url"], "result.url")
    _, job_origin = validate_public_url(job["url"])
    if origin != job_origin:
        raise RelayError(400, "Result origin differs from job origin")
    # Origin authorization is not article identity. Match the browser's URL
    # serialization (dot segments, UTF-8, special-query apostrophe), then ignore
    # only trailing slashes/fragments. Query order/values stay distinct.
    def resource_key(url):
        parts = urlsplit(url)
        path = quote(parts.path, safe="/%:@!$&'()*+,;=-._~")
        segments = []
        for segment in path.split("/"):
            dot = re.sub(r"%2e", ".", segment, flags=re.IGNORECASE)
            if dot == ".":
                continue
            if dot == "..":
                if len(segments) > 1:
                    segments.pop()
                continue
            segments.append(segment)
        path = "/".join(segments).rstrip("/") or "/"
        query = quote(parts.query, safe="%/?@:!$&()*+,;=-._~")
        return path, query
    if resource_key(result["url"]) != resource_key(job["url"]):
        raise RelayError(400, "Result page differs from requested resource")
    _string(result["title"], "result.title", maximum=1000)
    _string(result["text"], "result.text", maximum=job["max_chars"])
    if not result["text"].strip():
        raise RelayError(400, "Empty text cannot complete a job; fail it instead")
    if "degraded" in result and type(result["degraded"]) is not bool:
        raise RelayError(400, "Invalid result.degraded")
    if "quality" in result and (not isinstance(result["quality"], str) or result["quality"] not in {"full", "partial"}):
        raise RelayError(400, "Invalid result.quality")
    partial = result.get("quality") == "partial"
    degraded = result.get("degraded", False)
    if degraded != partial:
        raise RelayError(400, "Partial results require degraded=true and quality=partial together")
    if job.get("degraded") and not (degraded and partial):
        raise RelayError(400, "Timeout fallback must be explicitly labelled degraded=true and quality=partial")
    _string(result["markdown"], "result.markdown", maximum=2 * job["max_chars"])
    captured = _string(result["captured_at"], "result.captured_at", 1, 64)
    try:
        parsed = datetime.fromisoformat(captured.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError()
    except ValueError:
        raise RelayError(400, "Invalid result.captured_at; timezone required") from None
    if type(result["truncated"]) is not bool:
        raise RelayError(400, "Invalid result.truncated")
    if not isinstance(result["links"], list) or len(result["links"]) > 200:
        raise RelayError(400, "Invalid result.links")
    for link in result["links"]:
        _fields(link, {"text", "url"}, {"text", "url"})
        _string(link["text"], "link.text", maximum=1000)
        validate_public_url(link["url"], "link.url")
    return result


class Store:
    def __init__(self, db_path, *, clock=time.time, retention_days=RETENTION_DAYS, queue_max=QUEUE_MAX):
        self.clock, self.ttl, self.queue_max = clock, retention_days * 86400, queue_max
        self.lock = threading.RLock()
        path = str(db_path)
        if path != ":memory:":
            target = Path(path)
            target.parent.mkdir(parents=True, exist_ok=True)
            # O_NOFOLLOW avoids replacing permissions or writing through symlinks.
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
            os.fchmod(descriptor, 0o600)
            os.close(descriptor)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.conn.execute("PRAGMA secure_delete=ON")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY, url TEXT NOT NULL, purpose TEXT NOT NULL,
            max_chars INTEGER NOT NULL, state TEXT NOT NULL,
            created REAL NOT NULL, updated REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '', result_json TEXT,
            idempotency_key TEXT UNIQUE, fingerprint TEXT NOT NULL,
            human_timeout_seconds INTEGER NOT NULL DEFAULT 300,
            human_deadline REAL, degraded INTEGER NOT NULL DEFAULT 0
        )""")
        # Compatible migration for databases created by the first prototype.
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(jobs)")}
        for name, definition in (
            ("human_timeout_seconds", "INTEGER NOT NULL DEFAULT 300"),
            ("human_deadline", "REAL"), ("degraded", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if name not in columns:
                self.conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        if "human_timeout_seconds" not in columns:
            for row in self.conn.execute("SELECT id, url, purpose, max_chars FROM jobs").fetchall():
                identity = json.dumps([row["url"], row["purpose"], row["max_chars"], 300], ensure_ascii=False, separators=(",", ":"))
                self.conn.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (token_hash(identity), row["id"]))
        self.conn.execute("CREATE INDEX IF NOT EXISTS jobs_created ON jobs(created)")

    def close(self):
        with self.lock:
            self.conn.close()

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                # TTL cleanup is committed independently of invalid job operations.
                self.conn.execute("DELETE FROM jobs WHERE created <= ?", (self.clock() - self.ttl,))
                self.conn.execute("COMMIT")
                self.conn.execute("BEGIN IMMEDIATE")
                yield
                self.conn.execute("COMMIT")
            except BaseException:
                if self.conn.in_transaction:
                    self.conn.execute("ROLLBACK")
                raise

    @staticmethod
    def _job(row):
        return {
            "id": row["id"], "url": row["url"], "purpose": row["purpose"],
            "max_chars": row["max_chars"], "state": row["state"],
            "created_at": utc_iso(row["created"]), "updated_at": utc_iso(row["updated"]),
            "reason": row["reason"],
            "human_timeout_seconds": row["human_timeout_seconds"],
            "human_deadline_at": utc_iso(row["human_deadline"]) if row["human_deadline"] is not None else None,
            "degraded": bool(row["degraded"]),
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }

    def _get(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise RelayError(404, "Job not found")
        return self._job(row)

    def create(self, payload):
        _fields(payload, {"url", "purpose", "max_chars", "idempotency_key", "human_timeout_seconds"}, {"url", "purpose"})
        url, _ = validate_public_url(payload["url"])
        purpose = _string(payload["purpose"], "purpose", 1, 500)
        maximum = payload.get("max_chars", 20000)
        if type(maximum) is not int or not 100 <= maximum <= 100000:
            raise RelayError(400, "Invalid max_chars")
        human_timeout = payload.get("human_timeout_seconds", 300)
        if type(human_timeout) is not int or not 5 <= human_timeout <= 900:
            raise RelayError(400, "Invalid human_timeout_seconds")
        key = payload.get("idempotency_key")
        if "idempotency_key" in payload:
            _string(key, "idempotency_key", 1, 200)
        identity = json.dumps([url, purpose, maximum, human_timeout], ensure_ascii=False, separators=(",", ":"))
        fingerprint = token_hash(identity)
        with self.transaction():
            if key is not None:
                row = self.conn.execute("SELECT * FROM jobs WHERE idempotency_key = ?", (key,)).fetchone()
                if row:
                    if not hmac.compare_digest(row["fingerprint"], fingerprint):
                        raise RelayError(409, "Idempotency key is already used for a different job")
                    return self._job(row), False
            count = self.conn.execute("SELECT COUNT(*) FROM jobs WHERE state NOT IN ('completed', 'failed', 'cancelled')").fetchone()[0]
            if count >= self.queue_max:
                raise RelayError(429, "Job queue is full")
            job_id, now = str(uuid.uuid4()), self.clock()
            self.conn.execute("INSERT INTO jobs (id,url,purpose,max_chars,state,created,updated,idempotency_key,fingerprint,human_timeout_seconds) VALUES (?,?,?,?,?,?,?,?,?,?)",
                              (job_id, url, purpose, maximum, "queued", now, now, key, fingerprint, human_timeout))
            return self._get(job_id), True

    def get(self, job_id):
        with self.transaction():
            return self._get(job_id)

    def pending(self):
        with self.transaction():
            rows = self.conn.execute("SELECT * FROM jobs WHERE state NOT IN ('completed', 'failed', 'cancelled') ORDER BY created, rowid LIMIT 100").fetchall()
            return [self._job(row) for row in rows]

    def event(self, job_id, payload):
        _fields(payload, {"type", "reason", "result"}, {"type"})
        event = _string(payload["type"], "event.type", 1, 32)
        reason = _string(payload.get("reason", ""), "reason", maximum=1000)
        if re.search(r"<(?:!doctype|/?[a-z][a-z0-9]*[\s>])", reason, re.IGNORECASE):
            raise RelayError(400, "Reason must be a short plain-text explanation, not wall HTML")
        if event not in EVENT_STATES and event not in {"fail", "cancel", "timeout"}:
            raise RelayError(400, "Unknown event type")
        if "result" in payload and event != "complete":
            raise RelayError(400, "Only an explicit complete event may upload a result")
        with self.transaction():
            job = self._get(job_id)
            if job["state"] in TERMINAL:
                raise RelayError(409, "Job is terminal")
            now = self.clock()
            deadline = (datetime.fromisoformat(job["human_deadline_at"].replace("Z", "+00:00")).timestamp()
                        if job["human_deadline_at"] else None)
            degraded = job["degraded"]
            if event in {"fail", "cancel"}:
                next_state = "failed" if event == "fail" else "cancelled"
            elif event == "timeout":
                if job["state"] != "awaiting_human" or deadline is None or now < deadline:
                    raise RelayError(409, "Human wait has not reached its server deadline")
                next_state, degraded = "running", True
                reason = "Human wait timed out; continuing automatic public-content fallback"
            else:
                expected, next_state = EVENT_STATES[event]
                if job["state"] != expected:
                    raise RelayError(409, "Event is not allowed in current state")
                if event == "resume" and (deadline is None or now >= deadline):
                    raise RelayError(409, "Human wait deadline passed; timeout fallback is required")
            if event == "human_required":
                deadline = now + job["human_timeout_seconds"]
            elif next_state != "awaiting_human":
                deadline = None
            result = None
            if event == "complete":
                result = validate_result(payload.get("result"), job)
                degraded = degraded or result.get("degraded", False)
            self.conn.execute("UPDATE jobs SET state=?, updated=?, reason=?, result_json=?, human_deadline=?, degraded=? WHERE id=?",
                              (next_state, now, reason, json.dumps(result, ensure_ascii=False) if result is not None else None,
                               deadline, int(degraded), job_id))
            return self._get(job_id)

    def cancel(self, job_id):
        return self.event(job_id, {"type": "cancel", "reason": "Cancelled by agent"})


def load_config(path):
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Invalid server config")
    for role in ("agent", "browser"):
        if not re.fullmatch(r"[0-9a-f]{64}", config.get(f"{role}_token_hash", "")):
            raise ValueError("Config requires separate SHA256 token hashes")
    if hmac.compare_digest(config["agent_token_hash"], config["browser_token_hash"]):
        raise ValueError("Agent and browser token hashes must differ")
    if "agent_token" in config or "browser_token" in config:
        raise ValueError("Server config must not contain plaintext tokens")
    return config


class RelayServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, config, extension_id=None):
        if extension_id is not None and not re.fullmatch(r"[a-p]{32}", extension_id):
            raise ValueError("Invalid extension ID")
        self.store, self.config, self.extension_id = store, config, extension_id
        super().__init__(address, RelayHandler)


class RelayHandler(BaseHTTPRequestHandler):
    # Close each response to avoid unread-request/body desynchronization.
    protocol_version = "HTTP/1.0"
    server_version = "ArmoryRelay/0.1"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(10)

    def log_message(self, format, *args):
        # Do not log tokens, job URLs, result text, paths or user-provided headers.
        pass

    def _origin(self):
        origins = self.headers.get_all("Origin", [])
        if len(origins) > 1:
            raise RelayError(403, "Origin not allowed")
        origin = origins[0] if origins else None
        if origin is not None:
            match = EXTENSION_ORIGIN.fullmatch(origin)
            if not match or (self.server.extension_id and match.group(1) != self.server.extension_id):
                raise RelayError(403, "Origin not allowed")
        return origin

    def _role(self):
        authorization = self.headers.get_all("Authorization", [])
        value = authorization[0] if len(authorization) == 1 else ""
        if not value.startswith("Bearer ") or len(value) > 2048:
            raise RelayError(401, "Valid bearer token required")
        digest = token_hash(value[7:])
        agent = hmac.compare_digest(digest, self.server.config["agent_token_hash"])
        browser = hmac.compare_digest(digest, self.server.config["browser_token_hash"])
        if agent:
            return "agent"
        if browser:
            return "browser"
        raise RelayError(401, "Valid bearer token required")

    def _body(self):
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1 or not re.fullmatch(r"[0-9]+", lengths[0]):
            raise RelayError(400, "Exactly one Content-Length is required")
        size = int(lengths[0])
        if size > MAX_BODY_BYTES:
            raise RelayError(413, "Request body is too large")
        if self.headers.get_content_type() != "application/json":
            raise RelayError(415, "Content-Type must be application/json")
        data = self.rfile.read(size)
        if len(data) != size:
            raise RelayError(400, "Incomplete request body")
        def unique_fields(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError()
                result[key] = value
            return result
        def reject_constant(value):
            raise ValueError()
        try:
            return json.loads(data.decode("utf-8"), object_pairs_hook=unique_fields, parse_constant=reject_constant)
        except (ValueError, UnicodeError, RecursionError):
            raise RelayError(400, "Invalid JSON body") from None

    def _send(self, status, payload=None, origin=None, preflight=False):
        data = b"" if payload is None else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        if preflight:
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Authorization, Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if data:
            self.wfile.write(data)

    def _dispatch(self):
        origin = None
        try:
            origin = self._origin()
            parsed = urlsplit(self.path)
            if parsed.query or parsed.fragment or parsed.scheme or parsed.netloc:
                raise RelayError(404, "Endpoint not found")
            path = parsed.path
            if self.command == "OPTIONS":
                if not path.startswith("/v1/") or origin is None:
                    raise RelayError(403, "Preflight requires an allowed extension origin")
                if self.headers.get("Access-Control-Request-Method") not in {"GET", "POST"}:
                    raise RelayError(403, "Preflight method not allowed")
                requested = {part.strip().lower() for part in self.headers.get("Access-Control-Request-Headers", "").split(",") if part.strip()}
                if requested - {"authorization", "content-type"}:
                    raise RelayError(403, "Preflight headers not allowed")
                self._send(204, origin=origin, preflight=True)
                return
            if path == "/health" and self.command == "GET":
                self._send(200, {"status": "ok", "version": 1, "single_user": True}, origin)
                return
            if not path.startswith("/v1/"):
                raise RelayError(404, "Endpoint not found")
            role = self._role()
            job_path = re.fullmatch(r"/v1/jobs/([0-9a-f-]{36})(/cancel)?", path)
            event_path = re.fullmatch(r"/v1/browser/jobs/([0-9a-f-]{36})/events", path)
            if path == "/v1/jobs" and self.command == "POST":
                if role != "agent":
                    raise RelayError(403, "Agent role required")
                job, created = self.server.store.create(self._body())
                self._send(201 if created else 200, {"job": job}, origin)
            elif job_path and self.command == "GET" and not job_path.group(2):
                self._send(200, {"job": self.server.store.get(job_path.group(1))}, origin)
            elif job_path and self.command == "POST" and job_path.group(2):
                if role != "agent":
                    raise RelayError(403, "Agent role required")
                payload = self._body()
                _fields(payload, ())
                self._send(200, {"job": self.server.store.cancel(job_path.group(1))}, origin)
            elif path == "/v1/browser/jobs" and self.command == "GET":
                if role != "browser":
                    raise RelayError(403, "Browser role required")
                self._send(200, {"jobs": self.server.store.pending()}, origin)
            elif event_path and self.command == "POST":
                if role != "browser":
                    raise RelayError(403, "Browser role required")
                job = self.server.store.event(event_path.group(1), self._body())
                self._send(200, {"job": job}, origin)
            else:
                raise RelayError(404, "Endpoint not found")
        except RelayError as error:
            self._send(error.status, {"error": error.message}, origin)
        except (TimeoutError, ConnectionError):
            self.close_connection = True
        except Exception:
            # Never serialize database errors, request contents or configuration.
            self._send(500, {"error": "Internal relay error"}, origin)

    do_GET = _dispatch
    do_POST = _dispatch
    do_OPTIONS = _dispatch


def init_config(output_dir, relay_url="http://127.0.0.1:8765"):
    try:
        parsed = urlsplit(relay_url)
        if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError()
        if not parsed.hostname or (parsed.port is not None and not 1 <= parsed.port <= 65535):
            raise ValueError()
        is_loopback = parsed.hostname.lower() == "localhost"
        try:
            is_loopback = is_loopback or ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            pass
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise ValueError()
    except ValueError:
        raise ValueError("relay_url requires HTTPS, except loopback development HTTP") from None
    directory = Path(output_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths = [directory / name for name in ("server-config.json", "agent-client.json", "browser-client.json")]
    if any(path.exists() or path.is_symlink() for path in paths):
        raise FileExistsError("Refusing to overwrite existing relay credentials")
    agent, browser = secrets.token_urlsafe(48), secrets.token_urlsafe(48)
    bodies = [
        {"version": 1, "agent_token_hash": token_hash(agent), "browser_token_hash": token_hash(browser)},
        {"relay_url": relay_url.rstrip("/"), "agent_token": agent},
        {"relay_url": relay_url.rstrip("/"), "browser_token": browser},
    ]
    created = []
    try:
        for path, body in zip(paths, bodies):
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created.append(path)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(body, handle, indent=2)
                handle.write("\n")
        return paths
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Create separate private client tokens and hashed server config")
    initialize.add_argument("--output-dir", required=True)
    initialize.add_argument("--relay-url", default="http://127.0.0.1:8765")
    serve = commands.add_parser("serve", help="Run single-user relay; default loopback only")
    serve.add_argument("--config", required=True)
    serve.add_argument("--db", required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--extension-id", help="Restrict CORS to one Chrome extension ID")
    args = parser.parse_args(argv)
    if args.command == "init":
        for path in init_config(args.output_dir, args.relay_url):
            print(path)
        return 0
    config = load_config(args.config)
    store = Store(args.db)
    server = RelayServer((args.host, args.port), store, config, args.extension_id)
    print(f"Armory single-user relay listening on {args.host}:{server.server_port}; no job URL is fetched", flush=True)
    if args.host not in {"127.0.0.1", "localhost", "::1"}:
        print("WARNING: non-loopback binding requires a trusted HTTPS reverse proxy and access controls; prototype only", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
