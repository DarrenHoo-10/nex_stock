# -*- coding: utf-8 -*-
"""Multi-user P2a: portfolio data is private per user (offline, no network / LLM).

Boots the real app over a temp SQLite database; the acting user is switched per request.
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from api.deps import get_current_user
from api.v1.endpoints import portfolio as portfolio_endpoint
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.portfolio_service import PortfolioService
from src.storage import PortfolioAccount, PortfolioTrade
from tests.multi_user_helpers import (
    PRIMARY_ID,
    USER_A,
    USER_B,
    build_multi_user_app,
    give_user_model,
    stub_dns,
    teardown_multi_user_env,
)

BASE = "/api/v1/portfolio"
CSV = (
    "成交日期,证券代码,买卖标志,成交数量,成交均价,成交编号,手续费,印花税\n"
    "2026-01-02,600519,买入,10,100,HT-001,1,0\n"
).encode("utf-8")


@pytest.fixture
def app(tmp_path, monkeypatch):
    mu = build_multi_user_app(tmp_path, monkeypatch)
    stub_dns(monkeypatch)
    give_user_model(USER_B)  # position analysis runs on the requester's own model
    yield mu
    teardown_multi_user_env()


def _create_account(client, name="Main"):
    resp = client.post(f"{BASE}/accounts", json={"name": name, "broker": "Demo", "market": "cn", "base_currency": "CNY"})
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _seed_user_b(app):
    """User B owns an account with one trade, one cash entry and one corporate action."""
    client = app.as_user(USER_B)
    account_id = _create_account(client, "B account")
    trade = client.post(f"{BASE}/trades", json={
        "account_id": account_id, "symbol": "600519", "trade_date": "2026-01-02", "side": "buy",
        "quantity": 10, "price": 100, "trade_uid": "B-UID-1",
    })
    assert trade.status_code == 200, trade.text
    cash = client.post(f"{BASE}/cash-ledger", json={
        "account_id": account_id, "event_date": "2026-01-01", "direction": "in", "amount": 5000,
    })
    assert cash.status_code == 200, cash.text
    action = client.post(f"{BASE}/corporate-actions", json={
        "account_id": account_id, "symbol": "600519", "effective_date": "2026-01-03",
        "action_type": "cash_dividend", "cash_dividend_per_share": 1.0,
    })
    assert action.status_code == 200, action.text
    return {
        "account_id": account_id,
        "trade_id": trade.json()["id"],
        "cash_id": cash.json()["id"],
        "action_id": action.json()["id"],
    }


def test_every_portfolio_route_depends_on_current_user():
    routes = [r for r in portfolio_endpoint.router.routes if isinstance(r, APIRoute)]
    assert routes
    missing = []
    for route in routes:
        dependants = {d.call for d in route.dependant.dependencies}
        if get_current_user not in dependants:
            missing.append(f"{sorted(route.methods)} {route.path}")
    assert not missing, f"routes without get_current_user: {missing}"


def test_account_list_is_private_and_accounts_stamped_with_owner(app):
    a_id = _create_account(app.as_user(USER_A), "A account")
    b_id = _create_account(app.as_user(USER_B), "B account")
    p_id = _create_account(app.as_user(PRIMARY_ID), "Primary account")

    for user, own in ((USER_A, a_id), (USER_B, b_id), (PRIMARY_ID, p_id)):
        listed = app.as_user(user).get(f"{BASE}/accounts").json()["accounts"]
        assert [item["id"] for item in listed] == [own]

    with app.db.get_session() as session:
        owners = {row.id: row.user_id for row in session.query(PortfolioAccount).all()}
    assert owners == {a_id: USER_A, b_id: USER_B, p_id: PRIMARY_ID}


def test_user_cannot_update_or_delete_foreign_account(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)

    update = client.put(f"{BASE}/accounts/{seeded['account_id']}", json={"name": "hijacked"})
    delete = client.delete(f"{BASE}/accounts/{seeded['account_id']}")
    assert update.status_code == 404
    assert delete.status_code == 404
    assert update.json()["error"] == "not_found"

    still_there = app.as_user(USER_B).get(f"{BASE}/accounts").json()["accounts"]
    assert [(item["id"], item["name"], item["is_active"]) for item in still_there] == [
        (seeded["account_id"], "B account", True)
    ]


def test_user_cannot_write_into_foreign_account(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)
    aid = seeded["account_id"]

    trade = client.post(f"{BASE}/trades", json={
        "account_id": aid, "symbol": "600519", "trade_date": "2026-01-05", "side": "buy",
        "quantity": 1, "price": 1,
    })
    cash = client.post(f"{BASE}/cash-ledger", json={
        "account_id": aid, "event_date": "2026-01-05", "direction": "in", "amount": 1,
    })
    action = client.post(f"{BASE}/corporate-actions", json={
        "account_id": aid, "symbol": "600519", "effective_date": "2026-01-05",
        "action_type": "cash_dividend", "cash_dividend_per_share": 1.0,
    })
    assert trade.status_code == cash.status_code == action.status_code == 404

    b_client = app.as_user(USER_B)
    assert b_client.get(f"{BASE}/trades").json()["total"] == 1
    assert b_client.get(f"{BASE}/cash-ledger").json()["total"] == 1
    assert b_client.get(f"{BASE}/corporate-actions").json()["total"] == 1


def test_event_lists_are_scoped_and_foreign_account_filter_is_404(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)
    own = _create_account(client, "A account")
    client.post(f"{BASE}/trades", json={
        "account_id": own, "symbol": "000001", "trade_date": "2026-01-02", "side": "buy",
        "quantity": 5, "price": 10,
    })

    for path in ("trades", "cash-ledger", "corporate-actions"):
        unfiltered = client.get(f"{BASE}/{path}").json()
        assert all(item["account_id"] == own for item in unfiltered["items"]), path
        foreign = client.get(f"{BASE}/{path}", params={"account_id": seeded["account_id"]})
        assert foreign.status_code == 404, path

    own_trades = client.get(f"{BASE}/trades").json()
    assert own_trades["total"] == 1 and own_trades["items"][0]["symbol"] == "000001"


def test_user_cannot_delete_foreign_events(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)

    assert client.delete(f"{BASE}/trades/{seeded['trade_id']}").status_code == 404
    assert client.delete(f"{BASE}/cash-ledger/{seeded['cash_id']}").status_code == 404
    assert client.delete(f"{BASE}/corporate-actions/{seeded['action_id']}").status_code == 404

    b_client = app.as_user(USER_B)
    assert b_client.get(f"{BASE}/trades").json()["total"] == 1
    assert b_client.get(f"{BASE}/cash-ledger").json()["total"] == 1
    assert b_client.get(f"{BASE}/corporate-actions").json()["total"] == 1
    # the owner can still delete them
    assert b_client.delete(f"{BASE}/trades/{seeded['trade_id']}").status_code == 200


def test_snapshot_risk_fx_and_position_analysis_hide_foreign_account(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)
    aid = seeded["account_id"]

    assert client.get(f"{BASE}/snapshot", params={"account_id": aid, "include_realtime": False}).status_code == 404
    assert client.get(f"{BASE}/risk", params={"account_id": aid, "include_realtime": False}).status_code == 404
    assert client.post(f"{BASE}/fx/refresh", params={"account_id": aid}).status_code == 404
    analysis = client.post(f"{BASE}/positions/600519/analysis", json={"account_id": aid})
    assert analysis.status_code == 404

    # unfiltered snapshot of user A sees no account at all (B's holdings never leak in)
    snapshot = client.get(f"{BASE}/snapshot", params={"include_realtime": False}).json()
    assert snapshot["account_count"] == 0 and snapshot["accounts"] == []


def test_snapshot_cache_tables_stay_with_owner(app):
    seeded = _seed_user_b(app)
    snapshot = app.as_user(USER_B).get(
        f"{BASE}/snapshot", params={"include_realtime": False, "as_of": "2026-01-10"},
    ).json()
    assert snapshot["account_count"] == 1
    assert snapshot["accounts"][0]["account_id"] == seeded["account_id"]
    # A has none and still none after B populated positions/lots/snapshots
    other = app.as_user(USER_A).get(f"{BASE}/snapshot", params={"include_realtime": False}).json()
    assert other["accounts"] == []


def test_csv_import_commit_rejects_foreign_account_without_leaking_dedup(app):
    seeded = _seed_user_b(app)
    client = app.as_user(USER_A)
    files = {"file": ("trades.csv", CSV, "text/csv")}

    for dry_run in ("true", "false"):
        resp = client.post(
            f"{BASE}/imports/csv/commit",
            data={"account_id": str(seeded["account_id"]), "broker": "huatai", "dry_run": dry_run},
            files=files,
        )
        assert resp.status_code == 404, resp.text

    with app.db.get_session() as session:
        assert session.query(PortfolioTrade).count() == 1  # only B's seeded trade


def test_csv_import_commit_into_own_account_still_works(app):
    client = app.as_user(USER_A)
    own = _create_account(client, "A account")
    resp = client.post(
        f"{BASE}/imports/csv/commit",
        data={"account_id": str(own), "broker": "huatai", "dry_run": "false"},
        files={"file": ("trades.csv", CSV, "text/csv")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["inserted_count"] == 1


def test_trade_uid_is_unique_per_account_not_per_user_namespace(app):
    """B's trade_uid does not block A's identical uid in A's own account."""
    _seed_user_b(app)
    client = app.as_user(USER_A)
    own = _create_account(client, "A account")
    resp = client.post(f"{BASE}/trades", json={
        "account_id": own, "symbol": "600519", "trade_date": "2026-01-02", "side": "buy",
        "quantity": 10, "price": 100, "trade_uid": "B-UID-1",
    })
    assert resp.status_code == 200, resp.text


def test_primary_user_behaviour_unchanged_and_owns_legacy_rows(app):
    """Rows without an explicit owner belong to the primary user; the primary user sees only those."""
    repo = PortfolioRepository(app.db)
    with app.db.get_session() as session:
        session.add(PortfolioAccount(name="Legacy", market="cn", base_currency="CNY"))  # ORM default owner
        session.commit()
    listed = app.as_user(PRIMARY_ID).get(f"{BASE}/accounts").json()["accounts"]
    assert [item["name"] for item in listed] == ["Legacy"]
    assert app.as_user(USER_A).get(f"{BASE}/accounts").json()["accounts"] == []
    assert [row.name for row in repo.list_accounts(PRIMARY_ID)] == ["Legacy"]


def test_service_defaults_to_primary_user_and_repo_requires_owner(app):
    service = PortfolioService(repo=PortfolioRepository(app.db))
    assert service.user_id == PRIMARY_ID
    created = service.create_account(name="CLI account", broker=None, market="cn", base_currency="CNY")
    other = PortfolioService(repo=PortfolioRepository(app.db), user_id=USER_A)
    assert other.list_accounts() == []
    assert [item["id"] for item in service.list_accounts()] == [created["id"]]
    # a service bound to another user cannot resolve the foreign account id
    assert other.update_account(created["id"], name="x") is None
    assert other.deactivate_account(created["id"]) is False
    with pytest.raises(ValueError):
        other.record_cash_ledger(account_id=created["id"], event_date=__import__("datetime").date(2026, 1, 1),
                                 direction="in", amount=1)


def _hold(client, quantity=10):
    account_id = _create_account(client, "Held")
    trade = client.post(f"{BASE}/trades", json={
        "account_id": account_id, "symbol": "600519", "trade_date": "2026-01-02", "side": "buy",
        "quantity": quantity, "price": 100,
    })
    assert trade.status_code == 200, trade.text
    return account_id


@pytest.mark.parametrize(("user_id", "notify"), [(USER_B, True), (PRIMARY_ID, True)])
def test_position_analysis_notifies_the_requester_only(app, user_id, notify):
    # P3a: the request asks for a push for everyone; a non-primary requester's goes to their own channels (the
    # pipeline's notifier), the primary user's to the .env channels - tests/test_multi_user_p3a_pushes.py
    from types import SimpleNamespace
    from unittest.mock import patch

    client = app.as_user(user_id)
    account_id = _hold(client)
    queue = MagicMock()
    queue.submit_tasks_batch.return_value = (
        [SimpleNamespace(task_id="t-1", trace_id="tr-1", stock_code="600519", analysis_phase="postmarket")], [],
    )

    with patch(
        "src.services.portfolio_service.PortfolioService._fetch_realtime_position_price", return_value=(None, None),
    ), patch("api.v1.endpoints.portfolio.get_task_queue", return_value=queue):
        resp = client.post(f"{BASE}/positions/600519/analysis", json={"account_id": account_id})

    assert resp.status_code == 202, resp.text
    kwargs = queue.submit_tasks_batch.call_args.kwargs
    assert kwargs["notify"] is notify
    # the context names its owner so the shared decision signal can drop a non-primary user's holding state
    assert kwargs["portfolio_context"]["owner_user_id"] == user_id
    assert kwargs["portfolio_context"]["quantity"] == 10.0
