"""Cache East Money's latest broker reports and enrich them with locally stored Tushare forecasts."""
from datetime import date, timedelta
import json
import logging
from threading import Lock
import unicodedata

from sqlalchemy.dialects.sqlite import insert

from src.services.on_demand_fetch import allow_on_demand_fetch
from src.services.sources import SourceError, eastmoney_research
from src.storage import ResearchIndexCache, utc_naive_now

logger = logging.getLogger(__name__)
# Serialize refreshes for the same stock without allocating a permanent lock for every requested code.
_locks = [Lock() for _ in range(32)]
LOOKBACK = timedelta(days=1095)


def _update_cache(repo, code: str, today: date, user_id):
    now = utc_naive_now()
    with repo.db.get_session() as session:
        entry = session.get(ResearchIndexCache, code)
        stored = json.loads(entry.reports) if entry else []
        synced = entry.synced_at if entry else None
        checked = entry.checked_at if entry else None

    if checked:
        failed = synced != checked
        ttl = timedelta(minutes=10) if failed else timedelta(hours=24)
        if now - checked < ttl:
            return stored, "unavailable" if failed else "ok", synced
    if not allow_on_demand_fetch(user_id):
        return stored, "rate_limited", synced

    since = today - LOOKBACK
    if synced:
        since = max(since, synced.date() - timedelta(days=1))
    try:
        fresh = eastmoney_research.fetch(code, since, today)
    except SourceError as exc:
        logger.warning("研报索引 %s: %s", code, exc)
        values = {"checked_at": now}
        status = "unavailable"
    else:
        merged = {r["payload"]["info_code"]: r for r in [*stored, *fresh]}
        stored = sorted(merged.values(), key=lambda r: (r["event_date"], r["payload"]["info_code"]),
                        reverse=True)[:eastmoney_research.LIMIT]
        synced = now
        values = {"checked_at": now, "synced_at": now, "reports": json.dumps(stored, ensure_ascii=False)}
        status = "ok"
    with repo.db.get_session() as session:
        stmt = insert(ResearchIndexCache).values(code=code, **values)
        session.execute(stmt.on_conflict_do_update(index_elements=["code"], set_=values,
                                                  where=ResearchIndexCache.checked_at <= now))
        session.commit()
    return stored, status, synced


def _normalized(value):
    return "".join(unicodedata.normalize("NFKC", str(value or "")).split()).casefold()


def _key(event):
    payload = event["payload"]
    return event["event_date"], _normalized(payload.get("org")), _normalized(payload.get("title"))


def library_for(repo, code: str, today: date, user_id=None):
    with _locks[int(code) % len(_locks)]:
        originals, status, synced = _update_cache(repo, code, today, user_id)
    since = today - LOOKBACK
    originals = [row for row in originals if since.isoformat() <= row["event_date"] <= today.isoformat()]
    # Tushare already ingests these forecasts; opening the index must not trigger its whole-stock backfill.
    local = repo.list_for_code(code, kinds=("research",), since=since, until=today)
    by_key = {}
    for row in local:
        by_key.setdefault(_key(row), row)
    reports = []
    matched = set()
    for original in originals:
        key = _key(original)
        forecast = by_key.get(key)
        if forecast:
            payload = {**original["payload"], **forecast["payload"]}
            payload["rating"] = forecast["payload"].get("rating") or original["payload"].get("rating")
            original = {**original, "payload": payload}
            matched.add(key)
        reports.append(original)
    reports.extend(row for key, row in by_key.items() if key not in matched)
    reports.sort(key=lambda r: r["event_date"], reverse=True)
    return reports[:eastmoney_research.LIMIT], {"status": status, "synced_at": synced and synced.isoformat() + "Z"}
