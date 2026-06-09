"""Stdlib HTTP server for the inspector — zero third-party dependencies.

A :class:`http.server.ThreadingHTTPServer` exposing a small read-only JSON
API over a :class:`pherix.inspector.reader.JournalReader`, plus the static
frontend. Live mode is plain polling: the page re-fetches the list and the
open timeline on an interval, so a demo run *animates* as effects land and
unwind — no websockets, no SSE, nothing to break offline.

Routes:

==============================  ===========================================
``GET /``                       the console (static ``index.html``)
``GET /static/<file>``          frontend assets (allow-listed names only)
``GET /api/stats``              headline counts + the filter vocabulary
``GET /api/transactions``       list + filter (query params below)
``GET /api/transactions/<id>``  one transaction's full timeline
``GET /api/reliability``        reliability metrics (Prong #2) over the journal
``GET /api/contention``         isolation collision map — where agents collide (Prong #2)
``GET /api/policy``             per-rule policy ledger — what each rule/cap/allowlist decided
``GET /api/recovery``           reconciliation queue — txns that didn't undo cleanly
``GET /api/approvals``          over-the-wire gate queue — held + cleared irreversibles
``GET /api/lineage``            causal read→write provenance (``?txn=<id>``)
``GET /api/undo-impact/<id>``   blast radius of undoing one transaction
``GET /api/provenance/<id>``    transitive upstream ancestry of one transaction
==============================  ===========================================

``/api/transactions`` accepts ``state``, ``client_id``, ``tool``, ``since``,
``until``, ``include_dry_run`` (``0``/``1``), and ``limit``.

``/api/reliability`` accepts ``include_dry_run`` (``0``/``1``, default ``0``
— a dry-run touched nothing, so it is excluded from reliability rates by
default; pass ``include_dry_run=1`` to fold dry-runs back in).

``/api/lineage`` accepts an optional ``txn`` — present scopes the focus to one
transaction (upstream producers still resolved across the whole journal),
absent folds the entire journal.

``/api/undo-impact/<id>`` folds the blast radius of reversing one transaction —
who committed-read the exact versions it produced and which of its keys a later
live write has since superseded — into an undo-safety verdict. 404 if the
transaction is unknown (same shape as ``/api/transactions/<id>``).

``/api/provenance/<id>`` walks the version-grounded produces relation backward
across transaction boundaries — the transitive chain of prior transactions
whose writes fed this one's inputs, each ancestor at its shortest hop depth,
the walk bottoming out in external inputs at the journal's edge. 404 if the
transaction is unknown.

The reader is shared across request threads behind a lock — SQLite reads are
fast and the console is single-operator, so serialising them is simpler and
safer than juggling a connection pool.
"""

from __future__ import annotations

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import cast
from urllib.parse import parse_qs, urlparse

from pherix.inspector.reader import JournalReader

# Allow-listed static assets — no filesystem path is ever derived from the
# request, so path traversal is impossible by construction.
_STATIC = {
    "index.html": "text/html; charset=utf-8",
    "app.js": "application/javascript; charset=utf-8",
    "style.css": "text/css; charset=utf-8",
}


def _static_bytes(name: str) -> bytes:
    return (resources.files("pherix.inspector.static") / name).read_bytes()


class InspectorServer(ThreadingHTTPServer):
    """Server subclass that declares the read-only journal handles the
    handler reaches for. Declaring them here (rather than ad-hoc attribute
    assignment) keeps the handler's ``self.srv`` access type-clean."""

    reader: JournalReader
    lock: threading.Lock
    verbose: bool = False


class InspectorHandler(BaseHTTPRequestHandler):
    server_version = "PherixInspector/1.0"

    @property
    def srv(self) -> InspectorServer:
        # self.server is typed as the base BaseServer; it is always an
        # InspectorServer here (make_server builds one). cast is a no-op at
        # runtime and avoids per-access type-ignores.
        return cast(InspectorServer, self.server)

    # --- helpers ------------------------------------------------------------

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Local console; the journal is read-only. No caching so live mode
        # always sees the freshest rows.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: object) -> None:
        self._send(
            code,
            json.dumps(payload).encode("utf-8"),
            "application/json; charset=utf-8",
        )

    def _read(self, fn, *args, **kwargs):
        """Run a reader call under the shared lock (SQLite read serialisation)."""
        with self.srv.lock:
            return fn(*args, **kwargs)

    # --- routing ------------------------------------------------------------

    def do_HEAD(self) -> None:  # noqa: N802 (http.server naming)
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        parsed = urlparse(self.path)
        path = parsed.path
        try:
            if path == "/" or path == "/index.html":
                self._send(200, _static_bytes("index.html"), _STATIC["index.html"])
                return
            if path.startswith("/static/"):
                name = path[len("/static/"):]
                if name in _STATIC:
                    self._send(200, _static_bytes(name), _STATIC[name])
                else:
                    self._json(404, {"error": "not found"})
                return
            if path == "/api/stats":
                self._json(200, self._read(self.srv.reader.stats))
                return
            if path == "/api/reliability":
                q = parse_qs(parsed.query)
                vals = q.get("include_dry_run")
                # Default excludes dry-runs (reliability() default); the param
                # only flips it ON when explicitly "1".
                include_dry_run = bool(vals) and vals[0] == "1"
                self._json(
                    200,
                    self._read(
                        self.srv.reader.reliability,
                        include_dry_run=include_dry_run,
                    ),
                )
                return
            if path == "/api/accountability":
                q = parse_qs(parsed.query)
                vals = q.get("include_dry_run")
                # Same default as reliability(): dry-runs excluded unless the
                # param is explicitly "1" — a dry-run touched nothing, so it is
                # not part of "what did this principal actually do?".
                include_dry_run = bool(vals) and vals[0] == "1"
                self._json(
                    200,
                    self._read(
                        self.srv.reader.accountability,
                        include_dry_run=include_dry_run,
                    ),
                )
                return
            if path == "/api/contention":
                self._json(200, self._read(self.srv.reader.contention))
                return
            if path == "/api/policy":
                self._json(200, self._read(self.srv.reader.policy))
                return
            if path == "/api/recovery":
                self._json(200, self._read(self.srv.reader.recovery))
                return
            if path == "/api/approvals":
                self._json(200, self._read(self.srv.reader.approvals))
                return
            if path == "/api/transactions":
                self._json(200, self._list(parse_qs(parsed.query)))
                return
            if path == "/api/lineage":
                q = parse_qs(parsed.query)
                txn = (q.get("txn") or [None])[0]
                self._json(200, self._read(self.srv.reader.lineage, txn))
                return
            if path.startswith("/api/undo-impact/"):
                txn_id = path[len("/api/undo-impact/"):]
                impact = self._read(self.srv.reader.undo_impact, txn_id)
                if impact is None:
                    self._json(404, {"error": f"no transaction {txn_id!r}"})
                else:
                    self._json(200, impact)
                return
            if path.startswith("/api/provenance/"):
                txn_id = path[len("/api/provenance/"):]
                prov = self._read(self.srv.reader.provenance, txn_id)
                if prov is None:
                    self._json(404, {"error": f"no transaction {txn_id!r}"})
                else:
                    self._json(200, prov)
                return
            if path.startswith("/api/transactions/"):
                txn_id = path[len("/api/transactions/"):]
                timeline = self._read(self.srv.reader.get_timeline, txn_id)
                if timeline is None:
                    self._json(404, {"error": f"no transaction {txn_id!r}"})
                else:
                    self._json(200, timeline)
                return
            self._json(404, {"error": "not found"})
        except BrokenPipeError:
            # Client navigated away mid-response (common during live polling).
            pass
        except Exception as exc:  # surface as JSON rather than a stack-trace page
            self._json(500, {"error": str(exc)})

    def _list(self, q: dict[str, list[str]]) -> list[dict]:
        def one(key: str) -> str | None:
            vals = q.get(key)
            return vals[0] if vals else None

        include_dry_run = one("include_dry_run")
        limit = one("limit")
        return self._read(
            self.srv.reader.list_transactions,
            state=one("state"),
            client_id=one("client_id"),
            tool=one("tool"),
            since=one("since"),
            until=one("until"),
            include_dry_run=(include_dry_run != "0"),
            limit=int(limit) if (limit and limit.isdigit()) else 200,
        )

    def log_message(self, *args) -> None:  # quieter: one line, opt-in
        if self.srv.verbose:
            super().log_message(*args)


def make_server(db_path: str, host: str = "127.0.0.1", port: int = 8765,
                verbose: bool = False) -> InspectorServer:
    """Build (but don't start) the inspector server over ``db_path``."""
    httpd = InspectorServer((host, port), InspectorHandler)
    httpd.reader = JournalReader(db_path)
    httpd.lock = threading.Lock()
    httpd.verbose = verbose
    return httpd


def serve(db_path: str, host: str = "127.0.0.1", port: int = 8765,
          verbose: bool = False) -> None:
    httpd = make_server(db_path, host, port, verbose)
    url = f"http://{host}:{port}/"
    print(f"Pherix inspector → {url}")
    print(f"  journal: {db_path}  (read-only)")
    print("  Ctrl-C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped.")
    finally:
        httpd.reader.close()
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="python -m pherix.inspector",
        description="Live governance console over a Pherix audit journal.",
    )
    ap.add_argument(
        "--db",
        default=None,
        help=(
            "path to the audit journal SQLite file "
            "(default: the standard Pherix journal location)"
        ),
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--verbose", action="store_true", help="log each request")
    args = ap.parse_args(argv)

    if args.db is None:
        from pherix.core.audit import default_journal_path  # parallel stream

        db_path = str(default_journal_path())
    else:
        db_path = args.db

    import os

    if not os.path.exists(db_path):
        print(
            f"No journal yet at {db_path!r} — run an agent first, or pass --db."
        )
        return 1

    serve(db_path, args.host, args.port, args.verbose)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
