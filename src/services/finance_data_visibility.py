"""Conservative data classification shared by API tools and derived report persistence.

This describes the returned data, never the permission to execute an operation.
Only known market-only GET views are public. Mixed, user-filtered, unknown and
malformed operations remain private even when the caller can read them.
"""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import unquote, urlsplit

_PUBLIC_GET_VIEWS = (
    re.compile(r"^/api/v1/stocks/[^/]+/(quote|history|intraday|events|capital|research|qa)$"),
    re.compile(r"^/api/v1/(stocks/search|screening/strategies|boards/anomalies|data/(overview|capabilities|macro))$"),
    re.compile(r"^/api/v1/data/indices/[^/]+/constituents$"),
    re.compile(r"^/api/v1/closing-analysis/public/[0-9]{4}-[0-9]{2}-[0-9]{2}$"),
)


def classify_api_data(method: Any, path: Any, *, response: Any = None) -> dict[str, str]:
    verb = str(method or "").strip().upper()
    raw = str(path or "").strip()
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return {"audience": "private", "method": verb, "route": "", "classification": "personal_or_unverified"}
    route = unquote(parsed.path).rstrip("/")
    if not route.startswith("/"):
        route = "/" + route
    if not route.startswith("/api/"):
        route = "/api/v1" + route
    public = (
        verb == "GET" and not parsed.scheme and not parsed.netloc
        and not any(part in {".", ".."} for part in route.split("/"))
        and not any(char in route for char in ("\\", "\x00", "%"))
        and any(pattern.fullmatch(route) for pattern in _PUBLIC_GET_VIEWS)
    )
    # A view can only narrow a route's classification. Caller/model arguments
    # cannot promote a private or unknown endpoint to a public view.
    if isinstance(response, dict):
        evidence = response.get("data_visibility") or response.get("visibility")
        if response.get("is_private") or evidence in ("private", "mixed"):
            public = False
        elif isinstance(evidence, dict) and evidence.get("audience") != "public":
            public = False
    return {"audience": "public" if public else "private", "method": verb, "route": route,
            "classification": "market_view" if public else "personal_or_unverified"}


def api_tool_call_is_private(entry: dict[str, Any]) -> bool:
    arguments = entry.get("arguments_summary", entry.get("arguments", entry.get("args")))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return True
    if not isinstance(arguments, dict):
        return True
    return classify_api_data(arguments.get("method"), arguments.get("path"),
                             response=entry.get("result"))["audience"] != "public"
