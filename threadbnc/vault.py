"""Encryption at rest for account session tokens.

Passwords are never stored: logging in exchanges them for a server-issued
token, and only that token is kept, encrypted with Fernet (AES-128-CBC + HMAC).
The key comes from THREADBNC_CREDENTIALS_KEY, or is generated once and kept in
the data directory -- deliberately *not* in the database, so a database dump or
backup alone can't be used to post as you.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class VaultError(Exception):
    """A stored token can't be decrypted (key changed or data corrupted)."""


def _load_key(configured: str | None, data_dir: Path) -> bytes:
    if configured:
        # Accept any passphrase; derive a proper 32-byte Fernet key from it.
        return base64.urlsafe_b64encode(hashlib.sha256(configured.encode()).digest())
    path = data_dir / "credentials_key"
    if path.exists():
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    path.write_bytes(key)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return key


class TokenVault:
    def __init__(self, configured_key: str | None, data_dir: Path):
        self._fernet = Fernet(_load_key(configured_key, data_dir))

    def encrypt(self, token: str) -> str:
        return self._fernet.encrypt(token.encode()).decode()

    def decrypt(self, stored: str) -> str:
        try:
            return self._fernet.decrypt(stored.encode()).decode()
        except InvalidToken as exc:
            raise VaultError("stored token can't be decrypted with the current credentials key") from exc
