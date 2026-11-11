# -*- coding: utf-8 -*-
"""
必盈 API（biyingapi.com）A 股实时行情源

定位：
    盘中第二实时源（Tushare ``rt_k`` 之后、免费源之前），按包年版证书设计
    （每分钟 3000 次；全市场 bulk 接口独立限流约每分钟 3 次）。
    不参与日线 K 线链路（``is_available_for_request`` 仅放行 ``realtime_quote``，
    ``_DAILY_MARKET_FETCHER_SUPPORT`` 中支持市场为空集）。

接口（证书为 URL 路径最后一段，日志/异常/请求记录一律打码）：
    - 个股（券商源） ``hsstock/real/time/{code}/{licence}``
    - 个股（公开源） ``hsrl/ssjy/{code}/{licence}``
    - 多只（≤20）    ``hsrl/ssjy_more/{licence}?stock_codes=a,b``
    - 全市场（bulk） ``https://vip.biyingapi.com/hsrl/real/all/{licence}``
    - 买卖五档       ``hsstock/real/five/{code}/{licence}``
    - 5 分钟 K 线    ``hsstock/latest/{code}.{SH|SZ|BJ}/5/n/{licence}?lt=N``
    - 历史 5 分钟线  ``hsstock/history/{code}.{SH|SZ|BJ}/5/n/{licence}?st=YYYYMMDD&et=YYYYMMDD``
    - 指数           ``hsindex/real/time/{code}/{licence}``

单位换算（统一为 UnifiedRealtimeQuote 口径）：
    成交量 手 -> 股（×100）；成交额、市值为元，无需换算。
"""

from __future__ import annotations

import logging
import math
import re
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, Iterable, List, NamedTuple, Optional
from urllib.parse import quote as _url_quote

import pandas as pd
import requests

from .base import (
    BaseFetcher,
    DataFetchError,
    _is_non_cn_request,
    is_bse_code,
    normalize_stock_code,
)
from .realtime_types import (
    RealtimeSource,
    UnifiedRealtimeQuote,
    format_cn_provider_timestamp,
    safe_float,
)

logger = logging.getLogger(__name__)

BIYING_API_BASE_URL = "https://api.biyingapi.com"
BIYING_BULK_BASE_URL = "https://vip.biyingapi.com"
BIYING_DEFAULT_TIMEOUT_SECONDS = 8.0
BIYING_MULTI_QUOTE_MAX_CODES = 20
# 全市场 bulk 独立限流约每分钟 3 次：本地先挡一道，避免白白消耗配额并拖慢提问。
BIYING_BULK_MAX_CALLS_PER_WINDOW = 3
BIYING_BULK_WINDOW_SECONDS = 60.0
_LOTS_TO_SHARES = 100
_MASK = "***"
TICK_ARCHIVE_START = "2026-08-13"


class TickSession(NamedTuple):
    auction: List[Dict[str, Any]]
    bars: List[Dict[str, Any]]


class _LicenceMaskFilter(logging.Filter):
    """Mask registered licences in log records (urllib3 logs request paths at DEBUG)."""

    def __init__(self) -> None:
        super().__init__()
        self._secrets: set[str] = set()

    def add(self, secret: str) -> None:
        if secret:
            self._secrets.add(secret)
            self._secrets.add(_url_quote(secret, safe=""))

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._secrets:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        if any(secret in message for secret in self._secrets):
            for secret in self._secrets:
                message = message.replace(secret, _MASK)
            record.msg = message
            record.args = ()
        return True


_licence_log_filter = _LicenceMaskFilter()
_licence_log_filter_installed = False
_licence_log_filter_lock = threading.Lock()


def _install_licence_log_filter(licence: str) -> None:
    global _licence_log_filter_installed
    _licence_log_filter.add(licence)
    with _licence_log_filter_lock:
        if _licence_log_filter_installed:
            return
        for logger_name in ("urllib3.connectionpool", "urllib3", "requests"):
            logging.getLogger(logger_name).addFilter(_licence_log_filter)
        _licence_log_filter_installed = True


def _exchange_suffix(stock_code: str) -> str:
    """Return SH / SZ / BJ for an A-share code (explicit suffix/prefix wins)."""
    raw = (stock_code or "").strip().upper()
    for marker in ("SH", "SS"):
        if raw.startswith(marker) or raw.endswith("." + marker):
            return "SH"
    if raw.startswith("SZ") or raw.endswith(".SZ"):
        return "SZ"
    if raw.startswith("BJ") or raw.endswith(".BJ"):
        return "BJ"
    code = normalize_stock_code(raw)
    if is_bse_code(code):
        return "BJ"
    if code.startswith(("6", "5", "9")):
        return "SH"
    return "SZ"


def _lots_to_shares(value: Any) -> Optional[int]:
    lots = safe_float(value)
    if lots is None:
        return None
    return int(round(lots * _LOTS_TO_SHARES))


def _first_float(row: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = safe_float(row.get(key))
        if value is not None:
            return value
    return None


def biying_row_to_quote(row: Dict[str, Any], code: str) -> Optional[UnifiedRealtimeQuote]:
    """Map one Biying realtime row (broker / public / bulk share most field names).

    Broker and public sources differ slightly (``tr`` vs ``hs`` turnover, ``pb_ratio``
    vs ``sjl`` P/B), so both spellings are accepted. ``v`` is in lots (手) and is
    normalised to shares; the ambiguous ``pv`` / ``tv`` fields are not used.
    """
    price = safe_float(row.get("p"))
    if price is None or price <= 0:
        return None
    return UnifiedRealtimeQuote(
        code=normalize_stock_code(code),
        name=str(row.get("name") or row.get("mc") or ""),
        source=RealtimeSource.BIYING,
        provider_timestamp=format_cn_provider_timestamp(row.get("t")),
        price=price,
        change_pct=safe_float(row.get("pc")),
        change_amount=safe_float(row.get("ud")),
        volume=_lots_to_shares(row.get("v")),
        amount=safe_float(row.get("cje")),
        volume_ratio=safe_float(row.get("lb")),
        turnover_rate=_first_float(row, "hs", "tr"),
        amplitude=safe_float(row.get("zf")),
        open_price=safe_float(row.get("o")),
        high=safe_float(row.get("h")),
        low=safe_float(row.get("l")),
        pre_close=safe_float(row.get("yc")),
        pe_ratio=safe_float(row.get("pe")),
        pb_ratio=_first_float(row, "sjl", "pb_ratio"),
        total_mv=safe_float(row.get("sz")),
        circ_mv=safe_float(row.get("lt")),
        change_60d=safe_float(row.get("zdf60")),
    )


class BiyingFetcher(BaseFetcher):
    """必盈 API 实时行情源（仅实时能力，不提供日线）。"""

    name = "BiyingFetcher"
    priority = 6

    def __init__(
        self,
        licence: str = "",
        timeout: float = BIYING_DEFAULT_TIMEOUT_SECONDS,
        base_url: str = BIYING_API_BASE_URL,
        bulk_url: str = BIYING_BULK_BASE_URL,
    ) -> None:
        self._licence = (licence or "").strip()
        self.timeout = float(timeout)
        self.base_url = base_url.rstrip("/")
        self.bulk_url = bulk_url.rstrip("/")
        self._bulk_calls: Deque[float] = deque()
        self._bulk_lock = threading.Lock()
        if self._licence:
            _install_licence_log_filter(self._licence)

    # ------------------------------------------------------------------
    # availability / BaseFetcher plumbing
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        return bool(self._licence)

    def is_available_for_request(self, capability: str = "") -> bool:
        """Realtime-only: stay out of daily-data / stock-name / list routes."""
        return bool(self._licence) and capability == "realtime_quote"

    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        raise DataFetchError("BiyingFetcher does not provide daily K-lines")

    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        raise DataFetchError("BiyingFetcher does not provide daily K-lines")

    # ------------------------------------------------------------------
    # HTTP (licence masking lives here)
    # ------------------------------------------------------------------

    def _mask(self, text: Any) -> str:
        message = str(text)
        if self._licence:
            message = message.replace(self._licence, _MASK).replace(
                _url_quote(self._licence, safe=""), _MASK
            )
        return message

    def _get_json(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        *,
        bulk: bool = False,
    ) -> Any:
        """GET ``path`` (``{licence}`` placeholder is substituted) and return parsed JSON."""
        if not self._licence:
            raise DataFetchError("Biying licence is not configured")
        base = self.bulk_url if bulk else self.base_url
        url = f"{base}/{path.format(licence=self._licence)}"
        try:
            response = requests.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise DataFetchError(f"Biying request failed: {self._mask(exc)}") from None
        if response.status_code != 200:
            raise DataFetchError(f"Biying HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise DataFetchError("Biying returned a non-JSON response") from None
        if isinstance(payload, dict) and not payload.get("p") and (
            "error" in payload or "msg" in payload or "message" in payload
        ):
            detail = payload.get("error") or payload.get("msg") or payload.get("message")
            raise DataFetchError(f"Biying error: {self._mask(detail)}")
        return payload

    def _acquire_bulk_slot(self) -> None:
        now = time.monotonic()
        with self._bulk_lock:
            while self._bulk_calls and now - self._bulk_calls[0] >= BIYING_BULK_WINDOW_SECONDS:
                self._bulk_calls.popleft()
            if len(self._bulk_calls) >= BIYING_BULK_MAX_CALLS_PER_WINDOW:
                raise DataFetchError("Biying full-market snapshot rate limit reached (3/min)")
            self._bulk_calls.append(now)

    @staticmethod
    def _as_row(payload: Any) -> Optional[Dict[str, Any]]:
        if isinstance(payload, list):
            payload = payload[0] if payload else None
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _as_rows(payload: Any) -> List[Dict[str, Any]]:
        if isinstance(payload, dict):
            payload = [payload]
        if not isinstance(payload, list):
            return []
        return [row for row in payload if isinstance(row, dict)]

    # ------------------------------------------------------------------
    # realtime quotes
    # ------------------------------------------------------------------

    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """Single A-share quote: broker source first, public ``ssjy`` as fallback."""
        if not self._licence or _is_non_cn_request(stock_code):
            return None
        code = normalize_stock_code(stock_code)
        suffixed = f"{code}.{_exchange_suffix(stock_code)}"

        sources = (
            ("broker", f"hsstock/real/time/{suffixed}/{{licence}}"),
            ("public", f"hsrl/ssjy/{code}/{{licence}}"),
        )
        last_error: Optional[Exception] = None
        for label, path in sources:
            try:
                row = self._as_row(self._get_json(path))
                quote = biying_row_to_quote(row, code) if row else None
                if quote is not None:
                    return quote
                logger.debug("Biying %s quote empty for %s", label, code)
            except DataFetchError as exc:
                last_error = exc
                logger.debug("Biying %s quote failed for %s: %s", label, code, exc)
        if last_error is not None:
            raise last_error
        return None

    def get_realtime_quotes(self, stock_codes: Iterable[str]) -> Dict[str, UnifiedRealtimeQuote]:
        """Multi-stock quotes (≤20 per request); returns ``{6-digit code: quote}``."""
        codes: List[str] = []
        for raw in stock_codes:
            code = normalize_stock_code(raw)
            if code and code not in codes and not _is_non_cn_request(raw):
                codes.append(code)
        result: Dict[str, UnifiedRealtimeQuote] = {}
        for start in range(0, len(codes), BIYING_MULTI_QUOTE_MAX_CODES):
            chunk = codes[start:start + BIYING_MULTI_QUOTE_MAX_CODES]
            payload = self._get_json(
                "hsrl/ssjy_more/{licence}", params={"stock_codes": ",".join(chunk)}
            )
            for row in self._as_rows(payload):
                dm = str(row.get("dm") or "")
                quote = biying_row_to_quote(row, dm) if dm else None
                if quote is not None:
                    result[quote.code] = quote
        return result

    def get_all_realtime_quotes(self) -> Dict[str, UnifiedRealtimeQuote]:
        """Full-market snapshot via the public bulk interface (``{6-digit code: quote}``)."""
        self._acquire_bulk_slot()
        payload = self._get_json("hsrl/real/all/{licence}", bulk=True)
        result: Dict[str, UnifiedRealtimeQuote] = {}
        for row in self._as_rows(payload):
            dm = str(row.get("dm") or "")
            quote = biying_row_to_quote(row, dm) if dm else None
            if quote is not None:
                result[quote.code] = quote
        if not result:
            raise DataFetchError("Biying full-market snapshot returned no rows")
        return result

    def get_five_level(self, stock_code: str) -> Optional[Dict[str, Any]]:
        """Five-level order book: ``{asks, bids, time}``; volumes normalised lots -> shares."""
        code = normalize_stock_code(stock_code)
        suffixed = f"{code}.{_exchange_suffix(stock_code)}"
        row = self._as_row(self._get_json(f"hsstock/real/five/{suffixed}/{{licence}}"))
        if not row:
            return None

        def levels(price_key: str, volume_key: str) -> List[Dict[str, Optional[float]]]:
            prices = row.get(price_key) or []
            volumes = row.get(volume_key) or []
            return [
                {"price": safe_float(price), "volume": _lots_to_shares(volume)}
                for price, volume in zip(prices, volumes)
            ]

        return {
            "code": code,
            "asks": levels("ps", "vs"),
            "bids": levels("pb", "vb"),
            "provider_timestamp": format_cn_provider_timestamp(row.get("t")),
            "source": RealtimeSource.BIYING.value,
        }

    def get_intraday_bars(self, stock_code: str, limit: int = 48) -> List[Dict[str, Any]]:
        """Latest 5-minute bars, oldest first. ``volume`` is in shares, ``amount`` in yuan."""
        code = normalize_stock_code(stock_code)
        suffixed = f"{code}.{_exchange_suffix(stock_code)}"
        payload = self._get_json(
            f"hsstock/latest/{suffixed}/5/n/{{licence}}", params={"lt": int(limit)}
        )
        return self._rows_to_bars(payload)

    def get_tick_session(self, code: str, day: str, is_today: bool) -> TickSession:
        """One ordinary-plan tick request supplies both the auction and the regular minute chart."""
        if not is_today and day < TICK_ARCHIVE_START:
            return TickSession([], [])
        dated_path = "" if is_today else day.replace("-", "") + "/"
        rows = self._as_rows(self._get_json(f"hsrl/zbjy/{dated_path}{code}/{{licence}}"))
        return TickSession(self._auction_from_ticks(rows, day), self._minutes_from_ticks(rows, day))

    @staticmethod
    def _minutes_from_ticks(rows: List[Dict[str, Any]], day: str) -> List[Dict[str, Any]]:
        bars: Dict[int, Dict[str, Any]] = {}
        # The provider returns newest first; preserve execution order for coincident timestamps.
        for row in sorted(reversed(rows), key=lambda tick: str(tick.get("t", ""))):
            stamp = str(row.get("t", ""))
            if row.get("d") != day or not re.fullmatch(r"[0-2][0-9]:[0-5][0-9]:[0-5][0-9]", stamp):
                continue
            if not ("09:30:00" <= stamp <= "11:30:00" or "13:00:00" <= stamp <= "15:00:00"):
                continue
            price, volume = safe_float(row.get("p")), safe_float(row.get("v"))
            if not price or not math.isfinite(price) or price <= 0:
                continue
            if not volume or not math.isfinite(volume) or not volume.is_integer() or volume <= 0:
                continue
            # End-of-minute labels: 09:30:01 through 09:31:00 belong to 09:31.
            hour, minute, second = map(int, stamp.split(":"))
            slot = hour * 60 + minute + int(second > 0)
            if slot not in bars:
                bars[slot] = {
                    "time": f"{day}T{slot // 60:02}:{slot % 60:02}:00+08:00",
                    "open": price, "high": price, "low": price, "close": price, "volume": 0, "amount": 0.0,
                }
            bar = bars[slot]
            bar["high"] = max(bar["high"], price)
            bar["low"] = min(bar["low"], price)
            bar["close"] = price
            bar["volume"] += int(volume)  # zbjy reports shares, not lots.
            bar["amount"] += price * volume
        return list(bars.values())

    @staticmethod
    def _auction_from_ticks(rows: List[Dict[str, Any]], day: str) -> List[Dict[str, Any]]:
        quotes = {}
        matches = []
        for row in rows:
            stamp = str(row.get("t", ""))
            if row.get("d") != day or not re.fullmatch(r"09:(?:1[5-9]|2[0-5]):[0-5][0-9]", stamp):
                continue
            price = safe_float(row.get("p"))
            if price is None or not math.isfinite(price) or price <= 0:
                continue
            minute = stamp[:5]
            if minute == "09:25":
                volume = safe_float(row.get("v"))
                if volume and math.isfinite(volume) and volume > 0:
                    matches.append((stamp, price, int(volume)))
            elif minute not in quotes or stamp > quotes[minute]["time"]:
                quotes[minute] = {"time": stamp, "price": price}

        points = [quotes[minute] for minute in sorted(quotes)]
        if matches and len({price for _, price, _ in matches}) == 1:
            points.append({"time": max(stamp for stamp, _, _ in matches), "price": matches[0][1],
                           "volume": sum(volume for _, _, volume in matches)})
        return points

    def get_history_bars(self, stock_code: str, trade_date: str) -> List[Dict[str, Any]]:
        """Minute bars of one session (``YYYY-MM-DD``), oldest first.

        ``hsstock/history/{code}/5/n/{licence}?st=YYYYMMDD&et=YYYYMMDD``; same row shape as
        :meth:`get_intraday_bars` (``volume`` in shares, ``amount`` in yuan). Rows outside
        ``trade_date`` are dropped so a provider that pads the range cannot leak other days.
        """
        code = normalize_stock_code(stock_code)
        suffixed = f"{code}.{_exchange_suffix(stock_code)}"
        day = trade_date.replace("-", "")
        payload = self._get_json(
            f"hsstock/history/{suffixed}/5/n/{{licence}}", params={"st": day, "et": day}
        )
        return [bar for bar in self._rows_to_bars(payload) if bar["time"][:10] == trade_date]

    def _rows_to_bars(self, payload: Any) -> List[Dict[str, Any]]:
        bars: List[Dict[str, Any]] = []
        for row in self._as_rows(payload):
            bars.append(
                {
                    "time": format_cn_provider_timestamp(row.get("t")),
                    "open": safe_float(row.get("o")),
                    "high": safe_float(row.get("h")),
                    "low": safe_float(row.get("l")),
                    "close": safe_float(row.get("c")),
                    "volume": _lots_to_shares(row.get("v")),
                    "amount": safe_float(row.get("a")),
                }
            )
        return [bar for bar in bars if bar["time"] and bar["close"] is not None]

    def get_index_quote(self, index_code: str) -> Optional[UnifiedRealtimeQuote]:
        """Exchange index quote, e.g. ``000001.SH`` (volume normalised to shares)."""
        row = self._as_row(self._get_json(f"hsindex/real/time/{index_code}/{{licence}}"))
        quote = biying_row_to_quote(row, index_code) if row else None
        if quote is not None:
            quote.code = index_code  # keep the exchange suffix: 000001.SH is not stock 000001
        return quote
