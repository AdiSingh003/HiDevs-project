"""Ed25519 signing for contracts (one keypair per signer, persisted as PKCS#8 PEM)."""

from __future__ import annotations

import base64
import hashlib
import re
import threading
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def _slug(signer_id: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", signer_id).strip("_")[:120]


class KeyStore:
    def __init__(self, directory: Path | None = None):
        self.directory = directory
        self._keys: dict[str, Ed25519PrivateKey] = {}
        self._lock = threading.Lock()

    def _key(self, signer_id: str) -> Ed25519PrivateKey:
        with self._lock:
            if signer_id in self._keys:
                return self._keys[signer_id]
            key: Ed25519PrivateKey | None = None
            path = self.directory / f"{_slug(signer_id)}.pem" if self.directory else None
            if path is not None and path.exists():
                loaded = serialization.load_pem_private_key(path.read_bytes(), password=None)
                if isinstance(loaded, Ed25519PrivateKey):
                    key = loaded
            if key is None:
                key = Ed25519PrivateKey.generate()
                if path is not None:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                       serialization.NoEncryption()))
            self._keys[signer_id] = key
            return key

    def public_key_b64(self, signer_id: str) -> str:
        raw = self._key(signer_id).public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.b64encode(raw).decode()

    def fingerprint(self, signer_id: str) -> str:
        return fingerprint_of(self.public_key_b64(signer_id))

    def sign(self, signer_id: str, message: bytes) -> str:
        return base64.b64encode(self._key(signer_id).sign(message)).decode()


def fingerprint_of(public_key_b64: str) -> str:
    return hashlib.sha256(base64.b64decode(public_key_b64)).hexdigest()[:16]


def verify_signature(public_key_b64: str, message: bytes, signature_b64: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64)).verify(base64.b64decode(signature_b64),
                                                                                   message)
        return True
    except (InvalidSignature, ValueError):
        return False
