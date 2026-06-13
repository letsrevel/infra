#!/usr/bin/env python3
"""Synthetic login canary for the Revel frontend.

Every CANARY_INTERVAL seconds it performs a *real* end-to-end login through the
public edge (Cloudflare -> Caddy -> SvelteKit -> backend), exercising the exact
path that broke during the 2026-06-10 -> 06-12 outage (SSR internal rewrite
dropping POST bodies). It exposes Prometheus metrics on :8080/metrics; the
`CanaryLoginFailed` alert pages when no login has succeeded for >30 min.

Stdlib only — no third-party deps, so it runs in a plain `python:3.13-slim`
container with this file bind-mounted.

Environment:
  CANARY_BASE_URL     Public base URL (default https://letsrevel.io)
  CANARY_EMAIL        Canary account email          (required)
  CANARY_PASSWORD     Canary account password       (required)
  CANARY_INTERVAL     Seconds between checks         (default 600)
  CANARY_METRICS_PORT Port for /metrics              (default 8080)
  CANARY_TIMEOUT      Per-request timeout seconds    (default 20)

The canary account MUST have 2FA disabled (a 2FA-gated login returns HTTP 200
with a temp token, not the 303 redirect this canary treats as success).
"""

import http.client
import json
import os
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

BASE_URL = os.environ.get("CANARY_BASE_URL", "https://letsrevel.io").rstrip("/")
EMAIL = os.environ.get("CANARY_EMAIL", "")
PASSWORD = os.environ.get("CANARY_PASSWORD", "")
INTERVAL = int(os.environ.get("CANARY_INTERVAL", "600"))
METRICS_PORT = int(os.environ.get("CANARY_METRICS_PORT", "8080"))
TIMEOUT = int(os.environ.get("CANARY_TIMEOUT", "20"))

USER_AGENT = "revel-login-canary/1.0"

# Shared metric state, guarded by a lock (the HTTP server is multi-threaded).
_lock = threading.Lock()
_state = {
    "last_success_ts": 0.0,  # 0 => never succeeded; makes the alert fire on a broken-from-boot login
    "failures_total": 0,
    "attempts_total": 0,
    "last_duration_seconds": 0.0,
    "last_status": "startup",
}


def log(level: str, event: str, **fields: object) -> None:
    """Emit one structlog-shaped JSON line (same contract as the app logger)."""
    record = {
        "event": event,
        "level": level,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z",
        "logger": "canary",
        **fields,
    }
    stream = sys.stderr if level in ("error", "warning") else sys.stdout
    stream.write(json.dumps(record) + "\n")
    stream.flush()


def _request(method: str, url: str, headers: dict[str, str], body: bytes | None = None):
    """Issue a single HTTPS request WITHOUT following redirects.

    Returns (status_code, location_header, body_bytes). http.client never
    auto-follows redirects, so a SvelteKit 303 (login success) is observable.
    """
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme == "http":
        conn = http.client.HTTPConnection(parsed.hostname, parsed.port or 80, timeout=TIMEOUT)
    else:
        conn = http.client.HTTPSConnection(parsed.hostname, parsed.port or 443, timeout=TIMEOUT)
    try:
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
        data = resp.read()
        return resp.status, resp.getheader("Location"), data
    finally:
        conn.close()


def check_login() -> bool:
    """Run one GET /login + POST login-action cycle. Returns True on success."""
    started = time.monotonic()
    try:
        # 1) The login page must render through the edge.
        get_status, _, _ = _request(
            "GET",
            f"{BASE_URL}/login",
            headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
        )
        if get_status != 200:
            _record_failure(started, f"get_login_{get_status}")
            log("error", "canary_get_login_failed", status_code=get_status)
            return False

        # 2) POST the real SvelteKit `login` form action. A successful login
        #    throws redirect(303) -> 303 with a Location header. `fail()` -> 400,
        #    a returned object (e.g. 2FA required) -> 200. Only 303 is success.
        #    SvelteKit's CSRF guard requires the Origin header to match the site.
        form = urllib.parse.urlencode({"email": EMAIL, "password": PASSWORD}).encode()
        post_status, location, _ = _request(
            "POST",
            f"{BASE_URL}/login?/login",
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Accept": "text/html",
            },
            body=form,
        )
        if post_status == 303 and location and "/login" not in location:
            duration = time.monotonic() - started
            with _lock:
                _state["last_success_ts"] = time.time()
                _state["attempts_total"] += 1
                _state["last_duration_seconds"] = duration
                _state["last_status"] = "ok"
            log("info", "canary_login_succeeded", duration_ms=round(duration * 1000), location=location)
            return True

        _record_failure(started, f"post_login_{post_status}")
        log("error", "canary_login_failed", status_code=post_status, location=location)
        return False
    except Exception as exc:  # network error, DNS, TLS, timeout
        _record_failure(started, f"exception_{type(exc).__name__}")
        log("error", "canary_login_exception", error=str(exc), error_type=type(exc).__name__)
        return False


def _record_failure(started: float, status: str) -> None:
    with _lock:
        _state["failures_total"] += 1
        _state["attempts_total"] += 1
        _state["last_duration_seconds"] = time.monotonic() - started
        _state["last_status"] = status


def _render_metrics() -> bytes:
    with _lock:
        s = dict(_state)
    lines = [
        "# HELP canary_login_last_success_timestamp_seconds Unix time of the last successful canary login.",
        "# TYPE canary_login_last_success_timestamp_seconds gauge",
        f"canary_login_last_success_timestamp_seconds {s['last_success_ts']}",
        "# HELP canary_login_failures_total Total canary login failures.",
        "# TYPE canary_login_failures_total counter",
        f"canary_login_failures_total {s['failures_total']}",
        "# HELP canary_login_attempts_total Total canary login attempts.",
        "# TYPE canary_login_attempts_total counter",
        f"canary_login_attempts_total {s['attempts_total']}",
        "# HELP canary_login_last_duration_seconds Duration of the last canary login attempt.",
        "# TYPE canary_login_last_duration_seconds gauge",
        f"canary_login_last_duration_seconds {s['last_duration_seconds']}",
    ]
    return ("\n".join(lines) + "\n").encode()


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (stdlib casing)
        if self.path != "/metrics":
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")
            return
        body = _render_metrics()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        # Silence the default access logging — the metrics scrape is high-volume noise.
        return


def _loop() -> None:
    while True:
        check_login()
        time.sleep(INTERVAL)


def main() -> None:
    if not EMAIL or not PASSWORD:
        log("error", "canary_misconfigured", reason="CANARY_EMAIL/CANARY_PASSWORD missing")
        sys.exit(1)
    log("info", "canary_starting", base_url=BASE_URL, interval_seconds=INTERVAL, metrics_port=METRICS_PORT)
    threading.Thread(target=_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", METRICS_PORT), MetricsHandler).serve_forever()


if __name__ == "__main__":
    main()
