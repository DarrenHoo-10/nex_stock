# -*- coding: utf-8 -*-
"""Reuse of the collected inputs of an earlier analysis (docs/design/multi-user.md section 7).

When an analysis cannot simply return an existing report (the request has holdings, or asks for another strategy
set, model channel, language, ... of a stock/trading date that was analysed before), it still need not fetch the
market data again. The inputs the earlier run collected are kept in its ``analysis_history.context_snapshot`` (the
existing snapshot store, ``SAVE_CONTEXT_SNAPSHOT``, on by default); this module lifts them out of it:

* reused: realtime quote (with its capture timestamps), chip distribution, fundamentals (incl. capital flow and
  board membership), market structure context, the news / search evidence text. Daily bars are not in the
  snapshot: they live in the shared ``stock_daily`` table, which the pipeline's own resume check
  (``has_today_data``) already reuses, and the technical indicators are recomputed from them;
* never reused: the earlier run's conclusions, advice, target / stop prices, scores, prompt, and anything personal.
  Only an explicit whitelist of snapshot keys is read, and holdings / portfolio fields are stripped from the
  result again defensively.

Which reports qualify as a source: the same stock and trading date (``report_trade_date``), shared reports and the
requester's own private reports. Another user's private report is never a source.

While the stock's market is in session the realtime quote is refreshed (not reused); so is a quote that the source
captured in session. Everything else is reused.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Optional, Set

from src.core.trading_calendar import get_effective_trading_date, infer_market_phase
from src.services.report_reuse_service import _IN_SESSION_PHASES, _market_of, history_code_candidates

logger = logging.getLogger(__name__)

PART_REALTIME_QUOTE = "realtime_quote"
PART_CHIP = "chip_distribution"
PART_FUNDAMENTAL = "fundamental_context"
PART_MARKET_STRUCTURE = "market_structure_context"
PART_NEWS = "news_context"

# Container keys that carry a user's holdings / portfolio; removed from every reused structure, at any depth
# (the whole value goes, whatever it holds). Exact key names only: ``avg_cost`` of the chip distribution or a
# ``holding`` figure of a fundamentals table is market data, not a position.
HOLDING_FIELD_KEYS = frozenset({
    "portfolio_context", "portfolio", "portfolio_snapshot", "holdings", "positions", "accounts", "holding_state",
    "position_context", "current_position", "user_position", "cost_basis",
})


def strip_holding_fields(value: Any) -> Any:
    """Copy of ``value`` without any holdings / portfolio field (dict keys, recursively)."""
    if isinstance(value, dict):
        return {k: strip_holding_fields(v) for k, v in value.items() if k not in HOLDING_FIELD_KEYS}
    if isinstance(value, (list, tuple)):
        return [strip_holding_fields(item) for item in value]
    return value


@dataclass
class ReusedMetadata:
    """Collected inputs lifted from an earlier analysis, handed to the pipeline of the new one."""

    source_report_id: int
    trade_date: str
    #: the stock's market is in session (or the source quote was captured in session): the quote is fetched again
    #: and not part of ``parts``
    refresh_realtime: bool
    parts: Dict[str, Any]
    #: part name -> ISO time the data was originally captured (chains keep the first capture time)
    captured_at: Dict[str, str]
    news_result_count: Optional[int] = None
    news_evidence_present: bool = False
    used: Set[str] = field(default_factory=set)

    def take(self, part: str) -> Any:
        """The reusable value of ``part`` (``None`` = fetch it), recording that it was used."""
        value = self.parts.get(part)
        if value is not None:
            self.used.add(part)
        return value

    def snapshot_block(self) -> Optional[Dict[str, Any]]:
        """What the new report's snapshot records about the reuse (``None`` = nothing was reused)."""
        if not self.used:
            return None
        return {
            "source_report_id": self.source_report_id,
            "reused_parts": {part: self.captured_at.get(part) for part in sorted(self.used)},
            "realtime_quote": "reused" if PART_REALTIME_QUOTE in self.used else "fetched",
        }


def _quote_from_dict(raw: Any) -> Optional[Any]:
    from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote

    if not isinstance(raw, dict) or not raw.get("code"):
        return None
    data = strip_holding_fields(raw)
    names = {f.name for f in dataclasses.fields(UnifiedRealtimeQuote)} - {"source"}
    try:
        source = RealtimeSource(data.get("source"))
    except ValueError:
        source = RealtimeSource.FALLBACK
    quote = UnifiedRealtimeQuote(source=source, **{k: v for k, v in data.items() if k in names})
    return quote if quote.has_basic_data() else None


def _chip_from_dict(raw: Any) -> Optional[Any]:
    from data_provider.realtime_types import ChipDistribution

    if not isinstance(raw, dict) or not raw.get("code"):
        return None
    names = {f.name for f in dataclasses.fields(ChipDistribution)}
    return ChipDistribution(**{k: v for k, v in strip_holding_fields(raw).items() if k in names})


def _dict_part(value: Any) -> Optional[Dict[str, Any]]:
    return strip_holding_fields(value) if isinstance(value, dict) and value else None


def _source_parts(snapshot: Dict[str, Any], *, use_quote: bool) -> Dict[str, Any]:
    """The whitelisted, defensively stripped inputs of a stored snapshot."""
    parts: Dict[str, Any] = {}
    if use_quote:
        quote = _quote_from_dict(snapshot.get("realtime_quote_raw"))
        if quote is not None:
            parts[PART_REALTIME_QUOTE] = quote
    chip = _chip_from_dict(snapshot.get("chip_distribution_raw"))
    if chip is not None:
        parts[PART_CHIP] = chip
    enhanced = snapshot.get("enhanced_context")
    fundamental = _dict_part(enhanced.get("fundamental_context") if isinstance(enhanced, dict) else None)
    if fundamental is not None:
        parts[PART_FUNDAMENTAL] = fundamental
    structure = _dict_part(snapshot.get("market_structure_context"))
    if structure is not None:
        parts[PART_MARKET_STRUCTURE] = structure
    news = snapshot.get("news_content")
    if isinstance(news, str) and news.strip():
        parts[PART_NEWS] = news
    return parts


def _created_in_session(record: Any, market: Optional[str]) -> bool:
    created = getattr(record, "created_at", None)
    if not isinstance(created, datetime):
        return True  # unknown capture time: do not trust a quote
    try:
        return infer_market_phase(market, current_time=created.astimezone()) in _IN_SESSION_PHASES
    except Exception:  # noqa: BLE001
        return True


def load_reusable_metadata(
    *,
    stock_code: str,
    viewer_user_id: str,
    analysis_target: Any = None,
    current_time: Optional[datetime] = None,
    db: Any = None,
) -> Optional[ReusedMetadata]:
    """Inputs of the newest qualifying earlier analysis of ``stock_code`` on the current trading date, or ``None``.

    ``viewer_user_id`` is always explicit (the requester, or the primary user for system / primary requests):
    the sources are shared reports plus that user's own private ones, never anybody else's private report.
    Any problem counts as a miss: the analysis then simply fetches its inputs.
    """
    try:
        market = _market_of(stock_code, analysis_target)
        trade_date = get_effective_trading_date(market, current_time=current_time).isoformat()
        candidates = history_code_candidates(stock_code, market)
        if not candidates:
            return None

        from src.storage import get_db

        records = (db or get_db()).find_metadata_source_analyses(
            code_candidates=candidates, report_trade_date=trade_date, viewer_user_id=viewer_user_id,
        )
        in_session = infer_market_phase(market, current_time=current_time) in _IN_SESSION_PHASES
        for record in records:
            try:
                snapshot = json.loads(record.context_snapshot)
            except (TypeError, ValueError):
                continue
            if not isinstance(snapshot, dict):
                continue
            quote_ok = not in_session and not _created_in_session(record, market)
            parts = _source_parts(snapshot, use_quote=quote_ok)
            if not parts:
                continue
            earlier = (snapshot.get("metadata_reuse") or {}).get("reused_parts") if isinstance(
                snapshot.get("metadata_reuse"), dict
            ) else None
            created_iso = record.created_at.isoformat() if isinstance(record.created_at, datetime) else ""
            captured = {
                part: ((earlier or {}).get(part) if isinstance(earlier, dict) else None) or created_iso
                for part in parts
            }
            count = snapshot.get("news_result_count")
            count = count if isinstance(count, int) and not isinstance(count, bool) else None
            stored_flag = snapshot.get("news_evidence_present")
            evidence = bool(stored_flag) if isinstance(stored_flag, bool) else bool(count)
            return ReusedMetadata(
                source_report_id=int(record.id),
                trade_date=trade_date,
                refresh_realtime=not quote_ok,
                parts=parts,
                captured_at=captured,
                news_result_count=count,
                news_evidence_present=evidence,
            )
        return None
    except Exception as exc:  # noqa: BLE001 - reuse is an optimisation; a failed lookup just fetches the inputs
        logger.warning("Metadata reuse lookup failed for %s: %s", stock_code, exc)
        return None


__all__ = [
    "HOLDING_FIELD_KEYS",
    "PART_CHIP",
    "PART_FUNDAMENTAL",
    "PART_MARKET_STRUCTURE",
    "PART_NEWS",
    "PART_REALTIME_QUOTE",
    "ReusedMetadata",
    "load_reusable_metadata",
    "strip_holding_fields",
]
