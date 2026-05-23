"""The data↔control connection — the SDK *ship* side of the journal.

Three pieces, one trust boundary:

- :class:`~pherix.sync.crypto.PayloadCipher` — AES-256-GCM over the customer's
  per-org key. The control plane stores ciphertext it cannot read.
- :class:`~pherix.sync.redact.Redactor` — drops known PII at the edge, before
  encryption (defence in depth).
- :class:`~pherix.sync.shipper.JournalShipper` — forward-reads the local journal
  past a durable cursor, redacts + encrypts the payload, ships a batch to the
  control-plane ingest endpoint, advances the cursor. Never blocks the agent.

Everything here lazy-imports its heavy deps (``cryptography``, an HTTP client);
``import pherix`` stays dependency-free. Pull them via the ``pherix[sync]`` extra.
"""

from pherix.sync.crypto import PayloadCipher, generate_key
from pherix.sync.redact import DEFAULT_PII_KEYS, REDACTED, Redactor
from pherix.sync.shipper import JournalShipper

__all__ = [
    "PayloadCipher",
    "generate_key",
    "Redactor",
    "DEFAULT_PII_KEYS",
    "REDACTED",
    "JournalShipper",
]
