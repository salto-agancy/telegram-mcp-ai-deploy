"""Envelope encryption for one Telegram slot and its unlock credentials."""

import hashlib
import os
import secrets
from base64 import urlsafe_b64decode, urlsafe_b64encode
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def encode(value):
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode(value):
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def seal(key, value, context):
    nonce = secrets.token_bytes(12)
    return nonce + AESGCM(key).encrypt(nonce, value, context.encode())


def unseal(key, value, context):
    return AESGCM(key).decrypt(value[:12], value[12:], context.encode())


def wrapping_key(credential):
    # Separate this derivation from the digest stored in SQLite.
    return hashlib.sha256(b"telegram-cloud/wrap/v1\0" + credential.encode()).digest()


def wrap(credential, key, context):
    return seal(wrapping_key(credential), key, context)


def unwrap(credential, wrapped, context):
    return unseal(wrapping_key(credential), wrapped, context)


class SessionFile:
    def __init__(self, path, slot):
        self.path = Path(path)
        self.temp = self.path.with_suffix(".tmp")
        self.context = f"telegram-cloud/session/v1/{slot}"

    def read(self, key):
        return unseal(key, self.path.read_bytes(), self.context)

    def write(self, key, state):
        blob = seal(key, state, self.context)
        fd = os.open(self.temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(blob)
            handle.flush()
            os.fsync(handle.fileno())
        self.temp.replace(self.path)

    def delete(self):
        self.temp.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)
