# -*- coding: utf-8 -*-
"""Next-day plan (次日计划) service behind ``/api/v1/plan/*`` (docs/daily-plan-api.md).

Data flow
---------
* **Account**: cash + holdings snapshot in SQLite (``plan_account`` / ``plan_holding``), replaced as a whole by
  ``set_holdings_snapshot``. Holding rows are enriched from the local ``market_stock_daily`` (latest close, name,
  board market, ST); T+1 ``sellable`` compares the buy date with the latest ingested trade date.
* **Universe**: holdings first, then the watchlist (``WatchlistService``: ``STOCK_LIST`` for the primary user,
  ``user_watchlist`` for everyone else). Only 6-digit A-share codes are planned; other
  watchlist entries are skipped and listed in the run's ``data_gaps``.
* **Plans**: a run (``trigger_run``, scheduled or manual) plans every stock and stores the ``StockPlan`` payloads
  (``plan_stock``). ``list_stock_plans`` / ``get_stock_plan`` read the newest stored plan of the trade date, so page
  loads never call the model. A stock without a usable stored plan (newly added to the watchlist, or its holding
  changed since the plan was made) is planned on demand by ``get_stock_plan`` (one model call, then stored); the
  list shows it as ``missing`` instead. Sizing rules (``POSITION_*``) are applied when the plan is generated; changing
  them takes effect at the next run (spec 6.4: generated plans are not rewritten).
* **Run** (spec 7.1): boards (read the latest board run) -> picks (read the latest completed screening task; no new
  screening is started) -> per-stock plans (worker pool, one failing stock does not stop the others) -> switch
  decisions -> Feishu push. One run at a time per user and process (409 otherwise); a run left ``running`` by a
  restarted process is reported as failed.
* **Users** (docs/design/multi-user.md): account, holdings, runs and stored plans belong to one user. The service is
  bound to that user at construction (``user_id``; ``None`` = the primary user, which is what the scheduled job, the
  CLI and any legacy caller act as) and passes it explicitly to every repository call. Market, board and picks
  context is shared and read-only.
* **Model vs rules**: the analysis model decides buy / wait and the price levels (validated against the next day's
  price limits); shares, stops and loss figures always come from the rules (``daily_plan_sizing``). Without a model
  or when it fails, a deterministic rule plan is stored and ``data_gaps`` says so.

Only local data is read (no vendor requests): the plan is based on the latest day in ``market_stock_daily``.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from src.config import get_primary_user_id
from src.llm.credentials_guard import is_user_view
from src.repositories.daily_plan_repo import DailyPlanRepository
from src.services.board_common import default_calendar_fn
from src.services.daily_plan_engine import (
    HORIZONS,
    SWING_WINDOW,
    AccountCtx,
    Bar,
    Candidate,
    HoldingInfo,
    LlmFn,
    PlanInput,
    PlanOutcome,
    build_plan,
    build_switch,
    compute_levels,
    failed_plan,
    horizon_levels,
)
from src.services.daily_plan_push import build_push_markdown, feishu_configured, send_feishu
from src.services.daily_plan_sizing import (
    PositionRules,
    classify_board,
    compute_sizing,
    round_price,
)
from src.services.watchlist_service import WatchlistService

logger = logging.getLogger(__name__)

_CN_TZ = ZoneInfo("Asia/Shanghai")
CODE_RE = re.compile(r"^\d{6}$")
MAX_HOLDINGS_INPUT = 50
HISTORY_SESSIONS = SWING_WINDOW + 30  # sessions of local history read per stock
MARKET_CLOSE_HOUR = 15  # after this China hour a trading day's data counts as expected
PICK_CANDIDATES = 3  # screening picks (outside the watchlist) evaluated as switch candidates
INTERRUPTED_ERROR = "服务重启，这次运行没有完成"
NO_MODEL_GAP = "没有配置分析模型，这份计划按规则生成"
REFINING_GAP = "这份计划先按规则生成，模型分析在后台进行，稍后刷新查看"
_REFINING: set = set()  # (user id, code, trade date) with a background model plan in flight
_REFINING_LOCK = threading.Lock()
STEP_DEFS = (
    ("boards", "板块异动"),
    ("picks", "机会选股"),
    ("plans", "自选和持仓的次日计划"),
    ("switch", "换股判断"),
    ("push", "飞书推送"),
)
_FLAT_ORDER = {"buy": 0, "wait": 1, "skip": 2, "suspended": 3}
_STRONG_STATES = ("first_launch", "strengthening", "divergence")
_BOARD_STATE_ORDER = {"first_launch": 0, "strengthening": 1, "divergence": 2, "abnormal_drop": 3, "ebbing": 4}

_STATE_LOCK = threading.Lock()
_ACTIVE: Dict[str, str] = {}  # user id -> run id of that user's run executing in this process


class DailyPlanValidationError(ValueError):
    """Invalid input (maps to HTTP 400)."""


class DailyPlanRunConflictError(RuntimeError):
    """A plan run of the same user is already in progress (maps to HTTP 409)."""


def _iso_utc(value: Optional[datetime]) -> Optional[str]:
    return f"{value.isoformat()}Z" if value is not None else None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class _Context:
    """Account snapshot and universe at one point in time."""

    snapshot: Dict[str, Any]
    holdings: Dict[str, HoldingInfo]
    held_order: List[str]
    watch_codes: List[str]
    skipped: List[str]
    account: AccountCtx
    rules: PositionRules
    latest_date: Optional[date]
    rows: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def universe(self) -> List[str]:
        return self.held_order + [c for c in self.watch_codes if c not in self.holdings]

    def fingerprint(self, code: str) -> str:
        holding = self.holdings.get(code)
        if holding is None:
            return "flat"
        return f"held:{holding.shares}:{holding.cost:.4f}:{holding.buy_date or ''}:{holding.horizon or ''}"


class DailyPlanService:
    def __init__(
        self,
        db_manager: Any = None,
        config: Any = None,
        *,
        llm_call: Optional[LlmFn] = None,
        spawn: Optional[Callable[[Callable[[], None]], None]] = None,
        pusher: Optional[Callable[[str], None]] = None,
        push_configured: Optional[Callable[[], bool]] = None,
        anomaly_service: Any = None,
        calendar_fn: Optional[Callable[[date, date], Optional[Sequence[date]]]] = default_calendar_fn,
        now_fn: Optional[Callable[[], datetime]] = None,
        workers: Optional[int] = None,
        on_demand_sync: Optional[bool] = None,
        user_id: Optional[str] = None,
    ) -> None:
        # The user whose account / runs / plans this instance reads and writes; None = the primary user.
        self.user_id = user_id or get_primary_user_id()
        self._db = db_manager
        self._config = config
        self._llm_call = llm_call
        self._spawn = spawn or self._spawn_thread
        self._pusher = pusher or send_feishu
        self._push_configured = push_configured
        self._anomaly = anomaly_service
        self._calendar_fn = calendar_fn
        self._now_fn = now_fn or (lambda: datetime.now(_CN_TZ))
        self._workers = workers
        # Opening a stock without a stored plan: synchronous model call (tests, injected model) or a rule plan
        # right away with the model plan made in the background (default in production: a slow model must not
        # block the page or outlive a proxy's idle timeout).
        self._on_demand_sync = on_demand_sync
        self._repo: Optional[DailyPlanRepository] = None

    # ------------------------------------------------------------------ plumbing
    @property
    def config(self) -> Any:
        if self._config is not None:
            return self._config
        # Model, POSITION_* rules and report preferences are personal settings: a non-primary user's own view.
        if self.user_id != get_primary_user_id():
            from src.services.user_config_service import get_user_config

            return get_user_config(self.user_id)
        from src.config import get_config

        return get_config()

    @property
    def repo(self) -> DailyPlanRepository:
        if self._repo is None:
            self._repo = DailyPlanRepository(self._db)
        return self._repo

    @property
    def db(self) -> Any:
        return self.repo.db

    @staticmethod
    def _spawn_thread(target: Callable[[], None]) -> None:
        threading.Thread(target=target, name="daily-plan-run", daemon=True).start()

    def anomaly(self) -> Any:
        if self._anomaly is None:
            from src.services.board_anomaly_service import BoardAnomalyService

            self._anomaly = BoardAnomalyService()
        return self._anomaly

    def _model_label(self) -> str:
        return (getattr(self.config, "litellm_model", "") or "").strip()

    def _llm(self) -> Optional[LlmFn]:
        """The model call, or None when no analysis model is configured (rule plans only)."""
        if self._llm_call is not None:
            return self._llm_call
        if not self._model_label():
            return None
        config = self.config
        if is_user_view(config):
            # Rule plans only unless the user brought a model of their own; never the system's.
            from src.services.user_config_service import has_usable_model

            if not has_usable_model(config):
                return None

        def call(prompt: str, system_prompt: str) -> str:
            from src.analyzer import GeminiAnalyzer
            from src.config import get_primary_reasoning_effort

            return GeminiAnalyzer(config=config).generate_text_with_models(
                prompt,
                system_prompt=system_prompt,
                temperature=0.2,
                call_type="daily_plan",
                reasoning_effort=get_primary_reasoning_effort(config) or None,
            )

        return call

    # ------------------------------------------------------------------ dates
    def _is_trading_day(self, day: date) -> bool:
        if self._calendar_fn is not None:
            try:
                sessions = self._calendar_fn(day, day)
            except Exception as exc:  # noqa: BLE001 - calendar is best effort
                logger.debug("[DailyPlan] calendar lookup failed: %s", exc)
                sessions = None
            if sessions:
                return day in set(sessions)
        return day.weekday() < 5

    def _expected_trade_date(self) -> date:
        """The newest session whose close should already be in the data (today after 15:00 China, else before today)."""
        now = self._now_fn()
        day = now.date() if now.hour >= MARKET_CLOSE_HOUR else now.date() - timedelta(days=1)
        for _ in range(15):
            if self._is_trading_day(day):
                return day
            day -= timedelta(days=1)
        return day

    def next_trade_date(self, day: date) -> date:
        if self._calendar_fn is not None:
            try:
                sessions = self._calendar_fn(day + timedelta(days=1), day + timedelta(days=21))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[DailyPlan] calendar lookup failed: %s", exc)
                sessions = None
            later = sorted(s for s in (sessions or []) if s > day)
            if later:
                return later[0]
        nxt = day + timedelta(days=1)
        while nxt.weekday() >= 5:
            nxt += timedelta(days=1)
        return nxt

    def _resolve_trade_date(self, requested: Optional[date]) -> date:
        if requested is not None:
            return requested
        return self.repo.latest_ingested_date() or self._expected_trade_date()

    # ------------------------------------------------------------------ account
    def _rules(self) -> PositionRules:
        return PositionRules.from_config(self.config)

    def _watchlist(self) -> Tuple[List[str], List[str]]:
        """(6-digit A-share codes, skipped entries) of this user's watchlist."""
        tokens = WatchlistService(db_manager=self.db, config=self._config).list_codes(self.user_id)
        from data_provider.base import normalize_stock_code
        from src.services.stock_list_parser import ParseStatus, parse_analysis_target

        codes: List[str] = []
        skipped: List[str] = []
        for token in tokens:
            text = str(token).strip()
            if not text:
                continue
            try:
                if parse_analysis_target(text).asset_type == ParseStatus.INDEX:
                    skipped.append(text)
                    continue
            except Exception:  # noqa: BLE001 - parsing is only used to detect indices
                pass
            normalized = normalize_stock_code(text)
            if CODE_RE.match(normalized):
                if normalized not in codes:
                    codes.append(normalized)
            else:
                skipped.append(text)
        return codes, skipped

    def _load_context(self) -> _Context:
        snapshot = self.repo.get_account(self.user_id)
        latest = self.repo.latest_ingested_date()
        watch, skipped = self._watchlist()
        held_codes = [h["code"] for h in snapshot["holdings"]]
        rows = self.repo.latest_rows(list(dict.fromkeys(held_codes + watch)))
        holdings: Dict[str, HoldingInfo] = {}
        for item in snapshot["holdings"]:
            row = rows.get(item["code"]) or {}
            close = row.get("close")
            buy_date = item["buy_date"]
            sellable, note = True, None
            if buy_date is None:
                note = "没有填买入日期，请确认是否满足 T+1"
            elif latest is not None and buy_date >= latest:
                sellable = False
                note = f"T+1：{buy_date.isoformat()} 买入，今天不能卖出，下一个交易日起可卖"
            holdings[item["code"]] = HoldingInfo(
                code=item["code"], shares=item["shares"], cost=item["cost"], buy_date=buy_date,
                horizon=item["horizon"], sellable=sellable, sellable_note=note, name=row.get("name") or "",
                close=close, market_value=round_price(item["shares"] * (close if close else item["cost"])),
            )
        market_value = round_price(sum(h.market_value for h in holdings.values()))
        account = AccountCtx(cash=snapshot["cash"], market_value=market_value, held_count=len(holdings))
        return _Context(
            snapshot=snapshot, holdings=holdings, held_order=held_codes, watch_codes=watch, skipped=skipped,
            account=account, rules=self._rules(), latest_date=latest, rows=rows,
        )

    def get_account(self) -> Dict[str, Any]:
        ctx = self._load_context()
        holdings = []
        for code in ctx.held_order:
            h = ctx.holdings[code]
            row = ctx.rows.get(code) or {}
            pnl = (h.close - h.cost) * h.shares if h.close else 0.0
            holdings.append({
                "code": code, "shares": h.shares, "cost": h.cost,
                "buy_date": h.buy_date.isoformat() if h.buy_date else None, "horizon": h.horizon,
                "name": h.name, "board_market": classify_board(code, row.get("exchange_board")),
                "is_st": bool(row.get("is_st")), "close": h.close, "change_pct": row.get("pct_chg"),
                "market_value": h.market_value, "pnl": round_price(pnl),
                "pnl_pct": round((h.close / h.cost - 1) * 100, 2) if h.close and h.cost else 0.0,
                "sellable": h.sellable, "sellable_note": h.sellable_note,
            })
        total = ctx.account.total_capital
        return {
            "cash": ctx.account.cash, "market_value": ctx.account.market_value, "total_capital": round_price(total),
            "position_pct": round(ctx.account.market_value / total * 100, 2) if total > 0 else 0.0,
            "holdings": holdings, "rules": ctx.rules.to_dict(), "updated_at": ctx.snapshot["updated_at"],
        }

    def set_holdings_snapshot(self, cash: Any, holdings: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
        """Replace cash and holdings; raises ``DailyPlanValidationError`` on bad input."""
        try:
            cash_value = float(cash)
        except (TypeError, ValueError):
            raise DailyPlanValidationError("现金必须是数字")
        if cash_value < 0:
            raise DailyPlanValidationError("现金不能为负")
        if len(holdings) > MAX_HOLDINGS_INPUT:
            raise DailyPlanValidationError(f"最多录入 {MAX_HOLDINGS_INPUT} 只持仓")
        clean: List[Dict[str, Any]] = []
        seen: set = set()
        for item in holdings:
            code = str(item.get("code") or "").strip()
            if not CODE_RE.match(code):
                raise DailyPlanValidationError(f"股票代码 {code or '（空）'} 不是 6 位数字")
            if code in seen:
                raise DailyPlanValidationError(f"持仓里 {code} 重复，同一只股票请合并成一条")
            seen.add(code)
            try:
                shares = int(item.get("shares"))
                cost = float(item.get("cost"))
            except (TypeError, ValueError):
                raise DailyPlanValidationError(f"{code} 的股数和成本必须是数字")
            if shares <= 0 or cost <= 0:
                raise DailyPlanValidationError(f"{code} 的股数和成本必须大于 0")
            raw_date = item.get("buy_date")
            buy_date: Optional[date] = None
            if isinstance(raw_date, date):
                buy_date = raw_date
            elif raw_date not in (None, ""):
                try:
                    buy_date = date.fromisoformat(str(raw_date).strip())
                except ValueError:
                    raise DailyPlanValidationError(f"{code} 的买入日期必须是 YYYY-MM-DD")
            horizon = item.get("horizon") or None
            if horizon not in (None, *HORIZONS):
                raise DailyPlanValidationError(f"{code} 的计划周期必须是 short 或 swing")
            clean.append({"code": code, "shares": shares, "cost": cost, "buy_date": buy_date, "horizon": horizon})
        self.repo.replace_account(self.user_id, cash_value, clean)
        return self.get_account()

    # ------------------------------------------------------------------ plan inputs
    def _board_context(self, codes: Sequence[str], trade_date: date) -> Dict[str, Any]:
        """Boards (with today's signal) of each stock from the latest completed board run, plus the strongest boards."""
        result: Dict[str, Any] = {"by_stock": {}, "top": [], "run": None, "gaps": []}
        try:
            repo = self.anomaly().repo
            run = repo.latest_completed_run(trade_date) or repo.latest_completed_run(None)
            if run is None:
                result["gaps"].append("还没有完成的板块运行，计划里没有板块信号")
                return result
            result["run"] = run
            if run["trade_date"] != trade_date:
                result["gaps"].append(
                    f"板块判定是 {run['trade_date'].isoformat()} 的结果，不是 {trade_date.isoformat()} 的"
                )
            listing = self.anomaly().list_anomalies(run["trade_date"], None, None)
            rows = {row["id"]: row for row in listing.get("boards") or []
                    if row.get("state") not in ("none", "insufficient_history")}
            result["top"] = list(rows.values())[:10]
            refs_by_stock = self.repo.boards_of_stocks(list(codes), run["trade_date"])
            for code, refs in refs_by_stock.items():
                boards = [rows[ref] for ref in refs if ref in rows]
                boards.sort(key=lambda r: (_BOARD_STATE_ORDER.get(r["state"], 9),
                                           -(r["excess_pct"] if r.get("excess_pct") is not None else -1e9)))
                result["by_stock"][code] = boards[:3]
        except Exception as exc:  # noqa: BLE001 - boards are context, never a reason to fail the plans
            logger.warning("[DailyPlan] 读取板块信号失败: %s", exc, exc_info=True)
            result["gaps"].append(f"板块信号读取失败（{type(exc).__name__}），计划里没有板块信号")
        return result

    def _latest_picks(self) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
        """(task, picks) of the newest completed screening task; picks are ``{code, name, rank}``."""
        try:
            tasks, _total = self.db.list_screening_tasks(limit=30)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[DailyPlan] 读取选股任务失败: %s", exc)
            return None, []
        for task in tasks:
            if task.get("status") != "completed" or not task.get("run_id"):
                continue
            run = self.db.get_screening_run(task["run_id"])
            result = (run or {}).get("result") or {}
            picks = []
            for index, candidate in enumerate(result.get("candidates") or [], start=1):
                code = str(candidate.get("code") or "").strip()
                code = code.split(".")[0][-6:] if code else ""
                if CODE_RE.match(code):
                    picks.append({"code": code, "name": str(candidate.get("name") or ""),
                                  "rank": candidate.get("rank") or index})
            return task, picks
        return None, []

    def _build_inputs(
        self, codes: Sequence[str], trade_date: date, ctx: _Context, boards: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, PlanInput]:
        """Everything the (thread-safe, I/O free) plan builder needs, loaded in one pass."""
        next_date = self.next_trade_date(trade_date)
        history = self.repo.stock_history(codes, trade_date, HISTORY_SESSIONS)
        starts = [rows[0]["trade_date"] for rows in history.values() if rows]
        ohlc = self.repo.ohlc_bars(
            codes, min(starts) if starts else trade_date - timedelta(days=120), trade_date,
        ) if codes else {}
        inputs: Dict[str, PlanInput] = {}
        for code in codes:
            rows = history.get(code) or []
            day_ohlc = ohlc.get(code, {})
            bars: List[Bar] = []
            for row in rows:
                if row.get("close") is None:
                    continue
                extra = day_ohlc.get(row["trade_date"]) or {}
                bars.append(Bar(
                    date=row["trade_date"], close=float(row["close"]), pct_chg=row.get("pct_chg"),
                    amount=row.get("amount"), turnover=row.get("turnover_rate"), open=extra.get("open"),
                    high=extra.get("high"), low=extra.get("low"),
                ))
            gaps: List[str] = []
            if not bars and day_ohlc:  # not in the market snapshot: fall back to the stock_daily bars
                for day in sorted(day_ohlc)[-HISTORY_SESSIONS:]:
                    item = day_ohlc[day]
                    if item.get("close") is not None:
                        bars.append(Bar(date=day, close=float(item["close"]), pct_chg=item.get("pct_chg"),
                                        amount=item.get("amount"), open=item.get("open"), high=item.get("high"),
                                        low=item.get("low")))
                if bars:
                    gaps.append("这只股票不在全市场快照里，行情取自日线库，名称、停牌和涨跌停状态未知")
            last = rows[-1] if rows else {}
            holding = ctx.holdings.get(code)
            name = last.get("name") or (holding.name if holding else "") or ""
            inputs[code] = PlanInput(
                code=code, name=name, board_market=classify_board(code, last.get("exchange_board")),
                is_st=bool(last.get("is_st")),
                suspended=bool(last and last["trade_date"] == trade_date and last.get("is_suspended")),
                bars=bars, rules=ctx.rules, account=ctx.account, trade_date=trade_date, next_trade_date=next_date,
                limit_state=last.get("limit_state") if last and last["trade_date"] == trade_date else None,
                boards=list(boards.get(code, [])), holding=holding, gaps=gaps,
            )
        return inputs

    def _plan_one(self, inp: PlanInput, llm: Optional[LlmFn], label: str) -> PlanOutcome:
        try:
            outcome = build_plan(inp, llm, label)
        except Exception as exc:  # noqa: BLE001 - one stock failing must not stop the others
            logger.error("[DailyPlan] %s 计划生成失败: %s", inp.code, exc, exc_info=True)
            outcome = failed_plan(inp, f"计划生成出错：{type(exc).__name__}: {str(exc)[:120]}")
        # the payload is stored as JSON: normalise now so what is returned equals what is read back later
        outcome.payload = json.loads(json.dumps(outcome.payload, ensure_ascii=False, default=str))
        return outcome

    def _plan_many(self, inputs: Sequence[PlanInput], llm: Optional[LlmFn], label: str) -> List[PlanOutcome]:
        workers = self._workers if self._workers is not None else int(getattr(self.config, "max_workers", 3) or 1)
        workers = max(1, min(workers, len(inputs) or 1))
        if workers == 1:
            return [self._plan_one(inp, llm, label) for inp in inputs]
        results: Dict[int, PlanOutcome] = {}
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="daily-plan") as pool:
            futures = {pool.submit(self._plan_one, inp, llm, label): index for index, inp in enumerate(inputs)}
            for future in as_completed(futures):
                results[futures[future]] = future.result()
        return [results[i] for i in range(len(inputs))]

    # ------------------------------------------------------------------ switch
    def _candidates(
        self, ctx: _Context, trade_date: date, stored: Dict[str, Dict[str, Any]], picks: Sequence[Dict[str, Any]],
    ) -> List[Candidate]:
        out: List[Candidate] = []
        for index, code in enumerate(c for c in ctx.watch_codes if c not in ctx.holdings):
            row = stored.get(code)
            if row and row["status"] == "ready" and row["fingerprint"] == "flat":
                out.append(Candidate(
                    code=code, name=row["payload"].get("name") or code, payload=row["payload"],
                    raw_verdicts=row["meta"].get("raw_verdicts") or {}, source="watchlist", order=index,
                    board_strength=int(row["meta"].get("board_strength") or 0),
                ))
        extra = [p for p in picks if p["code"] not in ctx.holdings and p["code"] not in ctx.watch_codes][:PICK_CANDIDATES]
        if extra:
            codes = [p["code"] for p in extra]
            try:
                inputs = self._build_inputs(codes, trade_date, ctx, self._board_context(codes, trade_date)["by_stock"])
                for order, pick in enumerate(extra):
                    outcome = build_plan(inputs[pick["code"]], None, "")  # rule plan: no extra model calls
                    if outcome.payload["status"] == "ready":
                        out.append(Candidate(
                            code=pick["code"], name=outcome.payload.get("name") or pick["name"] or pick["code"],
                            payload=outcome.payload, raw_verdicts=outcome.meta.get("raw_verdicts") or {},
                            source="picks", order=order,
                            board_strength=int(outcome.meta.get("board_strength") or 0),
                        ))
            except Exception as exc:  # noqa: BLE001 - candidates are optional
                logger.warning("[DailyPlan] 选股候选评估失败: %s", exc, exc_info=True)
        return out

    def _switch_for(self, payload: Dict[str, Any], ctx: _Context, candidates: Sequence[Candidate]) -> Optional[Dict[str, Any]]:
        holding = ctx.holdings.get(payload["code"])
        if holding is None or ctx.account.held_count < ctx.rules.max_holdings:
            return None
        if payload.get("status") != "ready" or payload.get("verdict") == "suspended":
            return None
        close = payload.get("close") or holding.close or holding.cost
        return build_switch(
            payload, holding, close, candidates, list(ctx.holdings), ctx.account, ctx.rules,
            other_market_value=max(0.0, ctx.account.market_value - holding.market_value),
        )

    # ------------------------------------------------------------------ reading plans
    @staticmethod
    def _normalize_code(code: str) -> Optional[str]:
        text = str(code or "").strip()
        return text if CODE_RE.match(text) else None

    def _summary_from(self, code: str, ctx: _Context, stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        row = ctx.rows.get(code) or {}
        holding = ctx.holdings.get(code)
        base = {
            "code": code, "name": row.get("name") or (holding.name if holding else "") or "",
            "board_market": classify_board(code, row.get("exchange_board")), "is_st": bool(row.get("is_st")),
            "held": holding is not None, "close": row.get("close"), "change_pct": row.get("pct_chg"),
        }
        if stored is not None and stored["fingerprint"] == ctx.fingerprint(code):
            payload = stored["payload"]
            return {
                **base, "name": payload.get("name") or base["name"], "close": payload.get("close", base["close"]),
                "change_pct": payload.get("change_pct", base["change_pct"]),
                "board_market": payload.get("board_market") or base["board_market"],
                "is_st": bool(payload.get("is_st", base["is_st"])),
                "verdict": payload.get("verdict"), "headline": payload.get("headline") or "",
                "status": payload.get("status") or stored["status"], "failure_reason": payload.get("failure_reason"),
            }
        headline = ("持仓有变动，这份计划已过期，打开详情重新生成" if stored is not None
                    else "还没有这天的计划，打开详情会现算一份，或等下一次运行")
        return {**base, "verdict": None, "headline": headline, "status": "missing", "failure_reason": None}

    def list_stock_plans(self, trade_date: Optional[date] = None) -> Dict[str, Any]:
        day = self._resolve_trade_date(trade_date)
        ctx = self._load_context()
        codes = ctx.universe
        stored = self.repo.latest_stock_plans(self.user_id, codes, day)
        items = [self._summary_from(code, ctx, stored.get(code)) for code in codes]
        held = [item for item in items if item["held"]]
        others = [item for item in items if not item["held"]]
        rank = {code: index for index, code in enumerate(codes)}

        def sort_key(item: Dict[str, Any]) -> Tuple[int, int]:
            position = 4 if item["status"] != "ready" else _FLAT_ORDER.get(item["verdict"] or "", 3)
            return position, rank[item["code"]]

        others.sort(key=sort_key)
        return {"trade_date": day.isoformat(), "next_trade_date": self.next_trade_date(day).isoformat(),
                "items": held + others}

    def get_stock_plan(self, code: str, trade_date: Optional[date] = None) -> Optional[Dict[str, Any]]:
        """The stored plan of ``code`` (newest of the trade date); planned on demand when none is usable.

        Returns None for a code that is neither held, on the watchlist, nor has a stored plan.
        """
        code = self._normalize_code(code)
        if code is None:
            return None
        day = self._resolve_trade_date(trade_date)
        ctx = self._load_context()
        stored = self.repo.latest_stock_plans(self.user_id, [code], day).get(code)
        if stored is not None and stored["fingerprint"] == ctx.fingerprint(code):
            return stored["payload"]
        if code not in ctx.universe:
            return stored["payload"] if stored is not None else None
        llm = self._llm()
        sync = self._on_demand_sync if self._on_demand_sync is not None else self._llm_call is not None
        if llm is None or sync:
            return self._plan_on_demand(code, day, ctx, llm, self._model_label() if llm else "")
        # rule plan now (instant), model plan in the background; the next load returns the stored model plan
        payload = self._plan_on_demand(code, day, ctx, None, "", replace_gap=REFINING_GAP)
        key = (self.user_id, code, day.isoformat())
        with _REFINING_LOCK:
            start = key not in _REFINING
            if start:
                _REFINING.add(key)
        if start:
            def refine() -> None:
                try:
                    self._plan_on_demand(code, day, self._load_context(), llm, self._model_label())
                except Exception as exc:  # noqa: BLE001 - the rule plan stays in place
                    logger.warning("[DailyPlan] %s 后台模型计划失败: %s", code, exc, exc_info=True)
                finally:
                    with _REFINING_LOCK:
                        _REFINING.discard(key)

            threading.Thread(target=refine, name=f"daily-plan-{code}", daemon=True).start()
        return payload

    def _plan_on_demand(
        self, code: str, day: date, ctx: "_Context", llm: Optional[LlmFn], label: str, *, replace_gap: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Plan one stock outside a run (one model call at most), store it and return the payload."""
        board = self._board_context([code], day)
        inputs = self._build_inputs([code], day, ctx, board["by_stock"])
        inp = inputs[code]
        inp.gaps.extend(board["gaps"])
        outcome = self._plan_one(inp, llm, label)
        payload = outcome.payload
        if replace_gap:
            gaps = [gap for gap in payload.get("data_gaps") or [] if gap != NO_MODEL_GAP]
            payload["data_gaps"] = [replace_gap, *gaps]
        if payload["status"] == "ready" and ctx.holdings.get(code) is not None:
            _task, picks = self._latest_picks()
            stored_all = self.repo.latest_stock_plans(self.user_id, ctx.watch_codes, day)
            payload["switch"] = self._switch_for(payload, ctx, self._candidates(ctx, day, stored_all, picks))
        self.repo.save_stock_plan(
            self.user_id, None, day, code, payload["status"], ctx.fingerprint(code), payload, outcome.meta)
        return payload

    def preview_sizing(self, code: str, price: float, horizon: str) -> Dict[str, Any]:
        """Shares for a hypothetical buy at ``price`` under the current rules and account."""
        code = self._normalize_code(code)
        if code is None:
            raise DailyPlanValidationError("股票代码必须是 6 位数字")
        if horizon not in HORIZONS:
            raise DailyPlanValidationError("周期必须是 short 或 swing")
        try:
            price_value = float(price)
        except (TypeError, ValueError):
            raise DailyPlanValidationError("价格必须是数字")
        if price_value <= 0:
            raise DailyPlanValidationError("价格必须大于 0")
        ctx = self._load_context()
        day = self._resolve_trade_date(None)
        row = ctx.rows.get(code) or self.repo.latest_rows([code]).get(code) or {}
        tech_support = None
        close = row.get("close")
        try:
            inp = self._build_inputs([code], day, ctx, {})[code]
            if len(inp.bars) >= 5:
                tech_support = horizon_levels(horizon, compute_levels(inp.bars))["support"]
        except Exception as exc:  # noqa: BLE001 - the support is only a hint
            logger.debug("[DailyPlan] support for sizing preview unavailable: %s", exc)
        holding = ctx.holdings.get(code)
        return compute_sizing(
            price=price_value, board_market=classify_board(code, row.get("exchange_board")),
            is_st=bool(row.get("is_st")), rules=ctx.rules, cash=ctx.account.cash,
            market_value=ctx.account.market_value, held_shares=holding.shares if holding else 0,
            held_count=ctx.account.held_count, tech_support=tech_support, close=close,
        )

    # ------------------------------------------------------------------ runs
    def _run_payload(self, row: Dict[str, Any]) -> Dict[str, Any]:
        finished, started = row["finished_at"], row["started_at"]
        duration = round((finished - started).total_seconds(), 1) if finished and started else None
        push = dict(row["push"] or {})
        push.setdefault("channel", "feishu")
        push.setdefault("status", "pending")
        push.setdefault("sent_at", None)
        push.setdefault("error", None)
        push.setdefault("configured", False)
        return {
            "id": row["run_id"], "trade_date": row["trade_date"].isoformat(), "status": row["status"],
            "trigger": row["trigger"], "started_at": _iso_utc(started), "finished_at": _iso_utc(finished),
            "duration_sec": duration, "steps": row["steps"], "stock_total": row["stock_total"],
            "stock_succeeded": row["stock_succeeded"], "stock_failed": row["stock_failed"],
            "failures": row["failures"], "push": push, "push_preview_markdown": row["push_preview"],
            "model_calls": row["model_calls"], "data_gaps": row["data_gaps"],
        }

    def _reconcile_interrupted(self) -> None:
        with _STATE_LOCK:
            active = _ACTIVE.get(self.user_id)
        try:
            self.repo.fail_interrupted(self.user_id, active, INTERRUPTED_ERROR)
        except Exception as exc:  # noqa: BLE001
            logger.debug("[DailyPlan] 清理中断的运行失败: %s", exc)

    def get_latest_run(self) -> Optional[Dict[str, Any]]:
        self._reconcile_interrupted()
        row = self.repo.latest_run(self.user_id)
        return self._run_payload(row) if row else None

    def get_run(self, run_id: str) -> Optional[Dict[str, Any]]:
        self._reconcile_interrupted()
        row = self.repo.get_run(self.user_id, str(run_id))
        return self._run_payload(row) if row else None

    def trigger_run(self, trigger: str = "manual", *, wait: bool = False) -> Dict[str, Any]:
        """Start a run (background thread; ``wait=True`` runs it inline, used by the scheduler job).

        Raises ``DailyPlanRunConflictError`` when a run of this user is already in progress (other users' runs
        do not block it).
        """
        user_id = self.user_id
        with _STATE_LOCK:
            if _ACTIVE.get(user_id):
                raise DailyPlanRunConflictError("已有次日计划运行在进行中")
            self.repo.fail_interrupted(user_id, None, INTERRUPTED_ERROR)
            trade_date = self._resolve_trade_date(None)
            run_id = f"plan-{trade_date:%Y%m%d}-{uuid.uuid4().hex[:8]}"
            configured = self._is_push_configured()
            steps = [{"key": k, "label": label, "status": "pending", "finished_at": None, "detail": None}
                     for k, label in STEP_DEFS]
            self.repo.create_run(user_id, run_id, trade_date, trigger, steps,
                                 {"channel": "feishu", "status": "pending", "sent_at": None, "error": None,
                                  "configured": configured})
            _ACTIVE[user_id] = run_id

        def work() -> None:
            try:
                self._execute_run(run_id, trade_date)
            except Exception as exc:  # noqa: BLE001 - recorded on the run, never raised into the thread
                logger.error("[DailyPlan] 运行 %s 异常: %s", run_id, exc, exc_info=True)
                try:
                    self.repo.update_run(user_id, run_id, status="failed",
                                         error=f"{type(exc).__name__}: {str(exc)[:200]}",
                                         finished_at=datetime.now(timezone.utc).replace(tzinfo=None))
                except Exception:  # noqa: BLE001
                    pass
            finally:
                with _STATE_LOCK:
                    if _ACTIVE.get(user_id) == run_id:
                        _ACTIVE.pop(user_id, None)

        try:
            if wait:
                work()
            else:
                self._spawn(work)
        except Exception:
            with _STATE_LOCK:
                if _ACTIVE.get(user_id) == run_id:
                    _ACTIVE.pop(user_id, None)
            raise
        return self._run_payload(self.repo.get_run(user_id, run_id))

    def _is_primary_user(self) -> bool:
        return self.user_id == get_primary_user_id()

    def _is_push_configured(self) -> bool:
        # The Feishu channel is the server's (primary user's) own; a non-primary user's plan (their holdings) must
        # never go through it. Personal channels arrive in P3, until then push is "not configured" for them.
        if not self._is_primary_user():
            return False
        if self._push_configured is not None:
            return bool(self._push_configured())
        return feishu_configured(self.config)

    def _execute_run(self, run_id: str, trade_date: date) -> None:
        user_id = self.user_id
        started = time.monotonic()
        steps = [{"key": k, "label": label, "status": "pending", "finished_at": None, "detail": None}
                 for k, label in STEP_DEFS]
        by_key = {s["key"]: s for s in steps}

        def mark(key: str, status: str, detail: Optional[str] = None) -> None:
            step = by_key[key]
            step["status"] = status
            step["detail"] = detail
            step["finished_at"] = _iso_utc(datetime.now(timezone.utc).replace(tzinfo=None)) if status not in (
                "running", "pending") else None
            self.repo.update_run(user_id, run_id, steps=steps)

        next_date = self.next_trade_date(trade_date)
        ctx = self._load_context()
        codes = ctx.universe
        run_gaps: List[str] = []
        if ctx.skipped:
            run_gaps.append(f"自选里的 {len(ctx.skipped)} 个非 A 股标的（{'、'.join(ctx.skipped[:5])}"
                            f"{'…' if len(ctx.skipped) > 5 else ''}）不在次日计划范围，已跳过")
        expected = self._expected_trade_date()
        if trade_date < expected:
            run_gaps.append(
                f"本地最新行情是 {trade_date.isoformat()}，早于应有的交易日 {expected.isoformat()}（当天数据还没入库，"
                f"17:00 板块流程之后才有），这份计划基于较早的数据")

        # --- boards -------------------------------------------------------------------------------
        mark("boards", "running")
        board = self._board_context(codes, trade_date)
        run_gaps.extend(board["gaps"])
        if board["run"] is None:
            mark("boards", "partial", "没有完成的板块运行，计划里没有板块信号")
        elif board["gaps"]:
            mark("boards", "partial", board["gaps"][0])
        else:
            mark("boards", "succeeded", f"读取 {board['run']['trade_date'].isoformat()} 的板块判定，"
                                        f"{len(board['top'])} 个板块有异动信号")

        # --- picks --------------------------------------------------------------------------------
        mark("picks", "running")
        task, picks = self._latest_picks()
        if task is None:
            mark("picks", "skipped", "还没有完成的选股任务（运行不会自己发起选股）")
        else:
            finished = (task.get("finished_at") or "")[:10]
            note = f"读取最近完成的选股任务（{task.get('strategy')}，{finished}），{len(picks)} 只候选"
            if finished and finished < trade_date.isoformat():
                run_gaps.append(f"最近一次选股完成于 {finished}，早于交易日 {trade_date.isoformat()}，换股候选可能偏旧")
            mark("picks", "succeeded", note)

        # --- plans --------------------------------------------------------------------------------
        mark("plans", "running")
        failures: List[Dict[str, str]] = []
        model_calls = fallbacks = succeeded = 0
        plans: Dict[str, Tuple[Dict[str, Any], Dict[str, Any], int]] = {}
        llm = self._llm()
        label = self._model_label()
        gap_counter: Dict[str, int] = {}
        if not codes:
            mark("plans", "skipped", "没有自选股和持仓，没有可分析的股票")
        else:
            inputs = self._build_inputs(codes, trade_date, ctx, board["by_stock"])
            outcomes = self._plan_many([inputs[c] for c in codes], llm, label)
            for code, outcome in zip(codes, outcomes):
                payload = outcome.payload
                if outcome.model_called:
                    model_calls += 1
                    fallbacks += 0 if outcome.model_used else 1
                row_id = self.repo.save_stock_plan(
                    user_id, run_id, trade_date, code, payload["status"], ctx.fingerprint(code), payload, outcome.meta)
                plans[code] = (payload, outcome.meta, row_id)
                if payload["status"] == "ready":
                    succeeded += 1
                    for gap in payload.get("data_gaps") or []:
                        gap_counter[gap] = gap_counter.get(gap, 0) + 1
                else:
                    failures.append({"code": code, "name": payload.get("name") or "",
                                     "reason": payload.get("failure_reason") or "计划生成失败"})
            failed = len(failures)
            self.repo.update_run(user_id, run_id, stock_total=len(codes), stock_succeeded=succeeded,
                                 stock_failed=failed, failures=failures)
            detail = f"{len(codes)} 只，成功 {succeeded}，失败 {failed}"
            if llm is None:
                detail += "；没有配置分析模型，全部按规则生成"
            elif fallbacks:
                detail += f"；{fallbacks} 只模型没有给出结论，按规则生成"
            mark("plans", "failed" if succeeded == 0 else ("partial" if failed else "succeeded"), detail)
        for gap, count in gap_counter.items():
            run_gaps.append(f"{gap}（{count} 只）" if count > 1 or len(codes) > 1 else gap)

        # --- switch -------------------------------------------------------------------------------
        mark("switch", "running")
        held_ready = [c for c in ctx.held_order if c in plans and plans[c][0]["status"] == "ready"]
        if not held_ready:
            mark("switch", "skipped", "没有可判断的持仓")
        elif ctx.account.held_count < ctx.rules.max_holdings:
            mark("switch", "skipped", f"持仓 {ctx.account.held_count} 只，未达到最多持股数 {ctx.rules.max_holdings}，不涉及换股")
        else:
            try:
                stored = {c: {"status": p[0]["status"], "fingerprint": ctx.fingerprint(c), "payload": p[0],
                              "meta": p[1]} for c, p in plans.items()}
                candidates = self._candidates(ctx, trade_date, stored, picks)
                keep = 0
                for code in held_ready:
                    payload, _meta, row_id = plans[code]
                    switch = self._switch_for(payload, ctx, candidates)
                    if switch is not None:
                        payload["switch"] = switch
                        self.repo.update_stock_plan_payload(user_id, row_id, payload)
                        keep += 1 if switch["decision"] == "keep" else 0
                mark("switch", "succeeded", f"{len(held_ready)} 只持仓，候选 {len(candidates)} 只，"
                                            f"{keep} 只结论为不换")
            except Exception as exc:  # noqa: BLE001
                logger.error("[DailyPlan] 换股判断失败: %s", exc, exc_info=True)
                run_gaps.append("换股判断没有完成")
                mark("switch", "failed", f"{type(exc).__name__}: {str(exc)[:100]}")

        # --- push ---------------------------------------------------------------------------------
        mark("push", "running")
        configured = self._is_push_configured()
        model_calls_payload = [{
            "step": "plans", "calls": model_calls, "model": label or "未配置（按规则）",
            "note": ("没有配置分析模型，全部按规则生成" if llm is None
                     else (f"{fallbacks} 只没有得到模型结论，按规则生成" if fallbacks else None)),
        }]
        duration = round(time.monotonic() - started, 1)
        holdings_payloads = [plans[c][0] for c in ctx.held_order if c in plans and plans[c][0]["status"] == "ready"]
        watch_payloads = [plans[c][0] for c in ctx.universe if c not in ctx.holdings and c in plans
                          and plans[c][0]["status"] == "ready"]
        markdown = build_push_markdown(
            trade_date=trade_date.isoformat(), next_trade_date=next_date.isoformat(), holdings=holdings_payloads,
            watchlist=watch_payloads, failures=failures, boards=board["top"], data_gaps=run_gaps,
            duration_sec=duration, stock_total=len(codes), stock_failed=len(failures),
        )
        push: Dict[str, Any] = {"channel": "feishu", "status": "skipped", "sent_at": None, "error": None,
                                "configured": configured}
        if not codes:
            mark("push", "skipped", "没有内容可推送")
        elif not configured:
            mark("push", "skipped",
                 "没有配置飞书推送，计划只在网页里查看" if self._is_primary_user()
                 else "个人推送渠道尚未开放，计划只在网页里查看")
        else:
            try:
                self._pusher(markdown)
                push.update(status="sent", sent_at=_iso_utc(datetime.now(timezone.utc).replace(tzinfo=None)))
                mark("push", "succeeded", "已发送飞书摘要")
            except Exception as exc:  # noqa: BLE001 - a push failure never fails the run
                logger.warning("[DailyPlan] 飞书推送失败: %s", exc)
                push.update(status="failed", error=str(exc)[:200] or type(exc).__name__)
                mark("push", "failed", push["error"])

        # --- finish -------------------------------------------------------------------------------
        if not codes:
            status = "skipped"
        elif succeeded == 0:
            status = "failed"
        elif failures or any(by_key[k]["status"] in ("partial", "failed") for k in ("boards", "picks", "plans", "switch")):
            status = "partial"
        else:
            status = "succeeded"
        self.repo.update_run(
            user_id, run_id, status=status, steps=steps, push=push, push_preview=markdown, model_calls=model_calls_payload,
            data_gaps=list(dict.fromkeys(run_gaps)),
            finished_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        logger.info("[DailyPlan] 运行 %s 完成：%s（%s 只，失败 %s，推送 %s）", run_id, status, len(codes),
                    len(failures), push["status"])
