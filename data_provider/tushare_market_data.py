# -*- coding: utf-8 -*-
"""
Tushare 全市场 / 板块快照取数（docs/design/core-backend-design.md 第 2.9 节）。

以 ``TushareFetcher`` 的 mixin 形式提供，复用其限速（``_check_rate_limit``）和
统一调用入口（``_call_api_with_rate_limit``），不新增 HTTP 客户端。

约定：
- 所有接口按 ``trade_date`` 取全市场；单次行数达到接口上限时按 ``offset`` / ``limit``
  继续翻页，绝不假定一次调用已经取全。翻页无进展（接口忽略 offset）或超过最大页数时，
  抛出 :class:`IncompleteMarketDataError`，``partial`` 属性带已取到的数据。
- 金额统一换算为「元」：``daily.amount`` 千元 × 1000、``daily_basic.circ_mv`` 万元 × 10000、
  ``dc_index.total_mv`` 万元 × 10000。``dc_daily.amount`` 接口文档本身就是元，不换算。
- 限流或超时按指数退避重试；权限不足（积分或未开通）不重试，直接抛 ``DataFetchError``。
"""

from __future__ import annotations

import logging
import time
from datetime import date, datetime
from typing import Any, Callable, Iterable, List, Optional, Sequence, Union

import pandas as pd

from .base import DataFetchError

logger = logging.getLogger(__name__)

DateLike = Union[date, datetime, str]

# Tushare 文档标注的单次返回行数上限。
DAILY_PAGE_CAP = 6000
DAILY_BASIC_PAGE_CAP = 6000
LIMIT_LIST_PAGE_CAP = 2500
SUSPEND_PAGE_CAP = 5000
STK_LIMIT_PAGE_CAP = 5800
STOCK_BASIC_PAGE_CAP = 6000
INDEX_DAILY_PAGE_CAP = 8000
DC_INDEX_PAGE_CAP = 5000
DC_DAILY_PAGE_CAP = 2000
DC_MEMBER_PAGE_CAP = 5000
US_DAILY_PAGE_CAP = 6000

DC_IDX_TYPE_BY_BOARD_TYPE = {
    "industry": "行业板块",
    "concept": "概念板块",
    "region": "地域板块",
}
BOARD_TYPE_BY_DC_IDX_TYPE = {v: k for k, v in DC_IDX_TYPE_BY_BOARD_TYPE.items()}

_EXCHANGE_BOARD_BY_MARKET = {
    "主板": "main",
    "中小板": "main",
    "创业板": "gem",
    "科创板": "star",
    "北交所": "bse",
}

_PERMISSION_HINTS = ("权限", "积分", "permission")
_RATE_LIMIT_HINTS = ("每分钟", "频率", "频次", "too many", "rate limit")


class TushareRateLimitedError(DataFetchError):
    """接口频率超限且不值得等待重试（例如 ``us_daily`` 每分钟 1 次）：调用方应直接放弃本次运行的 Tushare 兜底。"""


class IncompleteMarketDataError(DataFetchError):
    """分页无法确认取全（接口忽略 offset 或超过最大页数）。"""

    def __init__(self, api_name: str, partial: pd.DataFrame, reason: str) -> None:
        super().__init__(f"Tushare {api_name} 数据可能不完整: {reason}")
        self.api_name = api_name
        self.partial = partial
        self.reason = reason


def to_ymd(value: DateLike) -> str:
    """Normalize a date-like value to Tushare's ``YYYYMMDD`` string."""
    if isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if isinstance(value, date):
        return value.strftime("%Y%m%d")
    text = str(value).strip()
    if len(text) == 10 and text[4] == "-" and text[7] == "-":
        return text.replace("-", "")
    if len(text) == 8 and text.isdigit():
        return text
    raise ValueError(f"无法识别的日期: {value!r}")


def _parse_ymd_series(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype("string"), format="%Y%m%d", errors="coerce").dt.date


def code_from_ts_code(ts_code: Any) -> str:
    """``600519.SH`` -> ``600519``."""
    return str(ts_code).strip().split(".")[0]


def infer_exchange_board(code: str) -> str:
    """Fallback board classification by code prefix when stock_basic has no row."""
    code = str(code)
    if code.startswith("688") or code.startswith("689"):
        return "star"
    if code.startswith(("300", "301")):
        return "gem"
    if code.startswith(("4", "8", "92")):
        return "bse"
    return "main"


def _empty(columns: Sequence[str]) -> pd.DataFrame:
    return pd.DataFrame({c: pd.Series(dtype="object") for c in columns})


def _numeric(df: pd.DataFrame, columns: Iterable[str]) -> None:
    for column in columns:
        if column in df.columns:
            df[column] = pd.to_numeric(df[column], errors="coerce")
        else:
            df[column] = float("nan")


class TushareMarketDataMixin:
    """Mixin for ``TushareFetcher``: whole-market snapshot interfaces by trade date."""

    # 类属性便于测试覆盖（避免真实 sleep）。
    _market_retry_attempts = 3
    _market_retry_base_delay = 2.0
    _market_rate_limit_wait = 30.0
    _market_max_pages = 100
    # 频率配额极低的接口（us_daily：本账号 1 次/分钟）：遇到频率超限不 sleep 重试，立即抛
    # TushareRateLimitedError，由调用方跳过 Tushare；其余接口仍按 _market_rate_limit_wait 退避重试。
    _market_no_rate_retry_apis = frozenset({"us_daily", "index_global"})

    # ------------------------------------------------------------------
    # 底层：重试与翻页
    # ------------------------------------------------------------------
    def _market_query(self, api_name: str, **params: Any) -> pd.DataFrame:
        last_exc: Optional[Exception] = None
        for attempt in range(self._market_retry_attempts):
            try:
                df = self._call_api_with_rate_limit(api_name, **params)  # type: ignore[attr-defined]
                return df if df is not None else pd.DataFrame()
            except DataFetchError:
                raise
            except Exception as exc:  # noqa: BLE001 - SDK / HTTP 层异常类型不固定
                last_exc = exc
                message = str(exc)
                if any(hint in message.lower() for hint in _PERMISSION_HINTS) and "每分钟" not in message:
                    raise DataFetchError(f"Tushare {api_name} 无权限或积分不足: {message}") from exc
                if "offset" in message and "100000" in message:
                    # 分页偏移超过 10 万行上限：重试不会成功，交给调用方缩小查询范围。
                    raise DataFetchError(f"Tushare {api_name} 超出分页上限: {message}") from exc
                rate_limited = any(hint in message.lower() for hint in _RATE_LIMIT_HINTS)
                if rate_limited and api_name in self._market_no_rate_retry_apis:
                    raise TushareRateLimitedError(f"Tushare {api_name} 频率超限，不再重试: {message}") from exc
                if attempt + 1 >= self._market_retry_attempts:
                    break
                if rate_limited:
                    delay = self._market_rate_limit_wait
                else:
                    delay = self._market_retry_base_delay * (2 ** attempt)
                logger.warning(
                    "[Tushare] %s 请求失败，%.1fs 后重试 (%d/%d): %s",
                    api_name, delay, attempt + 1, self._market_retry_attempts, message,
                )
                if delay > 0:
                    time.sleep(delay)
        raise DataFetchError(f"Tushare {api_name} 请求失败: {last_exc}") from last_exc

    def _market_query_paged(
        self,
        api_name: str,
        *,
        page_cap: int,
        key_cols: Sequence[str],
        normalize: Optional[Callable[[pd.DataFrame], pd.DataFrame]] = None,
        **params: Any,
    ) -> pd.DataFrame:
        """Query one interface and keep paging while the response hits ``page_cap``.

        ``normalize`` (unit conversion / column mapping) is applied to the final frame and
        also to ``IncompleteMarketDataError.partial``, so callers always see one schema.
        """
        norm = normalize or (lambda frame: frame)
        first = self._market_query(api_name, **params)
        frames: List[pd.DataFrame] = [first]
        if len(first) < page_cap:
            return norm(first)

        seen = self._key_set(first, key_cols)
        offset = len(first)
        incomplete_reason: Optional[str] = None
        for _ in range(self._market_max_pages):
            try:
                page = self._market_query(api_name, limit=page_cap, offset=offset, **params)
            except TushareRateLimitedError as exc:  # 只有 _market_no_rate_retry_apis 会走到这里；首页已取到，保留
                incomplete_reason = f"翻页被限流: {exc}"
                break
            if page is None or page.empty:
                break
            page_keys = self._key_set(page, key_cols)
            if not (page_keys - seen):
                incomplete_reason = "接口忽略 offset，翻页没有新增数据"
                break
            seen |= page_keys
            frames.append(page)
            offset += len(page)
            if len(page) < page_cap:
                break
        else:
            incomplete_reason = f"超过最大翻页数 {self._market_max_pages}"

        combined = pd.concat(frames, ignore_index=True).drop_duplicates(subset=list(key_cols), keep="first")
        if incomplete_reason:
            raise IncompleteMarketDataError(api_name, norm(combined), incomplete_reason)
        logger.info("[Tushare] %s 翻页完成: %d 页, %d 行", api_name, len(frames), len(combined))
        return norm(combined)

    @staticmethod
    def _key_set(df: pd.DataFrame, key_cols: Sequence[str]) -> set:
        if df.empty:
            return set()
        missing = [c for c in key_cols if c not in df.columns]
        if missing:
            raise DataFetchError(f"Tushare 返回缺少主键列 {missing}，无法翻页去重")
        return set(map(tuple, df[list(key_cols)].astype(str).itertuples(index=False, name=None)))

    # ------------------------------------------------------------------
    # 个股：daily / daily_basic / limit_list_d / suspend_d / stock_basic
    # ------------------------------------------------------------------
    def fetch_market_daily(self, trade_date: DateLike) -> pd.DataFrame:
        """``daily``：全市场未复权日线。``amount`` 换算为元。"""
        # open / high / low / vol 只用于在 limit_list_d、daily_basic 尚未发布时推算涨跌停与换手率，不入库。
        columns = [
            "ts_code", "code", "open", "high", "low", "pre_close", "close", "pct_chg", "vol", "amount",
        ]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            _numeric(df, ["open", "high", "low", "pre_close", "close", "pct_chg", "vol", "amount"])
            df["amount"] = df["amount"] * 1000  # 千元 -> 元；vol 保持「手」
            df["code"] = df["ts_code"].map(code_from_ts_code)
            return df[columns].reset_index(drop=True)

        return self._market_query_paged(
            "daily",
            page_cap=DAILY_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            fields="ts_code,trade_date,open,high,low,pre_close,close,pct_chg,vol,amount",
            normalize=_normalize,
        )

    def fetch_market_daily_basic(self, trade_date: DateLike) -> pd.DataFrame:
        """``daily_basic``：换手率、流通市值（万元 -> 元）、收盘涨跌状态。"""
        columns = ["ts_code", "code", "turnover_rate", "float_mv", "limit_status"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            _numeric(df, ["turnover_rate", "circ_mv", "limit_status"])
            df["float_mv"] = df["circ_mv"] * 10000  # 万元 -> 元
            df["code"] = df["ts_code"].map(code_from_ts_code)
            return df[columns].reset_index(drop=True)

        return self._market_query_paged(
            "daily_basic",
            page_cap=DAILY_BASIC_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            fields="ts_code,trade_date,turnover_rate,circ_mv,limit_status",
            normalize=_normalize,
        )

    def fetch_limit_list(self, trade_date: DateLike) -> pd.DataFrame:
        """``limit_list_d``：涨停（U）、跌停（D）、炸板（Z）榜；不含 ST 股。"""
        columns = ["ts_code", "code", "name", "limit", "open_times", "limit_times"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            _numeric(df, ["open_times", "limit_times"])
            df["limit"] = df["limit"].map(lambda v: str(v).strip().upper() if pd.notna(v) else None)
            df["code"] = df["ts_code"].map(code_from_ts_code)
            return df[columns].reset_index(drop=True)

        return self._market_query_paged(
            "limit_list_d",
            page_cap=LIMIT_LIST_PAGE_CAP,
            key_cols=["ts_code", "limit"],
            trade_date=to_ymd(trade_date),
            fields="trade_date,ts_code,name,open_times,limit_times,limit",
            normalize=_normalize,
        )

    def fetch_suspended(self, trade_date: DateLike) -> pd.DataFrame:
        """``suspend_d``：当日停牌股（``suspend_type='S'``）。"""
        columns = ["ts_code", "code", "suspend_timing"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            if "suspend_timing" not in df.columns:
                df["suspend_timing"] = None
            df["code"] = df["ts_code"].map(code_from_ts_code)
            return df[columns].drop_duplicates(subset=["ts_code"]).reset_index(drop=True)

        return self._market_query_paged(
            "suspend_d",
            page_cap=SUSPEND_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            suspend_type="S",
            fields="ts_code,trade_date,suspend_type,suspend_timing",
            normalize=_normalize,
        )

    def fetch_stk_limit(self, trade_date: DateLike) -> pd.DataFrame:
        """``stk_limit``：当日涨跌停价（约 09:00 发布，含基金，调用方按个股代码取用）。"""
        columns = ["ts_code", "code", "up_limit", "down_limit"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            _numeric(df, ["up_limit", "down_limit"])
            df["code"] = df["ts_code"].map(code_from_ts_code)
            return df[columns].drop_duplicates(subset=["ts_code"]).reset_index(drop=True)

        return self._market_query_paged(
            "stk_limit",
            page_cap=STK_LIMIT_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            fields="ts_code,trade_date,up_limit,down_limit",
            normalize=_normalize,
        )

    def fetch_stock_basic_snapshot(self) -> pd.DataFrame:
        """``stock_basic``：上市、退市、暂停上市三类状态合并，供名称 / 上市日期 / 板块查询。"""
        frames = []
        for status in ("L", "D", "P"):
            frames.append(
                self._market_query_paged(
                    "stock_basic",
                    page_cap=STOCK_BASIC_PAGE_CAP,
                    key_cols=["ts_code"],
                    exchange="",
                    list_status=status,
                    fields="ts_code,symbol,name,market,list_date,list_status",
                )
            )
        columns = ["ts_code", "code", "name", "exchange_board", "list_date", "list_status"]
        frames = [f for f in frames if f is not None and not f.empty]
        if not frames:
            return _empty(columns)
        df = pd.concat(frames, ignore_index=True).drop_duplicates(subset=["ts_code"], keep="first")
        df["code"] = df["ts_code"].map(code_from_ts_code)
        market = df["market"] if "market" in df.columns else pd.Series([None] * len(df), index=df.index)
        df["exchange_board"] = [
            _EXCHANGE_BOARD_BY_MARKET.get(str(m).strip()) or infer_exchange_board(c)
            for m, c in zip(market, df["code"])
        ]
        df["list_date"] = _parse_ymd_series(df["list_date"]) if "list_date" in df.columns else None
        if "list_status" not in df.columns:
            df["list_status"] = None
        return df[columns].reset_index(drop=True)

    # ------------------------------------------------------------------
    # 日历 / 基准
    # ------------------------------------------------------------------
    def fetch_trading_dates(
        self,
        start_date: DateLike,
        end_date: DateLike,
        exchange: str = "SSE",
    ) -> List[date]:
        """``trade_cal``：区间内的开市日，升序。"""
        df = self._market_query(
            "trade_cal",
            exchange=exchange,
            start_date=to_ymd(start_date),
            end_date=to_ymd(end_date),
            fields="exchange,cal_date,is_open",
        )
        if df.empty or "cal_date" not in df.columns:
            return []
        opened = df[pd.to_numeric(df["is_open"], errors="coerce") == 1]
        dates = _parse_ymd_series(opened["cal_date"]).dropna().tolist()
        return sorted(set(dates))

    def fetch_index_daily(
        self,
        ts_code: str,
        start_date: DateLike,
        end_date: DateLike,
    ) -> pd.DataFrame:
        """``index_daily``：指数日线（点位 / 涨跌幅 %）。"""
        df = self._market_query_paged(
            "index_daily",
            page_cap=INDEX_DAILY_PAGE_CAP,
            key_cols=["trade_date"],
            ts_code=ts_code,
            start_date=to_ymd(start_date),
            end_date=to_ymd(end_date),
            fields="ts_code,trade_date,close,pct_chg",
        )
        columns = ["trade_date", "close", "pct_chg"]
        if df.empty:
            return _empty(columns)
        df = df.copy()
        _numeric(df, ["close", "pct_chg"])
        df["trade_date"] = _parse_ymd_series(df["trade_date"])
        return df[columns].dropna(subset=["trade_date"]).sort_values("trade_date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 美股 / 国际指数：us_daily / index_global（docs/design/core-backend-design.md 5.2）
    # ------------------------------------------------------------------
    _OVERSEAS_COLUMNS = ["trade_date", "open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"]
    _US_DAILY_FIELDS = "ts_code,trade_date,close,open,high,low,pre_close,pct_change,vol,amount"

    def fetch_us_daily(self, ts_code: str, start_date: DateLike, end_date: DateLike) -> pd.DataFrame:
        """``us_daily``：单只美股区间日线（北京时间 13:00 发布前一美股交易日）。

        ``pct_change`` 统一为 ``pct_chg``（%）；一次一个代码，区间很短，不分页。本账号 ``us_daily``
        每分钟只能调用 1 次，多只代码请用 :meth:`fetch_us_daily_market`（一次取当日全部代码）。
        """
        df = self._market_query(
            "us_daily",
            ts_code=ts_code,
            start_date=to_ymd(start_date),
            end_date=to_ymd(end_date),
            fields=self._US_DAILY_FIELDS,
        )
        return self._normalize_overseas(df.rename(columns={"pct_change": "pct_chg"}))

    def fetch_us_daily_market(self, trade_date: DateLike) -> pd.DataFrame:
        """``us_daily`` 按 ``trade_date`` 取当日全部美股（含 ``ts_code`` 列），一次调用服务所有龙头。

        按页取全；``us_daily`` 频率超限时不重试：首页之后被限流则抛 :class:`IncompleteMarketDataError`
        （``partial`` 带已取到的行），首页就被限流则抛 :class:`TushareRateLimitedError`。
        """

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            return self._normalize_overseas(df.rename(columns={"pct_change": "pct_chg"}), with_code=True)

        return self._market_query_paged(
            "us_daily",
            page_cap=US_DAILY_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            fields=self._US_DAILY_FIELDS,
            normalize=_normalize,
        )

    def fetch_index_global(self, ts_code: str, start_date: DateLike, end_date: DateLike) -> pd.DataFrame:
        """``index_global``：国际指数区间日线（SPX / IXIC / DJI 等，北京时间 9:00 发布）。"""
        df = self._market_query(
            "index_global",
            ts_code=ts_code,
            start_date=to_ymd(start_date),
            end_date=to_ymd(end_date),
        )
        return self._normalize_overseas(df)

    def _normalize_overseas(self, df: pd.DataFrame, *, with_code: bool = False) -> pd.DataFrame:
        columns = (["ts_code"] if with_code else []) + self._OVERSEAS_COLUMNS
        if df is None or df.empty or "trade_date" not in df.columns or (with_code and "ts_code" not in df.columns):
            return _empty(columns)
        df = df.copy()
        _numeric(df, ["open", "high", "low", "close", "pre_close", "pct_chg", "vol", "amount"])
        df["trade_date"] = _parse_ymd_series(df["trade_date"])
        df = df.dropna(subset=["trade_date", "close"])
        return df[columns].sort_values("trade_date").reset_index(drop=True)

    # ------------------------------------------------------------------
    # 东方财富板块：dc_index / dc_daily / dc_member
    # ------------------------------------------------------------------
    def fetch_dc_index(self, trade_date: DateLike, idx_type: str) -> pd.DataFrame:
        """``dc_index``：某交易日某类板块的列表；``total_mv`` 万元 -> 元。

        ``idx_type`` 取 ``行业板块`` / ``概念板块`` / ``地域板块``。
        """
        columns = ["ts_code", "name", "idx_type", "level", "total_mv"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            for column in ("idx_type", "level"):
                if column not in df.columns:
                    df[column] = None
            df["idx_type"] = df["idx_type"].fillna(idx_type)
            _numeric(df, ["total_mv"])
            df["total_mv"] = df["total_mv"] * 10000  # 万元 -> 元
            return df[columns].reset_index(drop=True)

        return self._market_query_paged(
            "dc_index",
            page_cap=DC_INDEX_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            idx_type=idx_type,
            fields="ts_code,trade_date,name,idx_type,level,total_mv",
            normalize=_normalize,
        )

    def fetch_dc_daily(self, trade_date: DateLike) -> pd.DataFrame:
        """``dc_daily``：全部东方财富板块当日行情（涨跌幅 %、成交额 元）。"""
        columns = ["ts_code", "pct_change", "amount"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            _numeric(df, ["pct_change", "amount"])
            return df[columns].reset_index(drop=True)

        return self._market_query_paged(
            "dc_daily",
            page_cap=DC_DAILY_PAGE_CAP,
            key_cols=["ts_code"],
            trade_date=to_ymd(trade_date),
            fields="ts_code,trade_date,pct_change,amount",
            normalize=_normalize,
        )

    def fetch_dc_member(
        self,
        ts_code: Optional[str],
        trade_date: Optional[DateLike] = None,
        start_date: Optional[DateLike] = None,
        end_date: Optional[DateLike] = None,
    ) -> pd.DataFrame:
        """``dc_member``：板块成分，按单日或日期区间；2024-12-20 起有逐日数据。

        常规用法是逐板块：``ts_code=<板块>`` + ``trade_date``，单板块单日不超过 5000 行，一次请求取全。
        ``ts_code=None`` 且给定 ``trade_date`` 时取当日全部板块的成分，但 Tushare 限制翻页偏移不能大于
        100000（"offset不能大于100000"），而一天超过 10.5 万行，所以这条路径通常无法取全，只作为可选兜底。
        """
        params: dict = {} if ts_code is None else {"ts_code": ts_code}
        if trade_date is not None:
            params["trade_date"] = to_ymd(trade_date)
        if start_date is not None:
            params["start_date"] = to_ymd(start_date)
        if end_date is not None:
            params["end_date"] = to_ymd(end_date)
        columns = ["trade_date", "ts_code", "con_code", "code"]

        def _normalize(df: pd.DataFrame) -> pd.DataFrame:
            if df.empty:
                return _empty(columns)
            df = df.copy()
            df["trade_date"] = _parse_ymd_series(df["trade_date"])
            df["code"] = df["con_code"].map(code_from_ts_code)
            return df[columns].dropna(subset=["trade_date"]).reset_index(drop=True)

        return self._market_query_paged(
            "dc_member",
            page_cap=DC_MEMBER_PAGE_CAP,
            # ts_code is part of the key: without it, the all-boards query would collapse a stock that
            # belongs to several boards into one row
            key_cols=["trade_date", "ts_code", "con_code"],
            fields="trade_date,ts_code,con_code",
            **params,
            normalize=_normalize,
        )
