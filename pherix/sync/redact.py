"""Edge redaction — drop known PII before the payload is even encrypted.

Encryption stops *us* from reading the payload; redaction stops known-PII fields
from being shipped at all — defence in depth, and the right tool for data a
customer never wants leaving the host regardless of who holds the key. The base
is field-name based: any JSON object key matching the redact set (recursively)
has its value replaced with a marker. A buyer extends the set or swaps the whole
callable — this is the seam, not the final policy.
"""

from __future__ import annotations

import json
from typing import Any, Iterable

REDACTED = "«redacted»"

# A small, opinionated default: field names that are PII often enough that
# shipping them by accident is the failure mode. Buyers extend this set.
DEFAULT_PII_KEYS = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "token",
        "api_key",
        "apikey",
        "authorization",
        "ssn",
        "email",
        "phone",
        "credit_card",
        "card_number",
        "cvv",
        "address",
        "dob",
    }
)


class Redactor:
    """Recursively mask the values of PII-named keys in a JSON payload string."""

    def __init__(self, keys: Iterable[str] | None = None):
        source = keys if keys is not None else DEFAULT_PII_KEYS
        self._keys = {k.lower() for k in source}

    def redact_json(self, payload: str | None) -> str | None:
        """Mask PII-named values in a JSON string (an effect's args / result).

        ``None`` and any payload that is not parseable JSON pass through
        unchanged — the redactor only reaches into structured payloads it can
        parse. The output is canonical (``sort_keys``) so a re-ship is byte-equal.
        """
        if payload is None:
            return None
        try:
            obj = json.loads(payload)
        except (ValueError, TypeError):
            return payload
        return json.dumps(self._walk(obj), sort_keys=True)

    def _walk(self, obj: Any) -> Any:
        if isinstance(obj, dict):
            return {
                k: (REDACTED if k.lower() in self._keys else self._walk(v))
                for k, v in obj.items()
            }
        if isinstance(obj, list):
            return [self._walk(v) for v in obj]
        return obj
