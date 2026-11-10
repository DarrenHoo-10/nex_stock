# -*- coding: utf-8 -*-
"""Recommended boards for the next session, picked by the analysis model from one board run.

Input: the run's signalled boards (``BoardAnomalyService.list_anomalies``: state, tags, change, 5-day change,
excess return, amount ratio, breadth, limit-ups, reason). Recommendations are shared by every user, so no user's
personal follows go into the prompt (the web client marks a user's own followed boards from ``GET /boards/follows``).
The model (``LITELLM_MODEL`` with ``LLM_REASONING_EFFORT``, same routing as every other call) answers with a
JSON list of 3-5 board ids and a short reason each; ids that were not offered are dropped. When the model is not
configured, fails or returns nothing usable, a rule ranking is stored instead (strong states first, then excess
return) and the note says why. Results are saved per run in ``board_recommendation``.

Runs as the last (non-fatal) step of the 17:00 board pipeline, or on demand in a background thread
(``start_refresh``) for the latest run.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.repositories.board_preference_repo import BoardPreferenceRepository

logger = logging.getLogger(__name__)

MAX_RECOMMENDATIONS = 5
MAX_CANDIDATES = 40
_STRONG_STATES = ("first_launch", "strengthening")
_STATE_ORDER = {"first_launch": 0, "strengthening": 1, "divergence": 2, "abnormal_drop": 3, "ebbing": 4}
_WEAK_TAGS = {"single_stock", "cooling"}
_STATE_TEXT = {
    "first_launch": "首次启动", "strengthening": "走强", "divergence": "分歧", "ebbing": "退潮",
    "abnormal_drop": "异常下跌", "insufficient_history": "历史不足", "none": "无异动",
}

_SYSTEM_PROMPT = (
    "你是 A 股板块研究助手。根据收盘后的板块异动数据，挑出下一个交易日最值得重点跟踪的板块。"
    "只从给出的板块里选，只输出 JSON，不要输出其他文字。"
)

# (prompt) -> reply text
LlmCall = Callable[[str], str]

_refresh_lock = threading.Lock()
_refreshing: Dict[int, threading.Thread] = {}


def _fmt(value: Any, suffix: str = "%") -> str:
    if value is None:
        return "—"
    try:
        return f"{float(value):+.2f}{suffix}" if suffix == "%" else f"{float(value):.2f}{suffix}"
    except (TypeError, ValueError):
        return "—"


def build_prompt(trade_date: date, candidates: List[Dict[str, Any]]) -> str:
    lines = []
    for row in candidates:
        lines.append(
            f"- id={row['id']} | {row['name']}（{'行业' if row['board_type'] == 'industry' else '概念'}）"
            f" | 状态 {_STATE_TEXT.get(row['state'], row['state'])}"
            f"{'，标签 ' + '/'.join(row['tags']) if row.get('tags') else ''}"
            f" | 当日 {_fmt(row.get('change_pct'))}，5 日 {_fmt(row.get('change_5d_pct'))}，"
            f"超额 {_fmt(row.get('excess_pct'))}，量比 {_fmt(row.get('amount_ratio'), '倍')}，"
            f"上涨家数占比 {'—' if row.get('breadth_pct') is None else str(row['breadth_pct']) + '%'}"
            f"{'，涨停 ' + str(row['limit_up_count']) if row.get('limit_up_count') is not None else ''}"
            f" | 判定理由：{row.get('reason') or '无'}"
        )
    return (
        f"交易日 {trade_date.isoformat()} 收盘后的板块异动（按状态和超额收益排序）：\n"
        + "\n".join(lines)
        + "\n\n要求：\n"
        f"1. 从上面的异动板块里选 3 到 {MAX_RECOMMENDATIONS} 个下一个交易日最值得重点跟踪、在里面找机会的板块，按优先级排序。\n"
        "2. 优先考虑：持续性（5 日与超额收益）、量能、内部广度、不是个股拉动；退潮、个股拉动、过热的板块要谨慎。\n"
        "3. 每个给一句不超过 50 字的理由，说清楚为什么值得跟踪、要注意什么。\n"
        '4. 只输出 JSON 数组，例如 [{"board_id": "concept:BK1036", "reason": "……"}]。'
    )


def parse_reply(text: str, allowed: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Board ids from the model reply that were offered, in order, with their reasons."""
    if not text:
        return []
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("["), cleaned.rfind("]")
    if start < 0 or end <= start:
        return []
    try:
        payload = json.loads(cleaned[start:end + 1])
    except ValueError:
        return []
    picked: List[Dict[str, Any]] = []
    seen = set()
    for entry in payload if isinstance(payload, list) else []:
        if not isinstance(entry, dict):
            continue
        ref = str(entry.get("board_id") or entry.get("id") or "").strip()
        if ref not in allowed or ref in seen:
            continue
        seen.add(ref)
        row = allowed[ref]
        picked.append({
            "board_ref": ref,
            "board_name": row["name"],
            "board_type": row["board_type"],
            "reason": str(entry.get("reason") or "").strip()[:120],
        })
        if len(picked) >= MAX_RECOMMENDATIONS:
            break
    return picked


def rule_ranking(candidates: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Strong states without weak tags first (by excess return), then divergence boards to reach three."""
    def excess(row: Dict[str, Any]) -> float:
        value = row.get("excess_pct")
        return float(value) if isinstance(value, (int, float)) else -1e9

    strong = [r for r in candidates if r["state"] in _STRONG_STATES and not (set(r.get("tags") or []) & _WEAK_TAGS)]
    strong.sort(key=lambda r: (_STATE_ORDER.get(r["state"], 9), -excess(r)))
    picked = strong[:MAX_RECOMMENDATIONS]
    if len(picked) < 3:
        rest = [r for r in candidates if r["state"] == "divergence" and r not in picked]
        rest.sort(key=lambda r: -excess(r))
        picked.extend(rest[: 3 - len(picked)])
    return [
        {"board_ref": r["id"], "board_name": r["name"], "board_type": r["board_type"], "reason": r.get("reason") or ""}
        for r in picked
    ]


class BoardRecommendationService:
    def __init__(
        self,
        anomaly_service: Any = None,
        prefs: Optional[BoardPreferenceRepository] = None,
        config: Any = None,
        llm_call: Optional[LlmCall] = None,
    ) -> None:
        if anomaly_service is None:
            from src.services.board_anomaly_service import BoardAnomalyService

            anomaly_service = BoardAnomalyService()
        self.anomalies = anomaly_service
        self.prefs = prefs or BoardPreferenceRepository()
        if config is None:
            from src.config import get_config

            config = get_config()
        self.config = config
        self._llm_call = llm_call

    # -- model -----------------------------------------------------------
    def _model_label(self) -> str:
        return (getattr(self.config, "litellm_model", "") or "").strip()

    def _call_model(self, prompt: str) -> str:
        if self._llm_call is not None:
            return self._llm_call(prompt)
        from src.analyzer import GeminiAnalyzer
        from src.config import get_primary_reasoning_effort

        analyzer = GeminiAnalyzer(config=self.config)
        return analyzer.generate_text_with_models(
            prompt,
            system_prompt=_SYSTEM_PROMPT,
            temperature=0.2,
            call_type="board_recommend",
            reasoning_effort=get_primary_reasoning_effort(self.config) or None,
        )

    # -- generation ------------------------------------------------------
    def _inputs(self, trade_date: Optional[date]) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        run = self.anomalies.repo.latest_completed_run(trade_date)
        if run is None:
            return None, []
        listing = self.anomalies.list_anomalies(run["trade_date"], None, None)
        candidates = [row for row in listing.get("boards") or [] if row.get("state") not in ("none", "insufficient_history")]
        # Shared output: no user's personal follows go into the input.
        return run, candidates[:MAX_CANDIDATES]

    def generate(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """Pick and save the recommendations of the latest completed run (of ``trade_date``). Never raises for
        model failures (falls back to the rule ranking); returns ``{status, run_id, trade_date, source, count, note}``."""
        run, candidates = self._inputs(trade_date)
        if run is None:
            return {"status": "skipped", "run_id": None, "trade_date": None, "source": None, "count": 0,
                    "note": "还没有完成的板块运行"}
        day: date = run["trade_date"]
        if not candidates:
            self.prefs.replace_recommendations(run["id"], day, [], source="rule", model=None, note="今天没有异动板块")
            return {"status": "skipped", "run_id": run["id"], "trade_date": day.isoformat(), "source": "rule",
                    "count": 0, "note": "今天没有异动板块"}

        allowed = {row["id"]: row for row in candidates}
        items: List[Dict[str, Any]] = []
        note: Optional[str] = None
        if not self._llm_call and not self._model_label():
            note = "没有配置分析模型，按规则排序"
        else:
            try:
                items = parse_reply(self._call_model(build_prompt(day, candidates)), allowed)
                if not items:
                    note = "模型没有给出可用的板块，按规则排序"
            except Exception as exc:  # noqa: BLE001 - any model failure falls back to the rule ranking
                logger.warning("[BoardRecommend] model call failed: %s", exc)
                note = f"模型调用失败（{str(exc)[:80]}），按规则排序"
        source = "model" if items else "rule"
        if not items:
            items = rule_ranking(candidates)
        self.prefs.replace_recommendations(
            run["id"], day, items, source=source, model=self._model_label() if source == "model" else None, note=note,
        )
        return {"status": "succeeded" if source == "model" else "partial", "run_id": run["id"],
                "trade_date": day.isoformat(), "source": source, "count": len(items), "note": note}

    # -- reading ---------------------------------------------------------
    def view(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        """API payload: the latest run's recommendations with each board's current row."""
        run = self.anomalies.repo.latest_completed_run(trade_date)
        if run is None:
            return {"trade_date": None, "status": "no_run", "source": None, "model": None, "note": None,
                    "generated_at": None, "items": []}
        stored = self.prefs.get_recommendations(run["id"])
        generating = is_refreshing(run["id"])
        _, rows = self.anomalies.board_rows([item["board_ref"] for item in stored], run["trade_date"])
        first = stored[0] if stored else {}
        return {
            "trade_date": run["trade_date"].isoformat(),
            "status": "generating" if generating else ("ready" if stored else "none"),
            "source": first.get("source"),
            "model": first.get("model"),
            "note": first.get("note"),
            "generated_at": first.get("created_at"),
            "items": [
                {
                    "rank": item["rank"],
                    "reason": item["reason"],
                    "board": rows.get(item["board_ref"]) or {
                        "id": item["board_ref"], "name": item["board_name"], "board_type": item["board_type"],
                        "state": "none", "tags": [], "change_pct": None, "change_5d_pct": None, "excess_pct": None,
                        "amount_ratio": None, "breadth_pct": None, "limit_up_count": None, "broken_rate_pct": None,
                        "reason": "",
                    },
                }
                for item in stored
            ],
        }


def is_refreshing(run_id: int) -> bool:
    with _refresh_lock:
        thread = _refreshing.get(run_id)
        return bool(thread and thread.is_alive())


def start_refresh(service_factory: Callable[[], BoardRecommendationService]) -> Dict[str, Any]:
    """Regenerate the latest run's recommendations in a background thread (one at a time per run)."""
    service = service_factory()
    run = service.anomalies.repo.latest_completed_run(None)
    if run is None:
        return {"status": "no_run", "trade_date": None}
    run_id = run["id"]
    with _refresh_lock:
        thread = _refreshing.get(run_id)
        if thread and thread.is_alive():
            return {"status": "generating", "trade_date": run["trade_date"].isoformat()}

        def work() -> None:
            try:
                service.generate(run["trade_date"])
            except Exception as exc:  # noqa: BLE001
                logger.error("[BoardRecommend] refresh failed: %s", exc, exc_info=True)

        thread = threading.Thread(target=work, name=f"board-recommend-{run_id}", daemon=True)
        _refreshing[run_id] = thread
        thread.start()
    return {"status": "generating", "trade_date": run["trade_date"].isoformat()}
