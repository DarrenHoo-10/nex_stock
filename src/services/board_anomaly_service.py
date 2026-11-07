# -*- coding: utf-8 -*-
"""Read side of the board anomaly page (``/api/v1/boards``).

Maps the stored run results (``daily_runs`` / ``board_signal`` / ``board_daily`` /
``market_daily``) to the payloads in ``api/v1/schemas/boards.py``. Constructed without
arguments by ``api.deps.get_board_anomaly_service``. Nothing here calls an external API.

Data selection
--------------
* A page shows the newest completed (``succeeded`` / ``partial``) run of the trade date;
  with no date, the newest trade date that has a completed run.
* No completed run: an empty list with an explanatory ``history_note`` (never a fake zero),
  and ``get_board_detail`` still resolves known boards with an empty verdict.
* Listing rule: boards whose state is not ``none`` or that carry a tag (e.g. a single-stock
  driven rise that was filtered out) are listed; members of a merged concept group other than
  its representative are hidden; boards whose key inputs were missing are counted in the
  ``history_note`` instead of being listed one by one. ``state=none`` returns the strongest
  ``none`` boards (up to 100) for browsing.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.board_common import (
    MEMBERSHIP_OBSERVED_FROM,
    CalendarFn,
    default_calendar_fn,
    session_axis,
)
from src.services.board_thresholds import (
    describe_thresholds,
    load_current_thresholds,
    validate_overrides,
)

logger = logging.getLogger(__name__)

STATE_LABELS = {
    "first_launch": "首次启动",
    "strengthening": "持续走强",
    "divergence": "高位分歧",
    "ebbing": "退潮",
    "abnormal_drop": "异常下跌",
    "insufficient_history": "历史数据不足",
    "none": "未异动",
}
_STATE_ORDER = {
    s: i for i, s in enumerate(
        ("first_launch", "strengthening", "divergence", "abnormal_drop", "ebbing", "insufficient_history", "none")
    )
}
INDEX_NAMES = {
    "000985.CSI": "中证全指",
    "000300.SH": "沪深300",
    "000905.SH": "中证500",
    "000852.SH": "中证1000",
}
NONE_STATE_LIST_LIMIT = 100
EXCESS_SERIES_DAYS = 8
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _round(value: Optional[float], digits: int = 2) -> Optional[float]:
    return None if value is None else round(float(value), digits)


def _signed(value: Optional[float], digits: int = 2, suffix: str = "%") -> str:
    return "—" if value is None else f"{value:+.{digits}f}{suffix}"


def _plain(value: Optional[float], digits: int = 1, suffix: str = "") -> str:
    return "—" if value is None else f"{value:.{digits}f}{suffix}"


class BoardAnomalyService:
    def __init__(
        self,
        repo: Optional[BoardAnalysisRepository] = None,
        calendar_fn: Optional[CalendarFn] = default_calendar_fn,
    ) -> None:
        self._repo = repo
        self._calendar_fn = calendar_fn

    @property
    def repo(self) -> BoardAnalysisRepository:
        if self._repo is None:
            self._repo = BoardAnalysisRepository()
        return self._repo

    # ------------------------------------------------------------------
    # thresholds
    # ------------------------------------------------------------------
    def get_threshold_definitions(self) -> List[Dict[str, Any]]:
        effective, _warnings = load_current_thresholds()
        return describe_thresholds(effective)

    def validate_threshold_overrides(self, overrides: Dict[str, Any]) -> List[str]:
        return validate_overrides(overrides)

    # ------------------------------------------------------------------
    # list
    # ------------------------------------------------------------------
    def list_anomalies(
        self,
        trade_date: Optional[date],
        board_type: Optional[str],
        state: Optional[str],
    ) -> Dict[str, Any]:
        run = self.repo.latest_completed_run(trade_date)
        if run is None:
            day = trade_date or datetime.now(_CN_TZ).date()
            note = (
                f"{day.isoformat()} 暂无板块判定结果（尚未完成当日运行）" if trade_date
                else "暂无板块数据：请先回填个股与板块快照，再运行板块日度流程"
            )
            return {"trade_date": day.isoformat(), "market": self._market_summary(day, None), "boards": [],
                    "history_note": note}

        day: date = run["trade_date"]
        catalog = {b["id"]: b for b in self.repo.list_catalog(("industry", "concept"))}
        daily = self.repo.get_board_daily_range([day])
        signals = self.repo.get_signals(run["id"])

        rows: List[Dict[str, Any]] = []
        for sig in signals:
            board = catalog.get(sig["board_id"])
            if board is None or (board_type and board["board_type"] != board_type):
                continue
            flags = sig["flags"]
            if (flags.get("merge") or {}).get("role") == "member":
                continue
            if flags.get("core_missing") or flags.get("excluded"):
                continue
            tags = list(flags.get("tags") or [])
            stype = sig["signal_type"]
            if state is None:
                if stype == "none" and not tags:
                    continue
            elif stype != state:
                continue
            row = self._board_row(board, daily.get((day, board["id"])), sig)
            rows.append(row)

        def sort_key(row: Dict[str, Any]) -> Any:
            excess = row["excess_pct"]
            return (_STATE_ORDER.get(row["state"], 9), -(excess if excess is not None else -1e9))

        rows.sort(key=sort_key)
        if state == "none":
            rows = rows[:NONE_STATE_LIST_LIMIT]
        return {
            "trade_date": day.isoformat(),
            "market": self._market_summary(day, run),
            "boards": rows,
            "history_note": self._history_note(day, run),
        }

    # ------------------------------------------------------------------
    # detail
    # ------------------------------------------------------------------
    def get_board_detail(self, board_id: str, trade_date: Optional[date]) -> Optional[Dict[str, Any]]:
        matches = self.repo.find_boards(board_id)
        if not matches:
            return None
        board = matches[0]
        run = self.repo.latest_completed_run(trade_date)
        day: date = run["trade_date"] if run else (trade_date or datetime.now(_CN_TZ).date())

        sig: Optional[Dict[str, Any]] = None
        if run is not None:
            found = self.repo.get_signals(run["id"], board["id"])
            sig = found[0] if found else None
        daily_rows = self.repo.get_board_daily_range([day], [board["id"]])
        today = daily_rows.get((day, board["id"]))

        data_gaps: List[str] = []
        if run is None:
            data_gaps.append(f"{day.isoformat()} 暂无板块判定结果")
        elif sig is None:
            data_gaps.append("该板块在本次运行中没有判定记录（可能当日尚未入库）")
        sig = sig or {"signal_type": "none", "flags": {}, "evidence": {}, "driver": "unknown", "reason": ""}

        base = self._board_row(board, today, sig)
        evidence = sig["evidence"] or {}
        flags = sig["flags"] or {}
        tags = list(flags.get("tags") or [])
        quality = _quality(today)

        data_gaps.extend(evidence.get("data_gaps") or [])
        data_gaps.extend(_quality_gaps(quality))
        if day < MEMBERSHIP_OBSERVED_FROM or quality.get("member_origin") == "backfill":
            data_gaps.append("成分为倒推数据（早于 2024-12-20），存在幸存者偏差")
        merge = flags.get("merge") or evidence.get("merge")
        if merge and merge.get("role") == "member":
            data_gaps.append(f"该概念与 {merge.get('representative')} 成分高度重合，已合并展示")

        driver = evidence.get("driver") or {}
        excluded = (today or {}).get("excluded_reason")
        can_pick = bool(
            today is not None
            and excluded not in ("broad", "too_few")
            and "single_stock" not in tags
            and base["state"] != "insufficient_history"
            and (today.get("valid_member_count") or 0) > 0
        )
        reason = base["reason"]
        label = STATE_LABELS.get(base["state"], base["state"])
        verdict = f"{label}：{reason}" if reason else label
        if not reason and base["state"] == "none":
            verdict = "未达到异动条件"

        detail = dict(base)
        detail.update(
            {
                "verdict_text": verdict,
                "drivers": driver.get("text") or "原因未明：未接入消息 / 业绩数据，仅按行情判断",
                "driver_kind": sig.get("driver") or "unknown",
                "metrics": self._metrics(board, today),
                "excess_series": self._excess_series(board["id"], day),
                "representatives": self._representatives(board["id"], day, today),
                "evidence": list(evidence.get("lines") or ([reason] if reason else [])),
                "can_pick": can_pick,
                "data_gaps": list(dict.fromkeys(g for g in data_gaps if g)),
            }
        )
        return detail

    # ------------------------------------------------------------------
    # pieces
    # ------------------------------------------------------------------
    def _board_row(self, board: Dict[str, Any], daily: Optional[Dict[str, Any]], sig: Dict[str, Any]) -> Dict[str, Any]:
        d = daily or {}
        concept = board["board_type"] == "concept"
        return {
            "id": board["ref"],
            "name": board["name"],
            "board_type": board["board_type"],
            "state": sig["signal_type"],
            "tags": list((sig.get("flags") or {}).get("tags") or []),
            "change_pct": _round(d.get("ret_cap")),
            "change_5d_pct": _round(d.get("ret_5d")),
            "excess_pct": _round(d.get("excess_ret")),
            "amount_ratio": _round(d.get("amount_ratio_5d")),
            "breadth_pct": _round(d.get("up_ratio"), 1),
            "limit_up_count": d.get("limit_up_count") if concept else None,
            "broken_rate_pct": _round(d.get("break_rate"), 1) if concept else None,
            "reason": sig.get("reason") or "",
        }

    def _market_summary(self, day: date, run: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        summary: Dict[str, Any] = {
            "index_name": "中证全指", "index_change_pct": None, "up_count": None, "down_count": None,
            "limit_up_count": None, "limit_down_count": None, "turnover_yi": None, "broad_rally": False, "note": None,
        }
        if run is None:
            return summary
        market = self.repo.get_market_daily(day) or {}
        code = market.get("benchmark_code")
        if code:
            summary["index_name"] = INDEX_NAMES.get(code, code)
        summary.update(
            index_change_pct=_round(market.get("benchmark_ret")),
            up_count=market.get("up_count"),
            down_count=market.get("down_count"),
            limit_up_count=market.get("limit_up_count"),
            limit_down_count=market.get("limit_down_count"),
            turnover_yi=_round(None if market.get("total_amount") is None else market["total_amount"] / 1e8, 1),
        )
        values = (run.get("thresholds_snapshot") or {}).get("values") or load_current_thresholds()[0]
        all_a = market.get("all_a_ret")
        notes: List[str] = []
        if all_a is not None and all_a >= values["broad_rally_pct"]:
            summary["broad_rally"] = True
            notes.append(f"全 A 等权涨幅 {all_a:+.2f}%，普涨日保护生效：涨停与上涨家数类条件不参与判定，只看排名")
        for step in run.get("steps") or []:
            if step.get("key") in ("ingest", "metrics") and step.get("status") in ("partial", "failed") and step.get("detail"):
                notes.append(f"{step.get('label')}：{step['detail']}")
        if run.get("status") == "partial" and not notes:
            notes.append("本次运行存在数据缺口，部分判定可能不可用")
        summary["note"] = "；".join(notes) if notes else None
        return summary

    def _history_note(self, day: date, run: Dict[str, Any]) -> Optional[str]:
        signals_step = next((s for s in run.get("steps") or [] if s.get("key") == "signals"), None)
        data = (signals_step or {}).get("data") or {}
        parts: List[str] = []
        sessions = data.get("history_sessions")
        requirements = data.get("requirements") or {}
        if sessions is not None and requirements:
            short = sorted(
                ((need, state) for state, need in requirements.items() if sessions < need), key=lambda x: x[0]
            )
            if short:
                labels = "、".join(f"{STATE_LABELS.get(state, state)}需 {need} 天" for need, state in short)
                parts.append(f"板块历史积累 {sessions} 天，部分判定不可用（{labels}）")
        not_evaluated = data.get("not_evaluated") or {}
        if not_evaluated.get("no_membership"):
            parts.append(f"{not_evaluated['no_membership']} 个板块暂无成分数据，未参与判定")
        if data.get("core_missing"):
            parts.append(f"{data['core_missing']} 个板块因当日关键数据缺失无法判定")
        if day < MEMBERSHIP_OBSERVED_FROM:
            parts.append("成分含倒推数据（早于 2024-12-20），存在幸存者偏差")
        for warning in data.get("warnings") or []:
            parts.append(str(warning))
        return "；".join(parts) if parts else None

    def _metrics(self, board: Dict[str, Any], today: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        d = today or {}
        concept = board["board_type"] == "concept"
        ratio = d.get("amount_ratio_5d")
        up_ratio = d.get("up_ratio")
        up, down = d.get("up_count"), d.get("down_count")
        median = d.get("excess_vs_median")
        items = [
            {"label": "成交额倍数", "value": "—" if ratio is None else f"{ratio:.1f} 倍",
             "hint": "相对近 5 日均值" if ratio is not None else "近 5 日成交额均值缺失（历史不足）"},
            {"label": "上涨家数占比", "value": _plain(up_ratio, 1, "%"),
             "hint": f"{up} 只上涨 / {down} 只下跌" if up is not None and down is not None else "数据缺失"},
            {"label": "当日超额", "value": _signed(d.get("excess_ret")),
             "hint": "相对基准" + (f"；相对同类中位数 {median:+.2f}%" if median is not None else "")},
            {"label": "5 日涨幅", "value": _signed(d.get("ret_5d")),
             "hint": f"5 日超额 {d['excess_5d']:+.2f}%" if d.get("excess_5d") is not None else "5 日数据不足"},
        ]
        if concept:
            streak = d.get("max_consecutive")
            one_word = d.get("one_word_count")
            hint_bits = []
            if streak is not None:
                hint_bits.append(f"最高 {streak} 连板")
            if one_word:
                hint_bits.append(f"一字板 {one_word} 家")
            items.append({"label": "涨停家数",
                          "value": "—" if d.get("limit_up_count") is None else f"{d['limit_up_count']} 家",
                          "hint": "；".join(hint_bits) or ("数据缺失" if d.get("limit_up_count") is None else None)})
            touched = d.get("touched_up_count")
            br = d.get("break_rate")
            if br is not None:
                br_hint = f"触及涨停 {touched} 家"
            elif touched is not None:
                br_hint = f"触及涨停 {touched} 家，样本不足不计算"
            else:
                br_hint = "数据缺失"
            items.append({"label": "炸板率", "value": _plain(br, 1, "%"), "hint": br_hint})
        else:
            items.append({"label": "涨停家数", "value": "—", "hint": "行业板块不使用涨停类条件"})
            items.append({"label": "炸板率", "value": "—", "hint": "行业板块不使用涨停类条件"})
        return items

    def _excess_series(self, board_id: int, day: date) -> List[Dict[str, Any]]:
        ingested = self.repo.list_ingested_dates(upto=day, limit=EXCESS_SERIES_DAYS * 2)
        axis = session_axis(day, EXCESS_SERIES_DAYS, ingested, self._calendar_fn)
        if not axis:
            axis = [day]
        rows = self.repo.get_board_daily_range(axis, [board_id])
        series = []
        for d in sorted(axis):
            value = (rows.get((d, board_id)) or {}).get("excess_ret")
            series.append({"trade_date": d.isoformat(), "excess_pct": _round(value)})
        return series

    def _representatives(self, board_id: int, day: date, today: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if today is None or not today.get("valid_member_count"):
            return []
        members = sorted(self.repo.get_members_on(board_id, day))
        stocks = self.repo.get_stock_rows(day, members)
        usable = [
            r for r in stocks.values()
            if not r.get("is_suspended") and not r.get("is_st") and r.get("pct_chg") is not None
        ]
        if not usable:
            return []
        out: List[Dict[str, Any]] = []
        used: set = set()

        def add(row: Optional[Dict[str, Any]], role: str) -> None:
            if row is None or row["code"] in used:
                return
            used.add(row["code"])
            out.append({"code": row["code"], "name": row.get("name") or row["code"], "role": role,
                        "close": _round(row.get("close")), "change_pct": _round(row.get("pct_chg"))})

        by_code = {r["code"]: r for r in usable}
        add(by_code.get(today.get("leader_code")) or max(usable, key=lambda r: r["pct_chg"]), "leader")
        by_amount = sorted((r for r in usable if r.get("amount") is not None), key=lambda r: r["amount"], reverse=True)
        top_amount = by_code.get(today.get("top_amount_code"))
        if top_amount is not None and top_amount["code"] in used:
            top_amount = next((r for r in by_amount if r["code"] not in used), None)
        add(top_amount or next((r for r in by_amount if r["code"] not in used), None), "turnover")
        for row in sorted(usable, key=lambda r: r["pct_chg"], reverse=True):
            if len([x for x in out if x["role"] == "follower"]) >= 2:
                break
            add(row, "follower")
        return out


def _quality(row: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    import json

    raw = (row or {}).get("quality")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


_FLAG_TEXT = {
    "no_membership": "暂无成分数据",
    "float_mv_missing": "流通市值缺失，无法按市值加权",
    "limit_state_missing": "涨跌停状态缺失",
    "touched_limit_up_missing": "触及涨停数据缺失",
    "amount_missing": "成交额缺失",
    "ret_5d_incomplete": "近 5 日中有交易日缺少板块涨幅",
    "amount_history_incomplete": "前 5 日成交额不完整",
    "benchmark_5d_missing": "近 5 日基准涨幅不完整",
    "break_rate_state_missing": "炸板率所需的涨停状态缺失",
    "consecutive_up_missing": "连板数缺失",
}


def _quality_gaps(quality: Dict[str, Any]) -> List[str]:
    gaps: List[str] = []
    for flag in quality.get("flags") or []:
        name = str(flag).split(":")[0]
        text = _FLAG_TEXT.get(name)
        if text:
            gaps.append(text if ":" not in str(flag) else f"{text}（{str(flag).split(':', 1)[1]}）")
        elif name.startswith("history_short"):
            gaps.append("历史交易日不足，部分多日指标不可用")
        elif name.startswith("peer_pool_small"):
            gaps.append("同类板块数量不足，排名不可用")
    missing = quality.get("missing_sessions") or []
    if missing:
        gaps.append(f"近期缺少 {len(missing)} 个交易日的行情入库（{', '.join(missing[:3])}）")
    return gaps
