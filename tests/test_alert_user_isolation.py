# -*- coding: utf-8 -*-
"""Multi-user P2a: alert rules are personal data (API + worker), offline."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.routing import APIRoute

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_current_user
from api.v1.endpoints import alerts as alerts_endpoint
from src.notification import ChannelAttemptResult, NotificationDispatchResult
from src.services.alert_service import AlertService
from src.services.alert_worker import AlertWorker
from tests.multi_user_helpers import PRIMARY_ID, USER_A, USER_B, build_multi_user_app, teardown_multi_user_env

RULE = {
    "name": "Moutai breakout",
    "target_scope": "single_symbol",
    "target": "600519",
    "alert_type": "price_cross",
    "parameters": {"direction": "above", "price": 1800},
    "severity": "warning",
    "enabled": True,
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    yield built
    teardown_multi_user_env()


def _create(app, user_id, **overrides):
    resp = app.as_user(user_id).post("/api/v1/alerts/rules", json={**RULE, **overrides})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_every_alert_route_depends_on_current_user():
    for route in alerts_endpoint.router.routes:
        if not isinstance(route, APIRoute):
            continue
        calls = [dep.call for dep in route.dependant.dependencies]
        assert get_current_user in calls, f"{route.path} does not depend on get_current_user"


def test_rules_are_listed_only_for_their_owner(app):
    rule_a = _create(app, USER_A)
    rule_b = _create(app, USER_B, target="000001")

    listed_a = app.as_user(USER_A).get("/api/v1/alerts/rules").json()
    listed_b = app.as_user(USER_B).get("/api/v1/alerts/rules").json()
    listed_primary = app.as_user(PRIMARY_ID).get("/api/v1/alerts/rules").json()

    assert [item["id"] for item in listed_a["items"]] == [rule_a["id"]]
    assert [item["id"] for item in listed_b["items"]] == [rule_b["id"]]
    assert listed_primary["items"] == [] and listed_primary["total"] == 0


def test_same_named_rules_coexist_per_user(app):
    rule_a = _create(app, USER_A)
    rule_b = _create(app, USER_B)

    assert rule_a["id"] != rule_b["id"]
    assert rule_a["name"] == rule_b["name"]
    assert app.as_user(USER_A).get("/api/v1/alerts/rules").json()["total"] == 1
    assert app.as_user(USER_B).get("/api/v1/alerts/rules").json()["total"] == 1


@pytest.mark.parametrize(
    "method,path_suffix,body",
    [
        ("get", "", None),
        ("patch", "", {"name": "hijacked"}),
        ("delete", "", None),
        ("post", "/enable", None),
        ("post", "/disable", None),
        ("post", "/test", None),
    ],
)
def test_foreign_rule_id_is_404_with_the_not_found_body(app, method, path_suffix, body):
    rule_b = _create(app, USER_B)
    url = f"/api/v1/alerts/rules/{rule_b['id']}{path_suffix}"

    client = app.as_user(USER_A)
    resp = getattr(client, method)(url, **({"json": body} if body is not None else {}))
    missing = getattr(client, method)(
        f"/api/v1/alerts/rules/999999{path_suffix}", **({"json": body} if body is not None else {})
    )

    assert resp.status_code == 404
    assert resp.json()["error"] == "not_found"
    assert resp.json().keys() == missing.json().keys()
    # the owner's rule is untouched
    mine = app.as_user(USER_B).get(f"/api/v1/alerts/rules/{rule_b['id']}")
    assert mine.status_code == 200
    assert mine.json()["name"] == RULE["name"] and mine.json()["enabled"] is True


def test_trigger_notification_and_cooldown_history_follow_rule_ownership(app):
    rule_b = _create(app, USER_B)
    service_b = AlertService(user_id=USER_B)
    repo = service_b.repo
    from datetime import datetime, timedelta

    trigger = repo.create_trigger(USER_B, {
        "rule_id": rule_b["id"], "target": "600519", "status": "triggered", "reason": "hit",
    })
    repo.record_notification_attempt(USER_B, {"trigger_id": trigger.id, "channel": "custom", "success": True})
    now = datetime.now()
    repo.upsert_cooldown(
        USER_B, rule_id=rule_b["id"], rule_key="k", target="600519", severity="warning",
        last_triggered_at=now, cooldown_until=now + timedelta(hours=1),
    )

    owner = app.as_user(USER_B)
    assert owner.get("/api/v1/alerts/triggers").json()["total"] == 1
    assert owner.get("/api/v1/alerts/notifications").json()["total"] == 1
    assert owner.get(f"/api/v1/alerts/rules/{rule_b['id']}").json()["cooldown_active"] is True

    for other in (USER_A, PRIMARY_ID):
        client = app.as_user(other)
        assert client.get("/api/v1/alerts/triggers").json()["items"] == []
        assert client.get(f"/api/v1/alerts/triggers?rule_id={rule_b['id']}").json()["total"] == 0
        assert client.get("/api/v1/alerts/notifications").json()["items"] == []
        assert client.get(f"/api/v1/alerts/notifications?trigger_id={trigger.id}").json()["total"] == 0
        # repository level: another user cannot write history through B's rule either
        with pytest.raises(ValueError):
            repo.create_trigger(other, {"rule_id": rule_b["id"], "target": "600519", "status": "triggered"})
        with pytest.raises(ValueError):
            repo.record_notification_attempt(other, {"trigger_id": trigger.id, "channel": "custom"})
        with pytest.raises(ValueError):
            repo.upsert_cooldown(
                other, rule_id=rule_b["id"], rule_key="k", target="600519", severity="warning",
                last_triggered_at=now, cooldown_until=now,
            )
        assert repo.get_rule_cooldown_summary(other, rule_id=rule_b["id"], target="600519", severity="warning") is None


def test_deleting_a_rule_removes_its_history_so_a_reused_id_cannot_leak(app):
    rule_a = _create(app, USER_A)
    repo = AlertService(user_id=USER_A).repo
    repo.create_trigger(USER_A, {"rule_id": rule_a["id"], "target": "600519", "status": "triggered"})

    assert app.as_user(USER_A).delete(f"/api/v1/alerts/rules/{rule_a['id']}").status_code == 200

    assert app.as_user(USER_A).get("/api/v1/alerts/triggers").json()["total"] == 0
    assert repo.list_triggers(USER_A)[1] == 0


def test_rule_less_legacy_triggers_belong_to_the_primary_user(app):
    repo = AlertService().repo  # primary
    repo.create_trigger(PRIMARY_ID, {"rule_id": None, "target": "000001", "status": "triggered"})

    assert app.as_user(PRIMARY_ID).get("/api/v1/alerts/triggers").json()["total"] == 1
    assert app.as_user(USER_A).get("/api/v1/alerts/triggers").json()["total"] == 0
    with pytest.raises(ValueError):
        repo.create_trigger(USER_A, {"rule_id": None, "target": "000001", "status": "triggered"})


def test_rule_sources_only_reported_for_primary(app):
    assert app.as_user(USER_A).get("/api/v1/alerts/rules").json()["rule_sources"] == {
        "legacy_configured": 0, "legacy_effective": 0,
    }


# --- worker ---------------------------------------------------------------


def _notifier():
    notifier = MagicMock()
    notifier.send_with_results.return_value = NotificationDispatchResult(
        dispatched=True, success=True, status="sent",
        channel_results=[ChannelAttemptResult(channel="custom", success=True)],
    )
    return notifier


async def _quote(_monitor, _stock_code):
    return SimpleNamespace(price=1810.0)


def _run_worker(notifier):
    config = SimpleNamespace(
        agent_event_monitor_enabled=True, agent_event_alert_rules_json="", trading_day_check_enabled=False,
    )
    worker = AlertWorker(config_provider=lambda: config, service=AlertService(), notifier=notifier)
    with patch("src.agent.events.EventMonitor._get_realtime_quote", new=_quote):
        return worker.run_once()


def test_worker_records_other_users_trigger_but_never_notifies_primary_channels(app):
    rule_b = _create(app, USER_B)
    notifier = _notifier()

    stats = _run_worker(notifier)

    assert stats["loaded"] == 1 and stats["triggered"] == 1 and stats["recorded"] == 1
    assert stats["notified"] == 0
    notifier.send_with_results.assert_not_called()
    triggers = app.as_user(USER_B).get("/api/v1/alerts/triggers").json()["items"]
    assert [item["rule_id"] for item in triggers] == [rule_b["id"]]
    notifications = app.as_user(USER_B).get("/api/v1/alerts/notifications").json()["items"]
    assert [(item["channel"], item["success"]) for item in notifications] == [("__no_channel__", False)]
    # primary user sees nothing of B's activity
    assert app.as_user(PRIMARY_ID).get("/api/v1/alerts/triggers").json()["items"] == []
    assert app.as_user(PRIMARY_ID).get("/api/v1/alerts/notifications").json()["items"] == []

    # every cycle records a new trigger (no data timestamp to dedupe on) but still never notifies
    _run_worker(notifier)
    assert app.as_user(USER_B).get("/api/v1/alerts/triggers").json()["total"] == 2
    channels = {item["channel"] for item in app.as_user(USER_B).get("/api/v1/alerts/notifications").json()["items"]}
    assert channels == {"__no_channel__"}
    notifier.send_with_results.assert_not_called()


def test_worker_still_notifies_primary_rules_and_users_do_not_suppress_each_other(app):
    rule_primary = _create(app, PRIMARY_ID)
    rule_b = _create(app, USER_B)  # identical semantic rule
    notifier = _notifier()

    stats = _run_worker(notifier)

    assert stats["loaded"] == 2 and stats["triggered"] == 2 and stats["recorded"] == 2
    assert stats["notified"] == 1
    assert notifier.send_with_results.call_count == 1
    primary_triggers = app.as_user(PRIMARY_ID).get("/api/v1/alerts/triggers").json()["items"]
    assert [item["rule_id"] for item in primary_triggers] == [rule_primary["id"]]
    primary_notifications = app.as_user(PRIMARY_ID).get("/api/v1/alerts/notifications").json()["items"]
    assert [(item["channel"], item["success"]) for item in primary_notifications] == [("custom", True)]
    b_triggers = app.as_user(USER_B).get("/api/v1/alerts/triggers").json()["items"]
    assert [item["rule_id"] for item in b_triggers] == [rule_b["id"]]
    # only the primary rule got a cooldown
    assert app.as_user(PRIMARY_ID).get(f"/api/v1/alerts/rules/{rule_primary['id']}").json()["cooldown_active"] is True
    assert app.as_user(USER_B).get(f"/api/v1/alerts/rules/{rule_b['id']}").json()["cooldown_active"] is False


def test_watchlist_scoped_rule_expands_the_owners_watchlist(app):
    from src.services.watchlist_service import WatchlistService

    WatchlistService().add(USER_A, "000001")
    WatchlistService().add(USER_B, "300750")
    rule_a = _create(
        app, USER_A, target_scope="watchlist", target="default", name="watch A",
    )
    row = AlertService(user_id=USER_A).repo.get_rule(USER_A, rule_a["id"])

    payloads = AlertService(user_id=USER_A).build_runtime_payloads(row)

    assert [(p.effective_target, getattr(p.rule, "message", None)) for p in payloads] == [("000001", None)]


# ---------------------------------------------------------------------------- per-user enabled-rule limit
def _set_limit(monkeypatch, value):
    from src.config import get_config

    monkeypatch.setattr(get_config(), "alert_max_enabled_rules_per_user", value)


def _target(index):
    return f"60000{index}"


def _post(app, user_id, index, **overrides):
    return app.as_user(user_id).post(
        "/api/v1/alerts/rules", json={**RULE, "name": f"rule {index}", "target": _target(index), **overrides},
    )


def test_enabled_rule_limit_is_enforced_per_user_on_create_enable_and_update(app, monkeypatch):
    _set_limit(monkeypatch, 2)
    first = _post(app, USER_A, 1)
    assert first.status_code == 200 and _post(app, USER_A, 2).status_code == 200

    blocked = _post(app, USER_A, 3)
    assert blocked.status_code == 400 and blocked.json()["error"] == "rule_limit_exceeded"
    # another user is counted separately; so is the primary user
    assert _post(app, USER_B, 3).status_code == 200
    assert _post(app, PRIMARY_ID, 4).status_code == 200
    # a disabled rule does not count, and creating one at the limit is fine
    disabled = _post(app, USER_A, 5, enabled=False)
    assert disabled.status_code == 200 and disabled.json()["enabled"] is False

    # enabling it (endpoint or PATCH) is refused while A is at the limit ...
    client = app.as_user(USER_A)
    rule_id = disabled.json()["id"]
    refused = client.post(f"/api/v1/alerts/rules/{rule_id}/enable")
    assert refused.status_code == 400 and refused.json()["error"] == "rule_limit_exceeded"
    assert client.patch(f"/api/v1/alerts/rules/{rule_id}", json={"enabled": True}).status_code == 400
    assert client.get(f"/api/v1/alerts/rules/{rule_id}").json()["enabled"] is False
    # ... while editing an already enabled rule at the limit still works
    assert client.patch(f"/api/v1/alerts/rules/{first.json()['id']}", json={"name": "renamed"}).status_code == 200
    # and freeing a slot lets the enable through
    assert client.post(f"/api/v1/alerts/rules/{first.json()['id']}/disable").status_code == 200
    assert client.post(f"/api/v1/alerts/rules/{rule_id}/enable").status_code == 200


def test_worker_selection_is_bounded_per_user_so_one_user_cannot_drop_anothers_rules(app, monkeypatch):
    # A has one old rule; B then creates many newer ones (global recency would have pushed A out).
    rule_a = _create(app, USER_A, target=_target(0))
    for index in range(1, 7):
        _create(app, USER_B, target=_target(index), name=f"b{index}")
    _set_limit(monkeypatch, 3)

    selected = AlertService().repo.list_all_enabled_rules(per_user_limit=3)

    by_user = {}
    for row in selected:
        by_user.setdefault(row.user_id, []).append(row.id)
    assert by_user[USER_A] == [rule_a["id"]]
    assert len(by_user[USER_B]) == 3  # B is cut to its own newest three
    assert len(selected) == 4

    runtime = AlertWorker(config_provider=lambda: SimpleNamespace(agent_event_alert_rules_json=""), service=AlertService())
    loaded = runtime._load_runtime_rules(SimpleNamespace(agent_event_alert_rules_json=""))
    assert sorted(rule.user_id for rule in loaded) == [USER_A, USER_B, USER_B, USER_B]


def test_worker_expansion_cap_applies_per_user_not_globally(app, monkeypatch):
    for index in range(4):
        _create(app, USER_B, target=_target(index), name=f"b{index}")
    rule_a = _create(app, USER_A, target=_target(9))  # newest rule overall

    with patch("src.services.alert_worker.ALERT_WORKER_RULE_LIMIT", 2):
        loaded = AlertWorker(service=AlertService())._load_runtime_rules(
            SimpleNamespace(agent_event_alert_rules_json=""),
        )

    owners = [rule.user_id for rule in loaded]
    assert owners.count(USER_B) == 2 and owners.count(USER_A) == 1
    assert [rule.rule.metadata["persisted_rule_id"] for rule in loaded if rule.user_id == USER_A] == [rule_a["id"]]


def test_deleting_a_rule_removes_all_its_history_rows(app):
    from datetime import datetime, timedelta

    from sqlalchemy import func, select

    from src.storage import AlertCooldownRecord, AlertNotificationRecord, AlertTriggerRecord

    rule_a = _create(app, USER_A)
    repo = AlertService(user_id=USER_A).repo
    trigger = repo.create_trigger(USER_A, {"rule_id": rule_a["id"], "target": "600519", "status": "triggered"})
    repo.record_notification_attempt(USER_A, {"trigger_id": trigger.id, "channel": "custom", "success": True})
    now = datetime.now()
    repo.upsert_cooldown(
        USER_A, rule_id=rule_a["id"], rule_key="k", target="600519", severity="warning",
        last_triggered_at=now, cooldown_until=now + timedelta(hours=1),
    )

    assert app.as_user(USER_A).delete(f"/api/v1/alerts/rules/{rule_a['id']}").status_code == 200

    with app.db.get_session() as session:
        for model in (AlertTriggerRecord, AlertNotificationRecord, AlertCooldownRecord):
            assert session.execute(select(func.count()).select_from(model)).scalar() == 0
