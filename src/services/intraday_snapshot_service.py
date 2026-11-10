# -*- coding: utf-8 -*-
"""On-demand intraday data for the chat assistant (A-share).

Rules (docs/design/core-backend-design.md section 3):

- Fetched only when someone asks: no background polling.
- Results live in an in-memory cache (``INTRADAY_SNAPSHOT_TTL_SECONDS``, default 60s);
  concurrent callers share one request (single-flight).
- Intraday data is never written to ``stock_daily`` or any other daily table; this
  module has no persistence at all.
- Every result carries its snapshot time, the source that produced it, and whether
  it is stale (refresh failed and an old snapshot was served, or the provider time
  lags while the market is open).

Source order: Tushare (``rt_k`` / ``rt_min_daily``) first, Biying as fallback;
single-stock quotes follow the regular ``REALTIME_SOURCE_PRIORITY`` chain.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from data_provider.realtime_types import (
    UnifiedRealtimeQuote,
    format_cn_provider_timestamp,
    safe_float,
)

logger = logging.getLogger(__name__)

_CN_TZ = timezone(timedelta(hours=8))
# Tushare ``rt_k`` per-call row cap forces batching by board prefix.
RT_K_BATCH_PATTERNS: Tuple[str, ...] = ("6*.SH", "0*.SZ", "3*.SZ", "9*.BJ")
STALE_AFTER_SECONDS = 180.0
MAX_STALE_SERVE_SECONDS = 600.0
SINGLE_FLIGHT_WAIT_SECONDS = 120.0
# A finished session never changes; keep it so repeated double-clicks cost no provider call.
PAST_SESSION_TTL_SECONDS = 6 * 3600.0


class IntradayDataUnavailable(RuntimeError):
    """No configured source could provide the requested intraday data.

    ``reason`` tells callers why, so users see the cause instead of a generic network error:

    * ``biying_not_configured`` - a past session needs Biying history bars and ``BIYING_LICENCE`` is unset;
    * ``not_configured`` - none of the sources for this request is configured;
    * ``no_bars`` - configured sources answered but had no bars (suspension, holiday);
    * ``source_failed`` - a configured source raised or timed out (default).
    """

    def __init__(self, message: str, *, reason: str = "source_failed") -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# result types
# ---------------------------------------------------------------------------


@dataclass
class MarketSnapshot:
    """Full-market snapshot; ``quotes`` maps 6-digit code -> quote."""

    quotes: Dict[str, UnifiedRealtimeQuote]
    source: str
    as_of: str
    fetched_at: str
    is_stale: bool = False
    stale_reason: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Metadata only (the quote map is large)."""
        return {
            "source": self.source,
            "as_of": self.as_of,
            "fetched_at": self.fetched_at,
            "is_stale": self.is_stale,
            "stale_reason": self.stale_reason,
            "stock_count": len(self.quotes),
        }


@dataclass
class QuoteResult:
    quote: UnifiedRealtimeQuote
    source: str
    as_of: str
    fetched_at: str
    is_stale: bool = False
    stale_reason: Optional[str] = None


@dataclass
class BarsResult:
    """5-minute bars of the latest session, oldest first (volume in shares)."""

    code: str
    bars: List[Dict[str, Any]]
    source: str
    as_of: str
    fetched_at: str
    is_stale: bool = False
    stale_reason: Optional[str] = None
    #: Session date (``YYYY-MM-DD``) when the bars were requested for a given day.
    trade_date: Optional[str] = None


@dataclass
class AggregateStats:
    requested: int
    matched: int
    up_count: int
    down_count: int
    flat_count: int
    equal_weight_change_pct: Optional[float]
    cap_weighted_change_pct: Optional[float]
    weight_coverage: float
    total_amount: float
    source: str
    as_of: str
    is_stale: bool


# ---------------------------------------------------------------------------
# aggregation helper (board-level intraday aggregation reuses this)
# ---------------------------------------------------------------------------


def _quote_change_pct(quote: UnifiedRealtimeQuote) -> Optional[float]:
    if quote.change_pct is not None:
        return quote.change_pct
    if quote.price and quote.pre_close and quote.pre_close > 0:
        return (quote.price - quote.pre_close) / quote.pre_close * 100.0
    return None


def aggregate_snapshot(
    snapshot: MarketSnapshot,
    codes: Optional[Iterable[str]] = None,
    weights: Optional[Mapping[str, float]] = None,
) -> AggregateStats:
    """Aggregate a snapshot over ``codes`` (``None`` = whole market).

    ``cap_weighted_change_pct`` is weighted by ``weights[code]`` when given
    (e.g. the previous day's float market cap from ``board_membership``), else by
    each quote's own ``circ_mv``. Codes missing from the snapshot, without a change
    figure, or without a positive weight are skipped from the respective average;
    ``weight_coverage`` reports the share of matched stocks that were weighted.
    """
    from data_provider.base import normalize_stock_code

    if codes is None:
        selected = list(snapshot.quotes.items())
        requested = len(selected)
    else:
        wanted: List[str] = []
        for raw in codes:
            code = normalize_stock_code(str(raw))
            if code and code not in wanted:
                wanted.append(code)
        requested = len(wanted)
        selected = [(code, snapshot.quotes[code]) for code in wanted if code in snapshot.quotes]

    up = down = flat = 0
    changes: List[float] = []
    weighted_sum = weight_total = 0.0
    weighted_n = matched = 0
    total_amount = 0.0
    for code, quote in selected:
        change = _quote_change_pct(quote)
        if change is None:
            continue
        matched += 1
        changes.append(change)
        if change > 0:
            up += 1
        elif change < 0:
            down += 1
        else:
            flat += 1
        total_amount += quote.amount or 0.0
        weight = weights.get(code) if weights is not None else quote.circ_mv
        weight = safe_float(weight)
        if weight is not None and weight > 0:
            weighted_sum += weight * change
            weight_total += weight
            weighted_n += 1

    return AggregateStats(
        requested=requested,
        matched=matched,
        up_count=up,
        down_count=down,
        flat_count=flat,
        equal_weight_change_pct=(sum(changes) / len(changes)) if changes else None,
        cap_weighted_change_pct=(weighted_sum / weight_total) if weight_total > 0 else None,
        weight_coverage=(weighted_n / matched) if matched else 0.0,
        total_amount=total_amount,
        source=snapshot.source,
        as_of=snapshot.as_of,
        is_stale=snapshot.is_stale,
    )


# ---------------------------------------------------------------------------
# cache with single-flight
# ---------------------------------------------------------------------------


class _Flight:
    __slots__ = ("event", "value", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.value: Any = None
        self.error: Optional[BaseException] = None


class _SingleFlightCache:
    """TTL cache; concurrent misses for one key share a single loader call."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._entries: Dict[Any, Tuple[float, Any]] = {}
        self._inflight: Dict[Any, _Flight] = {}

    def peek_fresh(self, key: Any, ttl: float) -> Optional[Any]:
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and self._monotonic() - entry[0] < ttl:
                return entry[1]
        return None

    def get(self, key: Any, loader: Callable[[], Any], ttl: float) -> Tuple[Any, bool]:
        """Return ``(value, served_stale)``; serves a recent old value if the refresh fails."""
        with self._lock:
            entry = self._entries.get(key)
            now = self._monotonic()
            if entry is not None and now - entry[0] < ttl:
                return entry[1], False
            flight = self._inflight.get(key)
            leader = flight is None
            if leader:
                flight = self._inflight[key] = _Flight()
        assert flight is not None

        if leader:
            try:
                flight.value = loader()
                with self._lock:
                    self._entries[key] = (self._monotonic(), flight.value)
            except BaseException as exc:  # shared with followers, re-raised below
                flight.error = exc
            finally:
                with self._lock:
                    self._inflight.pop(key, None)
                flight.event.set()
        elif not flight.event.wait(SINGLE_FLIGHT_WAIT_SECONDS):
            raise IntradayDataUnavailable("timed out waiting for the in-flight intraday request")

        if flight.error is None:
            return flight.value, False
        with self._lock:
            entry = self._entries.get(key)
        if entry is not None and self._monotonic() - entry[0] <= MAX_STALE_SERVE_SECONDS:
            logger.warning("intraday refresh failed (%s); serving previous snapshot", flight.error)
            return entry[1], True
        raise flight.error


# ---------------------------------------------------------------------------
# service
# ---------------------------------------------------------------------------


def _china_now() -> datetime:
    return datetime.now(_CN_TZ)


class IntradaySnapshotService:
    def __init__(
        self,
        fetcher_manager: Any = None,
        *,
        ttl_seconds: Optional[float] = None,
        now_fn: Callable[[], datetime] = _china_now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._manager = fetcher_manager
        if ttl_seconds is None:
            from src.config import get_config

            ttl_seconds = getattr(get_config(), "intraday_snapshot_ttl_seconds", 60)
        self.ttl_seconds = float(ttl_seconds)
        self._now = now_fn
        self._cache = _SingleFlightCache(monotonic)

    # -- plumbing ----------------------------------------------------------

    def _get_manager(self) -> Any:
        if self._manager is None:
            from src.agent.tools.data_tools import _get_fetcher_manager

            self._manager = _get_fetcher_manager()
        return self._manager

    def _fetcher(self, name: str) -> Any:
        return self._get_manager().get_fetcher(name, capability="realtime_quote")

    def _market_open(self) -> bool:
        try:
            from src.core.trading_calendar import build_market_phase_context

            ctx = build_market_phase_context(market="cn", current_time=self._now())
            return bool(ctx.is_market_open_now)
        except Exception:
            return False

    def _freshness(
        self, as_of: str, served_stale: bool, fetched_at: datetime
    ) -> Tuple[bool, Optional[str]]:
        if served_stale:
            return True, "refresh_failed"
        provider = None
        try:
            provider = datetime.fromisoformat(as_of)
        except ValueError:
            pass
        if (
            provider is not None
            and self._market_open()
            and (fetched_at - provider).total_seconds() > STALE_AFTER_SECONDS
        ):
            return True, "provider_delay"
        return False, None

    # -- full-market snapshot ----------------------------------------------

    def get_market_snapshot(self) -> MarketSnapshot:
        snapshot, served_stale = self._cache.get("market", self._load_market_snapshot, self.ttl_seconds)
        is_stale, reason = self._freshness(snapshot.as_of, served_stale, self._now())
        return replace(snapshot, is_stale=is_stale, stale_reason=reason)

    def _load_market_snapshot(self) -> MarketSnapshot:
        errors: List[str] = []
        for label, loader in (("tushare", self._market_from_tushare), ("biying", self._market_from_biying)):
            try:
                quotes = loader()
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                logger.info("[intraday] full-market snapshot via %s failed: %s", label, exc)
                continue
            return self._build_snapshot(quotes, label)
        raise IntradayDataUnavailable("full-market snapshot unavailable (" + "; ".join(errors) + ")")

    def _build_snapshot(self, quotes: Dict[str, UnifiedRealtimeQuote], source: str) -> MarketSnapshot:
        fetched_at = self._now().isoformat()
        stamps = [q.provider_timestamp for q in quotes.values() if q.provider_timestamp]
        for quote in quotes.values():
            quote.fetched_at = fetched_at
        return MarketSnapshot(
            quotes=quotes,
            source=source,
            as_of=max(stamps) if stamps else fetched_at,
            fetched_at=fetched_at,
        )

    def _market_from_tushare(self) -> Dict[str, UnifiedRealtimeQuote]:
        fetcher = self._fetcher("TushareFetcher")
        if fetcher is None or not hasattr(fetcher, "fetch_rt_k"):
            raise IntradayDataUnavailable("Tushare is not configured", reason="not_configured")
        quotes: Dict[str, UnifiedRealtimeQuote] = {}
        for pattern in RT_K_BATCH_PATTERNS:
            df = fetcher.fetch_rt_k(pattern)
            if df is None or df.empty:
                # A missing board would skew every aggregate: treat as a failed source.
                raise IntradayDataUnavailable(f"rt_k {pattern} returned no rows")
            for _, row in df.iterrows():
                code = str(row.get("ts_code") or "").split(".")[0]
                quote = fetcher.rt_k_row_to_quote(row, code) if code else None
                if quote is not None:
                    quotes[code] = quote
        try:  # rt_k has no market cap; weight by the previous day's float cap (fail open)
            circ_mv = fetcher.fetch_prev_circ_mv()
        except Exception as exc:
            logger.info("[intraday] previous circ_mv unavailable: %s", exc)
            circ_mv = {}
        for code, quote in quotes.items():
            if quote.circ_mv is None and code in circ_mv:
                quote.circ_mv = circ_mv[code]
        return quotes

    def _market_from_biying(self) -> Dict[str, UnifiedRealtimeQuote]:
        fetcher = self._fetcher("BiyingFetcher")
        if fetcher is None:
            raise IntradayDataUnavailable("Biying is not configured", reason="not_configured")
        return fetcher.get_all_realtime_quotes()

    # -- single stock ------------------------------------------------------

    def get_quote(self, stock_code: str) -> QuoteResult:
        from data_provider.base import normalize_stock_code

        code = normalize_stock_code(stock_code)
        fresh_market = self._cache.peek_fresh("market", self.ttl_seconds)
        if fresh_market is not None and code in fresh_market.quotes:
            return self._quote_result(fresh_market.quotes[code], fresh_market.source, False)
        quote, served_stale = self._cache.get(("quote", code), lambda: self._load_quote(stock_code), self.ttl_seconds)
        return self._quote_result(quote, getattr(quote.source, "value", str(quote.source)), served_stale)

    def _load_quote(self, stock_code: str) -> UnifiedRealtimeQuote:
        quote = self._get_manager().get_realtime_quote(stock_code)
        if quote is None:
            raise IntradayDataUnavailable(f"no realtime quote available for {stock_code}")
        return quote

    def _quote_result(self, quote: UnifiedRealtimeQuote, source: str, served_stale: bool) -> QuoteResult:
        now = self._now()
        fetched_at = quote.fetched_at or now.isoformat()
        as_of = quote.provider_timestamp or fetched_at
        is_stale, reason = self._freshness(as_of, served_stale, now)
        return QuoteResult(
            quote=quote, source=source, as_of=as_of, fetched_at=fetched_at,
            is_stale=is_stale, stale_reason=reason,
        )

    # -- 5-minute bars -----------------------------------------------------

    def get_intraday_bars(self, stock_code: str) -> BarsResult:
        from data_provider.base import normalize_stock_code

        code = normalize_stock_code(stock_code)
        (bars, source), served_stale = self._cache.get(
            ("bars", code, "5MIN"), lambda: self._load_bars(stock_code, code), self.ttl_seconds
        )
        now = self._now()
        as_of = bars[-1]["time"] if bars else now.isoformat()
        is_stale, reason = self._freshness(as_of, served_stale, now)
        return BarsResult(
            code=code, bars=bars, source=source, as_of=as_of, fetched_at=now.isoformat(),
            is_stale=is_stale, stale_reason=reason,
        )

    def _load_bars(self, stock_code: str, code: str) -> Tuple[List[Dict[str, Any]], str]:
        errors: List[str] = []
        reasons = set()
        for label, loader in (("tushare", self._bars_from_tushare), ("biying", self._bars_from_biying)):
            try:
                bars = loader(stock_code, code)
            except Exception as exc:
                errors.append(f"{label}: {exc}")
                reasons.add(getattr(exc, "reason", "source_failed"))
                logger.info("[intraday] 5-min bars via %s failed for %s: %s", label, code, exc)
                continue
            if bars:
                return bars, label
            errors.append(f"{label}: no bars")
            reasons.add("no_bars")
        reason = ("source_failed" if "source_failed" in reasons
                  else "no_bars" if "no_bars" in reasons else "not_configured")
        raise IntradayDataUnavailable("5-minute bars unavailable (" + "; ".join(errors) + ")", reason=reason)

    def _bars_from_tushare(self, stock_code: str, code: str) -> List[Dict[str, Any]]:
        fetcher = self._fetcher("TushareFetcher")
        if fetcher is None or not hasattr(fetcher, "fetch_rt_min_daily"):
            raise IntradayDataUnavailable("Tushare is not configured", reason="not_configured")
        df = fetcher.fetch_rt_min_daily(fetcher._convert_stock_code(stock_code), "5MIN")
        if df is None or df.empty:
            return []
        time_col = "time" if "time" in df.columns else "trade_time"
        bars = []
        for _, row in df.iterrows():
            stamp = format_cn_provider_timestamp(row.get(time_col))
            close = safe_float(row.get("close"))
            if stamp is None or close is None:
                continue
            volume = safe_float(row.get("vol"))
            bars.append({
                "time": stamp,
                "open": safe_float(row.get("open")),
                "high": safe_float(row.get("high")),
                "low": safe_float(row.get("low")),
                "close": close,
                "volume": int(volume) if volume is not None else None,  # rt_min_daily: 股
                "amount": safe_float(row.get("amount")),
            })
        return self._latest_session(bars)

    def _bars_from_biying(self, stock_code: str, code: str) -> List[Dict[str, Any]]:
        fetcher = self._fetcher("BiyingFetcher")
        if fetcher is None:
            raise IntradayDataUnavailable("Biying is not configured", reason="not_configured")
        return self._latest_session(fetcher.get_intraday_bars(stock_code, limit=48))

    # -- 5-minute bars of one given session --------------------------------

    def get_session_bars(self, stock_code: str, trade_date: str) -> BarsResult:
        """5-minute bars of ``trade_date`` (``YYYY-MM-DD``), oldest first.

        Today's session reuses the live chain (Tushare ``rt_min_daily`` then Biying
        ``latest``) and falls back to Biying history; any other date comes from Biying
        history only. A finished session never changes, so past dates are cached for
        ``PAST_SESSION_TTL_SECONDS``. Raises ``ValueError`` for a malformed or future date.
        """
        from data_provider.base import normalize_stock_code

        try:
            day = datetime.strptime(trade_date, "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"invalid date {trade_date!r}, expected YYYY-MM-DD") from None
        now = self._now()
        if day > now.date():
            raise ValueError(f"date {trade_date} is in the future")

        code = normalize_stock_code(stock_code)
        is_today = day == now.date()
        ttl = self.ttl_seconds if is_today else PAST_SESSION_TTL_SECONDS
        (bars, source), served_stale = self._cache.get(
            ("bars", code, "5MIN", trade_date),
            lambda: self._load_session_bars(stock_code, code, trade_date, is_today),
            ttl,
        )
        if is_today:
            as_of = bars[-1]["time"] if bars else now.isoformat()
            is_stale, reason = self._freshness(as_of, served_stale, now)
        else:
            as_of = bars[-1]["time"] if bars else trade_date
            is_stale, reason = (True, "refresh_failed") if served_stale else (False, None)
        return BarsResult(
            code=code, bars=bars, source=source, as_of=as_of, fetched_at=now.isoformat(),
            is_stale=is_stale, stale_reason=reason, trade_date=trade_date,
        )

    def get_opening_auction(self, stock_code: str, trade_date: str) -> dict:
        from data_provider.base import normalize_stock_code

        now = self._now()
        day = datetime.strptime(trade_date, "%Y-%m-%d").date()
        code = normalize_stock_code(stock_code)
        if len(code) != 6 or not code.isascii() or not code.isdigit() or day > now.date():
            raise ValueError("需要六位 A 股代码和有效交易日期")
        is_today = day == now.date()
        result, _ = self._cache.get(
            ("opening_auction", code, trade_date),
            lambda: self._load_opening_auction(code, trade_date, is_today),
            self.ttl_seconds,
        )
        return result

    def _load_opening_auction(self, code: str, day: str, is_today: bool) -> dict:
        from data_provider.auction_quotes import fetch_auction_quotes
        from src.config import get_config

        try:
            fetcher = self._fetcher("BiyingFetcher")
            points = fetcher.get_opening_auction(code, day, is_today) if fetcher else []
            if points:
                return {"points": points, "source": "biying"}
        except Exception as exc:
            logger.info("Biying opening auction unavailable: %s", type(exc).__name__)
        key = get_config().ashare_api_key
        if key:
            try:
                points = fetch_auction_quotes(code, day, is_today, key)
                if points:
                    return {"points": points, "source": "ashareapi"}
            except Exception as exc:
                logger.info("Opening auction quotes unavailable: %s", type(exc).__name__)
        try:
            fetcher = self._fetcher("TushareFetcher")
            point = fetcher.fetch_opening_auction(code, day.replace("-", "")) if fetcher else None
            if point:
                return {"points": [point], "source": "tushare"}
        except Exception as exc:
            logger.info("Opening auction match unavailable: %s", type(exc).__name__)
        return {"points": [], "source": None}

    def _load_session_bars(
        self, stock_code: str, code: str, trade_date: str, is_today: bool
    ) -> Tuple[List[Dict[str, Any]], str]:
        errors: List[str] = []
        live_reason: Optional[str] = None  # today only: why the live chain gave nothing
        if is_today:
            try:
                live, source = self._load_bars(stock_code, code)
                bars = [bar for bar in live if bar["time"][:10] == trade_date]
                if bars:
                    return bars, source
                errors.append("live: no bars for the date")
                live_reason = "no_bars"
            except IntradayDataUnavailable as exc:
                errors.append(str(exc))
                live_reason = exc.reason
        try:
            fetcher = self._fetcher("BiyingFetcher")
            bars = None if fetcher is None else fetcher.get_history_bars(stock_code, trade_date)
        except Exception as exc:
            errors.append(f"biying: {exc}")
            logger.info("[intraday] history bars via biying failed for %s %s: %s", code, trade_date, exc)
            history_reason = "source_failed"
        else:
            if fetcher is None:
                errors.append("biying: not configured")
                history_reason = "biying_not_configured"
            elif bars:
                return bars, "biying"
            else:
                errors.append("biying: no bars")
                history_reason = "no_bars"
        raise IntradayDataUnavailable(
            f"5-minute bars for {trade_date} unavailable (" + "; ".join(errors) + ")",
            reason=self._session_reason(live_reason, history_reason) if is_today else history_reason,
        )

    @staticmethod
    def _session_reason(live_reason: Optional[str], history_reason: str) -> str:
        """Today's reason: a source failure first, then no data, then missing configuration."""
        reasons = {live_reason, history_reason}
        if "source_failed" in reasons:
            return "source_failed"
        if "no_bars" in reasons:
            return "no_bars"
        return "not_configured"

    @staticmethod
    def _latest_session(bars: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Keep one trading session (the latest date present), oldest first."""
        if not bars:
            return []
        ordered = sorted(bars, key=lambda bar: bar["time"])
        latest_date = ordered[-1]["time"][:10]
        return [bar for bar in ordered if bar["time"][:10] == latest_date]


# ---------------------------------------------------------------------------
# process-wide instance
# ---------------------------------------------------------------------------

_service: Optional[IntradaySnapshotService] = None
_service_lock = threading.Lock()


def get_intraday_snapshot_service() -> IntradaySnapshotService:
    global _service
    if _service is None:
        with _service_lock:
            if _service is None:
                _service = IntradaySnapshotService()
    return _service


def reset_intraday_snapshot_service() -> None:
    """Drop the cached instance (config reload / tests)."""
    global _service
    with _service_lock:
        _service = None
