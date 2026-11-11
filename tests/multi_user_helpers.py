# -*- coding: utf-8 -*-
"""Shared helpers for multi-user P2a isolation tests (no network, no LLM).

``multi_user_app`` boots the real ``create_app`` over a temp SQLite database with a primary user
and lets a test switch the acting user per request by flipping ``switch.user_id``; the real
``get_current_user`` is replaced through ``dependency_overrides`` so route wiring, dependencies and
services all run for real.
"""

from __future__ import annotations

import importlib
import os
import sys
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Optional

import pytest
from fastapi import routing
from fastapi.testclient import TestClient

import src.auth as auth
from api.app import create_app
from api.deps import CurrentUser, get_current_user
from src.config import Config
from src.core.config_registry import SCOPE_USER, get_config_scope
from src.storage import DatabaseManager


def effective_api_routes(app):
    """Yield mounted paths and their dependency graphs for eager and lazy FastAPI routers."""
    contexts = getattr(routing, "iter_route_contexts", None)
    if contexts is None:
        for route in app.routes:
            if isinstance(route, routing.APIRoute):
                yield route.path, route.methods, route.dependant
        return
    for context in contexts(app.routes):
        if isinstance(context.original_route, routing.APIRoute):
            effective = getattr(context, "_effective_route", context.original_route)
            yield context.path, context.methods, effective.dependant

PRIMARY_ID = "uid-primary"
USER_A = "uid-user-a"
USER_B = "uid-user-b"


def make_user(user_id: str, *, is_admin: bool = False) -> CurrentUser:
    return CurrentUser(
        user_id=user_id,
        handle=user_id,
        display_name=user_id,
        role="admin" if is_admin else "user",
        is_admin=is_admin,
    )


class UserSwitch:
    """Callable dependency override: returns whichever user the test selected last."""

    def __init__(self, user_id: str = PRIMARY_ID):
        self.user_id = user_id

    def __call__(self) -> CurrentUser:
        return make_user(self.user_id, is_admin=self.user_id == PRIMARY_ID)


@dataclass
class MultiUserApp:
    client: TestClient
    switch: UserSwitch
    db: DatabaseManager
    env_file: str

    def as_user(self, user_id: str) -> TestClient:
        self.switch.user_id = user_id
        return self.client


# Loading the config runs load_dotenv on the test's .env file, which writes straight into os.environ
# behind monkeypatch's back (e.g. a test appending REPORT_LANGUAGE=en would leak it into later test
# modules). prepare_multi_user_env snapshots os.environ and teardown_multi_user_env restores it.
_ENV_SNAPSHOTS: list = []


def _restore_environ(snapshot: dict) -> None:
    for key in [k for k in os.environ if k not in snapshot]:
        del os.environ[key]
    for key, value in snapshot.items():
        if os.environ.get(key) != value:
            os.environ[key] = value


_REAL_LITELLM_MODULES: dict = {}


def _is_real_litellm(module) -> bool:
    return (
        module is not None
        and not getattr(module, "__dsa_test_stub__", False)
        and hasattr(module, "acompletion")
        and not type(module).__module__.startswith("unittest.mock")
    )


def _load_real_litellm_modules() -> dict:
    """Import the real LiteLLM once (even while a stub sits in sys.modules) and remember all its modules."""
    if not _REAL_LITELLM_MODULES:
        names = [n for n in list(sys.modules) if n == "litellm" or n.startswith("litellm.")]
        if _is_real_litellm(sys.modules.get("litellm")):
            _REAL_LITELLM_MODULES.update({n: sys.modules[n] for n in names})
        else:
            saved = {n: sys.modules.pop(n) for n in names}
            try:
                importlib.import_module("litellm")
                _REAL_LITELLM_MODULES.update(
                    {n: m for n, m in sys.modules.items() if n == "litellm" or n.startswith("litellm.")}
                )
            finally:
                for n in [n for n in list(sys.modules) if n == "litellm" or n.startswith("litellm.")]:
                    del sys.modules[n]
                sys.modules.update(saved)
    return _REAL_LITELLM_MODULES


def use_real_litellm(monkeypatch):
    """Make the real LiteLLM module the one every model call site sees, for this test only.

    Earlier test modules may have installed ``tests/litellm_stub`` (no ``acompletion``, dummy Router) or a
    MagicMock as ``sys.modules["litellm"]``; ``src.analyzer`` / ``src.agent.llm_adapter`` /
    ``src.services.image_stock_extractor`` bind whatever was there at import time. Patching ``"litellm.X"``
    then patches a different object than the code calls, so boundary assertions fail only in full runs.
    The real package is imported once and cached; each test swaps it in through ``monkeypatch``.
    """
    modules = _load_real_litellm_modules()
    real = modules["litellm"]
    if sys.modules.get("litellm") is not real:
        for name in [n for n in list(sys.modules) if (n == "litellm" or n.startswith("litellm.")) and n not in modules]:
            monkeypatch.delitem(sys.modules, name)
        for name, module in modules.items():
            monkeypatch.setitem(sys.modules, name, module)
    for module_name in ("src.analyzer", "src.agent.llm_adapter", "src.services.image_stock_extractor"):
        module = importlib.import_module(module_name)
        if getattr(module, "litellm", None) is not real:
            monkeypatch.setattr(module, "litellm", real, raising=False)
    return real


def prepare_multi_user_env(tmp_path, monkeypatch, *, stock_list: str = "") -> str:
    """Point config/database at ``tmp_path`` with PRIMARY_USER_ID=uid-primary; returns the env file path."""
    _ENV_SNAPSHOTS.append(dict(os.environ))
    env_file = tmp_path / ".env"
    env_file.write_text(f"STOCK_LIST={stock_list}\nADMIN_AUTH_ENABLED=false\n", encoding="utf-8")
    monkeypatch.setenv("ENV_FILE", str(env_file))
    monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "multi_user.db"))
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY_ID)
    monkeypatch.delenv("AUTH_MODE", raising=False)
    monkeypatch.delenv("STOCK_LIST", raising=False)
    for key in list(os.environ):
        if get_config_scope(key) == SCOPE_USER and key not in {"PRIMARY_USER_ID"}:
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(auth, "_is_auth_enabled_from_env", lambda: False)
    Config.reset_instance()
    DatabaseManager.reset_instance()
    auth.refresh_auth_state()
    return str(env_file)


def build_multi_user_app(tmp_path, monkeypatch, *, stock_list: str = "") -> MultiUserApp:
    env_file = prepare_multi_user_env(tmp_path, monkeypatch, stock_list=stock_list)
    db = DatabaseManager.get_instance()
    app = create_app(static_dir=tmp_path / "static")
    switch = UserSwitch(PRIMARY_ID)
    app.dependency_overrides[get_current_user] = switch
    return MultiUserApp(
        client=TestClient(app, base_url="http://testserver"), switch=switch, db=db, env_file=env_file,
    )


def teardown_multi_user_env() -> None:
    auth.refresh_auth_state()
    DatabaseManager.reset_instance()
    Config.reset_instance()
    if _ENV_SNAPSHOTS:
        _restore_environ(_ENV_SNAPSHOTS.pop())


@pytest.fixture
def multi_user_app(tmp_path, monkeypatch):
    app = build_multi_user_app(tmp_path, monkeypatch)
    yield app
    teardown_multi_user_env()


PUBLIC_ADDRESS = "93.184.216.34"


def stub_dns(monkeypatch, mapping=None, default=PUBLIC_ADDRESS):
    """Make SSRF-guard DNS lookups offline: ``mapping`` host -> address(es), every other host -> a public address.

    Returns the list of looked-up hosts so a test can assert the call-time re-check really resolved again.
    """
    lookups = []
    table = dict(mapping or {})

    def resolver(host, port):
        lookups.append(host)
        answer = table.get(host, default)
        return [answer] if isinstance(answer, str) else list(answer)

    monkeypatch.setattr("src.utils.url_safety._default_resolver", resolver)
    return lookups


def give_user_model(user_id: str, *, key: str = None, model: str = "mine-model", base_url: str = "https://llm.mine.example/v1") -> dict:
    """Store a complete personal model channel for ``user_id`` (call ``stub_dns`` first: the base URL is checked).

    Returns the plaintext key/base/model so a test can assert LiteLLM receives exactly these.
    """
    from src.services.user_settings_service import UserSettingsService

    api_key = key or f"sk-{user_id}-own-key"
    UserSettingsService().set_settings(user_id, {
        "LLM_CHANNELS": "mine",
        "LLM_MINE_PROTOCOL": "openai",
        "LLM_MINE_BASE_URL": base_url,
        "LLM_MINE_API_KEY": api_key,
        "LLM_MINE_MODELS": model,
    })
    return {"api_key": api_key, "api_base": base_url, "model": f"openai/{model}"}


SYSTEM_PROVIDER_ENV = {
    "OPENAI_API_KEY": "sk-SYSTEM-openai-key",
    "GEMINI_API_KEY": "SYSTEM-gemini-key",
    "ANTHROPIC_API_KEY": "sk-SYSTEM-anthropic-key",
    "DEEPSEEK_API_KEY": "sk-SYSTEM-deepseek-key",
    "OPENROUTER_API_KEY": "sk-SYSTEM-openrouter-key",
    "GROQ_API_KEY": "SYSTEM-groq-key",
    "XAI_API_KEY": "SYSTEM-xai-key",
    "DASHSCOPE_API_KEY": "SYSTEM-dashscope-key",
    "LLM_CHANNELS": "sysch",
    "LLM_SYSCH_PROTOCOL": "openai",
    "LLM_SYSCH_BASE_URL": "https://sys.llm.example/v1",
    "LLM_SYSCH_API_KEY": "sk-SYSTEM-channel-key",
    "LLM_SYSCH_MODELS": "sys-model",
    "LITELLM_MODEL": "openai/sys-model",
    "TAVILY_API_KEYS": "tvly-SYSTEM-search-key",
}


def set_system_provider_env(monkeypatch) -> None:
    """Fill the process environment with provider keys, channels and search keys the way a real server has them."""
    from src.config import Config

    for name, value in SYSTEM_PROVIDER_ENV.items():
        monkeypatch.setenv(name, value)
    Config.reset_instance()
