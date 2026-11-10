# -*- coding: utf-8 -*-
"""Shared report reuse (docs/design/multi-user.md section 7): direct reuse of an existing report.

An analysis report is shared by everybody, so a user who asks for a report that already exists does not need to
spend a model call (nor own a model key): the existing report is returned instead.

A report is reusable for a request when ALL of these match:

* the same stock (every stored spelling of its code);
* the same trading date (``get_effective_trading_date`` of the stock's market);
* the same model channel (``model_channel_fingerprint``: provider + Base URL + model name);
* the same effective strategy set (``skill_fingerprint``: the request's ``skills``, else the acting user's
  ``AGENT_SKILLS``, else the default skill; see ``compute_strategy_fingerprint``). A report without a strategy
  fingerprint (written before it existed, or not resolvable) is never reused;
* the same report language and report type;
* the stored report is shared (``is_private`` is false): a report produced with one user's personal context is
  never handed to anybody else.

Guards keep a reused report truthful: nothing is reused while the stock's market is in session (the report
embeds live quotes), and the request must be a plain one: no ``force_regenerate``/``force_refresh``, no analysis
phase override and no position (holdings) context. Everything else that does not match is not reused as a report;
the stock's collected inputs of the same trading date can still be reused for a fresh analysis
(``analysis_metadata_reuse``).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, List, Optional

from src.agent.factory import compute_strategy_fingerprint
from src.core.trading_calendar import MarketPhase, get_effective_trading_date, get_market_for_stock, infer_market_phase
from src.llm.channel_fingerprint import config_model_channel_fingerprint
from src.report_language import normalize_report_language

logger = logging.getLogger(__name__)

_IN_SESSION_PHASES = frozenset({MarketPhase.INTRADAY, MarketPhase.LUNCH_BREAK, MarketPhase.CLOSING_AUCTION})


def _market_of(stock_code: str, analysis_target: Any) -> Optional[str]:
    from data_provider.base import normalize_stock_code
    from src.services.stock_list_parser import ParseStatus

    if analysis_target is not None and getattr(analysis_target, "asset_type", None) == ParseStatus.INDEX:
        return "cn"
    return get_market_for_stock(normalize_stock_code(stock_code))


def history_code_candidates(stock_code: str, market: Optional[str]) -> List[str]:
    """Every spelling of ``stock_code`` an ``analysis_history`` row may be stored under."""
    from src.services.history_service import HistoryService

    return HistoryService._history_code_filter_candidates(stock_code, market_hint=market)


def find_reusable_report(
    *,
    stock_code: str,
    report_type: str,
    config: Any,
    report_language: Optional[str] = None,
    analysis_target: Any = None,
    skills: Optional[List[str]] = None,
    current_time: Optional[datetime] = None,
    db: Any = None,
) -> Optional[Any]:
    """The shared report a request can reuse, or ``None`` (any lookup problem counts as a miss).

    ``skills`` are the request's strategy skills (``None`` = none requested); the effective strategy set is
    resolved with the requester's ``config`` view.
    """
    try:
        fingerprint = config_model_channel_fingerprint(config)
        if not fingerprint:
            return None
        skill_fingerprint = compute_strategy_fingerprint(config, skills)
        if not skill_fingerprint:
            return None
        market = _market_of(stock_code, analysis_target)
        if infer_market_phase(market, current_time=current_time) in _IN_SESSION_PHASES:
            return None
        trade_date = get_effective_trading_date(market, current_time=current_time).isoformat()
        language = normalize_report_language(report_language or getattr(config, "report_language", None))

        from src.storage import get_db

        candidates = history_code_candidates(stock_code, market)
        if not candidates:
            return None
        return (db or get_db()).find_reusable_analysis(
            code_candidates=candidates,
            report_type=report_type,
            report_trade_date=trade_date,
            model_channel_fingerprint=fingerprint,
            report_language=language,
            skill_fingerprint=skill_fingerprint,
        )
    except Exception as exc:  # noqa: BLE001 - reuse is an optimisation; a failed lookup just generates a report
        logger.warning("Report reuse lookup failed for %s: %s", stock_code, exc)
        return None


__all__ = ["find_reusable_report", "history_code_candidates"]
