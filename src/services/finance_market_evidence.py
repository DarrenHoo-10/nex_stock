"""Preserve market observation precision; retrieval clock is never price evidence."""
from datetime import date, datetime, timezone
import hashlib
from zoneinfo import ZoneInfo

MARKETS = {"cn": ("Asia/Shanghai", "CNY"), "hk": ("Asia/Hong_Kong", "HKD"),
           "us": ("America/New_York", "USD"), "jp": ("Asia/Tokyo", "JPY"),
           "kr": ("Asia/Seoul", "KRW"), "tw": ("Asia/Taipei", "TWD")}


def trading_day(value):
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        return None


def security_metadata(data):
    from data_provider.base import _market_tag, canonical_stock_code
    from src.services.stock_list_parser import parse_analysis_target
    code = str(data.get("stock_code", ""))
    market = data.get("market") or (_market_tag(code) if code else None)
    if code.upper().endswith((".US", ".N", ".O")):
        market = "us"
    zone, currency = MARKETS.get(market, (None, None))
    target = parse_analysis_target(code) if code else None
    return {"providerCode": code or None, "canonicalCode": canonical_stock_code(code) or None,
            "canonicalId": data.get("canonical_id") or (target.canonical_id if target else None),
            "assetType": data.get("asset_type") or (target.asset_type if target else None),
            "market": market, "exchange": data.get("exchange") or (target.exchange if target else None),
            "currency": data.get("currency") or currency, "timezone": zone}


def observation(value, zone, trading_day=None, *, five_minute=False):
    if not isinstance(value, str) or not zone:
        return None
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if "T" not in value and " " not in value:
            return None  # A date-only value has no intraday precision.
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo(zone))
        local = stamp.astimezone(ZoneInfo(zone))
        if trading_day and local.date().isoformat() != trading_day:
            return None
        if five_minute and (local.minute % 5 or local.second or local.microsecond):
            return None
        return local.isoformat()
    except (TypeError, ValueError):
        return None


def evidence_for(path, data):
    retrieved = datetime.now(timezone.utc).isoformat()
    result = {"retrievedAt": retrieved, "marketTimestamp": None, "source": None,
              "endpoint": path, "granularity": "unknown", "coverage": "unknown",
              "marketTimezone": None, "tradingDay": None, "minuteEvidence": False, "rows": []}
    if not isinstance(data, dict):
        return result
    source = data.get("source")
    result["source"] = source
    security = security_metadata(data)
    code = security["canonicalCode"]
    result["security"] = security
    result["marketTimezone"] = security["timezone"]
    if path == "/api/v1/data/macro":
        result.update(granularity="mixed_periods", marketTimezone="Asia/Shanghai", units=data.get("units", {}),
                      datasets=data.get("evidence", {}), coverage="provider_reported")
        valuation = data.get("index_valuation") or {}
        result["indices"] = [{"canonicalId": identity, "assetType": "index", "exchange": identity.rsplit(".", 1)[-1],
                              "granularity": "daily", "tradingDay": trading_day(valuation.get("trade_date")), "observedAt": None}
                             for identity in valuation.get("indices", {})]
    elif path.endswith("/constituents"):
        result.update(granularity="monthly_snapshot", marketTimezone="Asia/Shanghai", tradingDay=trading_day(data.get("trade_date")),
                      source=(data.get("evidence") or {}).get("source"), units={"weight": "percent"},
                      security={"canonicalId": data.get("index_code"), "assetType": "index"})
    elif path.endswith("/intraday"):
        day = data.get("trade_date")
        try:
            day = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError):
            day = None
        result.update(granularity=data.get("interval", "intraday"), tradingDay=day,
                      coverage="stale" if data.get("is_stale") else "provider_reported",
                      units={"price": "index_points" if security["assetType"] == "index" else security["currency"], "volume": "shares", "amount": security["currency"]})
        valid_times = []
        for row in data.get("data", []):
            stamp = observation(row.get("time"), security["timezone"], day, five_minute=True) if day else None
            if stamp:
                valid_times.append(stamp)
            result["rows"].append({"id": hashlib.sha256(f"{code}:{source}:{row.get('time')}".encode()).hexdigest(),
                                   "observedAt": stamp, "granularity": data.get("interval", "5min")})
        as_of = observation(data.get("as_of"), security["timezone"], day, five_minute=True) if day else None
        result["marketTimestamp"] = as_of if as_of in valid_times else None
        valid = bool(source and day and valid_times and result["marketTimestamp"] and data.get("interval", "5min") == "5min")
        result["minuteEvidence"] = valid and not data.get("is_stale")
        if not valid:
            result["coverage"] = "partial" if result["rows"] else "missing"
    elif path.endswith("/history"):
        days = [day for row in data.get("data", []) if (day := trading_day(row.get("date"))) is not None]
        result.update(granularity="daily_bar", tradingDay=max(days) if days else None,
                      coverage="provider_reported" if days else "missing",
                      units={"price": "index_points" if security["assetType"] == "index" else security["currency"], "amount": security["currency"],
                             "volume": "provider_standardized; unit not proven by daily response", "change_percent": "percent"})
        result["rows"] = [{"id": hashlib.sha256(f"{code}:{source}:{day}".encode()).hexdigest(),
                           "tradingDay": day, "observedAt": None, "granularity": "daily_bar"} for day in days]
    elif path.endswith("/quote"):
        # Legacy update_time is the server clock, not provider time.
        stamp = observation(data.get("provider_timestamp"), security["timezone"])
        result.update(granularity="snapshot", marketTimestamp=stamp,
                      coverage="stale" if data.get("is_stale") else "unknown" if not data.get("provider_timestamp") else "provider_reported",
                      units={"price": "index_points" if security["assetType"] == "index" else security["currency"], "amount": security["currency"], "volume": "shares", "change_percent": "percent"})
        if not stamp:
            result["coverage"] = "unknown"
    return result
