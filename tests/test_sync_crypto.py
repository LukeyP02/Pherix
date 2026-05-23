"""Unit tests for the payload cipher — the data↔control encryption boundary.

Runs offline; needs only ``cryptography`` (the ``pherix[sync]`` extra).
"""

from __future__ import annotations

import pytest

pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidTag

from pherix.sync.crypto import PayloadCipher, generate_key


def test_round_trip():
    cipher = PayloadCipher(generate_key())
    plain = '{"amount": 4200, "to": "acct_9"}'
    token = cipher.encrypt(plain)
    assert token != plain
    assert cipher.decrypt(token) == plain


def test_none_passes_through():
    cipher = PayloadCipher(generate_key())
    assert cipher.encrypt(None) is None
    assert cipher.decrypt(None) is None


def test_nonce_is_fresh_each_call():
    # Same plaintext, two encryptions → different tokens. The control plane
    # cannot even tell two payloads are equal.
    cipher = PayloadCipher(generate_key())
    a = cipher.encrypt("identical")
    b = cipher.encrypt("identical")
    assert a != b
    assert cipher.decrypt(a) == cipher.decrypt(b) == "identical"


def test_wrong_key_cannot_decrypt():
    token = PayloadCipher(generate_key()).encrypt("secret")
    with pytest.raises(InvalidTag):
        PayloadCipher(generate_key()).decrypt(token)


def test_tampered_ciphertext_is_rejected():
    cipher = PayloadCipher(generate_key())
    token = cipher.encrypt("integrity matters")
    # Flip a character in the middle of the token — GCM's auth tag must reject it
    # rather than return garbage.
    i = len(token) // 2
    flipped = token[:i] + ("A" if token[i] != "A" else "B") + token[i + 1 :]
    with pytest.raises(Exception):  # InvalidTag or base64/decoding error
        cipher.decrypt(flipped)


def test_key_length_validated():
    with pytest.raises(ValueError, match="256-bit"):
        PayloadCipher(b"too-short")


def test_generate_key_is_256_bit():
    assert len(generate_key()) == 32
    assert generate_key() != generate_key()
