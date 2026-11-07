# -*- coding: utf-8 -*-
"""Board pipeline for one trade date D (docs/design/core-backend-design.md 2.9).

Designed to run at 17:00 on D, when ``daily_basic`` / ``limit_list_d`` / ``dc_*`` of D are not
published yet (they arrive 17:00-19:00 / 18:30-19:30 / 20:30):

    previous  ->  membership  ->  ingest  ->  metrics  ->  signals

* ``previous``: complete D-1 (the previous session): its real ``daily_basic`` / ``limit_list_d``
  replace the values derived the day before, and its ``dc_index`` / ``dc_daily`` are fetched.
* ``membership``: fold D-1's ``dc_member`` (all boards, one paged query) into the zipper table.
  D uses the latest membership, so new concepts appear one day late.
* ``ingest``: D's ``daily`` (required) / ``stk_limit`` / ``suspend_d``; turnover, float cap and
  limit facts are derived when the real interfaces are not there yet (recorded in
  ``market_daily.quality``).
* ``metrics``: recompute D-1's facts-only metrics when step 1 or 2 changed its inputs, then D.
* ``signals``: D only. Signals already written for D-1 are judgments tied to their run and are
  never rewritten.

and one ``daily_runs`` row carrying the per-step results and the threshold snapshot.
Every step is idempotent: re-running a date re-uses ingested data, writes the same metric
rows, and (with ``force``) adds a new run whose signals supersede the older run's for
readers (the older run is kept as history).

``board_pipeline_schedule`` registers this flow with the scheduler (``BOARD_PIPELINE_*``);
``scripts/run_board_pipeline.py`` runs it by hand.
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
) -> Dict[str, Any]:
    """Run previous -> membership -> ingest -> metrics -> signals for ``trade_date`` (default: today in CN).

    Returns ``{"status", "run_id", "trade_date", "steps", ...}``; ``status`` is one of
    ``succeeded`` / ``partial`` / ``failed`` / ``skipped``. Never raises for step failures:
    they are recorded on the run so the caller (a scheduler) can continue with the rest of the
    daily flow.
    """
    day = as_date(trade_date) if trade_date is not None else _china_today()
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

            # 2. membership of D-1 (all boards, one paged query) ----------------------------------
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
    """Fold D-1's full-day membership; returns (step, membership_changed)."""
    label = "板块成分"
    if previous_day is None:
        return _step("membership", label, "skipped", "无法确定上一交易日"), False
    try:
        result = ingest_service.refresh_membership_for_day(previous_day)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[BoardPipeline] membership refresh failed: %s", exc, exc_info=True)
        return _step("membership", label, "failed", f"{previous_day}: {str(exc)[:200]}"), False
    state = result.get("status")
    if state == "ok":
        detail = (f"{previous_day} 全量成分 {result.get('rows')} 条（{result.get('boards')} 个板块），"
                  f"新增 {result.get('opened')}，移出 {result.get('closed')}")
        return _step("membership", label, "succeeded", detail, result), True
    if state == "skipped":
        return _step("membership", label, "skipped", f"{previous_day} {result.get('reason', '无需刷新')}", result), False
    reason = result.get("reason") or result.get("error") or ""
    if state == "incomplete":
        detail = f"{previous_day} 全量成分不完整（{reason}），保留上一日成分"
    elif state == "empty":
        detail = f"{previous_day} 全量成分返回为空，保留上一日成分"
    elif state == "no_boards":
        detail = "板块目录为空，无法折叠成分（先运行 scripts/backfill_market_snapshot.py）"
    else:
        detail = f"{previous_day} 全量成分获取失败: {reason}"
    return _step("membership", label, "failed" if state == "error" else "partial", detail, result), False


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
