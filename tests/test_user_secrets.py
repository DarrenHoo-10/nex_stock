# -*- coding: utf-8 -*-
"""Per-user data keys and the server root key (docs/design/multi-user.md section 3.5)."""

import os
import stat
import sys

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import text

from src.config import Config
from src.repositories.user_repo import UserRepository
from src.services.user_settings_service import UserSettingError, UserSettingsService
from src.storage import DatabaseManager
from src.user_secrets import ROOT_KEY_FILENAME, UserSecretError, UserSecretStore, root_key_path


@pytest.fixture
def env(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "stock.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", "primary")
    monkeypatch.delenv("USER_SECRET_KEY", raising=False)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    DatabaseManager.get_instance()
    yield tmp_path
    DatabaseManager.reset_instance()
    Config.reset_instance()


def test_root_key_is_not_created_until_a_secret_is_written(env):
    store = UserSecretStore()

    assert not (env / ROOT_KEY_FILENAME).exists()
    with pytest.raises(UserSecretError):
        store.decrypt("alice", "gAAAA-not-a-token")
    assert not (env / ROOT_KEY_FILENAME).exists()

    store.encrypt("alice", "sk-alice")

    assert (env / ROOT_KEY_FILENAME).is_file()
    assert root_key_path() == (env / ROOT_KEY_FILENAME).resolve()
    Fernet((env / ROOT_KEY_FILENAME).read_bytes())  # a valid Fernet key


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_root_key_file_is_owner_only(env):
    UserSecretStore().encrypt("alice", "sk-alice")

    assert stat.S_IMODE(os.stat(env / ROOT_KEY_FILENAME).st_mode) == 0o600


def test_primary_user_settings_never_create_the_root_key(env):
    service = UserSettingsService()

    with pytest.raises(UserSettingError) as exc:
        service.set_setting("primary", "OPENAI_API_KEY", "sk-should-not-be-stored")

    assert exc.value.code == "primary_user_uses_env"
    assert not (env / ROOT_KEY_FILENAME).exists()
    assert UserRepository().list_settings("primary") == []


def test_round_trip_and_user_isolation(env):
    store = UserSecretStore()
    token_a = store.encrypt("alice", "sk-shared-looking-value")
    token_b = store.encrypt("bob", "sk-shared-looking-value")

    assert store.decrypt("alice", token_a) == "sk-shared-looking-value"
    assert store.decrypt("bob", token_b) == "sk-shared-looking-value"
    # Same plaintext, independent ciphertexts, and a user's key cannot open another user's value.
    assert token_a != token_b
    with pytest.raises(UserSecretError):
        store.decrypt("bob", token_a)
    with pytest.raises(UserSecretError):
        store.decrypt("alice", token_b)
    # Each user has their own wrapped data key.
    repo = UserRepository()
    assert repo.get_data_key("alice") != repo.get_data_key("bob")
    assert "sk-shared-looking-value" not in repo.get_data_key("alice")


def test_data_key_is_stable_per_user(env):
    store = UserSecretStore()
    first = store.encrypt("alice", "one")
    second = store.encrypt("alice", "two")

    assert store.decrypt("alice", first) == "one"
    assert store.decrypt("alice", second) == "two"
    assert UserRepository().get_data_key("alice") is not None


def test_corrupting_one_users_data_key_does_not_affect_another(env):
    store = UserSecretStore()
    token_a = store.encrypt("alice", "sk-alice")
    token_b = store.encrypt("bob", "sk-bob")

    UserRepository().db._run_write_transaction(
        "corrupt",
        lambda s: s.execute(text("UPDATE user_data_key SET wrapped_key = 'garbage' WHERE user_id = 'alice'")),
    )

    with pytest.raises(UserSecretError):
        store.decrypt("alice", token_a)
    with pytest.raises(UserSecretError):
        store.encrypt("alice", "sk-new")  # never silently replaces a damaged key
    assert store.decrypt("bob", token_b) == "sk-bob"
    assert store.decrypt("bob", store.encrypt("bob", "sk-bob-2")) == "sk-bob-2"


def test_destroying_one_users_data_key_does_not_affect_another(env):
    store = UserSecretStore()
    token_a = store.encrypt("alice", "sk-alice")
    token_b = store.encrypt("bob", "sk-bob")

    assert store.destroy_user_key("alice") is True

    with pytest.raises(UserSecretError):
        store.decrypt("alice", token_a)
    assert store.decrypt("bob", token_b) == "sk-bob"
    # Alice can start over: a fresh data key is created, old ciphertexts stay unreadable.
    new_token = store.encrypt("alice", "sk-alice-2")
    assert store.decrypt("alice", new_token) == "sk-alice-2"
    with pytest.raises(UserSecretError):
        store.decrypt("alice", token_a)


def test_unreadable_secret_is_skipped_for_that_user_only(env):
    service = UserSettingsService()
    service.set_setting("alice", "OPENAI_API_KEY", "sk-alice")
    service.set_setting("alice", "REPORT_LANGUAGE", "en")
    service.set_setting("bob", "OPENAI_API_KEY", "sk-bob")
    UserSecretStore().destroy_user_key("alice")

    assert service.get_plain_values("alice") == {"REPORT_LANGUAGE": "en"}
    assert service.get_plain_values("bob") == {"OPENAI_API_KEY": "sk-bob"}


def test_stored_secret_value_is_ciphertext_and_masked_in_listings(env):
    service = UserSettingsService()
    service.set_setting("alice", "OPENAI_API_KEY", "sk-plain-123")

    stored = UserRepository().get_setting("alice", "OPENAI_API_KEY")
    assert stored["is_secret"] is True
    assert "sk-plain-123" not in stored["value"]
    listed = service.list_settings("alice")
    assert listed[0]["value"] == "******"
    assert service.get_plain_values("alice") == {"OPENAI_API_KEY": "sk-plain-123"}
    # A masked value from the UI keeps the stored secret.
    service.set_setting("alice", "OPENAI_API_KEY", "******")
    assert service.get_plain_values("alice") == {"OPENAI_API_KEY": "sk-plain-123"}


def test_env_override_replaces_the_root_key_file(env, monkeypatch):
    monkeypatch.setenv("USER_SECRET_KEY", "a-long-random-string-from-the-operator")
    store = UserSecretStore()
    token = store.encrypt("alice", "sk-alice")

    assert not (env / ROOT_KEY_FILENAME).exists()
    assert UserSecretStore().decrypt("alice", token) == "sk-alice"
    # A different override cannot unwrap the stored data key.
    monkeypatch.setenv("USER_SECRET_KEY", "another-string")
    with pytest.raises(UserSecretError):
        UserSecretStore().decrypt("alice", token)


def test_a_fernet_key_is_accepted_as_the_env_override(env, monkeypatch):
    monkeypatch.setenv("USER_SECRET_KEY", Fernet.generate_key().decode())
    store = UserSecretStore()

    assert store.decrypt("alice", store.encrypt("alice", "sk-alice")) == "sk-alice"


def test_missing_or_damaged_root_key_file_is_an_error_not_a_silent_regeneration(env):
    store = UserSecretStore()
    token = store.encrypt("alice", "sk-alice")
    key_file = env / ROOT_KEY_FILENAME

    key_file.write_bytes(b"not-a-fernet-key")
    with pytest.raises(UserSecretError):
        UserSecretStore().decrypt("alice", token)
    assert key_file.read_bytes() == b"not-a-fernet-key"

    key_file.unlink()
    with pytest.raises(UserSecretError):
        UserSecretStore().decrypt("alice", token)
    assert not key_file.exists()  # decrypting never creates a replacement


def test_errors_and_logs_do_not_contain_plaintext(env, caplog):
    service = UserSettingsService()
    with pytest.raises(UserSettingError) as exc:
        service.set_setting("alice", "ANTHROPIC_MAX_TOKENS", "sk-looks-secret-but-not-an-int")
    assert "sk-looks-secret" not in str(exc.value)

    with pytest.raises(UserSettingError) as exc:
        service.set_setting("alice", "OPENAI_API_KEY", "bad\nsk-secret-with-newline")
    assert "sk-secret-with-newline" not in repr(exc.value.issues)
    assert "sk-secret-with-newline" not in str(exc.value)

    with caplog.at_level("DEBUG"):
        service.set_setting("alice", "OPENAI_API_KEY", "sk-never-logged")
        UserSecretStore().destroy_user_key("alice")
        service.get_plain_values("alice")
    assert "sk-never-logged" not in caplog.text
