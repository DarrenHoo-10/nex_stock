# -*- coding: utf-8 -*-
"""Multi-user P2b-3 security fixes (docs/design/multi-user.md section 18): no network, no real model.

1. a non-primary user's model route must be an allowed one (a fixed provider allowlist on its public default
   endpoint, or an own public https endpoint): ``ollama/``, ``sagemaker/``, ``bedrock/`` ... are refused at save time
   and at call time, and no allowlisted route can be bent to the server's environment;
2. user-supplied URLs (model Base URL, SearXNG, Hindsight) never follow a redirect;
3. backtests / skill samples never contain a private (position-aware) report;
4. the system Hindsight URL typed by a user never gives access to another user's bank;
5. ``/me/settings`` never returns an inherited system value;
6. a user's analysis never writes the shared market review;
7. system third-party paid keys (social sentiment, FXMacroData) are not used by user analyses.

Real LiteLLM / httpx / requests code runs for the wire-level tests; only the transport underneath is replaced.
"""

from __future__ import annotations

import copy
import functools
import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import httpx
import pytest
import requests

from src.llm.credentials_guard import (
    KEYLESS_PROVIDERS,
    USER_OWN_ENDPOINT_PROVIDERS,
    USER_PROVIDER_DEFAULT_BASES,
    ModelKeyRequiredError,
    ModelRouteNotAllowedError,
    check_user_model_route,
    harden_user_params,
    user_call_scope,
    wire_provider,
)
from src.services.user_config_service import get_user_config, invalidate_user_config, require_usable_model
from src.services.user_settings_service import UserSettingError, UserSettingsService, is_secret_setting_key
from src.utils.url_safety import UnsafeUrlError, forbid_httpx_redirects
from tests.multi_user_helpers import (
    use_real_litellm,
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    set_system_provider_env,
    stub_dns,
    teardown_multi_user_env,
)

INTERNAL_TARGETS = ("http://169.254.169.254/latest/meta-data/", "https://127.0.0.1:8000/api/v1/admin")


def _response(text="OK"):
    message = SimpleNamespace(content=text, tool_calls=None, reasoning_content=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model="openai/mine-model",
    )


class Boundary:
    """Everything that would reach LiteLLM (module level), recorded."""

    def __init__(self):
        self.completion_calls = []
        self.router_model_lists = []
        self.router_calls = []

    @property
    def touched(self) -> bool:
        return bool(self.completion_calls or self.router_model_lists or self.router_calls)

    def completion(self, **kwargs):
        self.completion_calls.append(kwargs)
        return _response()

    def router_class(self):
        boundary = self

        class FakeRouter:
            def __init__(self, model_list=None, **kwargs):
                boundary.router_model_lists.append(model_list)

            def completion(self, **kwargs):
                boundary.router_calls.append(kwargs)
                return _response()

        return FakeRouter


@pytest.fixture(autouse=True)
def _real_litellm_for_every_test(monkeypatch):
    """The allowlist and wire tests check the installed LiteLLM; never let an earlier module's stub skip them."""
    use_real_litellm(monkeypatch)


@pytest.fixture
def real_stack(tmp_path, monkeypatch):
    """Two users over a temp database, the system environment full of provider keys; LiteLLM itself is NOT patched."""
    use_real_litellm(monkeypatch)
    app = build_multi_user_app(tmp_path, monkeypatch)
    set_system_provider_env(monkeypatch)
    stub_dns(monkeypatch)
    app.env_path = Path(app.env_file)
    yield app
    teardown_multi_user_env()


@pytest.fixture
def mu(real_stack, monkeypatch):
    """``real_stack`` with everything that would reach LiteLLM (completion / Router) replaced by a recorder."""
    app = real_stack
    boundary = Boundary()
    router = boundary.router_class()
    monkeypatch.setattr("litellm.completion", boundary.completion)
    monkeypatch.setattr("litellm.Router", router)
    monkeypatch.setattr("src.analyzer.Router", router)
    monkeypatch.setattr("src.agent.llm_adapter.Router", router)
    app.boundary = boundary
    return app


def force_user_settings(user_id: str, values: dict) -> None:
    """Store settings the way a database written before the route policy existed would hold them (no validation)."""
    from src.repositories.user_repo import UserRepository

    service = UserSettingsService()
    repo = UserRepository()
    for key, value in values.items():
        secret = is_secret_setting_key(key)
        repo.upsert_setting(user_id, key, service._secrets.encrypt(user_id, value) if secret else value, secret)
    invalidate_user_config(user_id)


def channel_settings(model: str, *, base_url: str = "https://llm.mine.example/v1", key: str = "sk-mine-key",
                     protocol: str = "openai") -> dict:
    values = {
        "LLM_CHANNELS": "mine", "LLM_MINE_PROTOCOL": protocol, "LLM_MINE_API_KEY": key, "LLM_MINE_MODELS": model,
    }
    if base_url:
        values["LLM_MINE_BASE_URL"] = base_url
    return values


# ======================================================================= 1. provider-prefixed models: save time

# Providers that authenticate with the server's ambient credentials or talk to a local default: refused even with a
# public https Base URL and an API key.
AMBIENT_OR_LOCAL = [
    "sagemaker/my-endpoint",
    "sagemaker_chat/my-endpoint",
    "bedrock/anthropic.claude-3-sonnet-20240229-v1:0",
    "bedrock/converse/anthropic.claude-3-haiku",
    "vertex_ai/gemini-1.5-pro",
    "vertex_ai/claude-3-sonnet",
    "azure/my-deployment",
    "azure_ai/mistral-large",
    "watsonx/ibm/granite-13b-chat-v2",
    "vllm/facebook/opt-125m",
    "llamafile/mistral",
]


@pytest.mark.parametrize("model", AMBIENT_OR_LOCAL)
@pytest.mark.parametrize("with_base_url", [True, False])
def test_ambient_credential_and_local_default_providers_are_rejected_at_save(mu, model, with_base_url):
    values = channel_settings(model, base_url="https://llm.mine.example/v1" if with_base_url else "")

    with pytest.raises(UserSettingError) as caught:
        UserSettingsService().set_settings(USER_B, values)

    assert caught.value.code == "model_route_not_allowed"
    assert UserSettingsService().get_plain_values(USER_B) == {}  # nothing stored


@pytest.mark.parametrize("model", [
    "ollama/llama3", "ollama_chat/qwen2", "hosted_vllm/llama", "lm_studio/qwen", "litellm_proxy/gpt-4o",
])
def test_own_endpoint_providers_need_a_public_https_base_url_at_save(mu, model):
    with pytest.raises(UserSettingError) as caught:  # no Base URL: LiteLLM would use localhost / the server's variable
        UserSettingsService().set_settings(USER_B, channel_settings(model, base_url=""))
    assert caught.value.code == "model_route_not_allowed"

    for private in ("https://10.0.0.5/v1", "https://127.0.0.1:11434", "http://localhost:11434", "https://[::1]/v1"):
        with pytest.raises(UserSettingError) as caught:
            UserSettingsService().set_settings(USER_B, channel_settings(model, base_url=private))
        assert caught.value.code == "unsafe_url", private
    assert UserSettingsService().get_plain_values(USER_B) == {}

    UserSettingsService().set_settings(USER_B, channel_settings(model, base_url="https://models.mine.example/v1"))
    assert get_user_config(USER_B).llm_model_list  # explicit public https endpoint: allowed


def test_route_policy_applies_to_every_model_name_a_user_can_set(mu):
    """The default / fallback / light / Agent / vision model names decide which provider a call is sent to."""
    settings = UserSettingsService()
    for key, model in (
        ("LITELLM_MODEL", "bedrock/anthropic.claude-v2"),
        ("LITELLM_FALLBACK_MODELS", "openai/gpt-4o,bedrock/anthropic.claude-v2"),
        ("LITELLM_LIGHT_MODEL", "sagemaker/endpoint"),
        ("AGENT_LITELLM_MODEL", "vertex_ai/gemini-1.5-pro"),
        ("OPENAI_VISION_MODEL", "azure/gpt-4o"),
        ("LITELLM_MODEL", "gemini-pro"),  # a bare name LiteLLM routes to vertex_ai (ambient Google credentials)
    ):
        with pytest.raises(UserSettingError) as caught:
            settings.set_settings(USER_B, {key: model})
        assert caught.value.code == "model_route_not_allowed", key
    assert settings.get_plain_values(USER_B) == {}

    # a channel's models and the names that refer to them are validated as a whole: a clean batch is accepted
    settings.set_settings(USER_B, {**channel_settings("gpt-4o"), "LITELLM_MODEL": "openai/gpt-4o"})


def test_unrelated_settings_are_not_blocked_by_the_route_policy(mu):
    UserSettingsService().set_settings(USER_B, {"REPORT_LANGUAGE": "en", "TAVILY_API_KEYS": "tvly-bob"})
    assert UserSettingsService().get_plain_values(USER_B)["REPORT_LANGUAGE"] == "en"


def test_settings_api_reports_the_rejected_route_with_a_stable_code(mu):
    items = [{"key": key, "value": value} for key, value in channel_settings("bedrock/anthropic.claude-v2").items()]

    response = mu.as_user(USER_B).put("/api/v1/me/settings", json={"items": items})

    assert response.status_code == 400 and response.json()["error"] == "model_route_not_allowed", response.text
    assert "sk-mine-key" not in response.text


@pytest.mark.parametrize("provider", sorted(USER_PROVIDER_DEFAULT_BASES))
def test_allowlisted_provider_with_a_key_is_allowed_without_a_base_url(mu, provider):
    UserSettingsService().set_settings(USER_B, channel_settings(f"{provider}/some-model", base_url="", protocol="openai"))

    deployments = get_user_config(USER_B).llm_model_list
    assert deployments and deployments[0]["litellm_params"]["model"].startswith(f"{provider}/")
    assert check_user_model_route(f"{provider}/some-model", "", "sk-x") == provider


def test_a_keyless_allowlisted_route_is_refused(mu):
    for provider in ("openai", "anthropic", "deepseek"):
        with pytest.raises(ModelKeyRequiredError):
            check_user_model_route(f"{provider}/m", "", "")


def test_only_ollama_may_run_without_a_key_and_then_only_on_its_own_endpoint(mu):
    assert KEYLESS_PROVIDERS <= USER_OWN_ENDPOINT_PROVIDERS
    assert check_user_model_route("ollama/llama3", "https://ollama.mine.example", "") == "ollama"
    for provider in sorted(USER_OWN_ENDPOINT_PROVIDERS - KEYLESS_PROVIDERS):
        with pytest.raises(ModelKeyRequiredError):
            check_user_model_route(f"{provider}/m", "https://models.mine.example/v1", "")
    with pytest.raises(ModelRouteNotAllowedError):
        check_user_model_route("ollama/llama3", "", "")


def test_the_allowlist_does_not_overlap_with_the_own_endpoint_set_and_names_real_providers():
    from src.config import get_litellm_model_providers

    assert not set(USER_PROVIDER_DEFAULT_BASES) & set(USER_OWN_ENDPOINT_PROVIDERS)
    known = get_litellm_model_providers()
    assert set(USER_PROVIDER_DEFAULT_BASES) | set(USER_OWN_ENDPOINT_PROVIDERS) <= set(known)
    for base in USER_PROVIDER_DEFAULT_BASES.values():
        assert base.startswith("https://")


def test_wire_provider_resolves_bare_names_like_litellm():
    assert wire_provider("ollama/llama3") == "ollama"
    assert wire_provider("__legacy_openai__") == "openai"
    assert wire_provider("") == ""
    assert wire_provider("gpt-4o") == "openai"
    # a bare name LiteLLM cannot place (or places on an ambient-credential provider) is never an allowed route
    assert wire_provider("gemini-pro") not in USER_PROVIDER_DEFAULT_BASES


# ======================================================================= 1. provider-prefixed models: call time


BAD_ROUTES = ["ollama/llama3", "bedrock/anthropic.claude-v2", "sagemaker/endpoint", "vertex_ai/gemini-1.5-pro"]


@pytest.mark.parametrize("model", BAD_ROUTES)
def test_a_disallowed_route_is_refused_at_every_call_site_before_litellm(mu, model):
    """A database written before the policy (or edited by hand) must not be able to reach LiteLLM."""
    from src.agent.llm_adapter import LLMToolAdapter
    from src.analyzer import GeminiAnalyzer

    base_url = "" if model.startswith("ollama/") else "https://llm.mine.example/v1"
    force_user_settings(USER_B, channel_settings(model, base_url=base_url))
    view = get_user_config(USER_B)
    assert view.llm_model_list, "the stored route must reach the view for this test to mean anything"

    with pytest.raises(ModelKeyRequiredError):
        require_usable_model(view)
    # building the Router of such a view is refused as well ...
    with pytest.raises(ModelKeyRequiredError):
        GeminiAnalyzer(config=view)
    with pytest.raises(ModelKeyRequiredError):
        LLMToolAdapter(view)
    # ... and so is every dispatch, even on an object built without its Router
    with patch.object(GeminiAnalyzer, "_init_litellm"), patch.object(LLMToolAdapter, "_init_litellm"):
        analyzer, adapter = GeminiAnalyzer(config=view), LLMToolAdapter(view)
    for use_router in (True, False):
        with pytest.raises(ModelKeyRequiredError):
            analyzer._dispatch_litellm_completion(
                model, {"model": model, "messages": []}, config=view, use_channel_router=use_router,
                router_model_names={model} if use_router else set(),
            )
    with pytest.raises(ModelKeyRequiredError):
        adapter._call_litellm_model([{"role": "user", "content": "hi"}], [], model)
    for path, body in (
        ("/api/v1/me/settings/test-model", None),
        ("/api/v1/agent/chat", {"message": "hello"}),
    ):
        response = mu.as_user(USER_B).post(path, **({"json": body} if body else {}))
        assert response.status_code == 400 and response.json()["error"] in {
            "model_route_not_allowed", "model_key_required"}, (path, response.text)

    assert not mu.boundary.touched, "LiteLLM must never be reached for a disallowed route"


def test_litellm_expands_environment_references_so_a_user_must_not_be_able_to_send_one(real_stack, monkeypatch):
    """``api_key: os.environ/NAME`` makes LiteLLM send the SERVER's variable to the user's endpoint (checked on the wire)."""
    litellm = _real_litellm()
    wire = Wire().install(monkeypatch)
    monkeypatch.setenv("ADMIN_ONLY_SECRET", "ADMIN-SECRET-VALUE")
    params = {"model": "openai/gpt-4o", "api_key": "os.environ/ADMIN_ONLY_SECRET", "api_base": "https://llm.mine.example/v1"}
    try:
        litellm.Router(model_list=[{"model_name": "m", "litellm_params": params}], num_retries=0).completion(
            model="m", messages=[{"role": "user", "content": "hi"}], timeout=5,
        )
    except Exception:  # noqa: BLE001 - the wire answers 401
        pass
    assert any("ADMIN-SECRET-VALUE" in wire.credentials(request) for request in wire.requests)  # the exploit is real

    from src.llm.credentials_guard import harden_user_model_list, require_explicit_credentials

    force_user_settings(USER_B, channel_settings("gpt-4o", key="os.environ/ADMIN_ONLY_SECRET"))
    view = get_user_config(USER_B)
    wire.requests.clear()
    with pytest.raises(ModelRouteNotAllowedError):
        require_usable_model(view)
    with pytest.raises(ModelRouteNotAllowedError):
        harden_user_model_list(view, view.llm_model_list)
    with pytest.raises(ModelRouteNotAllowedError):
        require_explicit_credentials(view, {"model": "openai/gpt-4o", "api_key": "os.environ/ADMIN_ONLY_SECRET"})
    for field in ("api_key", "organization", "extra_headers"):
        value = {"X": "os.environ/ADMIN_ONLY_SECRET"} if field == "extra_headers" else "OS.Environ/ADMIN_ONLY_SECRET"
        with pytest.raises(ModelRouteNotAllowedError):
            require_explicit_credentials(
                view, {"model": "openai/gpt-4o", "api_key": "sk-x", "api_base": "https://llm.mine.example/v1", field: value},
            )
    assert not wire.requests

    # free text in the conversation is not a routing parameter
    kwargs = {"model": "openai/gpt-4o", "api_key": "sk-x", "api_base": "https://llm.mine.example/v1",
              "messages": [{"role": "user", "content": "what does os.environ/PATH mean?"}]}
    require_explicit_credentials(view, kwargs, model="openai/gpt-4o")


def test_environment_references_are_rejected_when_a_user_saves_model_settings(mu):
    for key, value in (("LLM_MINE_API_KEY", "os.environ/OPENAI_API_KEY"), ("LLM_MINE_MODELS", "os.environ/MODEL")):
        values = channel_settings("gpt-4o")
        values[key] = value
        with pytest.raises(UserSettingError) as caught:
            UserSettingsService().set_settings(USER_B, values)
        assert caught.value.code == "model_route_not_allowed", key
    with pytest.raises(UserSettingError):
        UserSettingsService().set_settings(USER_B, {"OPENAI_API_KEY": "os.environ/OPENAI_API_KEY", "LITELLM_MODEL": "openai/gpt-4o"})
    assert UserSettingsService().get_plain_values(USER_B) == {}


def test_openai_without_a_base_url_never_uses_the_servers_openai_endpoint(mu, monkeypatch):
    """``openai/<model>`` with only a key is LiteLLM's "use OPENAI_API_BASE / OPENAI_BASE_URL of this server" case."""
    from src.analyzer import GeminiAnalyzer

    monkeypatch.setenv("OPENAI_API_BASE", "https://admin-gateway.internal.example/v1")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://admin-gateway.internal.example/v1")
    UserSettingsService().set_settings(USER_B, channel_settings("gpt-4o", base_url=""))
    view = get_user_config(USER_B)

    GeminiAnalyzer(config=view).generate_text_with_models("ping", call_type="light_task")

    deployments = [entry["litellm_params"] for models in mu.boundary.router_model_lists for entry in models]
    assert deployments
    for params in deployments:
        assert params["api_base"] == USER_PROVIDER_DEFAULT_BASES["openai"]  # passed explicitly: the env cannot win
        assert params["api_key"] == "sk-mine-key"
    assert "admin-gateway" not in repr(mu.boundary.router_model_lists)

    # and an `openai/` route with neither a key nor a base is refused outright (no env key fallback, no call)
    keyless = get_user_config(USER_A)
    for model in ("openai/gpt-4o", "gpt-4o"):
        with pytest.raises(ModelKeyRequiredError):
            GeminiAnalyzer(config=keyless)._dispatch_litellm_completion(
                model, {"model": model, "messages": []}, config=keyless, use_channel_router=False,
                router_model_names=set(),
            )
    assert not mu.boundary.completion_calls


def test_direct_call_of_an_allowlisted_route_passes_the_default_endpoint_explicitly(mu):
    from src.llm.credentials_guard import require_explicit_credentials

    force_user_settings(USER_B, channel_settings("gpt-4o", base_url=""))
    view = get_user_config(USER_B)
    kwargs = {"model": "openai/gpt-4o", "api_key": "sk-mine-key", "messages": []}

    require_explicit_credentials(view, kwargs, model="openai/gpt-4o")

    assert kwargs["api_base"] == "https://api.openai.com/v1"


def test_an_explicit_public_base_is_kept_and_a_private_one_is_refused_at_call_time(mu, monkeypatch):
    from src.llm.credentials_guard import require_explicit_credentials

    force_user_settings(USER_B, channel_settings("gpt-4o"))
    view = get_user_config(USER_B)
    kwargs = {"model": "openai/gpt-4o", "api_key": "sk-mine-key", "api_base": "https://llm.mine.example/v1"}
    require_explicit_credentials(view, kwargs, model="openai/gpt-4o")
    assert kwargs["api_base"] == "https://llm.mine.example/v1"

    stub_dns(monkeypatch, {"llm.mine.example": "10.1.2.3"})  # the name starts resolving to a private address
    with pytest.raises(UnsafeUrlError):
        require_explicit_credentials(view, dict(kwargs), model="openai/gpt-4o")
    for private in ("https://10.0.0.5/v1", "https://169.254.169.254/latest", "http://127.0.0.1:8000"):
        with pytest.raises(UnsafeUrlError):
            require_explicit_credentials(
                view, {"model": "openai/gpt-4o", "api_key": "k", "api_base": private}, model="openai/gpt-4o",
            )


def test_the_primary_user_keeps_provider_variables_and_any_route(mu):
    """The primary user's configuration is the environment: no route policy, no explicit endpoint."""
    from src.config import get_config
    from src.llm.credentials_guard import harden_user_model_list, require_explicit_credentials

    system = get_config()
    model_list = [{"model_name": "m", "litellm_params": {"model": "bedrock/x"}}]
    assert harden_user_model_list(system, model_list) is model_list
    kwargs = {"model": "bedrock/anthropic.claude-v2"}
    require_explicit_credentials(system, kwargs, model="bedrock/anthropic.claude-v2")
    assert kwargs == {"model": "bedrock/anthropic.claude-v2"}
    assert user_call_scope(system).__class__.__name__ == "nullcontext"


# ----------------------------------------------------------------- every allowlist entry vs the installed LiteLLM


def _real_litellm():
    import litellm

    if getattr(litellm, "__dsa_test_stub__", False) or not getattr(litellm, "__file__", None):
        pytest.skip("the real litellm package is not importable in this environment")
    return litellm


@functools.lru_cache(maxsize=1)
def _litellm_env_names():
    """Every environment variable the installed LiteLLM reads for an endpoint or a credential (by scanning its source)."""
    import litellm

    root = Path(litellm.__file__).parent
    pattern = re.compile(r"""(?:get_secret(?:_str)?|os\.getenv|os\.environ\.get)\(\s*["']([A-Z][A-Z0-9_]{3,})["']""")
    names = set()
    sources = [root / "main.py", root / "utils.py", root / "router.py"]
    sources += list((root / "llms").rglob("*.py")) + list((root / "litellm_core_utils").rglob("*.py"))
    for path in sources:
        try:
            names.update(pattern.findall(path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    names = {name for name in names if not name.startswith(("LITELLM", "REDIS", "OTEL", "LANGFUSE", "SLACK"))}
    bases = {n for n in names if re.search(r"(API_BASE|BASE_URL|_BASE|ENDPOINT|_URL|API_URL|HOST)$", n)}
    keys = {n for n in names if re.search(r"(API_KEY|_KEY|TOKEN|SECRET|CREDENTIALS)$", n)}
    return tuple(sorted(bases)), tuple(sorted(keys))


POISON_BASE = "https://poison-base.evil.test/v1"


class Wire:
    """The httpx transport underneath LiteLLM / the OpenAI SDK: records every request, answers like a provider."""

    def __init__(self, handler=None):
        self.requests = []
        self.handler = handler or (lambda request: httpx.Response(401, json={"error": {"message": "nope"}}))

    def install(self, monkeypatch):
        wire = self

        def handle_request(transport, request):
            wire.requests.append(request)
            return wire.handler(request)

        monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle_request)
        return self

    @property
    def urls(self):
        return [str(request.url) for request in self.requests]

    def credentials(self, request) -> str:
        return " ".join(
            f"{name}={value}" for name, value in request.headers.items()
            if name.lower() in {"authorization", "x-api-key", "x-goog-api-key", "api-key"}
        ) + " " + str(request.url)


def _complete(litellm, **params):
    try:
        litellm.completion(
            messages=[{"role": "user", "content": "hi"}], max_retries=0, num_retries=0, timeout=5, **params,
        )
    except Exception:  # noqa: BLE001 - the wire answers 401 / 302 on purpose
        pass


@pytest.mark.parametrize("provider", sorted(USER_PROVIDER_DEFAULT_BASES))
def test_every_allowlisted_provider_ignores_the_server_environment(provider, monkeypatch):
    """The justification of the allowlist, checked against the installed LiteLLM.

    With every ``*_API_BASE`` / ``*_BASE_URL`` / ``*_API_KEY`` / token variable LiteLLM knows poisoned, a call built by
    ``harden_user_params`` reaches exactly the URL an un-poisoned default call reaches, with the user's key and none
    of the environment's.
    """
    litellm = _real_litellm()
    wire = Wire().install(monkeypatch)
    base_names, key_names = _litellm_env_names()
    for name in (*base_names, *key_names):
        monkeypatch.delenv(name, raising=False)
    model = f"{provider}/test-model"

    _complete(litellm, model=model, api_key="USERKEY")  # un-poisoned default endpoint of the provider
    assert wire.requests, f"no request was made for {provider}"
    default_url = str(wire.requests[-1].url)
    wire.requests.clear()

    for name in base_names:
        monkeypatch.setenv(name, POISON_BASE)
    for name in key_names:
        monkeypatch.setenv(name, "ENVKEY-" + name)
    params = harden_user_params({"model": model, "api_key": "USERKEY"})
    assert params["api_base"] == USER_PROVIDER_DEFAULT_BASES[provider]
    _complete(litellm, **params)

    assert wire.requests, f"no request was made for {provider}"
    for request in wire.requests:
        seen = wire.credentials(request)
        assert "evil.test" not in seen and "ENVKEY" not in seen, (provider, seen)
        assert "USERKEY" in seen, (provider, seen)
    assert str(wire.requests[-1].url) == default_url


def test_the_environment_really_can_redirect_a_call_without_an_explicit_base(monkeypatch):
    """Guards the guard: the poisoned variable does win when the call carries no ``api_base``."""
    litellm = _real_litellm()
    wire = Wire().install(monkeypatch)
    monkeypatch.setenv("OPENAI_API_BASE", POISON_BASE)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)

    _complete(litellm, model="openai/gpt-4o", api_key="USERKEY")

    assert any("evil.test" in url for url in wire.urls)


@pytest.mark.parametrize("provider", sorted(KEYLESS_PROVIDERS))
def test_keyless_ollama_never_sends_the_servers_ollama_key(provider, monkeypatch):
    litellm = _real_litellm()
    wire = Wire().install(monkeypatch)
    monkeypatch.setenv("OLLAMA_API_KEY", "ENVKEY-OLLAMA")
    monkeypatch.setenv("OLLAMA_API_BASE", POISON_BASE)
    params = harden_user_params({"model": f"{provider}/llama3", "api_base": "https://ollama.mine.example"})

    _complete(litellm, **params)

    assert wire.requests and all("ENVKEY" not in wire.credentials(request) for request in wire.requests)
    assert all(request.url.host == "ollama.mine.example" for request in wire.requests)


# ======================================================================= 1. test-model never reflects the upstream


class _Upstream(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class APIConnectionError(_Upstream):
    pass


class Timeout(_Upstream):
    pass


@pytest.mark.parametrize("error, category, status", [
    (_Upstream("401 bad key", 401), "auth_failed", 401),
    (_Upstream("403 forbidden", 403), "auth_failed", 403),
    (APIConnectionError("connect failed"), "unreachable", None),
    (Timeout("read timed out"), "timeout", None),
    (_Upstream("<html>internal admin console</html>", 502), "bad_response", 502),
    (ValueError("garbled"), "bad_response", None),
])
def test_model_test_returns_a_generic_category_and_never_the_upstream_text(mu, error, category, status):
    give_user_model(USER_B, key="sk-bob-own-key-0001")
    secret_text = "SECRET-UPSTREAM-BODY 10.9.8.7 http://169.254.169.254/latest/meta-data/iam"
    error.args = (f"{error.args[0]} {secret_text}",)

    def boom(**kwargs):
        raise error

    with patch("litellm.completion", boom):
        response = mu.as_user(USER_B).post("/api/v1/me/settings/test-model")

    body = response.json()
    assert response.status_code == 200 and body["success"] is False and body["error_code"] == "model_call_failed"
    assert body["error_category"] == category and body["http_status"] == status
    for fragment in ("SECRET-UPSTREAM-BODY", "169.254", "10.9.8.7", "internal admin console", "sk-bob-own-key-0001"):
        assert fragment not in response.text
    assert set(body) <= {"success", "model", "latency_ms", "error_code", "error_category", "http_status", "message"}


def test_model_test_success_does_not_echo_the_models_reply(mu):
    give_user_model(USER_B)
    mu.boundary.completion = lambda **kwargs: _response("REPLY-FROM-THE-ENDPOINT")
    with patch("litellm.completion", mu.boundary.completion):
        response = mu.as_user(USER_B).post("/api/v1/me/settings/test-model")

    assert response.status_code == 200 and response.json()["success"] is True
    assert "REPLY-FROM-THE-ENDPOINT" not in response.text


# ======================================================================= 2. redirects


def _redirecting_wire(monkeypatch, target):
    def handler(request):
        if request.url.host == "llm.mine.example":
            return httpx.Response(302, headers={"location": target}, request=request)
        return httpx.Response(200, json={
            "id": "x", "object": "chat.completion", "created": 1, "model": "m",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "LEAKED"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }, request=request)

    return Wire(handler).install(monkeypatch)


def test_forbid_httpx_redirects_blocks_sync_and_async_redirects_and_only_inside_the_context():
    def handler(request):
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, text="internal")

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
        assert client.get("https://public.example/start").text == "internal"  # unchanged outside the context
        with forbid_httpx_redirects():
            blocked = client.get("https://public.example/start")
        assert blocked.status_code == 302
        assert client.get("https://public.example/start").text == "internal"  # and restored afterwards

    import asyncio

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            with forbid_httpx_redirects():
                return (await client.get("https://public.example/start")).status_code

    assert asyncio.run(run()) == 302


@pytest.mark.parametrize("target", INTERNAL_TARGETS)
def test_a_user_model_call_does_not_follow_a_redirect_to_an_internal_host(real_stack, monkeypatch, target):
    """Router path, direct path and the connectivity test, on the real LiteLLM + OpenAI SDK stack."""
    from src.analyzer import GeminiAnalyzer
    from src.llm.credentials_guard import harden_user_model_list

    litellm = _real_litellm()
    wire = _redirecting_wire(monkeypatch, target)
    own = give_user_model(USER_B)
    view = get_user_config(USER_B)
    model = own["model"]

    # direct dispatch of the analyzer (a model outside the channel router)
    with patch.object(GeminiAnalyzer, "_init_litellm"):
        analyzer = GeminiAnalyzer(config=view)
    direct = {
        "model": model, "messages": [{"role": "user", "content": "hi"}], "num_retries": 0, "timeout": 5,
        "api_key": own["api_key"], "api_base": own["api_base"],
    }
    with pytest.raises(Exception) as caught:  # noqa: B017 - the redirect surfaces as the provider's HTTP error
        analyzer._dispatch_litellm_completion(
            model, direct, config=view, use_channel_router=False, router_model_names=set(),
        )
    assert not isinstance(caught.value, ModelKeyRequiredError)  # it got as far as the wire
    assert wire.requests and {request.url.host for request in wire.requests} == {"llm.mine.example"}

    # the Router the analyzer / agent adapter build from the user's hardened deployments
    wire.requests.clear()
    router = litellm.Router(
        model_list=harden_user_model_list(view, view.llm_model_list), num_retries=0, routing_strategy="simple-shuffle",
    )
    with user_call_scope(view), pytest.raises(Exception):  # noqa: B017
        router.completion(model=model, messages=[{"role": "user", "content": "hi"}], timeout=5)
    assert wire.requests and {request.url.host for request in wire.requests} == {"llm.mine.example"}

    # the connectivity test endpoint
    wire.requests.clear()
    body = real_stack.as_user(USER_B).post("/api/v1/me/settings/test-model").json()
    assert body["success"] is False
    assert wire.requests and {request.url.host for request in wire.requests} == {"llm.mine.example"}
    assert "LEAKED" not in json.dumps(body)


def test_the_primary_users_model_calls_keep_following_redirects(real_stack, monkeypatch):
    """Policy scope: only user-supplied endpoints are guarded; the administrator's own endpoint is unchanged."""
    from src.config import get_config

    litellm = _real_litellm()
    wire = _redirecting_wire(monkeypatch, "https://elsewhere.example/v1/chat/completions")
    system = get_config()
    assert user_call_scope(system).__class__.__name__ == "nullcontext"

    _complete(litellm, model="openai/gpt-4o", api_key="k", api_base="https://llm.mine.example/v1")

    assert {request.url.host for request in wire.requests} == {"llm.mine.example", "elsewhere.example"}


class _RedirectAdapter(requests.adapters.BaseAdapter):
    """A ``requests`` transport that answers every request with a redirect to an internal address, and records."""

    def __init__(self, target, seen):
        super().__init__()
        self.target, self.seen = target, seen

    def send(self, request, **kwargs):
        self.seen.append(request.url)
        response = requests.Response()
        response.url = request.url
        response.request = request
        if "169.254" in request.url or "127.0.0.1" in request.url:
            response.status_code, response._content = 200, b'{"results": [{"url": "https://x.example/a", "title": "LEAKED"}]}'
        else:
            response.status_code, response._content = 302, b""
            response.headers["Location"] = self.target
        import io

        response.raw = io.BytesIO(b"")
        return response

    def close(self):
        pass


@pytest.fixture
def redirecting_requests(monkeypatch):
    seen = []
    monkeypatch.setattr(
        requests.sessions.Session, "get_adapter", lambda self, url: _RedirectAdapter(INTERNAL_TARGETS[0], seen),
    )
    return seen


@pytest.mark.parametrize("retry_enabled", [False, True])
@pytest.mark.parametrize("target", INTERNAL_TARGETS)
def test_searxng_of_a_user_does_not_follow_a_redirect(monkeypatch, retry_enabled, target):
    from src.search_service import SearXNGSearchProvider

    seen = []
    monkeypatch.setattr(requests.sessions.Session, "get_adapter", lambda self, url: _RedirectAdapter(target, seen))

    guarded = SearXNGSearchProvider(["https://searx.mine.example"], follow_redirects=False)
    response = guarded._do_search("q", "https://searx.mine.example", 3, timeout=3, retry_enabled=retry_enabled)
    assert response.success is False and "LEAKED" not in repr(response)
    assert [url.split("/")[2] for url in seen] == ["searx.mine.example"]

    seen.clear()  # the system configuration is unchanged: its own instance may redirect (e.g. http -> https)
    trusting = SearXNGSearchProvider(["https://searx.mine.example"])
    trusting._do_search("q", "https://searx.mine.example", 3, timeout=3, retry_enabled=retry_enabled)
    assert len(seen) == 2


def test_the_search_service_of_a_user_view_is_built_without_redirects(mu):
    from src.config import get_config
    from src.search_service import SearXNGSearchProvider, get_search_service

    UserSettingsService().set_settings(USER_B, {"SEARXNG_BASE_URLS": "https://searx.bob.example"})

    def provider_of(service):
        return next(p for p in service._providers if isinstance(p, SearXNGSearchProvider))

    assert provider_of(get_search_service(get_user_config(USER_B)))._request_options == {"allow_redirects": False}
    assert provider_of(get_search_service(get_user_config(USER_B)))._base_urls == ["https://searx.bob.example"]
    system_view = get_config()
    assert getattr(system_view, "user_view_id", None) is None


def test_the_pipeline_builds_its_search_service_without_redirects_for_a_user_view(mu, monkeypatch):
    from src.config import get_config
    from src.core import pipeline as pipeline_module

    created = []

    class RecordingSearchService:
        is_available = False

        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(pipeline_module, "SearchService", RecordingSearchService)
    UserSettingsService().set_settings(USER_B, {"SEARXNG_BASE_URLS": "https://searx.bob.example"})
    give_user_model(USER_B)

    pipeline_module.StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_B)
    pipeline_module.StockAnalysisPipeline(config=get_config())

    assert created[0]["searxng_follow_redirects"] is False
    assert created[0]["searxng_base_urls"] == ["https://searx.bob.example"]
    assert created[1]["searxng_follow_redirects"] is True


@pytest.mark.parametrize("target", INTERNAL_TARGETS)
def test_hindsight_of_a_user_does_not_follow_a_redirect(monkeypatch, target):
    from src.agent.recall.base import RecallError, RecallQuery
    from src.agent.recall.hindsight_provider import HindsightRecallProvider

    seen = []
    monkeypatch.setattr(requests.sessions.Session, "get_adapter", lambda self, url: _RedirectAdapter(target, seen))
    query = RecallQuery(text="q", tags=(), now=datetime(2026, 11, 10))

    guarded = HindsightRecallProvider(
        base_url="https://memory.bob.example", bank_id="b", api_key="bob-key", allow_redirects=False,
    )
    with pytest.raises(RecallError):  # the 302 is an error answer, never a hop
        guarded.recall(query)
    assert [url.split("/")[2] for url in seen] == ["memory.bob.example"]

    seen.clear()
    HindsightRecallProvider(base_url="https://memory.sys.example", bank_id="b", api_key="sys-key")
    # the system provider keeps the library default (the administrator's own server)
    assert HindsightRecallProvider(base_url="https://x.example", bank_id="b")._request_options == {}


def test_a_users_own_hindsight_server_builds_a_provider_that_does_not_follow_redirects(mu, monkeypatch):
    from src.agent.recall import get_user_recall_service, reset_recall_service

    monkeypatch.setenv("ASSISTANT_RECALL_PROVIDER", "hindsight")
    UserSettingsService().set_settings(USER_B, {"HINDSIGHT_BASE_URL": "https://memory.bob.example"})
    reset_recall_service()
    try:
        service = get_user_recall_service(USER_B, get_user_config(USER_B))
        assert service.provider._request_options == {"allow_redirects": False}
    finally:
        reset_recall_service()


# ----------------------------------------------------------------- the worker re-validates at dispatch


def test_the_worker_rechecks_the_model_endpoint_when_it_starts_the_task(mu, monkeypatch):
    """Accepted at request time, rebound to a private address before a queued task runs: refused in the worker."""
    from src.services.analysis_service import AnalysisService

    give_user_model(USER_B)
    require_usable_model(get_user_config(USER_B))  # the request-entry gate passes

    stub_dns(monkeypatch, {"llm.mine.example": "10.1.2.3"})  # DNS rebinding while the task waited in the queue
    with pytest.raises(UnsafeUrlError):
        AnalysisService().analyze_stock("600519", user_id=USER_B)

    assert not mu.boundary.touched


def test_the_task_queue_worker_uses_analysis_service_for_the_recheck():
    import inspect

    from src.services import task_queue

    source = inspect.getsource(task_queue)
    assert "service.analyze_stock(" in source and "user_id" in source


# ======================================================================= 3. backtests exclude private reports


def _save_report(db, query_id, code, *, requester, private, advice="买入"):
    from src.analyzer import AnalysisResult

    result = AnalysisResult(
        code=code, name=f"n{code}", sentiment_score=70, trend_prediction="看多", operation_advice=advice,
        analysis_summary="summary",
    )
    record_id = db.save_analysis_history(
        result=result, query_id=query_id, report_type="simple", news_content=None, context_snapshot=None,
        save_snapshot=False, requester_user_id=requester, is_private=private,
    )
    assert record_id > 0
    return record_id


def _seed_result(db, history_id, code, **extra):
    from src.storage import BacktestResult

    values = dict(
        analysis_history_id=history_id, code=code, analysis_date=datetime.now().date() - timedelta(days=30),
        eval_window_days=10, engine_version="v1", eval_status="completed", operation_advice="买入",
        position_recommendation="long", start_price=10.0, end_close=11.0, stock_return_pct=10.0,
        stop_loss=9.1234, take_profit=12.3456, hit_stop_loss=False, hit_take_profit=False, first_hit="neither",
        simulated_return_pct=9.0, outcome="win", direction_expected="up", direction_correct=True,
        evaluated_at=datetime.now(),
    )
    values.update(extra)
    with db.get_session() as session:
        session.add(BacktestResult(**values))
        session.commit()


@pytest.fixture
def backtests(mu):
    """A shared and a private (position-aware, holder-specific stop-loss) report of the same stock, both evaluated."""
    db = mu.db
    shared = _save_report(db, "q-shared", "600519", requester=USER_A, private=False)
    private = _save_report(db, "q-private", "600519", requester=USER_A, private=True, advice="减仓")
    _seed_result(db, shared, "600519")
    _seed_result(db, private, "600519", operation_advice="减仓", stop_loss=7.7777, take_profit=8.8888)
    mu.ids = SimpleNamespace(shared=shared, private=private)
    return mu


def test_a_private_report_is_never_a_backtest_candidate(backtests):
    from src.repositories.backtest_repo import BacktestRepository

    repo = BacktestRepository()
    for force in (False, True):
        ids = {
            row.id for row in repo.get_candidates(
                code=None, min_age_days=0, limit=50, eval_window_days=10, engine_version="v1", force=force,
            )
        }
        assert backtests.ids.private not in ids
    forced = {
        row.id for row in repo.get_candidates(
            code="600519", min_age_days=0, limit=50, eval_window_days=10, engine_version="v1", force=True,
        )
    }
    assert forced == {backtests.ids.shared}


def test_run_backtest_never_evaluates_a_private_report(backtests, monkeypatch):
    from src.services.backtest_service import BacktestService

    service = BacktestService(backtests.db)
    seen = []
    monkeypatch.setattr(service, "_resolve_analysis_date", lambda analysis: seen.append(analysis.id) or None)

    service.run_backtest(force=True, min_age_days=0, eval_window_days=10)

    assert backtests.ids.private not in seen and backtests.ids.shared in seen


def test_the_backtest_api_never_returns_a_private_reports_advice_or_levels(backtests):
    for user in (PRIMARY_ID, USER_A, USER_B):  # even the owner: a backtest is shared output
        client = backtests.as_user(user)
        results = client.get("/api/v1/backtest/results", params={"code": "600519"})
        assert results.status_code == 200, results.text
        body = results.json()
        assert body["total"] == 1 and len(body["items"]) == 1
        text = json.dumps(body)
        assert "7.7777" not in text and "8.8888" not in text and "减仓" not in text
        by_range = client.get("/api/v1/backtest/results", params={
            "code": "600519", "analysis_date_from": str((datetime.now() - timedelta(days=60)).date()),
            "analysis_date_to": str(datetime.now().date()), "analysis_phase": "unknown",
        })
        assert by_range.status_code == 200 and "7.7777" not in by_range.text
        performance = client.get("/api/v1/backtest/performance/600519", params={
            "analysis_date_from": str((datetime.now() - timedelta(days=60)).date()),
            "analysis_date_to": str(datetime.now().date()),
        })
        assert performance.status_code == 200, performance.text
        assert performance.json()["total_evaluations"] == 1


def test_a_stored_rollup_is_not_recomputed_from_private_results(backtests):
    from src.services.backtest_service import BacktestService

    service = BacktestService(backtests.db)
    service._recompute_summaries(touched_codes=["600519"], eval_window_days=10, engine_version="v1")

    stock = service.get_summary(scope="stock", code="600519", eval_window_days=10)
    overall = service.get_summary(scope="overall", code=None, eval_window_days=10)
    assert stock["total_evaluations"] == 1 and overall["total_evaluations"] == 1


def test_the_agent_backtest_tools_exclude_private_reports(backtests, monkeypatch):
    from src.agent.tools import backtest_tools
    from src.services.backtest_service import BacktestService

    monkeypatch.setattr(backtest_tools, "_backtest_service", BacktestService(backtests.db))
    BacktestService(backtests.db)._recompute_summaries(touched_codes=["600519"], eval_window_days=10, engine_version="v1")

    stock = backtest_tools._handle_get_stock_backtest_summary("600519", eval_window_days=10, limit=10)
    overall = backtest_tools._handle_get_overall_backtest_summary(eval_window_days=10)

    assert stock["total"] == 1 and len(stock["recent_evaluations"]) == 1
    assert stock["summary"]["total_evaluations"] == 1 and overall["total_evaluations"] == 1
    assert "减仓" not in json.dumps([stock, overall], ensure_ascii=False)


def test_a_private_report_never_yields_skill_opinion_samples_and_old_rows_are_not_counted(backtests):
    from src.agent.runtime_facts import SkillOpinionFact
    from src.repositories.skill_opinion_outcome_repo import SkillOpinionOutcomeRepository
    from src.repositories.skill_opinion_sample_repo import SkillOpinionSampleRepository
    from src.services.skill_opinion_sample_service import SkillOpinionSampleService
    from src.storage import SkillOpinionOutcomeRecord, SkillOpinionSampleRecord

    opinion = SkillOpinionFact(skill_id="bull_trend", signal="buy", confidence=0.7)
    service = SkillOpinionSampleService(db_manager=backtests.db)

    assert service.persist(analysis_history_id=backtests.ids.private, stock_code="600519", opinions=[opinion]) == 0
    assert service.persist(analysis_history_id=backtests.ids.shared, stock_code="600519", opinions=[opinion]) == 1
    assert SkillOpinionSampleRepository(backtests.db).list_for_history(backtests.ids.private) == []

    # a sample row (and its outcome) that predates the privacy flag: invisible to every outcome / performance read
    with backtests.db.get_session() as session:
        legacy = SkillOpinionSampleRecord(
            analysis_history_id=backtests.ids.private, stock_code="600519", skill_id="legacy_skill", signal="buy",
            confidence=0.9, horizon="5d", sample_schema_version="skill-opinion-sample-v1",
            opinion_created_at=datetime.now() - timedelta(days=20),
        )
        session.add(legacy)
        session.flush()
        pending = SkillOpinionSampleRecord(
            analysis_history_id=backtests.ids.private, stock_code="600519", skill_id="legacy_pending", signal="buy",
            confidence=0.9, horizon="5d", sample_schema_version="skill-opinion-sample-v1",
            opinion_created_at=datetime.now() - timedelta(days=20),
        )
        session.add(pending)
        session.add(SkillOpinionOutcomeRecord(
            skill_opinion_sample_id=legacy.id, horizon="5d", engine_version="v1", eval_status="evaluated",
            outcome="hit", direction_correct=True, directional_return_pct=5.0,
        ))
        session.commit()
    outcomes = SkillOpinionOutcomeRepository(backtests.db)
    assert "legacy_skill" not in repr(outcomes.list_performance_buckets(engine_version="v1"))
    candidates = outcomes.list_candidate_keys(horizons=["5d"], engine_version="v1", limit=50)
    assert "legacy_pending" not in repr([candidate.sample.skill_id for candidate in candidates])
    assert "bull_trend" in repr([candidate.sample.skill_id for candidate in candidates])  # the shared one still is


def test_the_migration_purges_derived_rows_of_private_reports_after_a_backup(tmp_path):
    """The existing P2b-2 purge step: results / rollups / samples / outcomes derived from private reports go."""
    import sqlite3

    from src.storage_multi_user_migration import _purge_private_derived_data

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE analysis_history (id INTEGER PRIMARY KEY, is_private BOOLEAN);
        CREATE TABLE backtest_results (id INTEGER PRIMARY KEY, analysis_history_id INTEGER, eval_window_days INTEGER,
                                       engine_version TEXT);
        CREATE TABLE backtest_summaries (id INTEGER PRIMARY KEY, eval_window_days INTEGER, engine_version TEXT);
        CREATE TABLE skill_opinion_samples (id INTEGER PRIMARY KEY, analysis_history_id INTEGER);
        CREATE TABLE skill_opinion_outcomes (id INTEGER PRIMARY KEY, skill_opinion_sample_id INTEGER);
        INSERT INTO analysis_history VALUES (1, 0), (2, 1);
        INSERT INTO backtest_results VALUES (1, 1, 10, 'v1'), (2, 2, 10, 'v1');
        INSERT INTO backtest_summaries VALUES (1, 10, 'v1'), (2, 5, 'v1');
        INSERT INTO skill_opinion_samples VALUES (1, 1), (2, 2);
        INSERT INTO skill_opinion_outcomes VALUES (1, 1), (2, 2);
        """
    )
    conn.commit()
    conn.close()
    from sqlalchemy import create_engine

    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        _purge_private_derived_data(connection)
    with engine.connect() as connection:
        rows = lambda table: connection.exec_driver_sql(f"SELECT id FROM {table} ORDER BY id").fetchall()  # noqa: E731
        assert rows("backtest_results") == [(1,)] and rows("skill_opinion_samples") == [(1,)]
        assert rows("skill_opinion_outcomes") == [(1,)]
        assert rows("backtest_summaries") == [(2,)]  # only the rollup bucket that lost a row is dropped


# ======================================================================= 4. Hindsight bank escape


SYSTEM_URL = "https://hindsight.sys.example"
SYSTEM_BANK = "sysbank"


@pytest.fixture
def hindsight(tmp_path, monkeypatch):
    from src.agent.recall import reset_recall_service
    from tests.multi_user_helpers import prepare_multi_user_env

    prepare_multi_user_env(tmp_path, monkeypatch)
    monkeypatch.setenv("ASSISTANT_RECALL_PROVIDER", "hindsight")
    monkeypatch.setenv("HINDSIGHT_BASE_URL", SYSTEM_URL)
    monkeypatch.setenv("HINDSIGHT_API_KEY", "hs-system-key")
    monkeypatch.setenv("HINDSIGHT_BANK_ID", SYSTEM_BANK)
    from src.config import Config

    Config.reset_instance()
    invalidate_user_config()
    reset_recall_service()
    stub_dns(monkeypatch)
    yield
    reset_recall_service()
    teardown_multi_user_env()


@pytest.mark.parametrize("typed_url", [
    SYSTEM_URL, SYSTEM_URL + "/", SYSTEM_URL.upper(), "https://Hindsight.SYS.example:443", SYSTEM_URL + "/.",
    SYSTEM_URL + "/a/..", "https://hindsight.sys.example.", SYSTEM_URL + "//", SYSTEM_URL + "/?x=1",
])
def test_typing_the_system_hindsight_url_keeps_the_users_derived_bank(hindsight, typed_url):
    from src.agent.recall import derive_user_bank_id
    from src.agent.recall.service import user_recall_settings

    UserSettingsService().set_settings(USER_A, {
        "HINDSIGHT_BASE_URL": typed_url, "HINDSIGHT_BANK_ID": SYSTEM_BANK, "HINDSIGHT_API_KEY": "guessed-key",
    })

    settings = user_recall_settings(USER_A, get_user_config(USER_A))

    assert settings["hindsight_bank_id"] == derive_user_bank_id(SYSTEM_BANK, USER_A)
    assert settings["hindsight_base_url"] == SYSTEM_URL
    assert settings["hindsight_api_key"] == "hs-system-key"  # the typed key is not used against the system server
    assert settings.get("hindsight_allow_redirects", True) is True  # the system server is the administrator's own


def test_a_different_hindsight_host_is_the_users_own_endpoint(hindsight):
    from src.agent.recall.service import user_recall_settings

    UserSettingsService().set_settings(USER_A, {
        "HINDSIGHT_BASE_URL": "https://memory.alice.example", "HINDSIGHT_BANK_ID": "alice-bank",
    })

    settings = user_recall_settings(USER_A, get_user_config(USER_A))

    assert (settings["hindsight_base_url"], settings["hindsight_bank_id"]) == ("https://memory.alice.example", "alice-bank")
    assert settings["hindsight_allow_redirects"] is False


def test_the_primary_users_bank_is_never_reachable_by_a_user(hindsight):
    from src.agent.recall import derive_user_bank_id
    from src.agent.recall.service import user_recall_settings

    banks = set()
    for user in (USER_A, USER_B):
        UserSettingsService().set_settings(user, {"HINDSIGHT_BASE_URL": SYSTEM_URL + "/.", "HINDSIGHT_BANK_ID": SYSTEM_BANK})
        banks.add(user_recall_settings(user, get_user_config(user))["hindsight_bank_id"])
    assert SYSTEM_BANK not in banks and len(banks) == 2
    assert banks == {derive_user_bank_id(SYSTEM_BANK, USER_A), derive_user_bank_id(SYSTEM_BANK, USER_B)}


# ======================================================================= 5. inherited system values are not displayed


def _fields(body):
    return {field["key"]: field for group in body["groups"] for field in group["fields"]}


def test_me_settings_never_returns_an_inherited_system_value(mu):
    mu.env_path.write_text(
        mu.env_path.read_text(encoding="utf-8")
        + "HINDSIGHT_BASE_URL=https://hindsight.admin-internal.example\n"
        + "LONGBRIDGE_OAUTH_CLIENT_ID=lb-admin-client-id-123\n"
        + "TUSHARE_TOKEN=tushare-admin-secret-token\n"
        + "REPORT_LANGUAGE=en\n"
        + "POSITION_MAX_PERCENT=37\n",
        encoding="utf-8",
    )
    from src.config import Config

    Config.reset_instance()
    invalidate_user_config()

    response = mu.as_user(USER_B).get("/api/v1/me/settings")

    assert response.status_code == 200
    for secret in ("hindsight.admin-internal", "lb-admin-client-id-123", "tushare-admin-secret-token"):
        assert secret not in response.text
    fields = _fields(response.json())
    for key in ("HINDSIGHT_BASE_URL", "LONGBRIDGE_OAUTH_CLIENT_ID", "TUSHARE_TOKEN", "REPORT_LANGUAGE"):
        assert fields[key]["source"] == "system_default" and fields[key]["configured"] is True, key
        assert fields[key]["value"] == "", key  # whatever the key: only "a default applies", never what it is
    assert fields["HINDSIGHT_API_KEY"]["configured"] is False


def test_me_settings_shows_a_users_own_value_and_masks_their_secrets(mu):
    UserSettingsService().set_settings(USER_B, {"REPORT_LANGUAGE": "en", "TUSHARE_TOKEN": "tushare-bob-token"})

    response = mu.as_user(USER_B).get("/api/v1/me/settings")

    fields = _fields(response.json())
    assert fields["REPORT_LANGUAGE"]["value"] == "en" and fields["REPORT_LANGUAGE"]["source"] == "user"
    assert fields["TUSHARE_TOKEN"]["value"] != "tushare-bob-token" and fields["TUSHARE_TOKEN"]["source"] == "user"
    assert "tushare-bob-token" not in response.text


def test_the_primary_users_read_only_view_still_shows_values_but_masks_secrets(mu):
    mu.env_path.write_text(
        mu.env_path.read_text(encoding="utf-8")
        + "HINDSIGHT_BASE_URL=https://hindsight.admin-internal.example\nTUSHARE_TOKEN=tushare-admin-secret-token\n",
        encoding="utf-8",
    )
    from src.config import Config

    Config.reset_instance()

    response = mu.as_user(PRIMARY_ID).get("/api/v1/me/settings")

    body = response.json()
    fields = _fields(body)
    assert body["is_primary"] is True and fields["HINDSIGHT_BASE_URL"]["value"] == "https://hindsight.admin-internal.example"
    assert fields["TUSHARE_TOKEN"]["configured"] is True and "tushare-admin-secret-token" not in response.text


# ======================================================================= 6. a user's analysis never writes the market review


def _bare_pipeline(config, *, db, analyzer):
    from tests.test_pipeline_market_phase_context import _make_pipeline

    pipeline = _make_pipeline(agent_mode=False)
    pipeline.config = config
    pipeline.db = db
    pipeline.analyzer = analyzer
    pipeline.notifier = MagicMock()
    pipeline.search_service = None
    pipeline.daily_market_context_enabled = True
    pipeline.daily_market_context_allow_generate = True
    return pipeline


def _market_review_ready(monkeypatch):
    monkeypatch.setenv("MARKET_REVIEW_ENABLED", "true")
    monkeypatch.setenv("DAILY_MARKET_CONTEXT_ENABLED", "true")
    from src.config import Config

    Config.reset_instance()
    invalidate_user_config()


def test_a_user_analysis_never_generates_or_persists_the_shared_market_review(mu, monkeypatch):
    from src.core import pipeline as pipeline_module
    from src.storage import AnalysisHistory

    _market_review_ready(monkeypatch)
    give_user_model(USER_B)
    view = get_user_config(USER_B)
    assert view.market_review_enabled is True
    analyzer = MagicMock()
    pipeline = _bare_pipeline(view, db=mu.db, analyzer=analyzer)
    pipeline._daily_market_context_service = pipeline_module.DailyMarketContextService(db_manager=mu.db)

    context = pipeline._load_daily_market_context("cn")

    assert context is None  # no same-day review exists: the analysis simply goes on without one
    assert analyzer.mock_calls == []  # the user's model was not asked to write a review
    with mu.db.get_session() as session:
        assert session.query(AnalysisHistory).filter(AnalysisHistory.report_type == "market_review").count() == 0


def test_a_user_analysis_still_reads_a_same_day_review_that_a_system_job_wrote(mu, monkeypatch):
    from src.core import pipeline as pipeline_module

    _market_review_ready(monkeypatch)
    give_user_model(USER_B)
    view = get_user_config(USER_B)
    pipeline = _bare_pipeline(view, db=mu.db, analyzer=MagicMock())
    spy = MagicMock()
    spy.get_context.return_value = "existing-context"
    pipeline._daily_market_context_service = spy

    assert pipeline._load_daily_market_context("cn") == "existing-context"
    assert spy.get_context.call_args.kwargs["allow_generate"] is False
    assert pipeline_module.is_user_view(view)


def test_the_primary_users_analysis_and_system_jobs_still_generate_the_market_review(mu, monkeypatch):
    from src.config import get_config

    _market_review_ready(monkeypatch)
    system = get_config()
    spy = MagicMock()
    pipeline = _bare_pipeline(system, db=mu.db, analyzer=MagicMock())
    pipeline._daily_market_context_service = spy

    pipeline._load_daily_market_context("cn")
    assert spy.get_context.call_args.kwargs["allow_generate"] is True

    pipeline.daily_market_context_allow_generate = False  # an explicit caller choice still wins
    pipeline._load_daily_market_context("cn")
    assert spy.get_context.call_args.kwargs["allow_generate"] is False


# ======================================================================= 7. system third-party keys


def test_a_user_view_has_no_social_sentiment_key_and_the_pipeline_does_not_build_it(mu, monkeypatch):
    from src.config import get_config
    from src.core import pipeline as pipeline_module

    monkeypatch.setenv("SOCIAL_SENTIMENT_API_KEY", "social-SYSTEM-key")
    from src.config import Config

    Config.reset_instance()
    invalidate_user_config()
    assert get_config().social_sentiment_api_key == "social-SYSTEM-key"
    view = get_user_config(USER_B)
    assert not view.social_sentiment_api_key

    created = []

    class RecordingService:
        is_available = False

        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(pipeline_module, "SocialSentimentService", RecordingService)
    give_user_model(USER_B)
    pipeline_module.StockAnalysisPipeline(config=get_user_config(USER_B), user_id=USER_B)
    # a view that (hand-built, or by a future regression) still carries the key is kept out as well
    carrying = copy.copy(get_user_config(USER_B))
    object.__setattr__(carrying, "social_sentiment_api_key", "social-SYSTEM-key")
    pipeline_module.StockAnalysisPipeline(config=carrying, user_id=USER_B)
    pipeline_module.StockAnalysisPipeline(config=get_config())

    assert created[0]["api_key"] is None and created[1]["api_key"] is None
    assert created[2]["api_key"] == "social-SYSTEM-key"  # primary user / system jobs unchanged


def test_fxmacrodata_uses_the_administrators_key_for_the_primary_user_only(monkeypatch):
    from src.agent.tools import fxmacrodata_tools
    from src.agent.tools.registry import ToolRegistry

    monkeypatch.setenv("FXMACRODATA_API_KEY", "fx-ADMIN-key")
    monkeypatch.setenv("PRIMARY_USER_ID", PRIMARY_ID)
    from src.config import Config

    Config.reset_instance()
    keys = []

    class Client:
        def __init__(self, **kwargs):
            keys.append(kwargs["api_key"])

        def execute(self, *args):
            return SimpleNamespace(as_dict=lambda: {"status": "ok", "records": []})

        def close(self):
            pass

    monkeypatch.setattr(fxmacrodata_tools, "FXMacroDataClient", Client)
    registry = ToolRegistry()
    tool = fxmacrodata_tools.build_fxmacrodata_tools()[0]
    registry.register(tool)

    registry.execute(tool.name, user_id=USER_B)
    registry.execute(tool.name, user_id=PRIMARY_ID)
    tool.handler()  # CLI / bots: no acting user = the primary user

    assert keys == ["", "fx-ADMIN-key", "fx-ADMIN-key"]
    with pytest.raises(Exception):  # a model-supplied user_id is discarded; no user at all fails closed
        registry.execute(tool.name)


def test_no_system_paid_key_other_than_the_documented_ones_is_reachable_from_a_user_view(mu, monkeypatch):
    """Secrets of the process environment that survive into a user view: only the data-source credentials the design
    shares on purpose (their data lands in the shared cache). Everything else is blanked or never inherited."""
    from src.config import Config
    from src.core.config_registry import get_registered_field_keys, get_field_definition
    from src.services.user_config_service import DATA_SOURCE_CREDENTIAL_KEYS, _BLANKED_SYSTEM_KEYS

    secret_keys = [
        key for key in get_registered_field_keys()
        if get_field_definition(key).get("is_sensitive") and key not in {"ADMIN_AUTH_PASSWORD"}
    ]
    for key in secret_keys + ["SOCIAL_SENTIMENT_API_KEY"]:
        monkeypatch.setenv(key, f"SYSTEM-{key}")
    Config.reset_instance()
    invalidate_user_config()
    view = get_user_config(USER_B)

    inherited = set()
    for key in secret_keys + ["SOCIAL_SENTIMENT_API_KEY"]:
        attr = key.lower()
        value = getattr(view, attr, None)
        values = value if isinstance(value, (list, tuple)) else [value]
        if any(f"SYSTEM-{key}" == str(item) for item in values):
            inherited.add(key)
    # the system's own paid third-party data keys (SOCIAL_SENTIMENT_API_KEY, ASHARE_API_KEY) are blanked in every view
    for key in ("SOCIAL_SENTIMENT_API_KEY", "ASHARE_API_KEY"):
        assert key in _BLANKED_SYSTEM_KEYS and key not in inherited and key not in DATA_SOURCE_CREDENTIAL_KEYS
    unexpected = inherited - DATA_SOURCE_CREDENTIAL_KEYS
    # Bot-app credentials are process-level (one process connects one bot application): they sit in the config of any
    # view, but must stay unreachable from a user's request. The audited list is explicit (not a prefix), and a
    # personal notifier - the only notification path a user's request can reach - resets every one of them.
    bot_app_secrets = {"DINGTALK_APP_KEY", "DINGTALK_APP_SECRET", "DISCORD_BOT_TOKEN", "FEISHU_APP_SECRET", "SLACK_BOT_TOKEN"}
    assert unexpected <= bot_app_secrets, unexpected - bot_app_secrets
    from src.services.user_notification_service import UserNotificationConfig

    personal = UserNotificationConfig(view)
    for key in unexpected:
        assert getattr(view, key.lower()) == f"SYSTEM-{key}"  # the view does carry it ...
        assert getattr(personal, key.lower()) in (None, ""), key  # ... the personal notifier never does
