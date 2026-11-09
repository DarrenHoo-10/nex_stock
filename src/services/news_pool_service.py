# -*- coding: utf-8 -*-
"""AKShare news polling into the intelligence pool (docs/design/core-backend-design.md 5.4).

Cadence (driven by ``news_schedule``; every interval is configurable):

* trading days 08:00-15:30 China time: every ``NEWS_POLL_INTERVAL_MINUTES`` (10) fetch the CLS
  telegraph *and* Eastmoney flash together, merge and dedup; Sina / Futu / THS are fallbacks that
  are used (first one that works) only when one of the two fails;
* other times and non-trading days: every ``NEWS_POLL_OFFHOURS_INTERVAL_MINUTES`` (30), same sources;
* once per trading day (``NEWS_DAILY_FETCH_TIME``, before 08:15): announcements for the watchlist and
  current holdings, and 财经早餐.

The flash APIs cannot be queried by time range - each call returns only the latest batch (CLS 20 rows,
Eastmoney 200, Sina 20, Futu 50, THS one page) - so every source keeps a checkpoint (newest stored
``published_at``). When a new batch's oldest item is newer than the checkpoint the batches do not
overlap and the window in between is recorded as a gap (``news_fetch_gap``); it is ``covered`` when
another full-window source in the same run reaches back past the window start. Open gaps surface in
``news_digest_service.get_news_health`` and are never silently ignored.

CLS fetched with ``重点`` is filtered on the client side, so its oldest row says nothing about
continuity: that source records ``gap_check = filtered`` and is not gap-checked (Eastmoney still is).

Announcement mode (``NEWS_ANNOUNCEMENT_MODE``, default ``auto``): ``stock_notice_report('全部', date)``
walks the whole market's day at 100 rows per request (about 1 + ceil(hits / 100) requests, ~20-60 on a
normal / peak day) and only one calendar day per call, while ``stock_individual_notice_report`` costs 2
requests per code for the whole date range. ``auto`` therefore uses the per-code call when the
watchlist + holdings has at most ``NEWS_ANNOUNCEMENT_BY_CODE_MAX`` codes (the typical case, and it also
backfills a weekend in one call) and falls back to the market listing of the last days otherwise.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from src.repositories.intelligence_repo import IntelligenceRepository
from src.services import news_akshare_source as ak_source
from src.services import news_cninfo_source as cninfo_source
from src.services.sources import investor_qa as qa_source
from src.services.sources import szse_disclosure as szse_source
from src.services.news_akshare_source import (
    CN_TZ,
    FALLBACK_FLASH_SOURCES,
    PRIMARY_FLASH_SOURCES,
    SOURCE_CJZC,
    SOURCE_CLS,
    SOURCE_CNINFO,
    SOURCE_IRM,
    SOURCE_NOTICE,
    SOURCE_SSE_E,
    SOURCE_SZSE,
    KIND_QA,
    NO_URL_PREFIX,
    NewsRecord,
    NewsSourceBusy,
    normalize_a_share_code,
)

logger = logging.getLogger(__name__)

TRADING_WINDOW_START = dtime(8, 0)
TRADING_WINDOW_END = dtime(15, 30)
_DUE_TOLERANCE = 0.9  # a tick that lands a little before the interval is still due
_BY_CODE_PAUSE_SEC = 0.5
_MARKET_DAY_PAUSE_SEC = 1.0
_MAX_CONSECUTIVE_CODE_FAILURES = 3
_MARKET_MODE_MAX_DAYS = 3
_NOTICE_MAX_LOOKBACK_DAYS = 5
_QA_FIRST_LOOKBACK_DAYS = 30  # the platforms only keep about a month
ARTICLE_LOOKBACK_HOURS = 24  # major_news window of the daily job (re-reads are de-duplicated by URL)


def _query_major_news(start: datetime, end: datetime) -> List[Dict[str, Any]]:
    """Tushare ``major_news`` (长篇新闻) between two China times; titles and links only."""
    from data_provider.base import DataFetchError
    from data_provider.tushare_fetcher import TushareFetcher
    from src.services.stock_event_service import _KindSpec, query_rows

    fetcher = TushareFetcher()
    if not fetcher.is_available():
        raise DataFetchError("Tushare 未配置（缺少 TUSHARE_TOKEN）")
    spec = _KindSpec("major_news", "title,pub_time,src,url", 1000, ("title", "pub_time"), "start_date")
    return query_rows(fetcher, spec, start_date=start.strftime("%Y-%m-%d %H:%M:%S"),
                      end_date=end.strftime("%Y-%m-%d %H:%M:%S"))


def _qa_record(item: Any) -> NewsRecord:
    question = item.question.strip()
    return NewsRecord(
        title=f"{item.name or item.code}：{question[:80]}",
        summary=f"问：{question}\n答：{item.answer.strip()}"[:2000],
        url=f"{NO_URL_PREFIX}{item.platform}:{item.item_id}",
        published_at=item.answered_at or item.asked_at,
        source_key=item.platform,
        kind=KIND_QA,
        scope_type="symbol",
        scope_value=item.code,
        stock_codes=[item.code],
        raw_payload={"asked_at": item.asked_at.isoformat() if item.asked_at else None, "platform": item.platform,
                     "question": question, "answer": item.answer.strip()},
    )


def _szse_record(item: Any) -> NewsRecord:
    title = item.title if not item.name or item.title.startswith(item.name) else f"{item.name}：{item.title}"
    return NewsRecord(
        title=title[:300], summary=f"{item.name}（{item.code}）公告，来源：深交所", url=item.url[:1000],
        published_at=item.published_at, source_key=SOURCE_SZSE, kind="announcement", scope_type="symbol",
        scope_value=item.code, stock_codes=[item.code], raw_payload={"name": item.name},
    )

_POLL_LOCK = threading.Lock()
_DAILY_LOCK = threading.Lock()


class _SourceTally:
    """Calls, records and errors of one announcement source in one daily run."""

    def __init__(self) -> None:
        self.successes = 0
        self.count = 0
        self.errors: List[str] = []

    @property
    def attempted(self) -> int:
        return self.successes + len(self.errors)

    def ok(self, count: int) -> None:
        self.successes += 1
        self.count += count

    def fail(self, error: str) -> None:
        self.errors.append(error)

    def status(self) -> str:
        if self.errors:
            return "partial" if self.successes else "failed"
        return "ok" if self.count else "empty"

    def summary(self) -> Dict[str, Any]:
        return {"status": self.status(), "calls": self.attempted, "fetched": self.count, "errors": self.errors}


def china_now() -> datetime:
    """Naive China-time now (all AKShare timestamps are stored as naive China time)."""
    return datetime.now(CN_TZ).replace(tzinfo=None)


def _is_cn_trading_day(day: date) -> bool:
    from src.core.trading_calendar import is_market_open

    return is_market_open("cn", day)  # fail-open when the calendar library is unavailable


def _sanitize(exc: BaseException) -> str:
    try:
        from src.services.run_diagnostics import sanitize_diagnostic_text

        return sanitize_diagnostic_text(f"{type(exc).__name__}: {exc}", max_length=300) or type(exc).__name__
    except Exception:  # noqa: BLE001
        return type(exc).__name__


def _default_holdings() -> List[Tuple[str, str]]:
    from src.repositories.portfolio_repo import PortfolioRepository

    return PortfolioRepository().list_cached_position_identities()


def in_trading_window(now: datetime, is_trading_day: Callable[[date], bool] = _is_cn_trading_day) -> bool:
    return bool(is_trading_day(now.date())) and TRADING_WINDOW_START <= now.time() < TRADING_WINDOW_END


class NewsPoolService:
    def __init__(
        self,
        repo: Optional[IntelligenceRepository] = None,
        config: Any = None,
        *,
        is_trading_day: Callable[[date], bool] = _is_cn_trading_day,
        holdings_provider: Callable[[], Sequence[Tuple[str, str]]] = _default_holdings,
        sleep_fn: Callable[[float], None] = time.sleep,
        events_runner: Optional[Callable[[], Dict[str, Any]]] = None,
        risk_lists_runner: Optional[Callable[[], Dict[str, Any]]] = None,
        macro_runner: Optional[Callable[[], Dict[str, Any]]] = None,
        articles_query: Optional[Callable[[datetime, datetime], List[Dict[str, Any]]]] = None,
    ) -> None:
        self.repo = repo or IntelligenceRepository()
        if config is None:
            from src.config import get_config

            config = get_config()
        self.config = config
        self._is_trading_day = is_trading_day
        self._holdings_provider = holdings_provider
        self._sleep = sleep_fn
        self._events_runner = events_runner
        self._risk_lists_runner = risk_lists_runner
        self._macro_runner = macro_runner
        self._articles_query = articles_query

    # ------------------------------------------------------------------
    # cadence
    # ------------------------------------------------------------------
    def current_interval_minutes(self, now: datetime) -> int:
        if in_trading_window(now, self._is_trading_day):
            return int(self.config.news_poll_interval_minutes)
        return int(self.config.news_poll_offhours_interval_minutes)

    def poll_tick(self, *, now: Optional[datetime] = None, force: bool = False) -> Dict[str, Any]:
        """Scheduler entry: poll only if the current-period interval has elapsed since the last poll."""
        now = now or china_now()
        interval = self.current_interval_minutes(now)
        if not force:
            last = self._last_poll_at()
            if last is not None and (now - last) < timedelta(minutes=interval) * _DUE_TOLERANCE:
                return {"status": "skipped", "reason": "not_due", "interval_minutes": interval}
        result = self.poll_flash(now=now)
        result["interval_minutes"] = interval
        return result

    def _last_poll_at(self) -> Optional[datetime]:
        stamps = []
        for key in PRIMARY_FLASH_SOURCES:
            state = self.repo.get_fetch_state(key)
            if state and state.get("last_fetch_at"):
                stamps.append(state["last_fetch_at"])
        return max(stamps) if stamps else None

    # ------------------------------------------------------------------
    # flash polling
    # ------------------------------------------------------------------
    def poll_flash(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        """One poll round: CLS + Eastmoney (+ first working fallback), merge, dedup, store, gap-check."""
        if not _POLL_LOCK.acquire(blocking=False):
            logger.warning("[News] 上一轮快讯轮询仍在进行，跳过本轮")
            return {"status": "skipped", "reason": "already_running"}
        try:
            return self._poll_flash_locked(now or china_now())
        finally:
            _POLL_LOCK.release()

    def _poll_flash_locked(self, now: datetime) -> Dict[str, Any]:
        timeout = float(self.config.news_fetch_timeout_sec)
        cls_symbol = str(getattr(self.config, "news_akshare_cls_symbol", "全部") or "全部")
        outcomes: Dict[str, Dict[str, Any]] = {}
        batches: Dict[str, List[NewsRecord]] = {}

        def run_source(key: str, *, fallback: bool = False) -> bool:
            via: Optional[str] = None
            try:
                records = ak_source.fetch_flash(key, timeout_sec=timeout, cls_symbol=cls_symbol)
            except NewsSourceBusy as exc:
                outcomes[key] = {"status": "busy", "error": str(exc), "fallback": fallback}
                return False
            except Exception as exc:  # noqa: BLE001 - one source must never take the round down
                error = _sanitize(exc)
                logger.warning("[News] 来源 %s 取数失败: %s", key, error)
                records = self._cls_direct(error, timeout, cls_symbol) if key == SOURCE_CLS else None
                if records is None:
                    outcomes[key] = {"status": "failed", "error": error, "fallback": fallback}
                    return False
                via = "direct"
            batches[key] = records
            outcomes[key] = {"status": "ok" if records else "empty", "count": len(records), "fallback": fallback}
            if via:
                outcomes[key]["via"] = via
            return True

        primary_ok = {key: run_source(key) for key in PRIMARY_FLASH_SOURCES}
        if not all(primary_ok.values()):
            for key in FALLBACK_FLASH_SOURCES:
                if run_source(key, fallback=True):
                    break

        gap_info = self._detect_gaps(batches, cls_symbol=cls_symbol, now=now)

        merged: List[NewsRecord] = []
        for key in (*PRIMARY_FLASH_SOURCES, *FALLBACK_FLASH_SOURCES):  # primary first: it wins a near-dup
            merged.extend(batches.get(key, []))
        stored = self.repo.upsert_news_records(
            merged, fetched_at=now, dedup_window_minutes=int(self.config.news_dedup_window_minutes)
        )

        self._save_source_states(outcomes, batches, gap_info["gap_check"], now)
        retention = self._apply_retention(now)

        succeeded = [k for k, o in outcomes.items() if o["status"] in ("ok", "empty")]
        if not succeeded:
            status = "failed"
        elif all(primary_ok.values()):
            status = "ok"
        else:
            status = "partial"
        result = {
            "status": status,
            "sources": outcomes,
            "fetched": len(merged),
            "inserted": len(stored["inserted"]),
            "inserted_ids": stored["inserted"],
            "same_source_duplicates": stored["same_source"],
            "near_duplicates": stored["near_duplicate"],
            "gaps": gap_info["gaps"],
            "retention": retention,
        }
        logger.info(
            "[News] 快讯轮询 %s: 取到 %d 条，新增 %d，同源重复 %d，跨源近似重复 %d，缺口 %d",
            status, len(merged), len(stored["inserted"]), stored["same_source"], stored["near_duplicate"],
            len(gap_info["gaps"]),
        )
        return result

    @staticmethod
    def _cls_direct(akshare_error: str, timeout: float, cls_symbol: str) -> Optional[List[NewsRecord]]:
        """财联社 without AKShare (sources/cls_telegraph.py); None when that fails too."""
        try:
            records = ak_source.fetch_cls_direct(timeout_sec=timeout, cls_symbol=cls_symbol)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[News] 财联社直连也失败（AKShare：%s）: %s", akshare_error, _sanitize(exc))
            return None
        logger.info("[News] AKShare 财联社失败，改用直连取到 %d 条", len(records))
        return records

    def _gap_check_mode(self, key: str, cls_symbol: str) -> str:
        return "filtered" if key == SOURCE_CLS and cls_symbol != "全部" else "full"

    def _detect_gaps(self, batches: Dict[str, List[NewsRecord]], *, cls_symbol: str, now: datetime) -> Dict[str, Any]:
        gap_check = {key: self._gap_check_mode(key, cls_symbol) for key in batches}
        spans: Dict[str, Tuple[datetime, datetime]] = {}
        for key, records in batches.items():
            times = [r.published_at for r in records if r.published_at is not None]
            if times:
                spans[key] = (min(times), max(times))

        try:
            pool_newest = self.repo.newest_flash_published_at()
        except Exception:  # noqa: BLE001
            pool_newest = None
        found: List[Dict[str, Any]] = []
        for key, (oldest, _newest) in spans.items():
            if gap_check[key] != "full":
                continue
            state = self.repo.get_fetch_state(key)
            checkpoint = state.get("checkpoint_at") if state else None
            if key in FALLBACK_FLASH_SOURCES:
                # A fallback is rarely polled, so its own checkpoint is stale by design: judge it
                # against the newest item the pool already has.
                checkpoint = pool_newest
            if checkpoint is None or oldest <= checkpoint:
                continue
            covered = any(
                other != key and gap_check.get(other) == "full" and span[0] <= checkpoint
                for other, span in spans.items()
            )
            found.append({"source": key, "from": checkpoint, "to": oldest, "covered": covered})
        for gap in found:
            try:
                self.repo.add_fetch_gap(gap["source"], gap["from"], gap["to"], covered=gap["covered"], detected_at=now)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[News] 记录缺口失败: %s", exc)
            level = logging.INFO if gap["covered"] else logging.WARNING
            logger.log(
                level, "[News] 来源 %s 与上次没有重叠，%s ~ %s 可能漏抓%s",
                gap["source"], gap["from"], gap["to"], "（已被其他来源覆盖）" if gap["covered"] else "",
            )
        return {
            "gap_check": gap_check,
            "gaps": [
                {"source": g["source"], "from": g["from"].isoformat(), "to": g["to"].isoformat(), "covered": g["covered"]}
                for g in found
            ],
        }

    def _save_source_states(
        self, outcomes: Dict[str, Dict[str, Any]], batches: Dict[str, List[NewsRecord]],
        gap_check: Dict[str, str], now: datetime,
    ) -> None:
        for key, outcome in outcomes.items():
            fields: Dict[str, Any] = {"last_fetch_at": now}
            if outcome["status"] in ("ok", "empty"):
                times = [r.published_at for r in batches.get(key, []) if r.published_at is not None]
                state = self.repo.get_fetch_state(key)
                old = state.get("checkpoint_at") if state else None
                newest = max(times) if times else None
                fields.update(
                    last_success_at=now, last_status=outcome["status"], last_error=None,
                    last_count=outcome.get("count", 0), gap_check=gap_check.get(key),
                    checkpoint_at=max(x for x in (old, newest) if x is not None) if (old or newest) else None,
                )
            else:
                fields.update(last_status=outcome["status"], last_error=outcome.get("error"))
            try:
                self.repo.save_fetch_state(key, **fields)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[News] 保存来源状态失败 %s: %s", key, exc)

    def _apply_retention(self, now: datetime) -> Dict[str, int]:
        try:
            return self.repo.apply_news_retention(
                flash_days=int(self.config.news_flash_retention_days),
                company_days=int(self.config.news_company_retention_days),
                now=now,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[News] 资讯保留期清理失败: %s", exc)
            return {}

    # ------------------------------------------------------------------
    # daily job: announcements + 财经早餐
    # ------------------------------------------------------------------
    def wanted_codes(self) -> List[str]:
        """Watchlist (``STOCK_LIST``) plus current A-share holdings, normalised and sorted."""
        codes = {normalize_a_share_code(c) for c in (getattr(self.config, "stock_list", None) or [])}
        try:
            for market, symbol in self._holdings_provider() or []:
                if str(market).lower() == "cn":
                    codes.add(normalize_a_share_code(symbol))
        except Exception as exc:  # noqa: BLE001 - holdings are an add-on; the watchlist still works
            logger.warning("[News] 读取持仓失败，仅按自选股取公告: %s", exc)
        return sorted(code for code in codes if code)

    def run_daily_fetch(self, *, day: Optional[date] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
        if not _DAILY_LOCK.acquire(blocking=False):
            return {"status": "skipped", "reason": "already_running"}
        try:
            now = now or china_now()
            day = day or now.date()
            result = {
                "day": day.isoformat(),
                "announcements": self._fetch_announcements(day, now),
                "morning_brief": self._fetch_morning_brief(day, now),
                # Company events of all stocks (Tushare); separate from the news status below.
                "events": self._fetch_events(),
                # Investor Q&A of watchlist + holdings (互动易 / 上证e互动); also separate.
                "qa": self._fetch_qa(now),
                # 东方财富重点监控名单 / 严重异常波动 (no Tushare needed); separate as well.
                "risk_lists": self._fetch_risk_lists(),
                # Tushare major_news of the last day as background articles; separate as well.
                "articles": self._fetch_articles(now),
                # PMI / 货币 / 社融 / LPR / 指数估值和成分 (Tushare; each series at most once in 20 h).
                "macro": self._fetch_macro(),
            }
            statuses = {part["status"] for part in (result["announcements"], result["morning_brief"])}
            result["status"] = (
                "failed" if statuses <= {"failed"} else "partial" if "failed" in statuses else "ok"
            )
            self._apply_retention(now)
            return result
        finally:
            _DAILY_LOCK.release()

    def _fetch_events(self) -> Dict[str, Any]:
        """Daily whole-market company events (src/services/stock_event_service.py); needs a Tushare token."""
        runner = self._events_runner
        if runner is None:
            if not str(getattr(self.config, "tushare_token", "") or "").strip():
                return {"status": "skipped", "reason": "tushare_not_configured"}
            from src.services.stock_event_service import StockEventService

            runner = StockEventService().run_daily
        try:
            return runner()
        except Exception as exc:  # noqa: BLE001 - events must not break the news job
            logger.warning("[News] 公司事件取数失败: %s", exc)
            return {"status": "failed", "error": _sanitize(exc)}

    def _fetch_risk_lists(self) -> Dict[str, Any]:
        runner = self._risk_lists_runner
        if runner is None:
            from src.services.stock_event_service import StockEventService

            runner = StockEventService().refresh_risk_lists
        try:
            return runner()
        except Exception as exc:  # noqa: BLE001 - risk lists must not break the news job
            logger.warning("[News] 东方财富风险名单取数失败: %s", exc)
            return {"status": "failed", "error": _sanitize(exc)}

    def _fetch_macro(self) -> Dict[str, Any]:
        runner = self._macro_runner
        if runner is None:
            if not str(getattr(self.config, "tushare_token", "") or "").strip():
                return {"status": "skipped", "reason": "tushare_not_configured"}
            from src.services.macro_service import MacroService

            runner = MacroService().refresh
        try:
            return runner()
        except Exception as exc:  # noqa: BLE001 - macro data must not break the news job
            logger.warning("[News] 宏观和指数数据取数失败: %s", exc)
            return {"status": "failed", "error": _sanitize(exc)}

    def _fetch_articles(self, now: datetime) -> Dict[str, Any]:
        """Tushare ``major_news`` since the last stored article (at most ``ARTICLE_LOOKBACK_HOURS``)."""
        if not str(getattr(self.config, "tushare_token", "") or "").strip() and self._articles_query is None:
            return {"status": "skipped", "reason": "tushare_not_configured"}
        start = now - timedelta(hours=ARTICLE_LOOKBACK_HOURS)
        try:
            rows = (self._articles_query or _query_major_news)(start, now)
            records = ak_source.parse_major_news(rows)
            stored = self.repo.upsert_news_records(
                records, fetched_at=now, dedup_window_minutes=int(self.config.news_dedup_window_minutes)
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[News] 长篇新闻取数失败: %s", exc)
            self._save_daily_state(ak_source.SOURCE_MAJOR, now, status="failed", count=0, error=_sanitize(exc))
            return {"status": "failed", "error": _sanitize(exc)}
        status = "ok" if records else "empty"
        self._save_daily_state(ak_source.SOURCE_MAJOR, now, status=status, count=len(records))
        return {"status": status, "fetched": len(records), "inserted": len(stored["inserted"])}

    def _announcement_mode(self, code_count: int) -> str:
        mode = str(getattr(self.config, "news_announcement_mode", "auto") or "auto")
        if mode in ("by_code", "market"):
            return mode
        return "by_code" if code_count <= int(self.config.news_announcement_by_code_max) else "market"

    def _announcement_window_begin(self, day: date) -> date:
        """From the last day any announcement source succeeded, within the lookback cap."""
        successes = []
        for key in (SOURCE_CNINFO, SOURCE_NOTICE):
            state = self.repo.get_fetch_state(key)
            if state and state.get("last_success_at"):
                successes.append(state["last_success_at"])
        begin = max(successes).date() if successes else day - timedelta(days=1)
        return min(max(begin, day - timedelta(days=_NOTICE_MAX_LOOKBACK_DAYS)), day - timedelta(days=1))

    def _fetch_announcements(self, day: date, now: datetime) -> Dict[str, Any]:
        """Watchlist + holdings announcements: 巨潮 per code first, 东方财富 as the fallback.

        ``by_code``: each code tries 巨潮, then 东方财富 for that code only. ``market`` (many codes):
        the 东方财富 one-day listing (巨潮 has no affordable market-wide listing); if every day fails,
        巨潮 per code instead. Each source keeps its own fetch state, so the data-status panel shows
        which one is failing; a code counts as failed only when every source failed for it.
        """
        codes = self.wanted_codes()
        if not codes:
            self._save_daily_state(SOURCE_CNINFO, now, status="empty", count=0)
            return {"status": "empty", "reason": "no_codes", "mode": None, "codes": 0}
        timeout = float(self.config.news_fetch_timeout_sec)
        mode = self._announcement_mode(len(codes))
        begin = self._announcement_window_begin(day)

        records: List[NewsRecord] = []
        errors: List[str] = []  # codes / days no source could cover
        tallies: Dict[str, _SourceTally] = {
            SOURCE_CNINFO: _SourceTally(), SOURCE_NOTICE: _SourceTally(), SOURCE_SZSE: _SourceTally(),
        }

        def per_code(fallback_to_em: bool) -> None:
            consecutive = 0
            for index, code in enumerate(codes):
                if index:
                    self._sleep(_BY_CODE_PAUSE_SEC)
                got = self._code_announcements(code, begin, day, timeout, tallies, fallback_to_em=fallback_to_em)
                if got is None:
                    consecutive += 1
                    errors.append(f"{code}: 所有公告来源都失败")
                    if consecutive >= _MAX_CONSECUTIVE_CODE_FAILURES:
                        errors.append("连续失败，停止本次公告抓取以避免触发频控")
                        break
                else:
                    consecutive = 0
                    records.extend(got)

        if mode == "by_code":
            per_code(fallback_to_em=True)
        else:
            span = (day - begin).days
            dates = [begin + timedelta(days=i) for i in range(span + 1)][-_MARKET_MODE_MAX_DAYS:]
            day_errors: List[str] = []
            for index, target in enumerate(dates):
                if index:
                    self._sleep(_MARKET_DAY_PAUSE_SEC)
                try:
                    got = ak_source.fetch_notices_market(target, timeout_sec=timeout, wanted_codes=codes)
                except Exception as exc:  # noqa: BLE001
                    tallies[SOURCE_NOTICE].fail(f"{target}: {_sanitize(exc)}")
                    day_errors.append(f"{target}: 东方财富全市场公告失败")
                    continue
                tallies[SOURCE_NOTICE].ok(len(got))
                records.extend(got)
            if day_errors and not tallies[SOURCE_NOTICE].successes:
                per_code(fallback_to_em=False)
            else:
                errors.extend(day_errors)

        stored = self.repo.upsert_news_records(
            records, fetched_at=now, dedup_window_minutes=int(self.config.news_dedup_window_minutes)
        )
        covered = sum(tally.successes for tally in tallies.values())
        if errors and not covered:
            status = "failed"
        elif errors:
            status = "partial"
        else:
            status = "ok" if records else "empty"
        for key, tally in tallies.items():
            if tally.attempted:
                source_status = tally.status()
                self._save_daily_state(
                    key, now, status=source_status, count=tally.count,
                    error="; ".join(tally.errors)[:500] if tally.errors else None,
                    success=source_status in ("ok", "empty"),
                )
        return {
            "status": status, "mode": mode, "codes": len(codes), "window": [begin.isoformat(), day.isoformat()],
            "api_calls": sum(tally.attempted for tally in tallies.values()),
            "fetched": len(records), "inserted": len(stored["inserted"]), "errors": errors,
            "sources": {key: tally.summary() for key, tally in tallies.items() if tally.attempted},
        }

    def _code_announcements(
        self, code: str, begin: date, day: date, timeout: float, tallies: Dict[str, "_SourceTally"],
        *, fallback_to_em: bool,
    ) -> Optional[List[NewsRecord]]:
        """Announcements of one code from 巨潮, else 东方财富, else 深交所 (深市 only); ``None`` when all failed."""
        try:
            got = cninfo_source.fetch_notices_by_code(code, begin, day, timeout_sec=timeout)
        except Exception as exc:  # noqa: BLE001
            tallies[SOURCE_CNINFO].fail(f"{code}: {_sanitize(exc)}")
            logger.info("[News] 巨潮公告失败 %s，%s: %s", code, "改用东方财富" if fallback_to_em else "跳过", exc)
        else:
            tallies[SOURCE_CNINFO].ok(len(got))
            return got
        if not fallback_to_em:
            return None
        try:
            got = ak_source.fetch_notices_by_code(code, begin, day, timeout_sec=timeout)
        except Exception as exc:  # noqa: BLE001
            tallies[SOURCE_NOTICE].fail(f"{code}: {_sanitize(exc)}")
        else:
            tallies[SOURCE_NOTICE].ok(len(got))
            return got
        if not szse_source.covers(code):
            return None
        try:
            items = ak_source.call_with_timeout(
                SOURCE_SZSE, lambda: szse_source.fetch_by_code(code, begin, day, timeout_sec=timeout), timeout,
            )
        except Exception as exc:  # noqa: BLE001
            tallies[SOURCE_SZSE].fail(f"{code}: {_sanitize(exc)}")
            return None
        got = [_szse_record(item) for item in items]
        tallies[SOURCE_SZSE].ok(len(got))
        return got

    # ------------------------------------------------------------------
    # investor Q&A (互动易 / 上证e互动)
    # ------------------------------------------------------------------
    def _qa_since(self, now: datetime) -> datetime:
        successes = []
        for key in (SOURCE_IRM, SOURCE_SSE_E):
            state = self.repo.get_fetch_state(key)
            if state and state.get("last_success_at"):
                successes.append(state["last_success_at"])
        since = max(successes) - timedelta(days=1) if successes else now - timedelta(days=_QA_FIRST_LOOKBACK_DAYS)
        return max(since, now - timedelta(days=_QA_FIRST_LOOKBACK_DAYS))

    def fetch_qa_for_codes(self, codes: Sequence[str], *, now: datetime, since: datetime) -> Dict[str, Any]:
        """Answered Q&A of ``codes`` from their platform, stored in the pool; one tally per platform."""
        timeout = float(self.config.news_fetch_timeout_sec)
        tallies: Dict[str, _SourceTally] = {SOURCE_IRM: _SourceTally(), SOURCE_SSE_E: _SourceTally()}
        records: List[NewsRecord] = []
        skipped: List[str] = []
        for index, code in enumerate(codes):
            platform = qa_source.route(code)
            if platform is None:
                skipped.append(code)
                continue
            if index:
                self._sleep(_BY_CODE_PAUSE_SEC)
            try:
                items = ak_source.call_with_timeout(
                    platform, lambda: qa_source.fetch_answered(code, since=since, timeout_sec=timeout), timeout * 3,
                )
            except Exception as exc:  # noqa: BLE001
                tallies[platform].fail(f"{code}: {_sanitize(exc)}")
                continue
            tallies[platform].ok(len(items))
            records.extend(_qa_record(item) for item in items)
        stored = self.repo.upsert_news_records(
            records, fetched_at=now, dedup_window_minutes=int(self.config.news_dedup_window_minutes)
        )
        for key, tally in tallies.items():
            if tally.attempted:
                source_status = tally.status()
                self._save_daily_state(key, now, status=source_status, count=tally.count,
                                       error="; ".join(tally.errors)[:500] if tally.errors else None,
                                       success=source_status in ("ok", "empty"))
        attempted = [k for k, tally in tallies.items() if tally.attempted]
        failed = [k for k in attempted if tallies[k].status() == "failed"]
        if not attempted:
            status = "empty"
        elif len(failed) == len(attempted):
            status = "failed"
        elif any(tallies[k].errors for k in attempted):
            status = "partial"
        else:
            status = "ok"
        return {
            "status": status, "codes": len(codes), "since": since.isoformat(timespec="minutes"),
            "fetched": len(records), "inserted": len(stored["inserted"]), "skipped": skipped,
            "sources": {key: tally.summary() for key, tally in tallies.items() if tally.attempted},
        }

    def _fetch_qa(self, now: datetime) -> Dict[str, Any]:
        codes = self.wanted_codes()
        if not codes:
            return {"status": "empty", "reason": "no_codes", "codes": 0}
        try:
            return self.fetch_qa_for_codes(codes, now=now, since=self._qa_since(now))
        except Exception as exc:  # noqa: BLE001 - Q&A must not break the news job
            logger.warning("[News] 投资者问答取数失败: %s", exc)
            return {"status": "failed", "error": _sanitize(exc)}

    def _fetch_morning_brief(self, day: date, now: datetime) -> Dict[str, Any]:
        timeout = float(self.config.news_fetch_timeout_sec)
        try:
            records = ak_source.fetch_morning_brief(timeout_sec=timeout)
        except Exception as exc:  # noqa: BLE001
            error = _sanitize(exc)
            logger.warning("[News] 财经早餐取数失败: %s", error)
            self._save_daily_state(SOURCE_CJZC, now, status="failed", count=0, error=error)
            return {"status": "failed", "error": error}
        floor = datetime.combine(day - timedelta(days=int(self.config.news_flash_retention_days)), dtime.min)
        records = [r for r in records if r.published_at is None or r.published_at >= floor]
        stored = self.repo.upsert_news_records(
            records, fetched_at=now, dedup_window_minutes=int(self.config.news_dedup_window_minutes)
        )
        status = "ok" if records else "empty"
        self._save_daily_state(SOURCE_CJZC, now, status=status, count=len(records), success=True)
        return {"status": status, "fetched": len(records), "inserted": len(stored["inserted"])}

    def _save_daily_state(
        self, key: str, now: datetime, *, status: str, count: int, error: Optional[str] = None, success: bool = True,
    ) -> None:
        fields: Dict[str, Any] = {
            "last_fetch_at": now, "last_status": status, "last_error": error, "last_count": count,
            "gap_check": "none",
        }
        if success and status in ("ok", "empty"):
            fields["last_success_at"] = now
        try:
            self.repo.save_fetch_state(key, **fields)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[News] 保存来源状态失败 %s: %s", key, exc)
