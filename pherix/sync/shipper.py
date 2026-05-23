"""JournalShipper — push new journal rows to the control plane, off the hot path.

The *ship* side of the data↔control connection. It forward-reads the local audit
journal past a durable cursor (:meth:`AuditJournal.export_since`), redacts PII at
the edge, encrypts the payload under the customer's key, POSTs a batch to the
control-plane ingest endpoint, then advances the cursor.

It never blocks the agent:

- :meth:`pump` does one drain and is cheap — read rows, transform, one POST. It
  runs on the thread that calls it (the journal's owning thread), so it touches
  the journal connection only from where that connection is safe to touch.
- :meth:`start` runs the drain on a *daemon thread* that opens its **own**
  connection to the same on-disk journal — SQLite connections are thread-confined,
  so the ship thread never shares the agent's connection. The agent loop is
  untouched; shipping happens entirely beside it.

The transport is injectable, so the whole path is offline-testable: point it at a
FastAPI ``TestClient``, a fake, or the default HTTP client from
:func:`http_transport`. On a transport failure the cursor is **not** advanced, so
the batch re-ships next pump; idempotent ingest makes that a no-op at the control
plane. That same property covers the crash-between-ship-and-advance window.
"""

from __future__ import annotations

import threading
from typing import Callable

from pherix.core.audit import AuditJournal
from pherix.sync.crypto import PayloadCipher
from pherix.sync.redact import Redactor

# A transport takes the batch dict and returns the ingest result dict (or raises
# on failure — the shipper treats any exception as "not shipped").
Transport = Callable[[dict], dict]


def http_transport(base_url: str, api_key: str, *, timeout: float = 10.0) -> Transport:
    """Default transport: POST the batch to ``{base_url}/ingest`` as the org.

    Uses ``requests`` (lazy import — pulled via ``pherix[sync]``). Raises on a
    non-2xx so the shipper leaves the cursor un-advanced and retries.
    """

    def _send(batch: dict) -> dict:
        import requests

        resp = requests.post(
            f"{base_url.rstrip('/')}/ingest",
            json=batch,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json()

    return _send


class JournalShipper:
    """Cursor-based, batched, non-blocking shipper of journal rows to the control plane."""

    def __init__(
        self,
        journal: AuditJournal,
        agent_id: str,
        transport: Transport,
        *,
        key: bytes | None = None,
        cipher: PayloadCipher | None = None,
        redactor: Redactor | None = None,
        host: str | None = None,
    ):
        self._journal = journal
        self._agent_id = agent_id
        self._transport = transport
        self._host = host
        self._redactor = redactor if redactor is not None else Redactor()
        # Encryption is on iff a key/cipher is supplied. Without one the shipper
        # ships redacted *cleartext* and marks the batch encrypted=False — honest
        # either way: the control plane records exactly whether it can read the
        # payload.
        if cipher is not None:
            self._cipher: PayloadCipher | None = cipher
        elif key is not None:
            self._cipher = PayloadCipher(key)
        else:
            self._cipher = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # --- the drain (one pass) ----------------------------------------------

    def _protect(self, payload: str | None) -> str | None:
        """Redact then (if a cipher is set) encrypt one payload field."""
        redacted = self._redactor.redact_json(payload)
        return self._cipher.encrypt(redacted) if self._cipher is not None else redacted

    def _build_batch(self, rows: dict) -> dict:
        transactions = [
            {
                "txn_id": t["txn_id"],
                "state": t["state"],
                "session_id": None,
                "created_at": t.get("created_at"),
                "updated_at": t.get("updated_at"),
                "dry_run": bool(t.get("dry_run", 0)),
                "client_id": t.get("client_id"),
            }
            for t in rows.get("transactions", [])
        ]
        effects = [
            {
                "txn_id": e["txn_id"],
                "idx": e["idx"],
                "effect_id": e["effect_id"],
                "tool": e["tool"],
                "resource": e["resource"],
                "reversible": bool(e.get("reversible", 0)),
                "status": e["status"],
                "args": self._protect(e.get("args")),
                "result": self._protect(e.get("result")),
                "ts": e.get("ts"),
            }
            for e in rows.get("effects", [])
        ]
        verdicts = [
            {
                "txn_id": v["txn_id"],
                "effect_index": v["effect_index"],
                "seq_in_txn": v["seq"],
                "phase": v["phase"],
                "allow": bool(v["allow"]),
                "kind": v.get("kind", "rule"),
                "rule_name": v.get("rule_name"),
                "reason": v.get("reason"),
            }
            for v in rows.get("verdicts", [])
        ]
        return {
            "agent_id": self._agent_id,
            "host": self._host,
            "transactions": transactions,
            "effects": effects,
            "verdicts": verdicts,
            "encrypted": self._cipher is not None,
        }

    def _drain(self, journal: AuditJournal) -> dict:
        """Read new rows from ``journal``, ship them, advance the cursor.

        Returns ``{"shipped": n, ...transport result}``. A no-op (nothing new)
        returns ``shipped=0`` without calling the transport.
        """
        cursor = journal.get_ship_cursor()
        rows, new_cursor = journal.export_since(cursor)
        batch = self._build_batch(rows)
        shipped = (
            len(batch["transactions"]) + len(batch["effects"]) + len(batch["verdicts"])
        )
        if shipped == 0:
            return {"shipped": 0, "accepted": 0, "skipped": 0}
        # Transport may raise — if it does, the cursor is NOT advanced, so the
        # batch re-ships next pass and idempotent ingest skips dupes.
        result = self._transport(batch)
        journal.set_ship_cursor(new_cursor)
        return {"shipped": shipped, **(result or {})}

    def pump(self) -> dict:
        """One synchronous drain on the shipper's journal. Cheap; agent-thread safe."""
        return self._drain(self._journal)

    # --- background operation ----------------------------------------------

    def start(self, interval: float = 1.0) -> None:
        """Run :meth:`_drain` on a daemon thread every ``interval`` seconds.

        The thread opens its **own** connection to the journal's on-disk path —
        an in-memory journal has no shareable path and is rejected here (use
        :meth:`pump` on the owning thread instead).
        """
        if self._journal.path == ":memory:":
            raise ValueError(
                "cannot background-ship an in-memory journal (no shareable path); "
                "use pump() on the journal's owning thread"
            )
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        path = self._journal.path

        def _loop() -> None:
            # Thread-confined journal connection — never touches the agent's.
            journal = AuditJournal(path)
            try:
                while not self._stop.is_set():
                    try:
                        self._drain(journal)
                    except Exception:
                        # A transport/encryption hiccup must not kill the loop;
                        # the cursor stays put and we retry next tick.
                        pass
                    self._stop.wait(interval)
            finally:
                journal.close()

        self._thread = threading.Thread(
            target=_loop, name=f"pherix-shipper-{self._agent_id}", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the background thread to finish its current tick and join it."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
