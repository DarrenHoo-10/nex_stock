# -*- coding: utf-8 -*-
"""
Per-user secret encryption (docs/design/multi-user.md section 3.5).

* Every user has an independent random data key (``user_data_key``), created the first
  time a secret is saved for that user; it only encrypts that user's secret settings.
  A broken or destroyed data key affects that user alone.
* Data keys are wrapped with one server-side root key. The root key is generated on
  first need and stored as ``user_secret.key`` (chmod 600 where supported) in the same
  directory as the database; the optional ``USER_SECRET_KEY`` environment variable
  overrides it. ``local`` single-user deployments never create it: it is only needed
  when a non-primary user's secret is actually written.
* Plaintext secrets, keys and tokens are never logged or put into error messages.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken

from src.repositories.user_repo import UserRepository

logger = logging.getLogger(__name__)

ROOT_KEY_FILENAME = "user_secret.key"
USER_SECRET_KEY_ENV = "USER_SECRET_KEY"

_root_key_lock = threading.Lock()


class UserSecretError(Exception):
    """A secret cannot be encrypted or decrypted (missing / unreadable key); never carries plaintext."""


def root_key_path() -> Path:
    """Root key file: next to the database file (``DATABASE_PATH``)."""
    db_path = os.getenv("DATABASE_PATH", "./data/stock_analysis.db")
    return Path(db_path).resolve().parent / ROOT_KEY_FILENAME


def _fernet_from_key_bytes(key: bytes) -> Fernet:
    try:
        return Fernet(key)
    except (ValueError, TypeError) as exc:
        raise UserSecretError("The user secret root key is malformed") from exc


def _fernet_from_env_value(value: str) -> Fernet:
    """``USER_SECRET_KEY`` may be a Fernet key or any high-entropy string (hashed to one)."""
    raw = value.strip().encode("utf-8")
    try:
        return Fernet(raw)
    except (ValueError, TypeError):
        derived = base64.urlsafe_b64encode(hashlib.sha256(raw).digest())
        return Fernet(derived)


def _read_root_key_file(path: Path) -> Optional[bytes]:
    try:
        data = path.read_bytes().strip()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UserSecretError("The user secret root key file cannot be read") from exc
    return data


def _create_root_key_file(path: Path) -> bytes:
    """Create the root key file atomically; a concurrent creator wins and its key is read back."""
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    try:
        fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        for _ in range(20):
            existing = _read_root_key_file(path)
            if existing:
                return existing
            time.sleep(0.05)
        raise UserSecretError("The user secret root key file exists but is empty")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(key)
    except OSError as exc:
        raise UserSecretError("The user secret root key file cannot be written") from exc
    try:
        os.chmod(str(path), 0o600)
    except OSError:  # pragma: no cover - platforms without POSIX modes
        pass
    logger.info("Generated user secret root key file %s", path)
    return key


class UserSecretStore:
    """Encrypt / decrypt a user's secret values with that user's own data key."""

    def __init__(self, repo: Optional[UserRepository] = None):
        self._repo = repo or UserRepository()
        self._root_cache: Optional[Tuple[Tuple[str, str], Fernet]] = None

    # ------------------------------------------------------------------ root key
    def _root_fernet(self, *, create: bool) -> Fernet:
        env_value = (os.getenv(USER_SECRET_KEY_ENV) or "").strip()
        path = root_key_path()
        signature = (env_value, str(path))
        cached = self._root_cache
        if cached is not None and cached[0] == signature:
            return cached[1]

        if env_value:
            fernet = _fernet_from_env_value(env_value)
        else:
            with _root_key_lock:
                data = _read_root_key_file(path)
                if data is None:
                    if not create:
                        raise UserSecretError("The user secret root key is missing")
                    data = _create_root_key_file(path)
            if not data:
                raise UserSecretError("The user secret root key file is empty")
            fernet = _fernet_from_key_bytes(data)
        self._root_cache = (signature, fernet)
        return fernet

    # ------------------------------------------------------------------ data keys
    def _data_fernet(self, user_id: str, *, create: bool) -> Fernet:
        wrapped = self._repo.get_data_key(user_id)
        if wrapped is None:
            if not create:
                raise UserSecretError("No data key exists for this user")
            root = self._root_fernet(create=True)
            candidate = root.encrypt(Fernet.generate_key()).decode("ascii")
            wrapped = self._repo.create_data_key(user_id, candidate)
            logger.info("Created a secret data key for user %s", user_id)
        root = self._root_fernet(create=False)
        try:
            return Fernet(root.decrypt(wrapped.encode("ascii")))
        except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
            raise UserSecretError("This user's data key cannot be unwrapped") from exc

    def encrypt(self, user_id: str, plaintext: str) -> str:
        """Encrypt for ``user_id``; creates the user's data key (and the root key) when needed."""
        return self._data_fernet(user_id, create=True).encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, user_id: str, token: str) -> str:
        """Decrypt a value stored for ``user_id``; never creates keys."""
        fernet = self._data_fernet(user_id, create=False)
        try:
            return fernet.decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError, TypeError, UnicodeError) as exc:
            raise UserSecretError("A stored secret cannot be decrypted") from exc

    def destroy_user_key(self, user_id: str) -> bool:
        """Destroy the user's data key: all of that user's stored secrets become unreadable."""
        destroyed = self._repo.delete_data_key(user_id)
        if destroyed:
            logger.info("Destroyed the secret data key of user %s", user_id)
        return destroyed
