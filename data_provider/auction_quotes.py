"""Minute samples of the opening call auction from ashareapi.com."""
import math
import re

import requests

from data_provider.realtime_types import safe_float


def fetch_auction_quotes(code: str, day: str, is_today: bool, key: str) -> list[dict]:
    params = {"code": code, "mode": "stock" if is_today else "stock-history"}
    if not is_today:
        params["date"] = day
    response = requests.get(
        "https://api.ashareapi.com/v1/auction", params=params,
        headers={"Authorization": f"Bearer {key}"}, timeout=6, allow_redirects=False,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("ok") is not True:
        return []

    # Some feeds publish several updates a minute. Retain the last actual quote.
    latest = {}
    for row in payload.get("data") or []:
        stamp = str(row.get("time", ""))
        if not re.fullmatch(r"09:(?:1[5-9]|2[0-5])(?::[0-5][0-9])?", stamp):
            continue
        if len(stamp) == 5:
            stamp += ":00"
        price = safe_float(row.get("price"))
        if price is None or not math.isfinite(price) or price <= 0:
            continue
        minute = stamp[:5]
        if minute not in latest or stamp >= latest[minute]["time"]:
            latest[minute] = {"time": stamp, "price": price}
    return [latest[minute] for minute in sorted(latest)]
