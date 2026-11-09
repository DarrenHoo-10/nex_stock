# -*- coding: utf-8 -*-
"""AKShare news sources for the intelligence pool (docs/design/core-backend-design.md 5.4).

This module only knows how to *call* the AKShare interfaces and how to turn their DataFrames into
``NewsRecord``s; storage, cadence, dedup and gap detection live in ``news_pool_service``.

Every AKShare call goes through ``call_with_timeout``: ``stock_info_global_cls`` retries up to 10
times with back-off and none of the interfaces set a request timeout, so a call runs in a daemon
worker thread that is abandoned after the timeout, and a source whose previous call is still running
is skipped (``NewsSourceBusy``) instead of piling up threads.

Column names (AKShare 1.19.3, read from the source, never executed here)::

    stock_info_global_cls    标题 内容 发布日期(date) 发布时间(time)   China time, no URL, 20 rows
    stock_info_global_em     标题 摘要 发布时间(str) 链接            200 rows
    stock_info_global_sina   时间 内容                                no title / URL, 20 rows
    stock_info_global_futu   标题 内容 发布时间(str) 链接            50 rows, host-local clock
    stock_info_global_ths    标题 内容 发布时间(str) 链接            one page, host-local clock
    stock_info_cjzc_em       标题 摘要 发布时间(str) 链接            财经早餐, two pages
    stock_notice_report /
    stock_individual_notice_report  代码 名称 公告标题 公告类型 公告日期(date) 网址
"""

from __future__ import annotations

import hashlib
import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set
from zoneinfo import ZoneInfo

from data_provider.eastmoney_gate import install as _install_eastmoney_gate

_install_eastmoney_gate()  # AkShare flash / notice calls hit East Money

logger = logging.getLogger(__name__)

CN_TZ = ZoneInfo("Asia/Shanghai")

SOURCE_TYPE = "akshare"
KIND_FLASH = "flash"
KIND_ANNOUNCEMENT = "announcement"
KIND_MORNING_BRIEF = "morning_brief"
KIND_QA = "qa"  # investor Q&A (互动易 / 上证e互动), src/services/sources/investor_qa.py

SOURCE_CLS = "cls"
SOURCE_EM = "em"
SOURCE_SINA = "sina"
SOURCE_FUTU = "futu"
SOURCE_THS = "ths"
SOURCE_NOTICE = "notice"
SOURCE_SZSE = "szse"  # 深交所公告, third announcement source (src/services/sources/szse_disclosure.py)
SOURCE_IRM = "irm"  # 巨潮互动易
SOURCE_SSE_E = "sse_e"  # 上证e互动
SOURCE_CNINFO = "cninfo"  # 巨潮资讯公告, fetched in news_cninfo_source (direct HTTP, not AKShare)
SOURCE_CJZC = "cjzc"

PRIMARY_FLASH_SOURCES = (SOURCE_CLS, SOURCE_EM)
FALLBACK_FLASH_SOURCES = (SOURCE_SINA, SOURCE_FUTU, SOURCE_THS)

SOURCE_LABELS: Dict[str, str] = {
    SOURCE_CLS: "财联社电报",
    SOURCE_EM: "东方财富快讯",
    SOURCE_SINA: "新浪财经快讯",
    SOURCE_FUTU: "富途快讯",
    SOURCE_THS: "同花顺快讯",
    SOURCE_CNINFO: "巨潮资讯公告",
    SOURCE_NOTICE: "东方财富公告",
    SOURCE_SZSE: "深交所公告",
    SOURCE_IRM: "巨潮互动易",
    SOURCE_SSE_E: "上证e互动",
    SOURCE_CJZC: "东方财富财经早餐",
}

NO_URL_PREFIX = "no-url:intel:"  # same marker as IntelligenceService for URL-less items
_MAX_TITLE = 300
_MAX_SUMMARY = 2000


class NewsSourceError(RuntimeError):
    """A news source could not be fetched or parsed."""


class NewsSourceTimeout(NewsSourceError):
    """The AKShare call did not return within the timeout."""


class NewsSourceBusy(NewsSourceError):
    """The previous call for this source is still running; this round skips it."""


@dataclass
class NewsRecord:
    """One normalised news item, ready for ``IntelligenceRepository.upsert_news_records``."""

    title: str
    summary: str
    url: str
    published_at: Optional[datetime]  # naive, China time
    source_key: str
    kind: str = KIND_FLASH
    scope_type: str = "market"
    scope_value: Optional[str] = None
    stock_codes: List[str] = field(default_factory=list)
    raw_payload: Dict[str, Any] = field(default_factory=dict)

    @property
    def source_name(self) -> str:
        return f"{SOURCE_TYPE}:{self.source_key}"

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source_key, self.source_key)


# ---------------------------------------------------------------------------
# timeout / skip-if-running wrapper
# ---------------------------------------------------------------------------
_INFLIGHT: Dict[str, threading.Thread] = {}
_INFLIGHT_LOCK = threading.Lock()


def call_with_timeout(key: str, func: Callable[[], Any], timeout_sec: float) -> Any:
    """Run ``func`` in a daemon thread and wait at most ``timeout_sec``.

    Raises ``NewsSourceBusy`` when a previous call for ``key`` is still running (an abandoned
    timed-out call keeps the key busy until it really finishes), ``NewsSourceTimeout`` on timeout and
    re-raises whatever ``func`` raised otherwise.
    """
    box: Dict[str, Any] = {}

    def _run() -> None:
        try:
            box["value"] = func()
        except BaseException as exc:  # noqa: BLE001 - handed back to the caller thread
            box["error"] = exc

    with _INFLIGHT_LOCK:
        previous = _INFLIGHT.get(key)
        if previous is not None and previous.is_alive():
            raise NewsSourceBusy(f"{key}: previous call still running")
        worker = threading.Thread(target=_run, daemon=True, name=f"news-ak-{key}")
        _INFLIGHT[key] = worker
        worker.start()
    worker.join(max(0.1, float(timeout_sec)))
    if worker.is_alive():
        raise NewsSourceTimeout(f"{key}: no response within {timeout_sec:g}s")
    if "error" in box:
        raise box["error"]
    return box.get("value")


def reset_inflight_for_tests() -> None:
    with _INFLIGHT_LOCK:
        _INFLIGHT.clear()


def _akshare() -> Any:
    """The akshare module (separate function so tests can substitute a stub)."""
    import akshare as ak  # heavy import, kept lazy

    return ak


# ---------------------------------------------------------------------------
# normalisation helpers
# ---------------------------------------------------------------------------
def _text(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:  # NaN
            return ""
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", str(value))).strip()


_BRACKET_TITLE_RE = re.compile(r"^[【\[]([^】\]]{2,120})[】\]]")
_SENTENCE_END_RE = re.compile(r"[。！？!?；;]")


def derive_title(content: str) -> str:
    """Title for items that only have a body (CLS telegraph, Sina): ``【标题】`` prefix or first sentence."""
    body = _text(content)
    if not body:
        return ""
    match = _BRACKET_TITLE_RE.match(body)
    if match:
        return match.group(1).strip()[:_MAX_TITLE]
    end = _SENTENCE_END_RE.search(body)
    first = body[: end.start()] if end and end.start() >= 8 else body
    return first[:60].strip()


def _to_cn_datetime(value: Any, *, assume_tz: Optional[ZoneInfo] = CN_TZ) -> Optional[datetime]:
    """Parse a DataFrame cell into a naive China-time datetime (``None`` when unparsable)."""
    if value is None:
        return None
    try:
        if value != value:  # NaN / NaT
            return None
    except Exception:  # noqa: BLE001
        pass
    parsed: Optional[datetime]
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime(value.year, value.month, value.day)
    else:
        raw = str(value).strip()
        if not raw:
            return None
        parsed = None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsed is None:
            try:
                parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            except ValueError:
                return None
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(CN_TZ).replace(tzinfo=None)
    return parsed


def _combine_date_time(day: Any, clock: Any) -> Optional[datetime]:
    if isinstance(clock, datetime):
        return _to_cn_datetime(clock)
    if isinstance(day, datetime):
        day = day.date()
    if isinstance(clock, str):
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                clock = datetime.strptime(clock.strip(), fmt).time()
                break
            except ValueError:
                continue
    if not isinstance(day, date) or not isinstance(clock, dtime):
        return None
    return datetime.combine(day, clock)


def synthetic_url(source_key: str, title: str, published_at: Optional[datetime], body: str = "") -> str:
    """Stable URL-less key (CLS / Sina have no link): hash of source, title and publish second."""
    stamp = published_at.isoformat() if published_at else ""
    digest = hashlib.sha256(f"{source_key}|{title}|{stamp}|{body[:80]}".encode("utf-8")).hexdigest()[:24]
    return f"{NO_URL_PREFIX}{source_key}:{digest}"


def _clean_url(value: Any) -> str:
    url = _text(value)
    return url if url.lower().startswith(("http://", "https://")) else ""


def _rows(df: Any) -> List[Dict[str, Any]]:
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


def _build_flash(
    source_key: str,
    *,
    title: Any,
    body: Any,
    url: Any,
    published_at: Optional[datetime],
    kind: str = KIND_FLASH,
    raw: Optional[Dict[str, Any]] = None,
) -> Optional[NewsRecord]:
    title_text = _text(title)
    body_text = _text(body)
    if not title_text:
        title_text = derive_title(body_text)
    if not title_text:
        return None
    link = _clean_url(url) or synthetic_url(source_key, title_text, published_at, body_text)
    return NewsRecord(
        title=title_text[:_MAX_TITLE],
        summary=body_text[:_MAX_SUMMARY],
        url=link[:1000],
        published_at=published_at,
        source_key=source_key,
        kind=kind,
        raw_payload=raw or {},
    )


# ---------------------------------------------------------------------------
# DataFrame parsers (pure)
# ---------------------------------------------------------------------------
def parse_cls(df: Any) -> List[NewsRecord]:
    records = []
    for row in _rows(df):
        published = _combine_date_time(row.get("发布日期"), row.get("发布时间"))
        record = _build_flash(
            SOURCE_CLS, title=row.get("标题"), body=row.get("内容"), url=None, published_at=published,
        )
        if record is not None:
            records.append(record)
    return records


def parse_em(df: Any) -> List[NewsRecord]:
    records = []
    for row in _rows(df):
        record = _build_flash(
            SOURCE_EM,
            title=row.get("标题"),
            body=row.get("摘要"),
            url=row.get("链接"),
            published_at=_to_cn_datetime(row.get("发布时间")),
        )
        if record is not None:
            records.append(record)
    return records


def parse_sina(df: Any) -> List[NewsRecord]:
    records = []
    for row in _rows(df):
        record = _build_flash(
            SOURCE_SINA, title=None, body=row.get("内容"), url=None,
            published_at=_to_cn_datetime(row.get("时间")),
        )
        if record is not None:
            records.append(record)
    return records


def _parse_title_content_link(source_key: str, df: Any, kind: str = KIND_FLASH) -> List[NewsRecord]:
    records = []
    for row in _rows(df):
        record = _build_flash(
            source_key,
            title=row.get("标题"),
            body=row.get("内容") if "内容" in row else row.get("摘要"),
            url=row.get("链接"),
            published_at=_to_cn_datetime(row.get("发布时间")),
            kind=kind,
        )
        if record is not None:
            records.append(record)
    return records


def parse_futu(df: Any) -> List[NewsRecord]:
    return _parse_title_content_link(SOURCE_FUTU, df)


def parse_ths(df: Any) -> List[NewsRecord]:
    return _parse_title_content_link(SOURCE_THS, df)


def parse_cjzc(df: Any) -> List[NewsRecord]:
    return _parse_title_content_link(SOURCE_CJZC, df, kind=KIND_MORNING_BRIEF)


def normalize_a_share_code(value: Any) -> str:
    """6-digit A-share code from ``600519`` / ``sh600519`` / ``600519.SH``; empty for anything else."""
    text = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).lower()
    for prefix in ("sh", "sz", "bj"):
        if text.startswith(prefix) and text[len(prefix):].isdigit():
            text = text[len(prefix):]
    if text.endswith(("sh", "sz", "bj")) and text[:-2].isdigit():
        text = text[:-2]
    return text if text.isdigit() and len(text) == 6 else ""


def parse_notices(df: Any, wanted_codes: Optional[Iterable[str]] = None) -> List[NewsRecord]:
    """Announcements -> stock-scope records; ``wanted_codes`` filters the market-wide listing."""
    wanted: Optional[Set[str]] = None
    if wanted_codes is not None:
        wanted = {code for code in (normalize_a_share_code(c) for c in wanted_codes) if code}
    records: List[NewsRecord] = []
    for row in _rows(df):
        code = normalize_a_share_code(row.get("代码"))
        if not code or (wanted is not None and code not in wanted):
            continue
        name = _text(row.get("名称"))
        headline = _text(row.get("公告标题"))
        if not headline:
            continue
        category = _text(row.get("公告类型"))
        published = _to_cn_datetime(row.get("公告日期"))
        title = f"{name}：{headline}" if name else headline
        url = _clean_url(row.get("网址")) or synthetic_url(SOURCE_NOTICE, f"{code}|{headline}", published)
        summary = f"{name}（{code}）公告" + (f"，类型：{category}" if category else "")
        records.append(
            NewsRecord(
                title=title[:_MAX_TITLE],
                summary=summary,
                url=url[:1000],
                published_at=published,
                source_key=SOURCE_NOTICE,
                kind=KIND_ANNOUNCEMENT,
                scope_type="symbol",
                scope_value=code,
                stock_codes=[code],
                raw_payload={"name": name, "category": category},
            )
        )
    return records


# ---------------------------------------------------------------------------
# fetchers (the only code that touches AKShare)
# ---------------------------------------------------------------------------
_FLASH_PARSERS: Dict[str, Callable[[Any], List[NewsRecord]]] = {
    SOURCE_CLS: parse_cls,
    SOURCE_EM: parse_em,
    SOURCE_SINA: parse_sina,
    SOURCE_FUTU: parse_futu,
    SOURCE_THS: parse_ths,
}


def fetch_flash(source_key: str, *, timeout_sec: float, cls_symbol: str = "全部") -> List[NewsRecord]:
    """One AKShare flash call (``cls`` / ``em`` / ``sina`` / ``futu`` / ``ths``), parsed."""
    parser = _FLASH_PARSERS.get(source_key)
    if parser is None:
        raise NewsSourceError(f"unknown flash source: {source_key}")

    def _call() -> Any:
        ak = _akshare()
        if source_key == SOURCE_CLS:
            return ak.stock_info_global_cls(symbol=cls_symbol)
        return getattr(ak, f"stock_info_global_{source_key}")()

    df = call_with_timeout(source_key, _call, timeout_sec)
    return parser(df)


MORNING_BRIEF_KEEP_DAYS = 2


def fetch_morning_brief(*, timeout_sec: float, now: Optional[datetime] = None) -> List[NewsRecord]:
    """财经早餐：接口一次返回数百期历史（实测约 400 期），只保留最近 ``MORNING_BRIEF_KEEP_DAYS`` 天。"""
    df = call_with_timeout(SOURCE_CJZC, lambda: _akshare().stock_info_cjzc_em(), timeout_sec)
    records = parse_cjzc(df)
    current = now or datetime.now(CN_TZ)
    if current.tzinfo is None:
        current = current.replace(tzinfo=CN_TZ)
    cutoff = (current - timedelta(days=MORNING_BRIEF_KEEP_DAYS)).replace(tzinfo=None)
    kept = []
    for record in records:
        published = record.published_at
        if published is None:
            continue
        stamp = published.astimezone(CN_TZ).replace(tzinfo=None) if published.tzinfo else published
        if stamp >= cutoff:
            kept.append(record)
    return kept


def fetch_notices_market(day: date, *, timeout_sec: float, wanted_codes: Sequence[str]) -> List[NewsRecord]:
    """Market-wide listing of one day (pages 100 rows per request internally), filtered locally."""
    stamp = day.strftime("%Y%m%d")
    df = call_with_timeout(
        SOURCE_NOTICE, lambda: _akshare().stock_notice_report(symbol="全部", date=stamp), timeout_sec
    )
    return parse_notices(df, wanted_codes)


def fetch_notices_by_code(code: str, begin: date, end: date, *, timeout_sec: float) -> List[NewsRecord]:
    """One code over ``begin..end`` (a single page for any realistic watchlist stock)."""
    df = call_with_timeout(
        SOURCE_NOTICE,
        lambda: _akshare().stock_individual_notice_report(
            security=code,
            symbol="全部",
            begin_date=begin.strftime("%Y-%m-%d"),
            end_date=end.strftime("%Y-%m-%d"),
        ),
        timeout_sec,
    )
    return parse_notices(df, [code])
