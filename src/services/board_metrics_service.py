# -*- coding: utf-8 -*-
"""Board / market metrics computed from the local snapshot tables.

Implements docs/design/core-backend-design.md 2.6 and 2.7: for one trade date it reads
``market_stock_daily`` and ``board_membership`` (members as of that date) and writes the
calculated columns of ``market_daily`` and ``board_daily``. No external request is made.

Formula summary (``CALC_VERSION`` changes whenever one of these does):

* Valid member: not suspended / ST / in delisting consolidation / a new listing without
  price limits / resuming from suspension, and with a close and a daily change.
  Exclusions are counted in ``quality["exclusions"]``; they are never silently dropped.
* ``ret_cap``: weighted by the previous day's float market cap
  (``float_mv / (1 + pct_chg/100)``), the usual index convention, so a stock does not get
  more weight for having risen. Needs >= 80% of the valid members weighted, else NULL.
* ``ret_eq``: plain mean of ``pct_chg``.
* ``excess_ret``: ``ret_cap - benchmark_ret``; ``excess_vs_median``: ``ret_cap`` minus the
  median ``ret_cap`` of the same-type peer pool (boards not excluded as broad / too_few).
* ``rank_pct``: mid-rank percentile of ``excess_ret`` in the peer pool, 100 = strongest
  (so "top 10%" is ``rank_pct >= 90``). NULL when the pool has fewer than 5 boards.
* 5-day fields compound the stored daily ``ret_cap`` (and benchmark) of the last 5
  sessions; they are NULL if any of the 5 days is missing.
* ``amount_ma5_prev``: mean board turnover of the previous 5 sessions (today excluded);
  ``amount_peak_20d``: max over the last 20 sessions (needs >= 10 days).
* Ratios (``up_ratio``, ``break_rate``, ``drop7_ratio``, ``top_contrib_share``) are percents.
* Missing inputs give NULL plus a flag in ``quality["flags"]``; a count is only produced
  when >= 95% of the valid members carry the underlying field.
"""

from __future__ import annotations

import logging
import statistics
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple
from zoneinfo import ZoneInfo

from src.repositories.board_analysis_repo import BoardAnalysisRepository, dumps_json
from src.services.board_common import (
    MEMBERSHIP_OBSERVED_FROM,
    CalendarFn,
    as_date,
    default_calendar_fn,
    session_axis,
)
from src.services.board_thresholds import load_current_thresholds

logger = logging.getLogger(__name__)

CALC_VERSION = "1"  # bump when any formula above changes; stored on every board_daily row
RANK_MIN_PEERS = 5
MIN_WEIGHT_COVERAGE = 0.8
MIN_FIELD_COVERAGE = 0.95
PEAK_MIN_DAYS = 10
HISTORY_SESSIONS = 20
# Sessions without price limits after listing, by exchange board.
NO_LIMIT_SESSIONS = {"main": 5, "gem": 5, "star": 5, "bse": 1}
DEFAULT_NO_LIMIT_SESSIONS = 5
UP_STATES = ("up", "one_word_up")
DOWN_STATES = ("down", "one_word_down")
BOARD_TYPES = ("industry", "concept")
_CN_TZ = ZoneInfo("Asia/Shanghai")


@dataclass
class MetricsReport:
    trade_date: date
    status: str = "pending"  # computed / no_data
    boards_total: int = 0
    boards_computed: int = 0
    excluded: Dict[str, int] = field(default_factory=dict)
    market_flags: List[str] = field(default_factory=list)
    merge_groups: int = 0
    calc_version: str = CALC_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_date": self.trade_date.isoformat(),
            "status": self.status,
            "boards_total": self.boards_total,
            "boards_computed": self.boards_computed,
            "excluded": dict(self.excluded),
            "market_flags": list(self.market_flags),
            "merge_groups": self.merge_groups,
            "calc_version": self.calc_version,
        }


def midrank_percentile(values: Sequence[float], value: float) -> float:
    """Mid-rank percentile in [0, 100]; the largest value of ``n`` gets ``100 - 50/n``."""
    n = len(values)
    less = sum(1 for v in values if v < value)
    equal = sum(1 for v in values if v == value)
    rank = less + (equal + 1) / 2.0
    return (rank - 0.5) / n * 100.0


def compound_pct(returns: Sequence[float]) -> float:
    total = 1.0
    for r in returns:
        total *= 1.0 + r / 100.0
    return (total - 1.0) * 100.0


def jaccard(a: Set[str], b: Set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


class BoardMetricsService:
    def __init__(
        self,
        repo: Optional[BoardAnalysisRepository] = None,
        thresholds: Optional[Dict[str, Any]] = None,
        calendar_fn: Optional[CalendarFn] = default_calendar_fn,
    ) -> None:
        self._repo = repo
        self._thresholds = thresholds
        self._calendar_fn = calendar_fn

    @property
    def repo(self) -> BoardAnalysisRepository:
        if self._repo is None:
            self._repo = BoardAnalysisRepository()
        return self._repo

    @property
    def thresholds(self) -> Dict[str, Any]:
        if self._thresholds is None:
            self._thresholds, _ = load_current_thresholds()
        return self._thresholds

    # ------------------------------------------------------------------
    # public
    # ------------------------------------------------------------------
    def ensure_history(self, trade_date: Any, sessions: int = HISTORY_SESSIONS) -> List[date]:
        """Compute earlier ingested days that have no current-version metrics yet (oldest first)."""
        day = as_date(trade_date)
        earlier = [d for d in self.repo.list_ingested_dates(upto=day, limit=sessions + 1) if d < day]
        computed: List[date] = []
        for d in sorted(earlier):
            if self.repo.count_computed_board_rows(d, CALC_VERSION) == 0:
                if self.compute_trade_date(d).status == "computed":
                    computed.append(d)
        return computed

    def compute_range(self, dates: Sequence[Any]) -> List[MetricsReport]:
        """Compute several dates oldest-first (later days read the earlier days' results)."""
        return [self.compute_trade_date(d) for d in sorted(as_date(x) for x in dates)]

    def compute_trade_date(self, trade_date: Any) -> MetricsReport:
        day = as_date(trade_date)
        report = MetricsReport(trade_date=day)
        stocks = self.repo.load_stock_day(day)
        if not stocks:
            report.status = "no_data"
            return report

        th = self.thresholds
        ingested = self.repo.list_ingested_dates(upto=day, limit=HISTORY_SESSIONS * 2)
        ingested_set = set(ingested)
        axis = session_axis(day, HISTORY_SESSIONS, ingested, self._calendar_fn)
        if not axis or axis[0] != day:
            axis = [day] + [d for d in axis if d < day]
        missing_sessions = [d for d in axis if d not in ingested_set]

        market_row = self.repo.get_market_daily(day) or {}
        benchmark_ret = market_row.get("benchmark_ret")

        states = self._classify_stocks(day, stocks, axis, ingested_set)
        market_values, market_flags, market_quality = self._market_metrics(day, stocks, states, th, benchmark_ret)
        report.market_flags = market_flags

        members_map = self.repo.get_all_members_on(day)
        board_rows = self._board_rows(
            day, states, members_map, market_values, benchmark_ret, axis, missing_sessions, ingested_set, th, report
        )
        report.merge_groups = self._merge_groups(day, members_map, th)

        ingestion = market_row.get("quality", {}).get("ingestion") or {}
        market_quality.update(
            {
                # where turnover / float cap and limit facts came from: "daily_basic" / "limit_list_d"
                # or "derived" (17:00 run, replaced by the real data when the day is completed)
                "input_sources": {"basic": ingestion.get("basic_source"), "limit": ingestion.get("limit_source")},
                "calc_version": CALC_VERSION,
                "history_sessions": len(axis),
                "missing_sessions": [d.isoformat() for d in missing_sessions],
                "boards_computed": report.boards_computed,
                "computed_at": datetime.now(_CN_TZ).isoformat(timespec="seconds"),
            }
        )
        self.repo.write_board_daily(day, board_rows)
        self.repo.write_market_metrics(day, market_values, market_quality)
        report.status = "computed"
        logger.info(
            "[BoardMetrics] %s: %d boards (%d computed), market flags=%s",
            day, report.boards_total, report.boards_computed, market_flags,
        )
        return report

    # ------------------------------------------------------------------
    # stock classification
    # ------------------------------------------------------------------
    def _classify_stocks(
        self,
        day: date,
        stocks: List[Dict[str, Any]],
        axis: List[date],
        ingested_set: Set[date],
    ) -> Dict[str, Tuple[Dict[str, Any], Optional[str]]]:
        """``{code: (row, exclusion_reason | None)}``."""
        prev_suspended: Optional[Set[str]] = None
        if len(axis) > 1 and axis[1] in ingested_set:
            prev_suspended = self.repo.get_suspended_codes(axis[1])

        def cutoff(sessions: int) -> date:
            if len(axis) >= sessions:
                return axis[sessions - 1]
            return day - timedelta(days=sessions + 2)

        cutoffs = {n: cutoff(n) for n in set(NO_LIMIT_SESSIONS.values()) | {DEFAULT_NO_LIMIT_SESSIONS}}

        result: Dict[str, Tuple[Dict[str, Any], Optional[str]]] = {}
        for row in stocks:
            name = row.get("name") or ""
            reason: Optional[str] = None
            if row.get("is_suspended"):
                reason = "suspended"
            elif row.get("is_st"):
                reason = "st"
            elif name.endswith("退") or name.startswith("退市"):
                reason = "delisting"
            elif row.get("list_date") is not None and row["list_date"] >= cutoffs[
                NO_LIMIT_SESSIONS.get(row.get("exchange_board") or "", DEFAULT_NO_LIMIT_SESSIONS)
            ]:
                reason = "new_listing"
            elif prev_suspended is not None and row["code"] in prev_suspended:
                reason = "resumed"
            elif row.get("close") is None or row.get("pct_chg") is None:
                reason = "missing_data"
            result[row["code"]] = (row, reason)
        return result

    # ------------------------------------------------------------------
    # market_daily
    # ------------------------------------------------------------------
    def _market_metrics(
        self,
        day: date,
        stocks: List[Dict[str, Any]],
        states: Dict[str, Tuple[Dict[str, Any], Optional[str]]],
        th: Dict[str, Any],
        benchmark_ret: Optional[float],
    ) -> Tuple[Dict[str, Any], List[str], Dict[str, Any]]:
        flags: List[str] = []
        trading = [r for r in stocks if not r.get("is_suspended") and r.get("pct_chg") is not None]
        valid = [row for row, reason in states.values() if reason is None]
        values: Dict[str, Any] = {
            "total_amount": None, "up_count": None, "down_count": None, "limit_up_count": None,
            "limit_down_count": None, "touched_up_count": None, "break_rate": None, "all_a_ret": None,
        }

        amounts = [r["amount"] for r in stocks if r.get("amount") is not None]
        if amounts:
            values["total_amount"] = float(sum(amounts))
        else:
            flags.append("total_amount_missing")

        if trading:
            values["up_count"] = sum(1 for r in trading if r["pct_chg"] > 0)
            values["down_count"] = sum(1 for r in trading if r["pct_chg"] < 0)
            known_state = [r for r in trading if r.get("limit_state") is not None]
            if len(known_state) / len(trading) >= MIN_FIELD_COVERAGE:
                values["limit_up_count"] = sum(1 for r in known_state if r["limit_state"] in UP_STATES)
                values["limit_down_count"] = sum(1 for r in known_state if r["limit_state"] in DOWN_STATES)
            else:
                flags.append("limit_state_missing")
            non_st = [r for r in trading if not r.get("is_st")]
            known_touch = [r for r in non_st if r.get("touched_limit_up") is not None]
            if non_st and len(known_touch) / len(non_st) >= MIN_FIELD_COVERAGE:
                touched = [r for r in known_touch if r["touched_limit_up"]]
                values["touched_up_count"] = len(touched)
                if len(touched) >= th["break_min_touched"] and all(r.get("limit_state") for r in touched):
                    broken = sum(1 for r in touched if r["limit_state"] not in UP_STATES)
                    values["break_rate"] = broken / len(touched) * 100.0
            else:
                flags.append("touched_limit_up_missing")
        else:
            flags.append("daily_missing")

        if valid:
            values["all_a_ret"] = sum(r["pct_chg"] for r in valid) / len(valid)
        else:
            flags.append("all_a_ret_missing")
        if benchmark_ret is None:
            flags.append("benchmark_missing")

        exclusions = Counter(reason for _row, reason in states.values() if reason)
        quality = {
            "flags": flags,
            "valid_stock_count": len(valid),
            "exclusions": dict(exclusions),
            "params": {"break_min_touched": th["break_min_touched"]},
        }
        return values, flags, quality

    # ------------------------------------------------------------------
    # board_daily
    # ------------------------------------------------------------------
    def _board_rows(
        self,
        day: date,
        states: Dict[str, Tuple[Dict[str, Any], Optional[str]]],
        members_map: Dict[int, Dict[str, str]],
        market_values: Dict[str, Any],
        benchmark_ret: Optional[float],
        axis: List[date],
        missing_sessions: List[date],
        ingested_set: Set[date],
        th: Dict[str, Any],
        report: MetricsReport,
    ) -> Dict[int, Dict[str, Any]]:
        catalog_all = self.repo.list_catalog(BOARD_TYPES)
        baseline = min((b["first_seen_date"] for b in catalog_all), default=None)
        boards = [
            b for b in catalog_all
            if b["first_seen_date"] <= day and (b["last_seen_date"] >= day or b["status"] == "active")
        ]
        report.boards_total = len(boards)
        ingested_asc = sorted(ingested_set)

        prior_axis = axis[1:]
        hist = self.repo.get_board_daily_range(prior_axis) if prior_axis else {}
        market_hist = self.repo.get_market_daily_range(axis)

        rows: Dict[int, Dict[str, Any]] = {}
        excluded_counter: Counter = Counter()
        for board in boards:
            members = members_map.get(board["id"], {})
            row = self._one_board(
                day, board, members, states, market_values, benchmark_ret, axis, missing_sessions,
                hist, market_hist, baseline, ingested_asc, th,
            )
            rows[board["id"]] = row
            if row["excluded_reason"]:
                excluded_counter[row["excluded_reason"]] += 1
            if row["ret_cap"] is not None or row["ret_eq"] is not None:
                report.boards_computed += 1
        report.excluded = dict(excluded_counter)

        self._apply_peer_ranks(rows, boards, th)
        for board in boards:
            row = rows[board["id"]]
            self._five_day_fields(day, board["id"], row, axis, hist, market_hist)
        self._apply_5d_ranks(rows, boards)
        now = datetime.now(_CN_TZ).replace(tzinfo=None)
        for row in rows.values():
            quality = row.pop("_quality")
            row["quality"] = dumps_json(quality)
            row["calc_version"] = CALC_VERSION
            row["computed_at"] = now
        return rows

    def _one_board(
        self,
        day: date,
        board: Dict[str, Any],
        members: Dict[str, str],
        states: Dict[str, Tuple[Dict[str, Any], Optional[str]]],
        market_values: Dict[str, Any],
        benchmark_ret: Optional[float],
        axis: List[date],
        missing_sessions: List[date],
        hist: Dict[Tuple[date, int], Dict[str, Any]],
        market_hist: Dict[date, Dict[str, Any]],
        baseline: Optional[date],
        ingested_asc: List[date],
        th: Dict[str, Any],
    ) -> Dict[str, Any]:
        flags: List[str] = []
        notes: List[str] = []
        exclusions: Counter = Counter()
        valid: List[Dict[str, Any]] = []
        origins: Counter = Counter()
        for code, origin in members.items():
            entry = states.get(code)
            if entry is None:
                exclusions["not_in_market"] += 1
                continue
            row, reason = entry
            if reason:
                exclusions[reason] += 1
                continue
            valid.append(row)
            origins[origin] += 1

        legacy = baseline is not None and board["first_seen_date"] <= baseline
        age_days = sum(1 for d in ingested_asc if board["first_seen_date"] <= d <= day)
        member_count = len(members)

        excluded: Optional[str] = None
        if member_count == 0:
            excluded = "too_few"
            flags.append("no_membership")
        elif member_count > th["broad_max_members"]:
            excluded = "broad"
        elif len(valid) < th["min_members"]:
            excluded = "too_few"
        elif board["board_type"] == "concept" and not legacy and age_days <= th["cooling_days"]:
            excluded = "cooling"

        member_origin = None
        if day < MEMBERSHIP_OBSERVED_FROM or (origins and set(origins) == {"backfill"}):
            member_origin = "backfill"
        elif origins:
            member_origin = "observed" if set(origins) == {"observed"} else "mixed"

        quality: Dict[str, Any] = {
            "flags": flags,
            "notes": notes,
            "member_origin": member_origin,
            "exclusions": dict(exclusions),
            "history_sessions": len(axis),
            "missing_sessions": [d.isoformat() for d in missing_sessions],
            "age_days": age_days,
            "age_known": not legacy,
            "params": {
                "min_members": th["min_members"],
                "broad_max_members": th["broad_max_members"],
                "cooling_days": th["cooling_days"],
                "break_min_touched": th["break_min_touched"],
            },
        }
        row: Dict[str, Any] = {column: None for column in _EMPTY_COLUMNS}
        row.update(
            member_count=member_count,
            valid_member_count=len(valid),
            excluded_reason=excluded,
            _quality=quality,
            _board_type=board["board_type"],
        )
        if excluded in ("broad", "too_few"):
            return row

        self._fill_day_metrics(row, valid, market_values, benchmark_ret, th, flags, notes, quality)
        return row

    def _fill_day_metrics(
        self,
        row: Dict[str, Any],
        valid: List[Dict[str, Any]],
        market_values: Dict[str, Any],
        benchmark_ret: Optional[float],
        th: Dict[str, Any],
        flags: List[str],
        notes: List[str],
        quality: Dict[str, Any],
    ) -> None:
        pct_rows = [r for r in valid if r.get("pct_chg") is not None]
        n = len(pct_rows)
        if n == 0:
            flags.append("pct_chg_missing")
            return

        row["ret_eq"] = sum(r["pct_chg"] for r in pct_rows) / n

        weighted: List[Tuple[Dict[str, Any], float]] = []
        for r in pct_rows:
            mv, pct = r.get("float_mv"), r["pct_chg"]
            if mv is not None and mv > 0 and 1.0 + pct / 100.0 > 0:
                weighted.append((r, mv / (1.0 + pct / 100.0)))
        if len(weighted) / n >= MIN_WEIGHT_COVERAGE:
            total_w = sum(w for _r, w in weighted)
            row["ret_cap"] = sum(w * r["pct_chg"] for r, w in weighted) / total_w
            if len(weighted) < n:
                flags.append(f"float_mv_partial:{n - len(weighted)}")
            contribs = sorted(
                ((r["code"], w * r["pct_chg"] / total_w) for r, w in weighted), key=lambda x: x[1], reverse=True
            )
            quality["top_contributors"] = [
                {"code": code, "contribution": round(c, 4)} for code, c in contribs[:3]
            ]
            top_code, top_contrib = contribs[0]
            row["top_contrib_code"] = top_code
            if row["ret_cap"] > 0 and top_contrib > 0:
                row["top_contrib_share"] = top_contrib / row["ret_cap"] * 100.0
        else:
            flags.append("float_mv_missing")

        if benchmark_ret is not None and row["ret_cap"] is not None:
            row["excess_ret"] = row["ret_cap"] - benchmark_ret

        amount_rows = [r for r in pct_rows if r.get("amount") is not None]
        if len(amount_rows) / n >= MIN_FIELD_COVERAGE:
            row["amount"] = float(sum(r["amount"] for r in amount_rows))
            total = market_values.get("total_amount")
            if total:
                row["amount_share"] = row["amount"] / total
        else:
            flags.append("amount_missing")

        row["up_count"] = sum(1 for r in pct_rows if r["pct_chg"] > 0)
        row["down_count"] = sum(1 for r in pct_rows if r["pct_chg"] < 0)
        row["up_ratio"] = row["up_count"] / n * 100.0
        row["drop7_ratio"] = sum(1 for r in pct_rows if r["pct_chg"] < -7.0) / n * 100.0

        leader = max(pct_rows, key=lambda r: (r["pct_chg"], r.get("amount") or 0.0))
        row["leader_code"] = leader["code"]
        if amount_rows:
            row["top_amount_code"] = max(amount_rows, key=lambda r: r["amount"])["code"]

        known_state = [r for r in pct_rows if r.get("limit_state") is not None]
        if len(known_state) / n >= MIN_FIELD_COVERAGE:
            up_rows = [r for r in known_state if r["limit_state"] in UP_STATES]
            row["limit_up_count"] = len(up_rows)
            row["limit_down_count"] = sum(1 for r in known_state if r["limit_state"] in DOWN_STATES)
            row["one_word_count"] = sum(1 for r in known_state if r["limit_state"] == "one_word_up")
            streaks = [r["consecutive_up"] for r in up_rows if r.get("consecutive_up") is not None]
            if not up_rows:
                row["max_consecutive"] = 0
            elif streaks:
                row["max_consecutive"] = int(max(streaks))
            else:
                flags.append("consecutive_up_missing")
        else:
            flags.append("limit_state_missing")

        known_touch = [r for r in pct_rows if r.get("touched_limit_up") is not None]
        if len(known_touch) / n >= MIN_FIELD_COVERAGE:
            touched = [r for r in known_touch if r["touched_limit_up"]]
            row["touched_up_count"] = len(touched)
            if len(touched) < th["break_min_touched"]:
                notes.append("break_rate_sample_low")
            elif all(r.get("limit_state") for r in touched):
                broken = sum(1 for r in touched if r["limit_state"] not in UP_STATES)
                row["break_rate"] = broken / len(touched) * 100.0
            else:
                flags.append("break_rate_state_missing")
        else:
            flags.append("touched_limit_up_missing")

    # ------------------------------------------------------------------
    # ranks and multi-day fields
    # ------------------------------------------------------------------
    def _apply_peer_ranks(self, rows: Dict[int, Dict[str, Any]], boards: List[Dict[str, Any]], th: Dict[str, Any]) -> None:
        for board_type in BOARD_TYPES:
            pool = [
                bid for bid, row in rows.items()
                if row["_board_type"] == board_type and row["excluded_reason"] in (None, "cooling")
            ]
            excess = [rows[b]["excess_ret"] for b in pool if rows[b]["excess_ret"] is not None]
            rets = [rows[b]["ret_cap"] for b in pool if rows[b]["ret_cap"] is not None]
            small = len(excess) < RANK_MIN_PEERS
            median_ret = statistics.median(rets) if len(rets) >= RANK_MIN_PEERS else None
            for bid in pool:
                row = rows[bid]
                if row["excess_ret"] is not None:
                    if small:
                        row["_quality"]["flags"].append(f"peer_pool_small:{len(excess)}")
                    else:
                        row["rank_pct"] = midrank_percentile(excess, row["excess_ret"])
                if median_ret is not None and row["ret_cap"] is not None:
                    row["excess_vs_median"] = row["ret_cap"] - median_ret

    def _five_day_fields(
        self,
        day: date,
        board_id: int,
        row: Dict[str, Any],
        axis: List[date],
        hist: Dict[Tuple[date, int], Dict[str, Any]],
        market_hist: Dict[date, Dict[str, Any]],
    ) -> None:
        if row["excluded_reason"] in ("broad", "too_few"):
            return
        flags = row["_quality"]["flags"]

        def past(d: date, column: str) -> Optional[float]:
            record = hist.get((d, board_id))
            return record.get(column) if record else None

        window5 = axis[:5]
        if len(window5) == 5:
            rets: List[Optional[float]] = [row["ret_cap"]] + [past(d, "ret_cap") for d in window5[1:]]
            if all(r is not None for r in rets):
                row["ret_5d"] = compound_pct([r for r in rets if r is not None])
                bench = [(market_hist.get(d) or {}).get("benchmark_ret") for d in window5]
                if all(b is not None for b in bench):
                    row["excess_5d"] = row["ret_5d"] - compound_pct([b for b in bench if b is not None])
                else:
                    flags.append("benchmark_5d_missing")
            else:
                flags.append("ret_5d_incomplete")
        else:
            flags.append("history_short:ret_5d")

        prev5 = axis[1:6]
        if len(prev5) == 5:
            amounts = [past(d, "amount") for d in prev5]
            if all(a is not None for a in amounts):
                row["amount_ma5_prev"] = sum(a for a in amounts if a is not None) / 5.0
                if row["amount"] is not None and row["amount_ma5_prev"] > 0:
                    row["amount_ratio_5d"] = row["amount"] / row["amount_ma5_prev"]
            else:
                flags.append("amount_history_incomplete")
        else:
            flags.append("history_short:amount_ma5")

        window20 = [row["amount"]] + [past(d, "amount") for d in axis[1:HISTORY_SESSIONS]]
        present = [a for a in window20 if a is not None]
        if len(present) >= PEAK_MIN_DAYS:
            row["amount_peak_20d"] = float(max(present))
        else:
            flags.append("history_short:amount_peak")

    def _apply_5d_ranks(self, rows: Dict[int, Dict[str, Any]], boards: List[Dict[str, Any]]) -> None:
        for board_type in BOARD_TYPES:
            pool = [
                bid for bid, row in rows.items()
                if row["_board_type"] == board_type and row["excluded_reason"] in (None, "cooling")
            ]
            values = [rows[b]["excess_5d"] for b in pool if rows[b]["excess_5d"] is not None]
            if len(values) < RANK_MIN_PEERS:
                continue
            for bid in pool:
                if rows[bid]["excess_5d"] is not None:
                    rows[bid]["rank_5d_pct"] = midrank_percentile(values, rows[bid]["excess_5d"])

    # ------------------------------------------------------------------
    # concept merge groups
    # ------------------------------------------------------------------
    def _merge_groups(self, day: date, members_map: Dict[int, Dict[str, str]], th: Dict[str, Any]) -> int:
        """Group concept boards with member Jaccard >= threshold (greedy, anchored on the
        largest board of each group). Skipped when a newer date already wrote groups."""
        catalog = [b for b in self.repo.list_catalog(["concept"]) if b["first_seen_date"] <= day]
        if any(b["merge_computed_on"] and b["merge_computed_on"] > day for b in catalog):
            return 0
        sets = {
            b["id"]: set(members_map.get(b["id"], {}))
            for b in catalog
            if 0 < len(members_map.get(b["id"], {})) <= th["broad_max_members"]
        }
        threshold = float(th["merge_jaccard"])
        order = sorted(sets, key=lambda bid: (-len(sets[bid]), bid))
        ref_by_id = {b["id"]: b["ref"] for b in catalog}
        anchors: List[int] = []
        assignment: Dict[int, int] = {}
        for bid in order:
            size = len(sets[bid])
            joined = None
            for anchor in anchors:
                anchor_size = len(sets[anchor])
                if size / anchor_size < threshold:  # Jaccard <= size_small / size_large
                    continue
                if jaccard(sets[bid], sets[anchor]) >= threshold:
                    joined = anchor
                    break
            if joined is None:
                anchors.append(bid)
                assignment[bid] = bid
            else:
                assignment[bid] = joined
        group_sizes = Counter(assignment.values())
        updates: Dict[int, Optional[str]] = {}
        for b in catalog:
            anchor = assignment.get(b["id"])
            if anchor is not None and group_sizes[anchor] > 1:
                updates[b["id"]] = ref_by_id[anchor]
            else:
                updates[b["id"]] = None
        self.repo.set_merge_groups(day, updates)
        return sum(1 for anchor, size in group_sizes.items() if size > 1)


_EMPTY_COLUMNS = (
    "ret_cap", "ret_eq", "excess_ret", "excess_vs_median", "rank_pct", "ret_5d", "excess_5d",
    "rank_5d_pct", "amount", "amount_ma5_prev", "amount_ratio_5d", "amount_share", "amount_peak_20d",
    "up_count", "down_count", "up_ratio", "limit_up_count", "limit_down_count", "one_word_count",
    "touched_up_count", "max_consecutive", "break_rate", "drop7_ratio", "top_contrib_code",
    "top_contrib_share", "leader_code", "top_amount_code",
)
