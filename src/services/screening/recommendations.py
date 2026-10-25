# -*- coding: utf-8 -*-
# Derived from AlphaSift revision 9f522747caafd3c0b1ddb7e14d5cf44c8580b6cf.
# Licensed under Apache-2.0 and modified for daily_stock_analysis.
"""Count distinct limit-up pool members by known theme; never infer from price changes."""

import re

import pandas as pd

from src.services.screening.normalize import normalize_code, safe_text


def aggregate_limit_up_topics(frame: pd.DataFrame, *, trade_date: str, mapping: dict) -> list[dict]:
    groups: dict[str, dict[str, dict]] = {}
    sources: dict[str, set[str]] = {}
    for row in frame.to_dict(orient="records"):
        code = normalize_code(row.get("代码", row.get("code")))
        if not re.fullmatch(r"\d{6}", code) or code == "000000":
            continue
        meta = mapping.get(code, {})
        industry = safe_text(row.get("所属行业", row.get("industry"))) or safe_text(meta.get("industry"))
        concepts = safe_text(meta.get("concepts"))
        labels = [(industry, "industry")] if industry else []
        labels.extend((name.strip(), "concept") for name in re.split(r"[,，;；、|]", concepts) if name.strip())
        for name, source in labels:
            stock = {"code": code, "name": safe_text(row.get("名称", row.get("name"))), "is_limit_up": True}
            groups.setdefault(name, {})[code] = stock
            sources.setdefault(name, set()).add(source)
    rows = []
    for name, stocks in groups.items():
        source = "concept" if "concept" in sources[name] else "industry"
        rows.append({
            "topic": name, "name": name, "source": source,
            "limit_up_count": len(stocks), "trade_date": trade_date,
            "count_coverage": "mapped_pool" if source == "concept" else "source_pool",
            "sample_stock_count": len(stocks), "leaders": [s["name"] for s in list(stocks.values())[:3]],
            "leader_stocks": list(stocks.values())[:10], "provider_used": "eastmoney_limit_up_pool",
        })
    rows.sort(key=lambda row: (-row["limit_up_count"], row["topic"]))
    for rank, row in enumerate(rows, 1):
        row["rank"] = rank
    return rows
