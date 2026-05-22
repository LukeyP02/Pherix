"""Pherix inspector — the live governance console (the "see it" layer).

A self-contained, read-only web app over the audit journal
(:mod:`pherix.core.audit`). It renders the four axes happening live:
the effect timeline (interception + adapter), the fold/gate/STUCK
(compensation), and per-effect policy verdicts (policy). Zero third-party
dependencies — stdlib :mod:`http.server` backend + a static vanilla-JS
frontend, served fully offline against a local SQLite journal.

Run it::

    python -m pherix.inspector --db path/to/audit.db

The reader (:mod:`pherix.inspector.reader`) is a pure read layer over the
schema and carries no engine imports, so it stays robust against a journal
written by any Pherix version that keeps the ``transactions`` / ``effects``
table shapes.
"""

from pherix.inspector.reader import JournalReader

__all__ = ["JournalReader"]
