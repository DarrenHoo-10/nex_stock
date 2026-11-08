# -*- coding: utf-8 -*-
"""Offline tests for news classification: reply parsing, board-name mapping, light/primary model order, retry cap.

The model call boundary (``llm_call``) is a scripted fake; prompt building, parsing, board mapping and
persistence run for real against an in-memory SQLite.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any, List, Optional

import pytest
from sqlalchemy import select

from src.config import get_effective_light_model
from src.repositories.intelligence_repo import IntelligenceRepository
from src.services import news_classifier as nc
from src.services.news_classifier import NewsClassifier, map_board_names, parse_classification
from src.storage import DatabaseManager, IntelligenceItem
from tests.news_fixtures import NOW, make_config

CATALOG = [
    {"id": 1, "name": "半导体", "board_type": "industry", "status": "active"},
    {"id": 2, "name": "算力概念", "board_type": "concept", "status": "active"},
    {"id": 3, "name": "CPO概念", "board_type": "concept", "status": "active"},
    {"id": 4, "name": "光伏设备", "board_type": "industry", "status": "active"},
    {"id": 5, "name": "光伏", "board_type": "concept", "status": "retired"},
    {"id": 6, "name": "人工智能", "board_type": "concept", "status": "active"},
    {"id": 7, "name": "人工智能", "board_type": "industry", "status": "active"},
]


@pytest.fixture()
def db():
    DatabaseManager.reset_instance()
    manager = DatabaseManager(db_url="sqlite:///:memory:")
    yield manager
    DatabaseManager.reset_instance()


def put_item(db, title: str, *, minutes_ago: int = 5, kind: Optional[str] = "flash", source_type: str = "akshare",
             attempts: Optional[int] = None, stock_codes: Optional[str] = None, url: Optional[str] = None) -> int:
    with db.session_scope() as session:
        row = IntelligenceItem(
            source_type=source_type, source_name=f"{source_type}:em", title=title, summary=f"{title}的详细内容",
            url=url or f"https://example.test/{title}", source="来源", published_at=NOW - timedelta(minutes=minutes_ago),
            fetched_at=NOW, scope_type="market", scope_value="__none__", market="cn", item_kind=kind,
            classify_attempts=attempts, stock_codes=stock_codes,
        )
        session.add(row)
        session.flush()
        return row.id


def load(db, item_id: int) -> IntelligenceItem:
    with db.get_session() as session:
        row = session.get(IntelligenceItem, item_id)
        session.expunge(row)
        return row


class FakeLlm:
    """Scripted model boundary: ``replies`` is consumed one call at a time (Exception instances are raised)."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: List[dict] = []

    def __call__(self, prompt: str, models: Optional[List[str]], reasoning_effort: Optional[str] = None) -> str:
        self.calls.append({"prompt": prompt, "models": models, "reasoning_effort": reasoning_effort})
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)


def classifier(db, llm: FakeLlm, **config: Any) -> NewsClassifier:
    return NewsClassifier(IntelligenceRepository(db), make_config(**config), llm_call=llm, catalog_loader=lambda: CATALOG)


# ----------------------------------------------------------------------
# parsing
# ----------------------------------------------------------------------
def test_parse_classification_normalises_fields():
    parsed = parse_classification({
        "scope": "板块", "importance": "4", "breaking": "true", "boards": ["半导体", "半导体", "算力"],
        "us_tickers": ["nvda", "AVGO", "not a ticker", "TSM"], "codes": ["sh600519", "300750", "bad", "600519"],
    })
    assert parsed == {
        "scope": "board", "importance": 4, "is_breaking": True, "board_names": ["半导体", "算力"],
        "us_tickers": ["NVDA", "AVGO", "TSM"], "codes": ["600519", "300750"],
    }
    assert parse_classification({"importance": 9})["importance"] == 5  # clamped
    assert parse_classification({"importance": 0})["importance"] == 1
    assert parse_classification({"importance": "high"}) is None and parse_classification("x") is None
    # missing/odd scope is inferred from what the model did give
    assert parse_classification({"importance": 3, "codes": ["600519"]})["scope"] == "stock"
    assert parse_classification({"importance": 3, "boards": ["银行"]})["scope"] == "board"
    assert parse_classification({"importance": 3})["scope"] == "market"


def test_reply_extraction_accepts_fences_prose_and_wrapped_objects():
    payload = [{"i": 0, "importance": 3}]
    assert nc.extract_json_array(json.dumps(payload)) == payload
    assert nc.extract_json_array("```json\n" + json.dumps(payload) + "\n```") == payload
    assert nc.extract_json_array("分类结果如下：" + json.dumps(payload) + " 以上。") == payload
    assert nc.extract_json_array(json.dumps({"items": payload})) == payload
    with pytest.raises(nc.ClassificationError):
        nc.extract_json_array("没有 JSON")
    with pytest.raises(nc.ClassificationError):
        nc.extract_json_array("")


def test_prompt_lists_every_item_with_index_and_truncates_long_text():
    prompt = nc.build_prompt([
        {"title": "标题甲", "summary": "摘要" * 200, "item_kind": "announcement"},
        {"title": "标题乙", "summary": "", "item_kind": None},
    ])
    assert "2 条财经资讯" in prompt and '"i": 0' in prompt and '"i": 1' in prompt
    assert '"kind": "announcement"' in prompt and '"kind": "flash"' in prompt
    assert "摘要" * 100 not in prompt  # 160 chars max


# ----------------------------------------------------------------------
# board mapping
# ----------------------------------------------------------------------
def test_board_names_map_exact_then_normalised_then_contains_and_unresolved_are_kept():
    ids, unresolved = map_board_names(["半导体"], CATALOG)
    assert (ids, unresolved) == ([1], [])
    ids, unresolved = map_board_names(["算力", "CPO"], CATALOG)  # normalised: strips the 概念 suffix
    assert (ids, unresolved) == ([2, 3], [])
    ids, unresolved = map_board_names(["光伏板块"], CATALOG)  # retired board 5 is ignored; contains -> 光伏设备
    assert (ids, unresolved) == ([4], [])
    ids, unresolved = map_board_names(["人工智能"], CATALOG)  # an exact name shared by two boards maps to both
    assert (ids, unresolved) == ([6, 7], [])
    ids, unresolved = map_board_names(["航天军工", "半导体设备"], CATALOG)  # unknown stays unresolved; contains hit
    assert ids == [1] and unresolved == ["航天军工"]
    assert map_board_names(["x"], CATALOG) == ([], ["x"])  # 1 char never matches by containment
    assert map_board_names([], CATALOG) == ([], [])


# ----------------------------------------------------------------------
# classification run
# ----------------------------------------------------------------------
def test_light_model_classifies_a_batch_and_results_are_persisted_with_mapped_boards(db):
    a = put_item(db, "国务院发布算力新规", minutes_ago=1)
    b = put_item(db, "某公司签订大单", minutes_ago=2, stock_codes=json.dumps(["600519"]))
    c = put_item(db, "央行例行公开市场操作", minutes_ago=3)
    llm = FakeLlm([
        {"i": 0, "scope": "board", "importance": 5, "breaking": True, "boards": ["算力", "航天军工"], "us_tickers": ["nvda"], "codes": []},
        {"i": 1, "scope": "stock", "importance": 3, "breaking": False, "boards": [], "us_tickers": [], "codes": ["300750"]},
        {"i": 2, "scope": "market", "importance": 2, "breaking": False, "boards": [], "us_tickers": [], "codes": []},
    ])
    result = classifier(db, llm, litellm_light_model="openai/light-model").classify_pending(now=NOW)

    assert result["status"] == "ok" and result["classified"] == 3 and result["llm_calls"] == 1
    assert llm.calls[0]["models"] == ["openai/light-model"]  # the light model, not the primary
    first = load(db, a)
    assert (first.news_scope, first.importance, first.is_breaking) == ("board", 5, True)
    assert json.loads(first.board_names) == ["算力", "航天军工"]  # unresolved name kept
    assert json.loads(first.board_ids) == [2]  # only the resolved one
    assert json.loads(first.us_tickers) == ["NVDA"] and first.classified_at == NOW
    second = load(db, b)
    assert json.loads(second.stock_codes) == ["600519", "300750"]  # prefilled + model codes
    assert load(db, c).importance == 2 and load(db, c).is_breaking is False


def test_empty_light_model_uses_the_primary_route_once_and_announcements_stay_stock_scope(db):
    ann = put_item(db, "回购公告", kind="announcement", stock_codes=json.dumps(["600519"]))
    llm = FakeLlm([{"i": 0, "scope": "market", "importance": 4, "breaking": False, "boards": [], "us_tickers": [], "codes": []}])
    result = classifier(db, llm, litellm_light_model="").classify_pending(now=NOW)
    assert result["classified"] == 1 and len(llm.calls) == 1
    assert llm.calls[0]["models"] == ["openai/primary-model"]  # empty light model = the effective primary model, one call
    assert load(db, ann).news_scope == "stock" and load(db, ann).importance == 4


def test_light_model_failure_falls_back_to_the_primary_model_once(db):
    ids = [put_item(db, f"快讯{i}") for i in range(2)]
    good = [{"i": i, "scope": "market", "importance": 3, "breaking": False} for i in range(2)]

    llm = FakeLlm(TimeoutError("light model timed out"), good)
    result = classifier(db, llm, litellm_light_model="openai/light-model").classify_pending(now=NOW)
    assert [c["models"] for c in llm.calls] == [["openai/light-model"], ["openai/primary-model"]]
    assert result["classified"] == 2 and result["fallback_calls"] == 1
    assert all(load(db, i).importance == 3 for i in ids)


def test_unusable_light_reply_also_triggers_the_single_fallback(db):
    put_item(db, "快讯")
    llm = FakeLlm("这不是 JSON", [{"i": 0, "scope": "market", "importance": 2, "breaking": False}])
    result = classifier(db, llm, litellm_light_model="openai/light-model").classify_pending(now=NOW)
    assert result["classified"] == 1 and len(llm.calls) == 2


def test_both_models_failing_leaves_items_unclassified_and_counts_an_attempt(db):
    item = put_item(db, "快讯")
    llm = FakeLlm(RuntimeError("light down"), RuntimeError("primary down"))
    result = classifier(db, llm, litellm_light_model="openai/light-model").classify_pending(now=NOW)
    assert result["status"] == "failed" and result["failed"] == 1 and len(llm.calls) == 2  # exactly one fallback
    row = load(db, item)
    assert row.classified_at is None and row.importance is None and row.classify_attempts == 1


def test_items_missing_from_the_reply_are_retried_later_until_the_attempt_cap(db):
    a = put_item(db, "有结果")
    b = put_item(db, "没结果", minutes_ago=6)
    only_a = lambda: [{"i": 0, "scope": "market", "importance": 3, "breaking": False}]  # noqa: E731
    service_config = dict(litellm_light_model="", news_classify_max_attempts=2)

    llm = FakeLlm(only_a())
    result = classifier(db, llm, **service_config).classify_pending(now=NOW)
    assert result["status"] == "partial" and (result["classified"], result["failed"]) == (1, 1)
    assert load(db, a).importance == 3 and load(db, b).classify_attempts == 1

    llm = FakeLlm([{"i": 5, "importance": 2}])  # only an index outside the batch: nothing usable
    classifier(db, llm, **service_config).classify_pending(now=NOW)
    assert load(db, b).classify_attempts == 2 and load(db, b).classified_at is None

    llm = FakeLlm()  # cap reached: the item is no longer a candidate, no model call at all
    result = classifier(db, llm, **service_config).classify_pending(now=NOW)
    assert result["status"] == "idle" and llm.calls == []
    assert IntelligenceRepository(db).count_unclassified(max_attempts=2) == 0


def test_batching_per_run_cap_and_candidate_selection(db):
    for i in range(7):
        put_item(db, f"快讯{i}", minutes_ago=i + 1)
    put_item(db, "RSS 不分类", source_type="rss")
    put_item(db, "太旧", minutes_ago=5 * 24 * 60)  # older than the flash retention window
    put_item(db, "newsnow 备用来源", source_type="newsnow", kind=None)

    def reply(count: int):
        return [{"i": i, "scope": "market", "importance": 2, "breaking": False} for i in range(count)]

    llm = FakeLlm(reply(3), reply(3))
    result = classifier(db, llm, news_classify_batch_size=3, news_classify_max_items_per_run=6).classify_pending(now=NOW)
    assert result["candidates"] == 6 and result["classified"] == 6 and len(llm.calls) == 2
    with db.get_session() as session:
        unclassified = {r.title for r in session.execute(select(IntelligenceItem).where(IntelligenceItem.classified_at.is_(None))).scalars()}
    assert "RSS 不分类" in unclassified and "太旧" in unclassified  # never candidates
    assert len(unclassified) == 4  # RSS, stale, and two of the 8 fresh ones beyond the per-run cap


def test_light_model_accessor_falls_back_to_primary_and_keeps_channel_names():
    assert get_effective_light_model(make_config(litellm_light_model="deepseek/flash")) == "deepseek/flash"
    assert get_effective_light_model(make_config(litellm_light_model="  ")) == "openai/primary-model"
    assert get_effective_light_model(make_config(litellm_light_model="", litellm_model="")) == ""
    plan = NewsClassifier(IntelligenceRepository(), make_config(litellm_light_model="deepseek/flash")).model_plan()
    assert plan == [["deepseek/flash"], ["openai/primary-model"]]
    assert NewsClassifier(IntelligenceRepository(), make_config(litellm_light_model="openai/primary-model")).model_plan() == [["openai/primary-model"]]
    assert NewsClassifier(IntelligenceRepository(), make_config(litellm_light_model="", litellm_model="")).model_plan() == [None]


def test_light_model_typed_as_listed_in_a_channel_resolves_to_its_routed_name():
    # Channel models are routed as "openai/<listed name>"; users copy the listed name.
    model_list = [
        {"model_name": "openai/auto/deepseek-v4.1-flash"},
        {"model_name": "openai/auto/claude-opus-5-5:high"},
    ]
    config = make_config(litellm_light_model="auto/deepseek-v4.1-flash", llm_model_list=model_list)
    assert get_effective_light_model(config) == "openai/auto/deepseek-v4.1-flash"
    # exact routed names are left alone
    config = make_config(litellm_light_model="openai/auto/deepseek-v4.1-flash", llm_model_list=model_list)
    assert get_effective_light_model(config) == "openai/auto/deepseek-v4.1-flash"
    # legacy single-endpoint setup (no channel list): an unknown provider prefix goes through the
    # OpenAI-compatible endpoint, a real LiteLLM provider prefix is kept
    config = make_config(litellm_light_model="auto/deepseek-v4.1-flash", llm_model_list=[])
    assert get_effective_light_model(config) == "openai/auto/deepseek-v4.1-flash"
    config = make_config(litellm_light_model="deepseek/deepseek-chat", llm_model_list=[])
    assert get_effective_light_model(config) == "deepseek/deepseek-chat"


def test_overlapping_classification_runs_do_not_run_twice(db):
    assert nc._CLASSIFY_LOCK.acquire(blocking=False)
    try:
        result = classifier(db, FakeLlm()).classify_pending(now=NOW)
        assert result == {"status": "skipped", "reason": "already_running"}
    finally:
        nc._CLASSIFY_LOCK.release()

# ----------------------------------------------------------------------
# no output cap / reasoning effort per attempt
# ----------------------------------------------------------------------
def test_reasoning_effort_follows_the_attempt_light_first_then_primary(db):
    put_item(db, "快讯")
    ok = [{"i": 0, "scope": "market", "importance": 2, "breaking": False}]
    llm = FakeLlm(TimeoutError("light down"), ok)
    classifier(
        db, llm, litellm_light_model="openai/light-model",
        litellm_light_reasoning_effort="low", llm_reasoning_effort="high",
    ).classify_pending(now=NOW)
    assert [(c["models"], c["reasoning_effort"]) for c in llm.calls] == [
        (["openai/light-model"], "low"),  # the light chore uses LITELLM_LIGHT_REASONING_EFFORT
        (["openai/primary-model"], "high"),  # the fallback to the primary model uses LLM_REASONING_EFFORT
    ]


def test_unset_efforts_are_not_sent_and_the_light_effort_never_leaks_into_the_primary_retry(db):
    put_item(db, "快讯")
    ok = [{"i": 0, "scope": "market", "importance": 2, "breaking": False}]
    llm = FakeLlm(TimeoutError("light down"), ok)
    classifier(db, llm, litellm_light_model="openai/light-model", litellm_light_reasoning_effort="max").classify_pending(now=NOW)
    assert [c["reasoning_effort"] for c in llm.calls] == ["max", None]  # LLM_REASONING_EFFORT unset: nothing for the retry

    llm = FakeLlm(ok)
    put_item(db, "另一条快讯")
    classifier(db, llm, litellm_light_model="openai/light-model").classify_pending(now=NOW)
    assert [c["reasoning_effort"] for c in llm.calls] == [None]  # both unset: nothing is sent


def test_effort_plan_matches_the_model_plan_length():
    both = NewsClassifier(IntelligenceRepository(), make_config(
        litellm_light_model="deepseek/flash", litellm_light_reasoning_effort="low", llm_reasoning_effort="xhigh"))
    assert both.model_plan() == [["deepseek/flash"], ["openai/primary-model"]]
    assert both.effort_plan() == ["low", "xhigh"]
    single = NewsClassifier(IntelligenceRepository(), make_config(litellm_light_model="", llm_reasoning_effort="xhigh"))
    assert len(single.model_plan()) == len(single.effort_plan()) == 1
    invalid = NewsClassifier(IntelligenceRepository(), make_config(
        litellm_light_model="deepseek/flash", litellm_light_reasoning_effort="turbo"))
    assert invalid.effort_plan() == [None, None]  # an unknown value is never sent


def test_default_llm_call_sends_no_output_cap():
    calls = []

    class FakeAnalyzer:
        def generate_text_with_models(self, prompt, **kwargs):
            calls.append(kwargs)
            return "[]"

    instance = NewsClassifier(IntelligenceRepository(), make_config())
    instance._analyzer = FakeAnalyzer()
    instance._default_llm_call("prompt", ["openai/light-model"], "low")
    assert "max_tokens" not in calls[0] and "max_output_tokens" not in calls[0]
    assert calls[0]["reasoning_effort"] == "low" and calls[0]["models"] == ["openai/light-model"]
    instance._default_llm_call("prompt", None, None)
    assert calls[1]["reasoning_effort"] is None
