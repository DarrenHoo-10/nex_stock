# -*- coding: utf-8 -*-
"""No project-side output cap, the responding-model log, and the standard ``reasoning_effort`` setting.

Everything is mocked at the LiteLLM boundary (``call_litellm_with_param_recovery`` / ``_dispatch_litellm_completion`` /
``litellm.completion``); no request leaves the process.
"""

import json
import logging
import os
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    from tests.litellm_stub import ensure_litellm_stub

    ensure_litellm_stub()

import litellm as _litellm

from src.agent import llm_adapter
from src.analyzer import GeminiAnalyzer, _describe_responding_model
from src.config import (
    Config,
    get_agent_reasoning_effort,
    get_light_reasoning_effort,
    get_primary_reasoning_effort,
)
from src.core.config_registry import get_field_definition, get_registered_field_keys
from src.llm import generation_params
from src.llm.errors import classify_litellm_generation_param_error
from src.llm.generation_backend import GenerationResult
from src.llm.generation_params import (
    build_litellm_reasoning_params,
    clear_litellm_generation_param_recovery_cache,
    normalize_reasoning_effort,
    resolve_required_max_tokens,
)
from src.market_analyzer import MarketAnalyzer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REASONING_KEYS = ("LLM_REASONING_EFFORT", "AGENT_REASONING_EFFORT", "LITELLM_LIGHT_REASONING_EFFORT")


def _load_config(env):
    with patch("src.config.setup_env"), patch.object(Config, "_parse_litellm_yaml", return_value=[]), \
            patch.object(Config, "_parse_stock_email_groups", return_value=[]), \
            patch.dict(os.environ, env, clear=True):
        return Config._load_from_env()


class ConfigTestCase(unittest.TestCase):
    def tearDown(self):
        Config.reset_instance()

    def test_defaults_send_nothing(self):
        config = _load_config({})
        self.assertEqual(
            (config.llm_reasoning_effort, config.agent_reasoning_effort, config.litellm_light_reasoning_effort),
            ("", "", ""),
        )

    def test_values_are_normalised_and_invalid_values_are_not_sent(self):
        config = _load_config({
            "LLM_REASONING_EFFORT": " High ", "AGENT_REASONING_EFFORT": "MAX", "LITELLM_LIGHT_REASONING_EFFORT": "low",
        })
        self.assertEqual(config.llm_reasoning_effort, "high")
        self.assertEqual(config.agent_reasoning_effort, "max")
        self.assertEqual(config.litellm_light_reasoning_effort, "low")
        with self.assertLogs("src.config", level=logging.WARNING) as logs:
            bad = _load_config({"LLM_REASONING_EFFORT": "turbo", "LITELLM_LIGHT_REASONING_EFFORT": "none"})
        self.assertEqual((bad.llm_reasoning_effort, bad.litellm_light_reasoning_effort), ("", ""))
        self.assertIn("LLM_REASONING_EFFORT", "\n".join(logs.output))

    def test_every_documented_value_is_accepted(self):
        for value in ("low", "medium", "high", "xhigh", "max"):
            self.assertEqual(_load_config({"LLM_REASONING_EFFORT": value}).llm_reasoning_effort, value)

    def test_agent_effort_follows_the_primary_effort_when_empty(self):
        config = SimpleNamespace(llm_reasoning_effort="high", agent_reasoning_effort="", litellm_light_reasoning_effort="")
        self.assertEqual(get_agent_reasoning_effort(config), "high")
        config.agent_reasoning_effort = "low"
        self.assertEqual(get_agent_reasoning_effort(config), "low")
        self.assertEqual(get_primary_reasoning_effort(config), "high")
        self.assertEqual(get_light_reasoning_effort(config), "")  # light never inherits

    def test_resolvers_tolerate_partial_or_mocked_configs(self):
        for config in (SimpleNamespace(), MagicMock(), None):
            self.assertEqual(get_primary_reasoning_effort(config), "")
            self.assertEqual(get_agent_reasoning_effort(config), "")
            self.assertEqual(get_light_reasoning_effort(config), "")

    def test_no_output_token_setting_exists(self):
        self.assertFalse(hasattr(_load_config({}), "llm_max_output_tokens"))
        self.assertNotIn("LLM_MAX_OUTPUT_TOKENS", get_registered_field_keys())


class RegistryTestCase(unittest.TestCase):
    def test_registry_entries(self):
        for key in REASONING_KEYS:
            field = get_field_definition(key)
            self.assertEqual(field["category"], "ai_model", key)
            self.assertEqual(field["ui_control"], "select", key)
            self.assertEqual(field["data_type"], "string", key)
            self.assertEqual(field["default_value"], "", key)
            self.assertEqual(
                [option["value"] for option in field["options"]], ["", "low", "medium", "high", "xhigh", "max"], key)
            self.assertEqual(field["validation"], {"enum": ["", "low", "medium", "high", "xhigh", "max"]}, key)
            self.assertEqual(field["help_key"], f"settings.ai_model.{key}")
            self.assertTrue(field["is_editable"] and not field["is_sensitive"], key)
            self.assertTrue(field["examples"] and field["docs"], key)

    def test_docs_env_example_and_help_cover_every_key(self):
        def read(*parts):
            with open(os.path.join(ROOT, *parts), encoding="utf-8") as fh:
                return fh.read()

        env_example = read(".env.example")
        guide_zh, guide_en = read("docs", "full-guide.md"), read("docs", "full-guide_EN.md")
        help_ts = read("apps", "dsa-web", "src", "locales", "settingsHelp.ts")
        for key in REASONING_KEYS:
            self.assertIn(f"# {key}=", env_example)
            self.assertIn(f"| `{key}` |", guide_zh)
            self.assertIn(f"| `{key}` |", guide_en)
            self.assertEqual(help_ts.count(f"'settings.ai_model.{key}'"), 2, key)  # zh + en
        self.assertNotIn("LLM_MAX_OUTPUT_TOKENS", env_example + guide_zh + guide_en + help_ts)


class ReasoningParamHelperTestCase(unittest.TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_reasoning_effort(" XHigh "), "xhigh")
        for bad in (None, "", "none", "minimal", "turbo", 3):
            self.assertEqual(normalize_reasoning_effort(bad), "")

    def test_unset_or_invalid_effort_adds_nothing(self):
        self.assertEqual(build_litellm_reasoning_params("openai/x", ""), {})
        self.assertEqual(build_litellm_reasoning_params("openai/x", None), {})
        self.assertEqual(build_litellm_reasoning_params("openai/x", "turbo"), {})

    def test_unmapped_openai_compatible_model_opts_in_so_litellm_forwards_the_parameter(self):
        with patch.object(generation_params, "_litellm_supports_openai_param", return_value=False):
            params = build_litellm_reasoning_params("openai/darren-cpa/claude-haiku-5-5", "max")
        self.assertEqual(params, {"reasoning_effort": "max", "allowed_openai_params": ["reasoning_effort"]})
        with patch.object(generation_params, "_litellm_supports_openai_param", return_value=None):  # cannot tell
            self.assertIn("allowed_openai_params", build_litellm_reasoning_params("openai/unknown", "high"))

    def test_natively_mapped_model_gets_the_plain_parameter(self):
        with patch.object(generation_params, "_litellm_supports_openai_param", return_value=True):
            self.assertEqual(build_litellm_reasoning_params("anthropic/claude-x", "high"), {"reasoning_effort": "high"})

    def test_support_probe_asks_litellm_about_the_wire_model_of_a_router_alias(self):
        seen = []

        def _probe(wire_model, param):
            seen.append((wire_model, param))
            return True

        model_list = [{"model_name": "alias", "litellm_params": {"model": "openai/gateway/model-a"}}]
        with patch.object(generation_params, "_litellm_supports_openai_param", side_effect=_probe):
            build_litellm_reasoning_params("alias", "low", model_list=model_list)
        self.assertEqual(seen, [("openai/gateway/model-a", "reasoning_effort")])

    def test_support_probe_uses_litellm_provider_params(self):
        with patch.object(_litellm, "get_llm_provider", create=True, return_value=("m", "openai", None, None)), \
                patch.object(_litellm, "get_supported_openai_params", create=True, return_value=["temperature"]):
            self.assertFalse(generation_params._litellm_supports_openai_param("openai/m", "reasoning_effort"))
        with patch.object(_litellm, "get_llm_provider", create=True, return_value=("m", "openai", None, None)), \
                patch.object(_litellm, "get_supported_openai_params", create=True, return_value=["reasoning_effort"]):
            self.assertTrue(generation_params._litellm_supports_openai_param("openai/m", "reasoning_effort"))
        with patch.object(_litellm, "get_llm_provider", create=True, side_effect=RuntimeError("unmapped")):
            self.assertIsNone(generation_params._litellm_supports_openai_param("openai/m", "reasoning_effort"))

    def test_unsupported_parameter_error_is_classified_as_a_one_shot_recovery(self):
        for message in (
            "openai does not support parameters: ['reasoning_effort'], for model=x",
            "Unsupported parameter: 'reasoning_effort' is not supported with this model.",
            "Unrecognized request argument supplied: reasoning_effort",
        ):
            recovery = classify_litellm_generation_param_error(RuntimeError(message))
            self.assertIsNotNone(recovery, message)
            self.assertEqual(set(recovery.omit_params), {"reasoning_effort", "allowed_openai_params"})
            self.assertEqual(recovery.reason, "reasoning_effort_unsupported")
        self.assertIsNone(classify_litellm_generation_param_error(RuntimeError("rate limit exceeded")))


class RequiredMaxTokensTestCase(unittest.TestCase):
    def test_native_anthropic_uses_the_models_own_maximum(self):
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_output_tokens": 64000}) as info:
            self.assertEqual(resolve_required_max_tokens("anthropic/claude-sonnet-x"), 64000)
        info.assert_called_once_with(model="anthropic/claude-sonnet-x")

    def test_falls_back_to_max_tokens_info_and_to_nothing_for_unknown_models(self):
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_tokens": 8192}):
            self.assertEqual(resolve_required_max_tokens("anthropic/claude-old"), 8192)
        with patch.object(_litellm, "get_model_info", create=True, side_effect=Exception("not mapped")):
            self.assertIsNone(resolve_required_max_tokens("anthropic/claude-future"))  # LiteLLM applies its own default
        with patch.object(_litellm, "get_model_info", create=True, return_value={}):
            self.assertIsNone(resolve_required_max_tokens("anthropic/claude-future"))

    def test_openai_compatible_routes_never_get_a_cap_even_for_claude_named_models(self):
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_output_tokens": 64000}) as info:
            self.assertIsNone(resolve_required_max_tokens("openai/darren-cpa/claude-opus-5-5"))
            self.assertIsNone(resolve_required_max_tokens("openai/gpt-x"))
            self.assertIsNone(resolve_required_max_tokens("gemini/gemini-x"))
        info.assert_not_called()

    def test_router_alias_is_resolved_to_its_wire_model(self):
        model_list = [{"model_name": "alias", "litellm_params": {"model": "anthropic/claude-x"}}]
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_output_tokens": 32000}):
            self.assertEqual(resolve_required_max_tokens("alias", model_list=model_list), 32000)


def _analyzer_config(**overrides):
    values = dict(
        generation_backend="litellm", generation_fallback_backend="litellm", litellm_model="openai/primary",
        litellm_fallback_models=[], llm_model_list=[], report_language="en", gemini_request_delay=0,
        llm_temperature=0.7, report_integrity_enabled=False, report_integrity_retry=0, news_max_age_days=3,
        news_strategy_profile="short",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _make_analyzer(**overrides):
    analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
    analyzer._router = None
    analyzer._litellm_available = True
    analyzer._skill_instructions_override = ""
    analyzer._default_skill_policy_override = ""
    analyzer._use_legacy_default_prompt_override = True
    analyzer._config_override = _analyzer_config(**overrides)
    return analyzer


def _context():
    return {
        "code": "AAPL", "stock_name": "Apple", "date": "2026-03-16",
        "today": {"close": 10.5, "open": 10.1, "high": 10.8, "low": 10.0, "pct_chg": 1.2,
                  "volume": 123456789, "amount": 987654321, "ma5": 10.2, "ma10": 10.0, "ma20": 9.8},
        "ma_status": "bullish",
    }


_OK_JSON = json.dumps({
    "stock_name": "Apple", "sentiment_score": 60, "trend_prediction": "up", "operation_advice": "hold",
    "analysis_summary": "ok",
})


def _ok_response(text="ok"):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))], usage=None)


class AnalysisCallTestCase(unittest.TestCase):
    def test_analysis_call_sets_no_output_cap(self):
        analyzer = _make_analyzer()
        captured = {}

        def _capture(prompt, generation_config, **kwargs):
            captured.update(generation_config)
            raise RuntimeError("stop after capturing generation config")

        with patch.object(analyzer, "_call_litellm", side_effect=_capture):
            analyzer.analyze(_context(), news_context="headline")
        self.assertEqual(captured, {"temperature": 0.7})  # no max_output_tokens / max_tokens

    def _kwargs_of_impl(self, generation_config=None, *, model="openai/primary", config=None, fallbacks=()):
        cfg = config or _analyzer_config(litellm_model=model, litellm_fallback_models=list(fallbacks))
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._router = None
        analyzer._litellm_available = True
        analyzer._config_override = cfg
        seen = []

        def _fake_recovery(call, **kwargs):
            seen.append(dict(kwargs["call_kwargs"]))
            return _ok_response()

        with patch("src.analyzer.call_litellm_with_param_recovery", side_effect=_fake_recovery), \
                patch.object(generation_params, "_litellm_supports_openai_param", return_value=False):
            analyzer._call_litellm_impl("prompt", generation_config or {"temperature": 0.7})
        return seen

    def test_default_request_carries_no_max_tokens_for_openai_compatible_routes(self):
        [kwargs] = self._kwargs_of_impl()
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("max_output_tokens", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)  # unset: nothing is sent
        self.assertNotIn("allowed_openai_params", kwargs)

    def test_explicit_caller_budget_is_still_honoured(self):
        [kwargs] = self._kwargs_of_impl({"temperature": 0.7, "max_tokens": 128})
        self.assertEqual(kwargs["max_tokens"], 128)
        [kwargs] = self._kwargs_of_impl({"temperature": 0.7, "max_output_tokens": 256})
        self.assertEqual(kwargs["max_tokens"], 256)

    def test_native_anthropic_gets_the_models_own_maximum_not_a_project_cap(self):
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_output_tokens": 64000}):
            [kwargs] = self._kwargs_of_impl(model="anthropic/claude-sonnet-x")
        self.assertEqual(kwargs["max_tokens"], 64000)
        with patch.object(_litellm, "get_model_info", create=True, side_effect=Exception("not mapped")):
            [kwargs] = self._kwargs_of_impl(model="anthropic/claude-future")
        self.assertNotIn("max_tokens", kwargs)  # unknown model: LiteLLM applies its own default

    def test_primary_effort_is_sent_with_the_opt_in_for_unmapped_models(self):
        cfg = _analyzer_config(litellm_model="openai/darren-cpa/claude-opus-5-5", llm_reasoning_effort="max")
        [kwargs] = self._kwargs_of_impl(config=cfg)
        self.assertEqual(kwargs["reasoning_effort"], "max")
        self.assertEqual(kwargs["allowed_openai_params"], ["reasoning_effort"])
        self.assertEqual(kwargs["model"], "openai/darren-cpa/claude-opus-5-5")  # the model name is never rewritten

    def test_fallback_models_use_the_same_primary_effort(self):
        cfg = _analyzer_config(litellm_model="openai/a", litellm_fallback_models=["openai/b"], llm_reasoning_effort="high")
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._router = None
        analyzer._litellm_available = True
        analyzer._config_override = cfg
        seen = []

        def _fake_recovery(call, **kwargs):
            seen.append((kwargs["call_kwargs"]["model"], kwargs["call_kwargs"].get("reasoning_effort")))
            if kwargs["call_kwargs"]["model"] == "openai/a":
                raise RuntimeError("primary down")
            return _ok_response()

        with patch("src.analyzer.call_litellm_with_param_recovery", side_effect=_fake_recovery):
            analyzer._call_litellm_impl("prompt", {"temperature": 0.7})
        self.assertEqual(seen, [("openai/a", "high"), ("openai/b", "high")])

    def test_per_call_override_wins_for_light_chores(self):
        cfg = _analyzer_config(llm_reasoning_effort="high")
        [kwargs] = self._kwargs_of_impl({"temperature": 0.1, "reasoning_effort": "low"}, config=cfg)
        self.assertEqual(kwargs["reasoning_effort"], "low")
        [kwargs] = self._kwargs_of_impl({"temperature": 0.1, "reasoning_effort": None}, config=cfg)
        self.assertNotIn("reasoning_effort", kwargs)  # an explicit None sends nothing, it does not inherit

    def test_unsupported_reasoning_effort_is_retried_once_without_it(self):
        clear_litellm_generation_param_recovery_cache()
        self.addCleanup(clear_litellm_generation_param_recovery_cache)
        cfg = _analyzer_config(litellm_model="openai/gateway/model-x", llm_reasoning_effort="high")
        analyzer = GeminiAnalyzer.__new__(GeminiAnalyzer)
        analyzer._router = None
        analyzer._litellm_available = True
        analyzer._config_override = cfg
        attempts = []

        def _dispatch(model, kwargs, **_):
            attempts.append(dict(kwargs))
            if "reasoning_effort" in kwargs:
                raise RuntimeError("Unsupported parameter: 'reasoning_effort' is not supported by this endpoint")
            return _ok_response("answer")

        with patch.object(analyzer, "_dispatch_litellm_completion", side_effect=_dispatch), \
                patch.object(generation_params, "_litellm_supports_openai_param", return_value=False), \
                self.assertLogs("src.analyzer", level=logging.WARNING) as logs:
            text, model_used, _usage = analyzer._call_litellm_impl("prompt", {"temperature": 0.7})
        self.assertEqual((text, model_used), ("answer", "openai/gateway/model-x"))
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", attempts[1])
        self.assertNotIn("allowed_openai_params", attempts[1])
        self.assertIn("reasoning_effort_unsupported", "\n".join(logs.output))

        # the repair is remembered for this route: the next call does not send it at all
        attempts.clear()
        with patch.object(analyzer, "_dispatch_litellm_completion", side_effect=_dispatch), \
                patch.object(generation_params, "_litellm_supports_openai_param", return_value=False):
            analyzer._call_litellm_impl("prompt", {"temperature": 0.7})
        self.assertEqual(len(attempts), 1)
        self.assertNotIn("reasoning_effort", attempts[0])


class TextHelperTestCase(unittest.TestCase):
    def test_generate_text_helpers_send_no_cap_by_default(self):
        analyzer = _make_analyzer()
        with patch.object(analyzer, "_call_litellm", return_value="text") as call:
            self.assertEqual(analyzer.generate_text("prompt"), "text")
        self.assertEqual(call.call_args.kwargs["generation_config"], {"temperature": 0.7})

        with patch.object(analyzer, "_call_litellm", return_value="text") as call:
            analyzer.generate_text("prompt", max_tokens=512, temperature=0.2)
        self.assertEqual(call.call_args.kwargs["generation_config"], {"temperature": 0.2, "max_tokens": 512})

    def test_generate_text_with_metadata_sends_no_cap_by_default(self):
        analyzer = _make_analyzer()
        result = GenerationResult(text="text", provider="litellm", model="m", backend="litellm", usage={})
        with patch.object(analyzer, "_call_litellm", return_value=result) as call, \
                patch("src.analyzer.should_persist_usage_telemetry", return_value=False):
            self.assertIs(analyzer.generate_text_with_metadata("prompt"), result)
        self.assertEqual(call.call_args.kwargs["generation_config"], {"temperature": 0.7})

    def test_generate_text_with_models_pins_models_and_carries_the_callers_effort(self):
        analyzer = _make_analyzer(llm_reasoning_effort="high")
        usage = {}
        with patch.object(analyzer, "_call_litellm", return_value=("text", "m", usage)) as call, \
                patch("src.analyzer.should_persist_usage_telemetry", return_value=False):
            analyzer.generate_text_with_models("prompt", models=["openai/light"], reasoning_effort="low")
            analyzer.generate_text_with_models("prompt", models=["openai/light"])
        first, second = (c.args[1] for c in call.call_args_list)
        self.assertEqual(first, {"temperature": 0.1, "reasoning_effort": "low", "models": ["openai/light"]})
        self.assertEqual(second, {"temperature": 0.1, "reasoning_effort": None, "models": ["openai/light"]})


class RespondingModelLogTestCase(unittest.TestCase):
    def test_describe_helper(self):
        self.assertEqual(_describe_responding_model("a/primary", "a/primary"), "a/primary")
        self.assertEqual(_describe_responding_model("a/primary", None), "a/primary")
        self.assertEqual(_describe_responding_model("", ""), "unknown")
        text = _describe_responding_model("a/primary", "b/fallback")
        self.assertIn("b/fallback", text)
        self.assertIn("a/primary", text)
        self.assertIn("备用模型", text)

    def _analyze_logging(self, model_used):
        analyzer = _make_analyzer(litellm_model="openai/primary", litellm_fallback_models=["openai/fallback"])
        usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
        with patch.object(analyzer, "_call_litellm", return_value=(_OK_JSON, model_used, usage)), \
                patch("src.analyzer.persist_llm_usage"):
            with self.assertLogs("src.analyzer", level=logging.INFO) as logs:
                analyzer.analyze(_context(), news_context="headline")
        return [line for line in logs.output if "[LLM返回]" in line and "预览" not in line]

    def test_log_reports_fallback_model_when_it_answered(self):
        lines = self._analyze_logging("openai/fallback")
        self.assertEqual(len(lines), 1)
        self.assertIn("openai/fallback", lines[0])
        self.assertIn("备用模型", lines[0])
        self.assertIn("openai/primary", lines[0])

    def test_log_reports_primary_model_plainly(self):
        lines = self._analyze_logging("openai/primary")
        self.assertEqual(len(lines), 1)
        self.assertIn("openai/primary 响应成功", lines[0])
        self.assertNotIn("备用模型", lines[0])


class MarketReviewTestCase(unittest.TestCase):
    def _review(self, analyzer_obj):
        reviewer = MarketAnalyzer.__new__(MarketAnalyzer)
        reviewer.config = SimpleNamespace()
        reviewer.analyzer = analyzer_obj
        return reviewer._generate_market_review_with_metadata("prompt", provider="litellm", model="m")

    def test_metadata_path_sends_no_max_tokens(self):
        calls = {}

        class _Analyzer:
            def generate_text_with_metadata(self, prompt, **kwargs):
                calls.update(kwargs)
                return "result"

        self.assertEqual(self._review(_Analyzer()), "result")
        self.assertEqual(calls, {"temperature": 0.7})

    def test_legacy_generate_text_path_sends_no_max_tokens(self):
        calls = {}

        class _Legacy:
            def generate_text(self, prompt, **kwargs):
                calls.update(kwargs)
                return "review text"

        self.assertEqual(self._review(_Legacy()).text, "review text")
        self.assertEqual(calls, {"temperature": 0.7})


class AgentAdapterTestCase(unittest.TestCase):
    def _adapter(self, **config):
        adapter = llm_adapter.LLMToolAdapter.__new__(llm_adapter.LLMToolAdapter)
        values = {
            "agent_litellm_model": "", "litellm_model": "openai/gateway/model-x", "litellm_fallback_models": [],
            "llm_model_list": [], "llm_temperature": 0.7, "gemini_api_keys": [], "anthropic_api_keys": [],
            "openai_api_keys": [], "deepseek_api_keys": [], "openai_base_url": None,
        }
        values.update(config)
        adapter._config = SimpleNamespace(**values)
        adapter._router = None
        adapter._legacy_router_model_list = []
        return adapter

    def _kwargs(self, adapter, *, model="openai/gateway/model-x", support=False, **call):
        seen = []

        def _completion(**kwargs):
            seen.append(dict(kwargs))
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        with patch.object(llm_adapter, "register_fallback_model_pricing"), \
                patch.object(llm_adapter.litellm, "completion", side_effect=_completion), \
                patch.object(generation_params, "_litellm_supports_openai_param", return_value=support):
            adapter._call_litellm_model([{"role": "user", "content": "hi"}], [], model, **call)
        return seen

    def test_agent_chat_sends_no_cap_and_no_effort_by_default(self):
        [kwargs] = self._kwargs(self._adapter())
        self.assertNotIn("max_tokens", kwargs)
        self.assertNotIn("reasoning_effort", kwargs)

    def test_agent_effort_is_sent_and_beats_the_primary_effort(self):
        [kwargs] = self._kwargs(self._adapter(llm_reasoning_effort="high", agent_reasoning_effort="low"))
        self.assertEqual(kwargs["reasoning_effort"], "low")
        self.assertEqual(kwargs["allowed_openai_params"], ["reasoning_effort"])

    def test_agent_effort_follows_the_primary_effort_when_empty(self):
        [kwargs] = self._kwargs(self._adapter(llm_reasoning_effort="xhigh"))
        self.assertEqual(kwargs["reasoning_effort"], "xhigh")

    def test_natively_mapped_route_gets_the_plain_parameter(self):
        [kwargs] = self._kwargs(self._adapter(agent_reasoning_effort="high"), support=True)
        self.assertEqual(kwargs["reasoning_effort"], "high")
        self.assertNotIn("allowed_openai_params", kwargs)

    def test_light_effort_never_reaches_the_agent(self):
        [kwargs] = self._kwargs(self._adapter(litellm_light_reasoning_effort="max"))
        self.assertNotIn("reasoning_effort", kwargs)

    def test_agent_retries_once_without_a_rejected_effort(self):
        clear_litellm_generation_param_recovery_cache()
        self.addCleanup(clear_litellm_generation_param_recovery_cache)
        adapter = self._adapter(agent_reasoning_effort="high")
        attempts = []

        def _completion(**kwargs):
            attempts.append(dict(kwargs))
            if "reasoning_effort" in kwargs:
                raise RuntimeError("openai does not support parameters: ['reasoning_effort']")
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=[]))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            )

        with patch.object(llm_adapter, "register_fallback_model_pricing"), \
                patch.object(llm_adapter.litellm, "completion", side_effect=_completion), \
                patch.object(generation_params, "_litellm_supports_openai_param", return_value=False):
            result = adapter._call_litellm_model([{"role": "user", "content": "hi"}], [], "openai/gateway/model-x")
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["reasoning_effort"], "high")
        self.assertNotIn("reasoning_effort", attempts[1])
        self.assertNotIn("allowed_openai_params", attempts[1])

    def test_native_anthropic_agent_call_gets_the_models_maximum_only_when_the_caller_sets_none(self):
        with patch.object(_litellm, "get_model_info", create=True, return_value={"max_output_tokens": 64000}):
            [kwargs] = self._kwargs(self._adapter(), model="anthropic/claude-sonnet-x", support=True)
            self.assertEqual(kwargs["max_tokens"], 64000)
            [kwargs] = self._kwargs(self._adapter(), model="anthropic/claude-sonnet-x", support=True, max_tokens=900)
            self.assertEqual(kwargs["max_tokens"], 900)


if __name__ == "__main__":
    unittest.main()
