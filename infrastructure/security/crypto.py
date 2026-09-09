"""Authenticated symmetric encryption for secrets held at rest.

Broker access tokens are bearer credentials: anyone holding one can trade as
the user. They therefore never sit in a column in the clear, and they are never
returned by the API. This module is the only place that turns a secret into
bytes for storage and back again.

Two properties matter more than the choice of cipher:

**Rotation is possible.** Every ciphertext records the id of the key that
produced it. Retiring a key means adding a new one to the front of the key list
and leaving the old one available for decryption until the rows have been
re-encrypted; nothing has to be decrypted and re-encrypted at the moment of the
rotation.

**Ciphertexts are bound to their row.** The associated data passed to AES-GCM
names the owner, provider and field. A ciphertext lifted out of one user's row
and pasted into another's fails to authenticate rather than decrypting into a
credential the second user was never granted.
"""

from __future__ import annotations

import base64
import os
from collections.abc import Mapping
from dataclasses import dataclass

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

#: AES-256. A shorter key is a configuration error, not a weaker mode.
KEY_BYTES = 32
#: 96-bit nonces are the size AES-GCM is specified for (NIST SP 800-38D 8.2).
NONCE_BYTES = 12


class CryptoError(Exception):
    """Base class for failures in this module."""


class KeyUnavailable(CryptoError):
    """No usable encryption key is configured.

    Raised instead of falling back to plaintext storage. A deployment without a
    key cannot hold broker credentials; it must not hold them badly.
    """


class DecryptionFailed(CryptoError):
    """The ciphertext did not authenticate under the key and associated data.

    Either the row was tampered with, the key that produced it is no longer
    configured, or the ciphertext belongs to a different row.
    """


@dataclass(frozen=True, slots=True)
class Ciphertext:
    """A stored secret: which key made it, its nonce, and the sealed bytes."""

    key_id: str
    nonce: bytes
    payload: bytes


def parse_keys(spec: str) -> tuple[dict[str, bytes], str | None]:
    """Parse ``"id:base64key,id2:base64key2"`` into a key map and the active id.

    The **first** entry is the active key — the one new ciphertexts are written
    with. Later entries are kept only so that rows written before a rotation
    can still be read.
    """
    keys: dict[str, bytes] = {}
    active: str | None = None
    for raw in spec.split(","):
        entry = raw.strip()
        if not entry:
            continue
        key_id, separator, encoded = entry.partition(":")
        if not separator:
            raise KeyUnavailable(
                f"encryption key entries must be written 'key_id:base64key'; got {entry[:16]!r}"
            )
        key_id = key_id.strip()
        try:
            material = base64.b64decode(encoded.strip(), validate=True)
        except (ValueError, TypeError) as exc:
            raise KeyUnavailable(f"encryption key {key_id!r} is not valid base64") from exc
        if len(material) != KEY_BYTES:
            raise KeyUnavailable(
                f"encryption key {key_id!r} is {len(material)} bytes; AES-256 needs {KEY_BYTES}"
            )
        if key_id in keys:
            raise KeyUnavailable(f"encryption key id {key_id!r} is defined twice")
        keys[key_id] = material
        if active is None:
            active = key_id
    return keys, active


def generate_key_spec(key_id: str = "v1") -> str:
    """Produce a ready-to-paste key entry. Used by the setup documentation."""
    return f"{key_id}:{base64.b64encode(os.urandom(KEY_BYTES)).decode('ascii')}"


class SecretBox:
    """AES-256-GCM sealing with key rotation and row-bound associated data."""

    def __init__(self, keys: Mapping[str, bytes], active_key_id: str) -> None:
        if active_key_id not in keys:
            raise KeyUnavailable(f"active key id {active_key_id!r} is not among the loaded keys")
        self._keys = dict(keys)
        self._active_key_id = active_key_id

    @classmethod
    def from_spec(cls, spec: str) -> SecretBox:
        keys, active = parse_keys(spec)
        if active is None:
            raise KeyUnavailable(
                "no encryption key is configured; set QIP_CREDENTIAL_ENCRYPTION_KEYS"
            )
        return cls(keys, active)

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    def encrypt(self, plaintext: str, *, context: str) -> Ciphertext:
        nonce = os.urandom(NONCE_BYTES)
        sealed = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce, plaintext.encode("utf-8"), context.encode("utf-8")
        )
        return Ciphertext(key_id=self._active_key_id, nonce=nonce, payload=sealed)

    def decrypt(self, ciphertext: Ciphertext, *, context: str) -> str:
        key = self._keys.get(ciphertext.key_id)
        if key is None:
            raise DecryptionFailed(
                f"ciphertext was written with key {ciphertext.key_id!r}, which is no longer "
                "configured; restore it to QIP_CREDENTIAL_ENCRYPTION_KEYS to read this row"
            )
        try:
            opened = AESGCM(key).decrypt(
                ciphertext.nonce, ciphertext.payload, context.encode("utf-8")
            )
        except InvalidTag as exc:
            raise DecryptionFailed(
                "ciphertext failed authentication under its recorded key and context"
            ) from exc
        return opened.decode("utf-8")

    def needs_rewrap(self, ciphertext: Ciphertext) -> bool:
        """True when a row was written with a retired key and should be rewritten."""
        return ciphertext.key_id != self._active_key_id
