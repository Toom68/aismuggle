"""Shared AES-GCM encryption helpers for aisearch client and server.

Wire format for an encrypted blob (a urlsafe-base64 string):
    base64( 12-byte nonce || AES-GCM ciphertext || 16-byte tag )

The key is a 32-byte secret shared between client and server, provided via the
AISEARCH_KEY environment variable as a urlsafe-base64 string. Generate one with:

    python client.py keygen
"""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

NONCE_LEN = 12


def generate_key() -> str:
    """Return a fresh urlsafe-base64-encoded 32-byte key."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def load_key(env: str | None = None) -> bytes:
    """Load the 32-byte key from the AISEARCH_KEY env var (urlsafe base64)."""
    raw = env if env is not None else os.environ.get("AISEARCH_KEY")
    if not raw:
        raise RuntimeError("AISEARCH_KEY is not set (generate one with `client.py keygen`)")
    pad = "=" * (-len(raw) % 4)
    key = base64.urlsafe_b64decode(raw + pad)
    if len(key) != 32:
        raise RuntimeError(f"AISEARCH_KEY must decode to 32 bytes, got {len(key)}")
    return key


def encrypt(key: bytes, plaintext: bytes, aad: bytes | None = None) -> str:
    nonce = os.urandom(NONCE_LEN)
    ct = AESGCM(key).encrypt(nonce, plaintext, aad)
    return base64.urlsafe_b64encode(nonce + ct).decode("ascii").rstrip("=")


def decrypt(key: bytes, blob: str, aad: bytes | None = None) -> bytes:
    pad = "=" * (-len(blob) % 4)
    raw = base64.urlsafe_b64decode(blob + pad)
    nonce, ct = raw[:NONCE_LEN], raw[NONCE_LEN:]
    return AESGCM(key).decrypt(nonce, ct, aad)


def passphrase_to_key(passphrase: str) -> str:
    """Convenience: derive a base64 key from a passphrase (SHA-256)."""
    return base64.urlsafe_b64encode(hashlib.sha256(passphrase.encode()).digest()).decode().rstrip("=")
