"""Field-level payload encryption — the data↔control trust boundary.

The control plane stores the journal's *shape* (tool / resource / status / ts —
cleartext, for metering and the shape-moat) but must never read the *payload* (an
effect's ``args`` and ``result``, which carry the customer's data). This module is
that boundary: the payload is encrypted with the customer's own key before it
leaves the host, so the control plane stores ciphertext it cannot decrypt.
"Anonymised so we can't read it, the customer can."

v1 is BYOK symmetric: one 256-bit key per org, held by the customer's SDK and
never sent to us. AES-256-GCM is an *authenticated* cipher — it gives
confidentiality AND integrity in one primitive: a tampered ciphertext fails to
decrypt (the GCM auth tag) rather than silently returning garbage, so the journal
a customer reads back is provably exactly what their agent wrote. Each call draws
a fresh random 96-bit nonce, so encrypting the same plaintext twice yields
different tokens — the control plane cannot even tell that two payloads are equal.

``cryptography`` is imported lazily so ``import pherix`` stays dependency-free;
the crypto dep is pulled only via the ``pherix[sync]`` extra, like the adapter
drivers.
"""

from __future__ import annotations

import base64
import os

_KEY_BYTES = 32  # AES-256
_NONCE_BYTES = 12  # GCM standard 96-bit nonce


def generate_key() -> bytes:
    """A fresh 256-bit key. The customer stores this; Pherix never sees it."""
    return os.urandom(_KEY_BYTES)


class PayloadCipher:
    """AES-256-GCM over the customer's per-org key.

    :meth:`encrypt` returns a urlsafe-base64 token framing ``nonce || ciphertext
    || tag``; :meth:`decrypt` reverses it and authenticates. ``None`` passes
    through unchanged — a journalled effect with no result stays ``None``, not an
    encryption of the string ``"null"`` — so the cleartext/ciphertext distinction
    never collapses on absence.
    """

    def __init__(self, key: bytes):
        if len(key) != _KEY_BYTES:
            raise ValueError(
                f"PayloadCipher key must be {_KEY_BYTES} bytes (256-bit); "
                f"got {len(key)}"
            )
        self._key = key

    def _aead(self):
        # Lazy import: the crypto dependency is pulled via pherix[sync], so the
        # kernel and a non-syncing agent never need it installed.
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

        return AESGCM(self._key)

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None:
            return None
        nonce = os.urandom(_NONCE_BYTES)
        ciphertext = self._aead().encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii")

    def decrypt(self, token: str | None) -> str | None:
        if token is None:
            return None
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
        # Raises cryptography.exceptions.InvalidTag on a tampered token or the
        # wrong key — decryption fails loudly rather than returning garbage.
        return self._aead().decrypt(nonce, ciphertext, None).decode("utf-8")
