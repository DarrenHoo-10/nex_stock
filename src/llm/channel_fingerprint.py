# -*- coding: utf-8 -*-
"""Model channel fingerprint (docs/design/multi-user.md section 7).

A shared analysis report may be reused for another user only when it came from the same model *channel*:
provider + Base URL + model name. The Base URL is part of the identity because a user can point a deployment at
an unofficial endpoint; comparing the model name alone would let a report generated through such an endpoint
pass for the same-named official model. The digest never contains an API key.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit, urlunsplit


def normalize_base_url(value: Any) -> str:
    """Lower-case scheme and host, drop default port, query, fragment and trailing slash; empty = provider default."""
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text if "://" in text else f"https://{text}")
    except ValueError:
        return text.rstrip("/").lower()
    host = (parts.hostname or "").lower()
    port = parts.port
    default_port = {"http": 80, "https": 443}.get(parts.scheme.lower())
    netloc = host if port in (None, default_port) else f"{host}:{port}"
    return urlunsplit((parts.scheme.lower(), netloc, parts.path.rstrip("/"), "", ""))


def _split_wire_model(model: str) -> Tuple[str, str]:
    provider, sep, name = str(model or "").strip().partition("/")
    if not sep:
        return "", provider
    return provider.lower(), name


def _is_gateway_prefix(provider: str) -> bool:
    """True for a prefix that names no LiteLLM provider (``auto/deepseek`` gateway groups)."""
    if not provider:
        return False
    try:
        from src.config import get_litellm_model_providers

        providers = get_litellm_model_providers()
    except Exception:  # noqa: BLE001 - the provider list is best effort
        return False
    return bool(providers) and provider not in providers


def _matching_deployments(model: str, model_list: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    matches: List[Dict[str, Any]] = []
    for entry in model_list or []:
        if not isinstance(entry, dict):
            continue
        params = entry.get("litellm_params") if isinstance(entry.get("litellm_params"), dict) else {}
        if model in (str(entry.get("model_name") or "").strip(), str(params.get("model") or "").strip()):
            matches.append(params)
    return matches


def channel_identity(
    model: Optional[str],
    model_list: Optional[List[Dict[str, Any]]],
    declared_base_urls: Optional[Mapping[str, Any]] = None,
) -> Optional[Tuple[str, str, str]]:
    """``(provider, base_url, model_name)`` of ``model`` as routed by ``model_list``; ``None`` when unknown.

    A router alias resolves to the wire model of its deployment(s); if several deployments (keys) of one alias
    point at different Base URLs, they are all part of the identity (sorted, so key order never matters).

    A model that no deployment routes (a user who declared the model and endpoint but has not saved a key yet:
    channels without a key are dropped when the config is built) falls back to the declared Base URL for its
    provider, ``declared_base_urls`` (``{"openai": OPENAI_BASE_URL, ...}``), so the very same channel gets the very
    same fingerprint with or without a key. A prefix that is not a provider (a gateway group such as
    ``auto/x``) is routed through the OpenAI-compatible endpoint, like the router does.
    """
    name = str(model or "").strip()
    if not name:
        return None
    deployments = _matching_deployments(name, model_list or [])
    if not deployments:
        provider, model_name = _split_wire_model(name)
        if provider not in {"openai", "ollama", "ollama_chat"} and _is_gateway_prefix(provider):
            provider, model_name = "openai", name
        declared = (declared_base_urls or {}).get(provider if provider != "ollama_chat" else "ollama")
        return provider, normalize_base_url(declared), model_name
    wire_models = sorted({str(params.get("model") or name).strip() for params in deployments}) or [name]
    bases = sorted({normalize_base_url(params.get("api_base")) for params in deployments}) or [""]
    providers = set()
    model_names = set()
    for wire in wire_models:
        provider, model_name = _split_wire_model(wire)
        providers.add(provider)
        model_names.add(model_name)
    return "+".join(sorted(providers)), "+".join(bases), "+".join(sorted(model_names))


def compute_model_channel_fingerprint(
    model: Optional[str],
    model_list: Optional[List[Dict[str, Any]]],
    declared_base_urls: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Stable digest (hex, 32 chars) of the channel behind ``model``; ``None`` when there is no model to identify."""
    identity = channel_identity(model, model_list, declared_base_urls)
    if identity is None:
        return None
    return hashlib.sha256("\x1f".join(identity).encode("utf-8")).hexdigest()[:32]


def config_model_channel_fingerprint(config: Any, model: Optional[str] = None) -> Optional[str]:
    """Fingerprint of the model ``config`` generates analysis reports with.

    ``model`` defaults to the configured primary model (the Agent primary when ``AGENT_MODE`` is on). Local CLI
    generation backends have no inspectable channel and return ``None`` (their reports are never reused).
    """
    from src.config import get_effective_agent_primary_model
    from src.llm.backend_registry import LOCAL_CLI_GENERATION_BACKEND_IDS

    if (getattr(config, "generation_backend", "") or "").strip().lower() in LOCAL_CLI_GENERATION_BACKEND_IDS:
        return None
    if model is None:
        if getattr(config, "agent_mode", False):
            model = get_effective_agent_primary_model(config)
        else:
            model = getattr(config, "litellm_model", "") or ""
    declared = {
        "openai": getattr(config, "openai_base_url", None),
        "ollama": getattr(config, "ollama_api_base", None),
    }
    return compute_model_channel_fingerprint(model, getattr(config, "llm_model_list", None) or [], declared)


__all__ = [
    "channel_identity",
    "compute_model_channel_fingerprint",
    "config_model_channel_fingerprint",
    "normalize_base_url",
]
