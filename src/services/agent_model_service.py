# -*- coding: utf-8 -*-
"""Helpers for exposing configured Agent model deployments."""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional

from src.config import get_effective_agent_models_to_try, get_effective_agent_primary_model
from src.agent.litellm_route_resolution import resolve_agent_litellm_route
from src.llm.backend_registry import GENERATION_ONLY_BACKEND_IDS
from src.llm.generation_params import (
    REASONING_EFFORT_VALUES,
    normalize_reasoning_effort,
    resolve_litellm_wire_model,
)


_PLACEHOLDER_TO_PROVIDER = {
    "__legacy_gemini__": "gemini",
    "__legacy_anthropic__": "anthropic",
    "__legacy_openai__": "openai",
    "__legacy_deepseek__": "deepseek",
}
_MANAGED_LEGACY_PROVIDERS = set(_PLACEHOLDER_TO_PROVIDER.values())


def _get_models_source(config) -> str:
    source = getattr(config, "llm_models_source", "")
    if source in {"litellm_config", "llm_channels", "legacy_env"}:
        return source
    return "legacy_env"


def _get_model_provider(model_name: str) -> str:
    if not model_name:
        return "unknown"
    if "/" in model_name:
        return model_name.split("/", 1)[0]
    return "openai"


def _build_non_legacy_deployments(config) -> List[Dict[str, Any]]:
    source = _get_models_source(config)
    resolution = resolve_agent_litellm_route(config)
    primary_model = resolution.primary_model or get_effective_agent_primary_model(config)
    fallback_models = set((resolution.models_to_try or [])[1:])
    model_list = resolution.model_list
    deployments: List[Dict[str, Any]] = []

    for index, entry in enumerate(model_list):
        params = entry.get("litellm_params", {}) or {}
        model_name = str(params.get("model") or "").strip()
        if not model_name or model_name.startswith("__legacy_"):
            continue

        api_base = params.get("api_base")
        deployment_name = entry.get("model_name")
        route_names = {model_name, str(deployment_name).strip() if deployment_name else model_name}
        deployments.append(
            {
                "deployment_id": f"{source}:{index}",
                "model": model_name,
                "provider": _get_model_provider(model_name),
                "source": source,
                "api_base": str(api_base).strip() if api_base else None,
                "deployment_name": str(deployment_name).strip() if deployment_name else None,
                # Router routing alias (model_name); what a chat request sends back as ``model``.
                "route_model": str(deployment_name).strip() if deployment_name else model_name,
                "is_primary": primary_model in route_names,
                "is_fallback": bool(route_names & fallback_models),
            }
        )

    return deployments


def _build_legacy_deployments(config) -> List[Dict[str, Any]]:
    primary_model = get_effective_agent_primary_model(config)
    ordered_models = get_effective_agent_models_to_try(config)
    if not ordered_models:
        return []

    placeholder_counts = {provider: 0 for provider in _PLACEHOLDER_TO_PROVIDER.values()}
    for entry in getattr(config, "llm_model_list", []) or []:
        provider = _PLACEHOLDER_TO_PROVIDER.get(entry.get("model_name"))
        if provider:
            placeholder_counts[provider] += 1

    deployments: List[Dict[str, Any]] = []
    seen_models = set()
    fallback_set = set(ordered_models[1:])
    for model_name in ordered_models:
        if model_name in seen_models:
            continue
        seen_models.add(model_name)

        provider = _get_model_provider(model_name)
        deployment_count = placeholder_counts.get(provider, 0)
        if deployment_count <= 0:
            # Legacy runtime still supports direct litellm calls for providers
            # whose credentials/base are resolved from environment variables
            # instead of managed placeholder deployments.
            if provider in _MANAGED_LEGACY_PROVIDERS:
                continue
            deployment_count = 1

        api_base = getattr(config, "openai_base_url", None) if provider == "openai" else None
        # Legacy runtime only load-balances the primary model via Router.
        # Fallback models call litellm directly with the first configured key,
        # so they expose at most one reachable deployment per model.
        if model_name == primary_model:
            deployment_indexes = range(deployment_count)
        else:
            deployment_indexes = range(1)

        for index in deployment_indexes:
            deployments.append(
                {
                    "deployment_id": f"legacy:{provider}:{index}:{model_name}",
                    "model": model_name,
                    "provider": provider,
                    "source": "legacy_env",
                    "api_base": api_base,
                    "deployment_name": f"legacy_{provider}_{index + 1}",
                    "route_model": model_name,
                    "is_primary": model_name == primary_model,
                    "is_fallback": model_name in fallback_set,
                }
            )

    return deployments


def list_agent_model_deployments(config) -> List[Dict[str, Any]]:
    """Return configured Agent model deployments without exposing secrets."""
    if (getattr(config, "agent_generation_backend", "") or "").strip().lower() in GENERATION_ONLY_BACKEND_IDS:
        return []

    deployments = _build_non_legacy_deployments(config)
    if not deployments:
        deployments = _build_legacy_deployments(config)

    return sorted(
        deployments,
        key=lambda item: (
            not item["is_primary"],
            not item["is_fallback"],
            item["source"],
            item["model"],
            item["api_base"] or "",
            item["deployment_name"] or "",
            item["deployment_id"],
        ),
    )


class AgentChatOverrideError(ValueError):
    """A per-request chat model / reasoning effort that this deployment does not offer."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def list_agent_selectable_models(config) -> List[str]:
    """Routing names a chat request may pick as ``model`` (deduped, primary first)."""
    seen: set = set()
    models: List[str] = []
    for item in list_agent_model_deployments(config):
        name = item.get("route_model") or item.get("model")
        if name and name not in seen:
            seen.add(name)
            models.append(name)
    return models


def apply_agent_chat_overrides(
    config,
    *,
    model: Optional[str] = None,
    reasoning_effort: Optional[str] = None,
):
    """Return ``config`` itself, or a shallow copy carrying this request's Agent model / reasoning effort.

    ``model`` must be one of :func:`list_agent_selectable_models`; ``reasoning_effort`` one of
    ``REASONING_EFFORT_VALUES`` or ``"none"`` (send no reasoning effort for this request). Blank means
    "use the configured value". The shared runtime config object is never mutated.
    """
    requested_model = (model or "").strip()
    requested_effort = (reasoning_effort or "").strip().lower()
    if not requested_model and not requested_effort:
        return config

    overrides: Dict[str, Any] = {}
    if requested_model:
        if requested_model not in list_agent_selectable_models(config):
            raise AgentChatOverrideError(
                "model_not_available",
                f"Model {requested_model!r} is not a configured Agent model",
            )
        overrides["agent_litellm_model"] = requested_model
    if requested_effort:
        if requested_effort == "none":
            # AGENT_REASONING_EFFORT empty would inherit LLM_REASONING_EFFORT, so clear both for this request.
            overrides["agent_reasoning_effort"] = ""
            overrides["llm_reasoning_effort"] = ""
        elif normalize_reasoning_effort(requested_effort):
            overrides["agent_reasoning_effort"] = requested_effort
        else:
            raise AgentChatOverrideError(
                "invalid_reasoning_effort",
                "reasoning_effort must be one of none, " + ", ".join(REASONING_EFFORT_VALUES),
            )

    overridden = copy.copy(config)
    for key, value in overrides.items():
        object.__setattr__(overridden, key, value)
    return overridden


def resolve_model_context_window(config, model: Optional[str] = None) -> Optional[int]:
    """Max input tokens LiteLLM knows for ``model`` (default: the effective Agent model); ``None`` when unknown."""
    name = (model or "").strip() or get_effective_agent_primary_model(config)
    if not name:
        return None
    try:
        import litellm

        wire_model = resolve_litellm_wire_model(name, getattr(config, "llm_model_list", None) or None)
        info = litellm.get_model_info(wire_model)
        value = info.get("max_input_tokens") or info.get("max_tokens")
        return int(value) if value else None
    except Exception:
        return None
