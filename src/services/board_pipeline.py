# -*- coding: utf-8 -*-
"""Board pipeline for one trade date D (docs/design/core-backend-design.md 2.9).

Designed to run at 17:00 on D, when ``daily_basic`` / ``limit_list_d`` / ``dc_*`` of D are not
published yet (they arrive 17:00-19:00 / 18:30-19:30 / 20:30):

    previous  ->  membership  ->  ingest  ->  metrics  ->  signals  ->  us_link

* ``previous``: complete D-1 (the previous session): its real ``daily_basic`` / ``limit_list_d``
  replace the values derived the day before, and its ``dc_index`` / ``dc_daily`` are fetched.
* ``membership``: fold D-1's ``dc_member`` for the active CONCEPT boards into the zipper table, one
  ``dc_member(ts_code, trade_date)`` call per board (the all-boards query cannot finish: Tushare caps the
  paging offset at 100000 and a day has more than 105k rows). A board whose call fails or returns nothing
  keeps its membership; boards already folded for D-1 are skipped, so a re-run resumes. D uses the latest
  membership, so new concepts appear one day late. Industry boards change rarely and are refreshed weekly by
  ``run_industry_membership`` (scheduled by ``board_pipeline_schedule``) instead.
* ``ingest``: D's ``daily`` (required) / ``stk_limit`` / ``suspend_d``; turnover, float cap and
  limit facts are derived when the real interfaces are not there yet (recorded in
  ``market_daily.quality``).
* ``metrics``: recompute D-1's facts-only metrics when step 1 or 2 changed its inputs, then D.
* ``signals``: D only. Signals already written for D-1 are judgments tied to their run and are
  never rewritten.
* ``us_link`` (non-fatal, last): fetch the previous completed US session of the US leaders
  (``board_us_link``, docs 5.1 - 5.3) and add ``evidence["us_link"]`` to the boards mapped from leaders
  that crossed their threshold. A failure only marks the step ``partial`` / ``failed`` (a warning);
  states and reasons are not touched. It runs when ``us_service`` is given, or by default when the
  pipeline does its own live ingestion (no ``ingest_service`` injected and not ``skip_ingest``).

and one ``daily_runs`` row carrying the per-step results and the threshold snapshot.
Every step is idempotent: re-running a date re-uses ingested data, writes the same metric
rows, and (with ``force``) adds a new run whose signals supersede the older run's for
readers (the older run is kept as history).

``board_pipeline_schedule`` registers this flow with the scheduler (``BOARD_PIPELINE_*``) and the weekly
industry-membership job (``BOARD_INDUSTRY_MEMBERSHIP_*``); ``scripts/run_board_pipeline.py`` runs either by hand.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from src.repositories.board_analysis_repo import BoardAnalysisRepository
from src.services.board_common import CalendarFn, as_date, default_calendar_fn
from src.services.board_metrics_service import BoardMetricsService
from src.services.board_signal_service import BoardSignalService

logger = logging.getLogger(__name__)

STALE_RUN_AFTER = timedelta(minutes=30)
_CN_TZ = ZoneInfo("Asia/Shanghai")


def _now_iso() -> str:
    return datetime.now(_CN_TZ).isoformat(timespec="seconds")


def _china_today() -> date:
    return datetime.now(_CN_TZ).date()


_AUTO_US = object()  # ``us_service`` default: run the US linkage only on a live (non-injected) ingestion


def _step(key: str, label: str, status: str, detail: str = "", data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    return {"key": key, "label": label, "status": status, "finished_at": _now_iso(), "detail": detail, "data": data or {}}


def run_board_pipeline(
    trade_date: Any = None,
    *,
    force: bool = False,
    skip_ingest: bool = False,
    skip_members: bool = False,
    recompute_days: int = 0,
    ingest_service: Any = None,
    repo: Optional[BoardAnalysisRepository] = None,
    metrics_service: Optional[BoardMetricsService] = None,
    signal_service: Optional[BoardSignalService] = None,
    calendar_fn: Optional[CalendarFn] = default_calendar_fn,
    us_service: Any = _AUTO_US,
) -> Dict[str, Any]:
    """Run previous -> membership -> ingest -> metrics -> signals for ``trade_date`` (default: today in CN).

    Returns ``{"status", "run_id", "trade_date", "steps", ...}``; ``status`` is one of
    ``succeeded`` / ``partial`` / ``failed`` / ``skipped``. Never raises for step failures:
    they are recorded on the run so the caller (a scheduler) can continue with the rest of the
    daily flow.
    """
    day = as_date(trade_date) if trade_date is not None else _china_today()
    if us_service is _AUTO_US:
        us_service = _AUTO_US if (ingest_service is None and not skip_ingest) else None
    repo = repo or BoardAnalysisRepository()
    signal_service = signal_service or BoardSignalService(repo=repo)
    metrics_service = metrics_service or BoardMetricsService(repo=repo, thresholds=signal_service.thresholds)

    repo.prune_stale_running(STALE_RUN_AFTER, "运行超时未结束，已标记为失败")
    running = repo.find_run(day, ["running"])
    if running is not None:
        return {"status": "skipped", "run_id": running["id"], "trade_date": day.isoformat(),
                "reason": "same-day run is still in progress", "steps": running["steps"]}
    if not force:
        done = repo.find_run(day, ["succeeded"])
        if done is not None:
            return {"status": "skipped", "run_id": done["id"], "trade_date": day.isoformat(),
                    "reason": "a succeeded run already exists (use force to run again)", "steps": done["steps"]}

    run_id = repo.start_run(day, signal_service.thresholds_snapshot())
    steps: List[Dict[str, Any]] = []
    run_status = "succeeded"
    error: Optional[str] = None

    def save() -> None:
        repo.update_run(run_id, steps=steps)

    try:
        previous_day: Optional[date] = None
        previous_changed = False
        if skip_ingest:
            steps.append(_step("previous", "补全上一交易日", "skipped", "按参数跳过，使用本地已入库的数据"))
            steps.append(_step("membership", "板块成分", "skipped", "按参数跳过"))
            steps.append(_step("ingest", "行情入库", "skipped", "按参数跳过，使用本地已入库的数据"))
        else:
            ingest_service = ingest_service or _default_ingest_service()
            previous_day = _previous_session(day, repo, calendar_fn)

            # 1. complete D-1 (real daily_basic / limit_list_d / boards replace the derived values) --
            step, previous_changed = _complete_previous(previous_day, ingest_service)
            steps.append(step)
            save()

            # 2. membership of D-1 (concept boards, one dc_member call per board) ---------------------
            if skip_members:
                steps.append(_step("membership", "板块成分", "skipped", "按参数跳过"))
            else:
                step, folded = _refresh_membership(previous_day, ingest_service)
                steps.append(step)
                previous_changed = previous_changed or folded
            save()

            # 3. ingest D -------------------------------------------------------------------------
            steps.append(_ingest_day(day, ingest_service))
        if repo.list_ingested_dates(upto=day, limit=1) != [day]:
            steps.append(_step("metrics", "板块指标", "skipped", "当日没有个股行情，无法计算"))
            raise _NoData("当日没有行情数据（非交易日或数据尚未入库）")
        save()

        # 4. metrics ----------------------------------------------------------------
        recomputed: set = set()
        if recompute_days > 0:
            dates = [d for d in repo.list_ingested_dates(upto=day, limit=recompute_days + 1) if d < day]
            metrics_service.compute_range(dates)
            recomputed.update(dates)
        else:
            recomputed.update(metrics_service.ensure_history(day))
        previous_recomputed = False
        if previous_day is not None and previous_changed:
            # D-1's facts (real daily_basic / limit_list_d) or membership changed: refresh its metrics
            # (facts only; its signals stay as judged) before D reads them for 5-day fields.
            if previous_day not in recomputed:
                metrics_service.compute_trade_date(previous_day)
            previous_recomputed = True
        metrics = metrics_service.compute_trade_date(day)
        flags = list(metrics.market_flags)
        m_status = "succeeded" if metrics.status == "computed" and not flags else "partial"
        if metrics.status != "computed":
            m_status = "failed"
        detail = f"{metrics.boards_computed}/{metrics.boards_total} 个板块已计算" + (f"；缺失 {flags}" if flags else "")
        if previous_recomputed:
            detail += f"；已重算上一交易日 {previous_day} 的指标"
        steps.append(_step(
            "metrics", "板块指标", m_status, detail,
            dict(metrics.to_dict(), previous_recomputed=previous_day.isoformat() if previous_recomputed else None),
        ))
        save()
        if metrics.status != "computed":
            raise _NoData("没有可计算的个股行情")

        # 4. signals ----------------------------------------------------------------
        batch = signal_service.evaluate_trade_date(day)
        repo.replace_signals(run_id, day, batch.signals)
        s_status = "partial" if (batch.core_missing or batch.warnings) else "succeeded"
        steps.append(_step(
            "signals", "异动判定", s_status,
            f"{len(batch.signals)} 个板块，状态分布 {batch.counts}", batch.summary(),
        ))
        save()

        # 5. us_link (non-fatal): previous US session -> evidence on mapped boards ------------
        if us_service is not None:
            steps.append(_us_link_step(run_id, day, repo, None if us_service is _AUTO_US else us_service))
            save()
    except _NoData as exc:
        run_status, error = "failed", str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.error("[BoardPipeline] %s failed: %s", day, exc, exc_info=True)
        run_status, error = "failed", f"{type(exc).__name__}: {exc}"[:300]
        steps.append(_step("error", "异常中断", "failed", error))
    else:
        statuses = [s["status"] for s in steps]
        if "failed" in statuses or "partial" in statuses:
            run_status = "partial"
    repo.update_run(run_id, status=run_status, steps=steps, error=error, finished=True)
    return {"status": run_status, "run_id": run_id, "trade_date": day.isoformat(), "steps": steps, "error": error}


def latest_completed_session(today: date, calendar_fn: Optional[CalendarFn] = default_calendar_fn) -> date:
    """The latest trading day strictly before ``today`` (its ``dc_member`` is published 20:30 that day).

    Exchange calendar first; without one, the previous Monday-Friday day.
    """
    sessions = None
    if calendar_fn is not None:
        try:
            sessions = calendar_fn(today - timedelta(days=20), today - timedelta(days=1))
        except Exception as exc:  # noqa: BLE001 - the calendar is best effort
            logger.debug("[BoardPipeline] calendar lookup failed: %s", exc)
    earlier = [s for s in (sessions or []) if s < today]
    if earlier:
        return max(earlier)
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def run_industry_membership(
    trade_date: Any = None,
    *,
    today: Optional[date] = None,
    ingest_service: Any = None,
    calendar_fn: Optional[CalendarFn] = default_calendar_fn,
    progress: Any = None,
) -> Dict[str, Any]:
    """Weekly job body: fold the latest trading day's ``dc_member`` for every active INDUSTRY board.

    ``trade_date`` defaults to the latest trading day before ``today`` (China date). One
    ``dc_member(ts_code, trade_date)`` call per board with the same failure rules as the daily concept step
    (failed / empty boards keep their membership, counts recorded under
    ``market_daily.quality.ingestion.membership.industry``); boards already folded for that day are
    skipped, so a repeated run makes no request. Never raises; returns
    ``{"status": succeeded / partial / failed / skipped, "trade_date", "detail", "result"}``.
    """
    day = as_date(trade_date) if trade_date is not None else latest_completed_session(today or _china_today(), calendar_fn)
    try:
        service = ingest_service or _default_ingest_service()
        result = service.refresh_board_membership(day, "industry", progress=progress)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BoardPipeline] industry membership refresh failed: %s", exc, exc_info=True)
        return {"status": "failed", "trade_date": day.isoformat(), "detail": f"{day}: {str(exc)[:200]}", "result": {}}
    step_status, detail = membership_step_outcome(day, result, "行业板块")
    return {"status": step_status, "trade_date": day.isoformat(), "detail": detail, "result": result}


def _us_link_step(run_id: int, day: date, repo: BoardAnalysisRepository, us_service: Any) -> Dict[str, Any]:
    from src.services.board_us_link import attach_us_link

    status, detail, data = attach_us_link(run_id, day, repo, us_service=us_service)
    return _step("us_link", "隔夜美股联动", status, detail, data)


class _NoData(RuntimeError):
    pass


def _default_ingest_service() -> Any:
    from src.services.market_snapshot_service import MarketSnapshotService

    return MarketSnapshotService()


_INGEST_STEP_STATUS = {"complete": "succeeded", "provisional": "succeeded", "skipped": "skipped", "partial": "partial"}


def _previous_session(day: date, repo: BoardAnalysisRepository, calendar_fn: Optional[CalendarFn]) -> Optional[date]:
    """The trading session before ``day``: exchange calendar first, else the newest ingested date."""
    sessions = None
    if calendar_fn is not None:
        try:
            sessions = calendar_fn(day - timedelta(days=20), day)
        except Exception as exc:  # noqa: BLE001 - the calendar is best effort
            logger.debug("[BoardPipeline] calendar lookup failed: %s", exc)
    earlier = [s for s in (sessions or []) if s < day]
    if earlier:
        return max(earlier)
    older = repo.list_ingested_dates(upto=day - timedelta(days=1), limit=1)
    return older[0] if older else None


def _complete_previous(previous_day: Optional[date], ingest_service: Any) -> Tuple[Dict[str, Any], bool]:
    """Re-ingest D-1 with real data (a no-op when it is already ``complete``); returns (step, changed)."""
    label = "补全上一交易日"
    if previous_day is None:
        return _step("previous", label, "skipped", "无法确定上一交易日（没有交易日历且本地没有历史行情）"), False
    try:
        report = ingest_service.ingest_trade_date(previous_day, allow_derived=True, boards=True)
    except Exception as exc:  # noqa: BLE001 - recorded on the run; D can still be computed
        logger.warning("[BoardPipeline] completing %s failed: %s", previous_day, exc, exc_info=True)
        return _step("previous", label, "failed", f"{previous_day}: {str(exc)[:200]}"), False
    data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
    if report.status == "skipped":
        return _step("previous", label, "skipped", f"{previous_day} 已是完整数据，无需补全", data), False
    if report.status == "no_data":
        return _step("previous", label, "partial", f"{previous_day} 没有行情数据，无法补全", data), False
    detail = f"{previous_day} 个股 {report.stock_rows} 行已按实际数据重写，板块目录与板块行情已更新"
    derived = sorted(set(getattr(report, "derived", []) or []))
    if derived:
        detail += f"；{derived} 仍未发布，继续使用推算值"
    if report.missing or report.incomplete:
        detail += f"；缺失 {sorted(set(report.missing))}，不完整 {sorted(set(report.incomplete))}"
    return _step("previous", label, _INGEST_STEP_STATUS.get(report.status, "partial"), detail, data), True


def _refresh_membership(previous_day: Optional[date], ingest_service: Any) -> Tuple[Dict[str, Any], bool]:
    """Fold D-1's concept-board membership board by board; returns (step, membership_changed)."""
    label = "板块成分"
    if previous_day is None:
        return _step("membership", label, "skipped", "无法确定上一交易日"), False
    try:
        result = ingest_service.refresh_daily_membership(previous_day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BoardPipeline] membership refresh failed: %s", exc, exc_info=True)
        return _step("membership", label, "failed", f"{previous_day}: {str(exc)[:200]}"), False
    status, detail = membership_step_outcome(previous_day, result, "概念板块")
    folded = result.get("ok", 0) > 0 or (bool(result.get("fallback")) and result.get("status") == "ok")
    return _step("membership", label, status, detail, result), folded


def membership_step_outcome(day: date, result: Dict[str, Any], label: str) -> Tuple[str, str]:
    """Step status + detail for a membership refresh result (shared by the daily step and the weekly job)."""
    state = result.get("status")
    reason = result.get("reason") or result.get("error") or ""
    if result.get("fallback"):  # per-board refresh failed, the opt-in all-boards query ran instead
        per_board = result.get("per_board") or {}
        if state == "ok":
            return "partial", (f"{day} {label}逐板块成分刷新失败（{per_board.get('error') or per_board.get('failed')}），"
                               f"已按 BOARD_MEMBERSHIP_FULL_DAY_FALLBACK 用全量成分补上：{result.get('rows')} 条，"
                               f"新增 {result.get('opened')}，移出 {result.get('closed')}")
        return "failed", f"{day} {label}逐板块成分刷新失败，全量成分兜底也未成功（{state}: {reason}），保留现有成分"
    if state == "skipped":
        return "skipped", f"{day} {label}{result.get('reason', '成分已是最新')}"
    if state == "no_boards":
        return "partial", f"{label}目录为空，无法折叠成分（先运行 scripts/backfill_market_snapshot.py）"
    if state == "error":
        return "failed", f"{day} {label}成分获取失败: {reason}"
    counts = (f"成功 {result.get('ok', 0)}，失败 {result.get('failed', 0)}，空返回 {result.get('empty', 0)}，"
              f"已是最新 {result.get('skipped', 0)}（共 {result.get('boards', 0)} 个）")
    effect = f"新增 {result.get('opened', 0)}，移出 {result.get('closed', 0)}"
    tail = ""
    if result.get("failed_boards"):
        tail += f"；失败/空返回的板块保留现有成分，如 {result['failed_boards'][:3]}"
    if result.get("aborted"):
        tail += f"；连续失败已中止，剩余 {result.get('remaining', 0)} 个未处理"
    detail = f"{day} {label}成分：{counts}；{effect}{tail}"
    if state == "ok":
        return "succeeded", detail
    return ("failed" if state == "failed" else "partial"), detail


def _ingest_day(day: date, ingest_service: Any) -> Dict[str, Any]:
    """Ingest D with derived fallbacks; D's boards (dc_index / dc_daily) come with the next run."""
    try:
        report = ingest_service.ingest_trade_date(day, allow_derived=True, boards=False)
        data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        status = _INGEST_STEP_STATUS.get(report.status, "failed")
        detail = f"个股 {report.stock_rows} 行"
        derived = sorted(set(getattr(report, "derived", []) or []))
        if derived:
            detail += f"；{derived} 尚未发布，已按上一交易日数据推算"
        if report.missing or report.incomplete:
            detail += f"；缺失 {sorted(set(report.missing))}，不完整 {sorted(set(report.incomplete))}"
        return _step("ingest", "行情入库", status, detail, data)
    except Exception as exc:  # noqa: BLE001 - recorded on the run
        logger.warning("[BoardPipeline] ingest failed: %s", exc, exc_info=True)
        return _step("ingest", "行情入库", "failed", str(exc)[:200])
