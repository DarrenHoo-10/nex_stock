# -*- coding: utf-8 -*-
"""LLM classification of pooled news (docs/design/core-backend-design.md 5.4).

Each unclassified AKShare / NewsNow item gets: ``scope`` (market / board / stock), ``importance`` 1-5,
``is_breaking``, related A-share board names (mapped to ``board_catalog`` ids, unresolved names are
kept), related US tickers and A-share codes.

The call uses the *light model* (``LITELLM_LIGHT_MODEL``, falling back to the primary model when
empty). When the light call fails (exception, or a reply with no usable item) it is retried once with
the primary model; items still unresolved stay unclassified and their ``classify_attempts`` grows until
``NEWS_CLASSIFY_MAX_ATTEMPTS`` stops retrying them.

Output format asked from the model (one JSON array, ``i`` is the input index)::

    [{"i": 0, "scope": "board", "importance": 4, "breaking": false,
      "boards": ["半导体", "算力"], "us_tickers": ["NVDA"], "codes": ["600519"]}, ...]
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from src.repositories.intelligence_repo import IntelligenceRepository
from src.services.news_akshare_source import KIND_ANNOUNCEMENT, normalize_a_share_code
from src.services.news_pool_service import china_now

logger = logging.getLogger(__name__)

SCOPES = ("market", "board", "stock")
_SYSTEM_PROMPT = "你是 A 股资讯分类助手。只输出符合要求的 JSON，不要输出解释或 Markdown。"
_PROMPT_TEMPLATE = """请对下面 {count} 条财经资讯逐条分类，每条输出：
- scope：影响范围，三选一：market（全市场 / 宏观 / 海外 / 政策）、board（一个或几个 A 股板块、行业、概念）、stock（个别公司）
- importance：重要性，1-5 的整数。5 = 重大政策 / 重大突发 / 明显超预期，可能显著影响大盘或板块；4 = 明确影响某个板块或龙头；3 = 一般行业或公司消息；2 = 例行信息；1 = 无关、重复、广告或纯数据罗列
- breaking：是否突发事件（突然发生、需要立即关注，如重大政策发布、突发事故、监管动作、重大并购），true / false
- boards：相关的 A 股板块名称数组，用东方财富常用板块名（如 "半导体"、"算力"、"CPO"、"光伏设备"、"银行"），没有则 []
- us_tickers：相关美股代码数组（大写，如 "NVDA"），没有则 []
- codes：资讯中明确出现的 A 股 6 位股票代码数组，没有则 []

只输出一个 JSON 数组，每条资讯对应一个对象：
{{"i": 序号, "scope": "...", "importance": 3, "breaking": false, "boards": [], "us_tickers": [], "codes": []}}
序号 i 必须与输入一致，不要遗漏。

资讯：
{items}
"""
_TITLE_CHARS = 120
_SUMMARY_CHARS = 160
_SCOPE_ALIASES = {
    "market": "market", "全市场": "market", "大盘": "market", "宏观": "market",
    "board": "board", "sector": "board", "板块": "board", "行业": "board", "概念": "board",
    "stock": "stock", "company": "stock", "个股": "stock", "公司": "stock",
}
_US_TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,5}$")
_BOARD_SUFFIXES = ("概念股", "产业链", "概念", "板块", "行业", "题材", "指数", "Ⅱ", "Ⅲ", "ⅱ", "ⅲ")
_MAX_BOARDS = 8
_MAX_TICKERS = 8
_MAX_CODES = 10

_CLASSIFY_LOCK = threading.Lock()

# (prompt, models, reasoning_effort) -> reply text; no output cap is sent (the project sets none).
LlmCall = Callable[[str, Optional[List[str]], Optional[str]], str]
CatalogLoader = Callable[[], List[Dict[str, Any]]]


class ClassificationError(RuntimeError):
    """The model reply could not be used at all."""


# ---------------------------------------------------------------------------
# parsing and board mapping (pure)
# ---------------------------------------------------------------------------
def extract_json_array(text: str) -> List[Any]:
    """Pull the JSON array out of a model reply (fenced, wrapped in prose, or slightly broken)."""
    raw = (text or "").strip()
    if not raw:
        raise ClassificationError("empty reply")
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.S | re.I)
    if fenced:
        raw = fenced.group(1).strip()
    candidates = [raw]
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end > start:
        candidates.append(raw[start:end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except ValueError:
            continue
        if isinstance(value, dict):
            for key in ("items", "results", "data"):
                if isinstance(value.get(key), list):
                    return value[key]
        if isinstance(value, list):
            return value
    try:  # optional dependency already used by the agent runner
        from json_repair import repair_json

        repaired = json.loads(repair_json(candidates[-1]))
        if isinstance(repaired, list):
            return repaired
    except Exception:  # noqa: BLE001
        pass
    raise ClassificationError("reply is not a JSON array")


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是")
    return bool(value)


def _string_list(value: Any, limit: int) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[,，、;；\s]+", value)
    if not isinstance(value, (list, tuple)):
        return []
    out: List[str] = []
    for item in value:
        text = str(item or "").strip()
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


def parse_classification(entry: Any) -> Optional[Dict[str, Any]]:
    """One reply object -> normalised fields, or ``None`` when importance is unusable."""
    if not isinstance(entry, dict):
        return None
    try:
        importance = int(round(float(entry.get("importance"))))
    except (TypeError, ValueError):
        return None
    if not 1 <= importance <= 5:
        importance = min(5, max(1, importance))
    boards = _string_list(entry.get("boards"), _MAX_BOARDS)
    tickers = [t.upper() for t in _string_list(entry.get("us_tickers"), _MAX_TICKERS)]
    tickers = [t for t in tickers if _US_TICKER_RE.match(t)]
    codes: List[str] = []
    for raw_code in _string_list(entry.get("codes"), _MAX_CODES * 2):
        code = normalize_a_share_code(raw_code)
        if code and code not in codes:
            codes.append(code)
    scope = _SCOPE_ALIASES.get(str(entry.get("scope") or "").strip().lower())
    if scope is None:
        scope = "stock" if codes else "board" if boards else "market"
    return {
        "scope": scope,
        "importance": importance,
        "is_breaking": _as_bool(entry.get("breaking", entry.get("is_breaking"))),
        "board_names": boards,
        "us_tickers": tickers,
        "codes": codes[:_MAX_CODES],
    }


def normalize_board_name(name: str) -> str:
    text = re.sub(r"[\s·\-_/（）()]+", "", str(name or "")).lower()
    changed = True
    while changed:
        changed = False
        for suffix in _BOARD_SUFFIXES:
            if text.endswith(suffix.lower()) and len(text) > len(suffix):
                text = text[: -len(suffix)]
                changed = True
    return text


def map_board_names(names: Sequence[str], catalog: Sequence[Dict[str, Any]]) -> Tuple[List[int], List[str]]:
    """Map model board names to ``board_catalog`` ids: exact name, then normalised equal, then contains.

    ``contains`` needs >= 2 normalised characters and picks the closest-length board (ties: lowest id).
    Returns ``(ids, unresolved_names)``.
    """
    boards = [b for b in catalog if b.get("status", "active") == "active" and b.get("name")]
    by_exact: Dict[str, List[int]] = {}
    by_norm: Dict[str, List[int]] = {}
    normalized_boards: List[Tuple[str, int]] = []
    for board in boards:
        by_exact.setdefault(str(board["name"]).strip().lower(), []).append(int(board["id"]))
        norm = normalize_board_name(board["name"])
        by_norm.setdefault(norm, []).append(int(board["id"]))
        normalized_boards.append((norm, int(board["id"])))

    ids: List[int] = []
    unresolved: List[str] = []

    def add(found: Sequence[int]) -> None:
        for board_id in found:
            if board_id not in ids:
                ids.append(board_id)

    for name in names:
        exact = by_exact.get(str(name).strip().lower())
        if exact:
            add(exact)
            continue
        norm = normalize_board_name(name)
        if norm and by_norm.get(norm):
            add(by_norm[norm])
            continue
        best: Optional[Tuple[int, int]] = None
        if len(norm) >= 2:
            for board_norm, board_id in normalized_boards:
                if len(board_norm) >= 2 and (norm in board_norm or board_norm in norm):
                    candidate = (abs(len(board_norm) - len(norm)), board_id)
                    if best is None or candidate < best:
                        best = candidate
        if best is not None:
            add([best[1]])
        else:
            unresolved.append(name)
    return ids, unresolved


def build_prompt(items: Sequence[Dict[str, Any]]) -> str:
    payload = [
        {
            "i": index,
            "kind": item.get("item_kind") or "flash",
            "title": str(item.get("title") or "")[:_TITLE_CHARS],
            "summary": str(item.get("summary") or "")[:_SUMMARY_CHARS],
        }
        for index, item in enumerate(items)
    ]
    return _PROMPT_TEMPLATE.format(count=len(payload), items=json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------
class NewsClassifier:
    def __init__(
        self,
        repo: Optional[IntelligenceRepository] = None,
        config: Any = None,
        *,
        llm_call: Optional[LlmCall] = None,
        catalog_loader: Optional[CatalogLoader] = None,
    ) -> None:
        self.repo = repo or IntelligenceRepository()
        if config is None:
            from src.config import get_config

            config = get_config()
        self.config = config
        self._llm_call = llm_call
        self._catalog_loader = catalog_loader
        self._analyzer: Any = None

    # -- model call ------------------------------------------------------
    def _default_llm_call(self, prompt: str, models: Optional[List[str]], reasoning_effort: Optional[str] = None) -> str:
        if self._analyzer is None:
            from src.analyzer import GeminiAnalyzer

            self._analyzer = GeminiAnalyzer(config=self.config)
        return self._analyzer.generate_text_with_models(
            prompt,
            models=models,
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.1,
            call_type="news_classify",
            reasoning_effort=reasoning_effort or None,
        )

    def model_plan(self) -> List[Optional[List[str]]]:
        """Try-order of ``models`` arguments: light model first, then the primary model once."""
        from src.config import get_effective_light_model

        light = get_effective_light_model(self.config)
        primary = (getattr(self.config, "litellm_model", "") or "").strip()
        plan: List[Optional[List[str]]] = [[light] if light else None]
        if light and light != primary:
            plan.append([primary] if primary else None)
        return plan

    def effort_plan(self) -> List[Optional[str]]:
        """``reasoning_effort`` per attempt of ``model_plan``: the first attempt is the light chore
        (``LITELLM_LIGHT_REASONING_EFFORT``, empty = none); the retry on the primary model uses
        ``LLM_REASONING_EFFORT``."""
        from src.config import get_light_reasoning_effort, get_primary_reasoning_effort

        plan = self.model_plan()
        efforts: List[Optional[str]] = [get_light_reasoning_effort(self.config) or None]
        efforts.extend(get_primary_reasoning_effort(self.config) or None for _ in plan[1:])
        return efforts

    def _load_catalog(self) -> List[Dict[str, Any]]:
        try:
            if self._catalog_loader is not None:
                return list(self._catalog_loader())
            from src.repositories.board_analysis_repo import BoardAnalysisRepository

            return BoardAnalysisRepository().list_catalog(("industry", "concept"))
        except Exception as exc:  # noqa: BLE001 - board mapping is an enrichment, names are kept anyway
            logger.warning("[NewsClassify] 读取板块目录失败，板块名称不做映射: %s", exc)
            return []

    # -- run -------------------------------------------------------------
    def classify_pending(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not _CLASSIFY_LOCK.acquire(blocking=False):
            return {"status": "skipped", "reason": "already_running"}
        try:
            return self._classify_locked(now or china_now())
        finally:
            _CLASSIFY_LOCK.release()

    def _classify_locked(self, now: datetime) -> Dict[str, Any]:
        cfg = self.config
        floor = now - timedelta(days=max(1, int(cfg.news_flash_retention_days)))
        items = self.repo.list_unclassified_items(
            limit=int(cfg.news_classify_max_items_per_run),
            max_attempts=int(cfg.news_classify_max_attempts),
            published_since=floor,
        )
        result: Dict[str, Any] = {
            "status": "ok", "candidates": len(items), "classified": 0, "failed": 0, "llm_calls": 0, "fallback_calls": 0,
        }
        if not items:
            result["status"] = "idle"
            return result
        catalog = self._load_catalog()
        batch_size = max(1, int(cfg.news_classify_batch_size))
        for start in range(0, len(items), batch_size):
            batch = items[start:start + batch_size]
            classified = self._classify_batch(batch, catalog, now, result)
            result["classified"] += classified
            result["failed"] += len(batch) - classified
        if result["classified"] == 0:
            result["status"] = "failed"
        elif result["failed"]:
            result["status"] = "partial"
        logger.info(
            "[NewsClassify] %s: 候选 %d，成功 %d，失败 %d，模型调用 %d（回退 %d）",
            result["status"], result["candidates"], result["classified"], result["failed"],
            result["llm_calls"], result["fallback_calls"],
        )
        return result

    def _classify_batch(
        self, batch: List[Dict[str, Any]], catalog: List[Dict[str, Any]], now: datetime, result: Dict[str, Any],
    ) -> int:
        prompt = build_prompt(batch)
        # No output cap is sent: reasoning models spend tokens on thinking before answering, and a tight budget
        # returned an empty answer live (736 tokens for 3 items); the gateway / model default applies.
        call = self._llm_call or self._default_llm_call
        efforts = self.effort_plan()
        parsed: Dict[int, Dict[str, Any]] = {}
        for attempt, models in enumerate(self.model_plan()):
            result["llm_calls"] += 1
            if attempt:
                result["fallback_calls"] += 1
            try:
                reply = call(prompt, models, efforts[attempt])
                parsed = self._parse_reply(reply, len(batch))
            except Exception as exc:  # noqa: BLE001 - any model / parse failure -> next model in the plan
                logger.warning("[NewsClassify] 模型 %s 分类失败: %s", models or "default", _short(exc))
                parsed = {}
                continue
            if parsed:
                break

        done = 0
        failed_ids: List[int] = []
        for index, item in enumerate(batch):
            fields = parsed.get(index)
            if fields is None:
                failed_ids.append(item["id"])
                continue
            self.repo.save_classification(item["id"], self._to_storage(item, fields, catalog, now))
            done += 1
        if failed_ids:
            self.repo.bump_classify_attempts(failed_ids)
        return done

    @staticmethod
    def _parse_reply(reply: str, size: int) -> Dict[int, Dict[str, Any]]:
        entries = extract_json_array(reply)
        parsed: Dict[int, Dict[str, Any]] = {}
        for position, entry in enumerate(entries):
            index = position
            if isinstance(entry, dict) and entry.get("i") is not None:
                try:
                    index = int(entry["i"])
                except (TypeError, ValueError):
                    continue
            if not 0 <= index < size or index in parsed:
                continue
            fields = parse_classification(entry)
            if fields is not None:
                parsed[index] = fields
        return parsed

    @staticmethod
    def _to_storage(
        item: Dict[str, Any], fields: Dict[str, Any], catalog: List[Dict[str, Any]], now: datetime,
    ) -> Dict[str, Any]:
        board_ids, _unresolved = map_board_names(fields["board_names"], catalog)
        codes = list(item.get("stock_codes") or [])
        for code in fields["codes"]:
            if code not in codes:
                codes.append(code)
        scope = "stock" if item.get("item_kind") == KIND_ANNOUNCEMENT else fields["scope"]
        return {
            "news_scope": scope,
            "importance": fields["importance"],
            "is_breaking": fields["is_breaking"],
            "board_names": json.dumps(fields["board_names"], ensure_ascii=False),
            "board_ids": json.dumps(board_ids),
            "us_tickers": json.dumps(fields["us_tickers"], ensure_ascii=False),
            "stock_codes": json.dumps(codes, ensure_ascii=False),
            "classified_at": now,
        }


def _short(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {str(exc)[:200]}"
