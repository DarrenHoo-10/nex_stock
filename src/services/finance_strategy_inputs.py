"""Model-independent cross-security strategy inputs, reusing the quantitative analyzer.

No process model/search credentials are read or consumed. The calling assistant
can reason across all supplied securities using its own full-capability model.
"""
import json
from dataclasses import asdict

from src.services.finance_identity import fail
from src.services.finance_market_evidence import evidence_for


def strategy_inputs(arguments, *, user_id):
    from api.internal_client import call
    from src.stock_analyzer import StockTrendAnalyzer
    import pandas as pd
    codes = arguments.get("stock_codes")
    days = arguments.get("days", 120)
    strategy_name = arguments.get("strategy")
    if set(arguments) - {"stock_codes", "days", "strategy"} or not isinstance(codes, list) or not codes:
        fail(422, "FINANCE_STRATEGY_INPUT_INVALID")
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        fail(422, "FINANCE_HISTORY_DAYS_INVALID")
    for code in codes:
        if (not isinstance(code, str) or not code or code in {".", ".."}
                or any(c in code for c in "/\\?#%") or any(c.isspace() or ord(c) < 32 for c in code)):
            fail(422, "FINANCE_SECURITY_INVALID")
    strategy = None
    if strategy_name:
        from src.services.screening.config import Config
        from src.services.screening.strategy import load_all_strategies_with_extra
        selected = load_all_strategies_with_extra(Config.from_env()).get(strategy_name)
        if selected is None:
            fail(404, "FINANCE_STRATEGY_NOT_FOUND")
        strategy = asdict(selected)
    results = []
    for code in dict.fromkeys(codes):
        path = f"/api/v1/stocks/{code}/history"
        status, _, text = call("GET", path, user_id=user_id, params={"days": days})
        data = json.loads(text)
        item = {"security": code, "status_code": status, "data": data, "evidence": evidence_for(path, data)}
        rows = data.get("data", []) if isinstance(data, dict) else []
        if status == 200 and len(rows) >= 20:
            frame = pd.DataFrame(rows).sort_values("date").drop_duplicates("date", keep="last")
            frame["pct_chg"] = frame.get("change_percent")
            item["technical"] = StockTrendAnalyzer().analyze(frame, code).to_dict()
            item["technicalAsOf"] = item["evidence"]["tradingDay"]
            item["technicalCoverage"] = {"dailyBars": len(frame), "incompleteWindows": [window for window in (20, 60, 120) if len(frame) < window]}
        else:
            item["technical"] = None
            item["limitation"] = "insufficient_daily_bars" if status == 200 else "source_unavailable"
        results.append(item)
    # No automatic pass/fail from incomplete data. Return actual strategy YAML
    # plus source evidence so the assistant can explain unknown fields explicitly.
    return {"version": "finance.v1", "strategy": strategy, "items": results,
            "modelUsage": "none", "selection": "assistant_applies_strategy_to_evidence",
            "limitations": ["Daily technical inputs cannot establish an intraday minute event."]}
