# -*- coding: utf-8 -*-
"""Keeps a non-primary user's model calls on the user's own credentials (docs/design/multi-user.md 3.3 / 12).

LiteLLM falls back to provider variables in ``os.environ`` (``OPENAI_API_KEY``, ``GEMINI_API_KEY``,
``AWS_*`` ...) whenever a call carries no explicit ``api_key``, to ``<PROVIDER>_API_BASE`` /
``OPENAI_BASE_URL`` whenever it carries no ``api_base``, and a few providers (Bedrock, SageMaker, Vertex AI
...) authenticate with the server's ambient cloud credentials or talk to ``localhost`` by default. For the
primary user that is the intended behaviour (the environment *is* their configuration). For every other user it
would spend the administrator's key or reach the server's own services, so their calls are refused before
LiteLLM is reached unless every route comes from that user's own configuration view and uses one of the
providers below.

Route policy of a non-primary user (:func:`check_user_model_route`), per deployment / direct call:

* the provider (the LiteLLM provider the wire model resolves to, not just a ``prefix/``) must be one of
  :data:`USER_PROVIDER_DEFAULT_BASES` (a public SaaS API that authenticates with the passed ``api_key``
  only; its default endpoint is passed explicitly so no ``*_API_BASE`` variable can redirect the call) or
  :data:`USER_OWN_ENDPOINT_PROVIDERS` (key-only servers the user runs themselves: they always need the
  user's own public https ``api_base``);
* an explicit ``api_base`` must be a public https URL (``src/utils/url_safety.py``);
* an ``api_key`` is required, except for Ollama on the user's own endpoint;
* no parameter may be an ``os.environ/NAME`` reference (LiteLLM expands those to the server's variable and would send
  the administrator's secret to the user's endpoint).

Everything else (``bedrock/``, ``sagemaker/``, ``vertex_ai/``, ``azure/``, ``watsonx/``, ``vllm/`` (in-process),
``llamafile/`` and other localhost defaults, ...) is refused, even with an ``api_base`` and a key.

The actual provider call additionally runs inside :func:`user_call_scope`, which forbids redirects
(``forbid_httpx_redirects``): a public endpoint must not be able to bounce the server to an internal one.

A configuration view of a non-primary user is recognised by ``Config.user_view_id``.
"""

from __future__ import annotations

import contextlib
import re
from typing import Any, ContextManager, Dict, List, Mapping, Optional

from src.utils.url_safety import UnsafeUrlError, assert_public_https_url, check_public_https_url, forbid_httpx_redirects

MODEL_KEY_REQUIRED = "model_key_required"
MODEL_ROUTE_NOT_ALLOWED = "model_route_not_allowed"

# Providers that authenticate with the passed ``api_key`` alone and whose default endpoint is a public SaaS API.
# value = that endpoint, passed explicitly when the user gave no ``api_base``. Every entry is checked against the
# installed LiteLLM by ``tests/test_multi_user_p2b3_security.py``: with the server environment poisoned
# (``<PROVIDER>_API_BASE`` / ``<PROVIDER>_API_KEY`` set), a call with this base and a user key reaches exactly
# the URL of the un-poisoned default call, with the user's key and none of the environment's.
USER_PROVIDER_DEFAULT_BASES: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",  # OPENAI_API_BASE / OPENAI_BASE_URL otherwise win
    "anthropic": "https://api.anthropic.com",
    "gemini": "https://generativelanguage.googleapis.com/v1beta",
    "deepseek": "https://api.deepseek.com/beta",
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "xai": "https://api.x.ai/v1",
    "together_ai": "https://api.together.xyz/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "moonshot": "https://api.moonshot.ai/v1",
    "zai": "https://api.z.ai/api/paas/v4",
    "perplexity": "https://api.perplexity.ai",
    "fireworks_ai": "https://api.fireworks.ai/inference/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "sambanova": "https://api.sambanova.ai/v1",
    "nvidia_nim": "https://integrate.api.nvidia.com/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "volcengine": "https://ark.cn-beijing.volces.com/api/v3",
}
# Key-only servers the user brings themselves: LiteLLM's default for these is ``localhost`` or an environment
# base (the server's own model process), so they are usable only with an explicit public https ``api_base``.
USER_OWN_ENDPOINT_PROVIDERS = frozenset({"ollama", "ollama_chat", "hosted_vllm", "lm_studio", "litellm_proxy"})
# Providers that may run without an ``api_key`` (on the user's own endpoint). LiteLLM would otherwise send
# ``OLLAMA_API_KEY`` of the server environment to the user's host, so a fixed placeholder is passed instead.
KEYLESS_PROVIDERS = frozenset({"ollama", "ollama_chat"})
KEYLESS_PLACEHOLDER_API_KEY = "ollama"

_LEGACY_PLACEHOLDER_RE = re.compile(r"^__legacy_([a-z0-9_]+)__$")


class ModelKeyRequiredError(RuntimeError):
    """A non-primary user has no usable model of their own; the request must not reach a provider."""

    code = MODEL_KEY_REQUIRED

    def __init__(self, message: str = "A model API key of your own is required; configure one in your personal settings"):
        super().__init__(message)
        self.message = message


class ModelRouteNotAllowedError(ModelKeyRequiredError):
    """A personal model route uses a provider / endpoint a non-primary user may not use (server credentials, local defaults)."""

    code = MODEL_ROUTE_NOT_ALLOWED


def config_user_id(config: Any) -> Optional[str]:
    """Owner of a per-user configuration view, ``None`` for the system config (primary user) or any other object."""
    value = getattr(type(config), "user_view_id", None)
    if value is None:  # not a Config (mocks, SimpleNamespace test doubles)
        return None
    owner = getattr(config, "user_view_id", None)
    return owner if isinstance(owner, str) and owner else None


def is_user_view(config: Any) -> bool:
    return config_user_id(config) is not None


def resolve_acting_user_id(config: Any, user_id: Optional[str] = None) -> str:
    """The user a piece of work runs for, checked against the config it runs with.

    ``user_id=None`` takes the owner of a per-user view, else the primary user (CLI, bots, scheduled
    jobs). An explicit non-primary ``user_id`` must come with that user's own config view: running a
    user's work on the system config would spend the administrator's keys (and the reverse would read
    another user's settings), so the mismatch is refused instead of silently picking one side.
    """
    from src.config import get_primary_user_id

    primary = get_primary_user_id()
    owner = config_user_id(config)
    if user_id is None:
        return owner or primary
    if owner is None and user_id != primary:
        raise ValueError(f"user {user_id!r} must run with its own config view, not the system config")
    if owner is not None and owner != user_id:
        raise ValueError("config view belongs to a different user than the acting user")
    return user_id


def user_call_scope(config: Any) -> ContextManager[None]:
    """Context around the call that actually sends a model request: user views never follow redirects."""
    return forbid_httpx_redirects() if is_user_view(config) else contextlib.nullcontext()


# ---------------------------------------------------------------------------------------------- route policy


def _params_of(deployment: Any) -> Mapping[str, Any]:
    params = deployment.get("litellm_params") if isinstance(deployment, dict) else None
    return params if isinstance(params, dict) else {}


def wire_provider(model: str, api_base: str = "") -> str:
    """The LiteLLM provider a wire model resolves to ('' when LiteLLM cannot route it).

    An exact ``<provider>/...`` prefix decides (as in LiteLLM); a bare name is resolved by LiteLLM itself
    (``gemini-pro`` -> ``vertex_ai`` and similar), so the ambient-credential providers cannot hide behind a
    prefix-less model name. ``__legacy_<provider>__`` placeholders (env keys expanded into deployments) map
    to their provider.
    """
    text = str(model or "").strip()
    if not text:
        return ""
    legacy = _LEGACY_PLACEHOLDER_RE.match(text)
    if legacy:
        return legacy.group(1)
    from src.config import get_litellm_model_providers

    prefix, separator, _rest = text.partition("/")
    if separator and prefix in get_litellm_model_providers():
        return prefix
    try:
        import litellm

        return str(litellm.get_llm_provider(model=text, api_base=str(api_base or "").strip() or None)[1] or "").lower()
    except Exception:  # noqa: BLE001 - unroutable: refused by the caller
        return ""


#: LiteLLM replaces a deployment parameter whose value starts with this by the server's environment variable of that
#: name (``api_key: os.environ/ADMIN_KEY`` is sent to the endpoint as the administrator's key).
ENV_REFERENCE_PREFIX = "os.environ/"
# Keys of a direct call that carry the conversation / tool schema, not routing parameters: free text may contain anything.
_CALL_PAYLOAD_KEYS = frozenset({
    "messages", "tools", "functions", "tool_choice", "function_call", "response_format", "prompt", "input", "stop",
    "metadata", "extra_body", "caching", "logit_bias",
})


def reject_environment_references(params: Any, *, skip: frozenset = frozenset()) -> None:
    """Refuse a user-supplied parameter (any depth) that LiteLLM would expand to a server environment variable."""
    if isinstance(params, str):
        if ENV_REFERENCE_PREFIX in params.lower():
            raise ModelRouteNotAllowedError(
                "Model settings must not reference server environment variables (os.environ/...); enter the value itself"
            )
    elif isinstance(params, Mapping):
        for key, value in params.items():
            if key not in skip:
                reject_environment_references(value)
    elif isinstance(params, (list, tuple)):
        for value in params:
            reject_environment_references(value)


def check_user_model_route(
    model: str,
    api_base: str = "",
    api_key: str = "",
    *,
    resolve: bool = True,
) -> str:
    """Raise unless ``model`` / ``api_base`` / ``api_key`` is a route a non-primary user may use; returns the provider.

    ``resolve=False`` skips the DNS part of the endpoint check (the entry-point / call-time gates do that
    with :func:`validate_user_endpoints`); syntax, scheme and IP-literal checks always run.
    """
    reject_environment_references([model, api_base, api_key])
    provider = wire_provider(model, api_base)
    if provider not in USER_PROVIDER_DEFAULT_BASES and provider not in USER_OWN_ENDPOINT_PROVIDERS:
        raise ModelRouteNotAllowedError(
            "This model provider is not available for personal models (it uses the server's own credentials or "
            "local services); use a provider with your own API key, or an OpenAI-compatible endpoint of your own"
        )
    base = str(api_base or "").strip()
    if base:
        check_public_https_url(base, resolve=resolve)
    elif provider in USER_OWN_ENDPOINT_PROVIDERS:
        raise ModelRouteNotAllowedError(
            "This model provider needs a Base URL of your own (a public https endpoint); none is configured"
        )
    if not str(api_key or "").strip() and provider not in KEYLESS_PROVIDERS:
        raise ModelKeyRequiredError("A model channel has no API key of its own; add one in your personal settings")
    return provider


def harden_user_params(params: Dict[str, Any]) -> Dict[str, Any]:
    """Make ``params`` independent of the server environment (call after :func:`check_user_model_route`).

    Passes the provider's default public endpoint when no ``api_base`` is set (otherwise ``<PROVIDER>_API_BASE``
    / ``OPENAI_BASE_URL`` of the server could redirect the call) and a placeholder key for keyless Ollama
    (otherwise ``OLLAMA_API_KEY`` of the server would be sent to the user's host). Mutates and returns ``params``.
    """
    provider = wire_provider(params.get("model") or "", params.get("api_base") or "")
    if not str(params.get("api_base") or "").strip() and provider in USER_PROVIDER_DEFAULT_BASES:
        params["api_base"] = USER_PROVIDER_DEFAULT_BASES[provider]
    if not str(params.get("api_key") or "").strip() and provider in KEYLESS_PROVIDERS:
        params["api_key"] = KEYLESS_PLACEHOLDER_API_KEY
    return params


def validate_user_deployments(config: Any) -> None:
    """Every deployment of a user view must be an allowed route with the user's own key (see the module docstring)."""
    for deployment in getattr(config, "llm_model_list", None) or []:
        params = _params_of(deployment)
        reject_environment_references(params)
        check_user_model_route(
            params.get("model") or "", params.get("api_base") or "", params.get("api_key") or "", resolve=False,
        )


def harden_user_model_list(config: Any, model_list: Any) -> Any:
    """``model_list`` for a Router of ``config``: unchanged for the system config; a user view's deployments are
    route-checked and made environment-independent (:func:`harden_user_params`) on copies."""
    if not is_user_view(config):
        return model_list
    hardened: List[Any] = []
    for deployment in model_list or []:
        if not isinstance(deployment, dict):
            hardened.append(deployment)
            continue
        params = dict(_params_of(deployment))
        reject_environment_references(params)
        check_user_model_route(
            params.get("model") or "", params.get("api_base") or "", params.get("api_key") or "", resolve=False,
        )
        copy = dict(deployment)
        copy["litellm_params"] = harden_user_params(params)
        hardened.append(copy)
    return hardened


def validate_user_endpoints(config: Any) -> None:
    """Call-time SSRF re-check of the model endpoints a user configured (DNS may have changed since saving)."""
    seen = set()
    for deployment in getattr(config, "llm_model_list", None) or []:
        base = str(_params_of(deployment).get("api_base") or "").strip()
        if base and base not in seen:
            seen.add(base)
            assert_public_https_url(base)
    openai_base = str(getattr(config, "openai_base_url", "") or "").strip()
    if openai_base and openai_base not in seen:
        assert_public_https_url(openai_base)


def require_explicit_credentials(config: Any, call_kwargs: Dict[str, Any], *, model: str = "") -> None:
    """Last line before a direct ``litellm.completion``: a user view must send an allowed route with its own key.

    Router-backed calls are safe because the Router only holds the hardened deployments of the user's view
    (:func:`validate_user_deployments` / :func:`harden_user_model_list`); a direct call has nothing but
    ``call_kwargs``. For a user view this checks the route (including a DNS re-check of ``api_base``) and
    completes ``call_kwargs`` in place (:func:`harden_user_params`); the system config is not touched.
    """
    if not is_user_view(config):
        return
    wire_model = str(call_kwargs.get("model") or model or "")
    reject_environment_references(call_kwargs, skip=_CALL_PAYLOAD_KEYS)
    probe = {"model": wire_model, "api_base": call_kwargs.get("api_base") or "", "api_key": call_kwargs.get("api_key") or ""}
    try:
        check_user_model_route(wire_model, probe["api_base"], probe["api_key"], resolve=True)
    except ModelRouteNotAllowedError:
        raise
    except ModelKeyRequiredError as exc:
        raise ModelKeyRequiredError(
            f"No API key of your own for model {wire_model}; the system key is not available to personal accounts"
        ) from exc
    harden_user_params(probe)
    for name in ("api_base", "api_key"):
        if probe[name]:
            call_kwargs[name] = probe[name]


__all__ = [
    "KEYLESS_PLACEHOLDER_API_KEY",
    "KEYLESS_PROVIDERS",
    "MODEL_KEY_REQUIRED",
    "MODEL_ROUTE_NOT_ALLOWED",
    "ModelKeyRequiredError",
    "ModelRouteNotAllowedError",
    "ENV_REFERENCE_PREFIX",
    "USER_OWN_ENDPOINT_PROVIDERS",
    "USER_PROVIDER_DEFAULT_BASES",
    "UnsafeUrlError",
    "check_user_model_route",
    "config_user_id",
    "harden_user_model_list",
    "harden_user_params",
    "is_user_view",
    "reject_environment_references",
    "require_explicit_credentials",
    "resolve_acting_user_id",
    "user_call_scope",
    "validate_user_deployments",
    "validate_user_endpoints",
    "wire_provider",
]
