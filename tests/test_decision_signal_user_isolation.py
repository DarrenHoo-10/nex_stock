# -*- coding: utf-8 -*-
"""Multi-user P2a: decision signals are shared, so only the administrator may mutate them (offline).

Reads stay open to every user; the four writes (create, reassess, feedback, status) change signals other
users rely on, and reassess calls the system model, so they are admin-only (``require_admin``).
"""

from __future__ import annotations

import sys
from unittest.mock import MagicMock

import pytest

try:
    import litellm  # noqa: F401
except ModuleNotFoundError:
    sys.modules["litellm"] = MagicMock()

from tests.multi_user_helpers import PRIMARY_ID, USER_A, build_multi_user_app, teardown_multi_user_env

BASE = "/api/v1/decision-signals"
SIGNAL = {
    "stock_code": "SH600519", "stock_name": "Moutai", "market": "cn", "source_type": "analysis",
    "trigger_source": "api", "action": "buy", "confidence": 0.7, "score": 80, "horizon": "3d",
    "entry_low": 1680, "stop_loss": 1600, "reason": "breakout",
}


@pytest.fixture
def app(tmp_path, monkeypatch):
    built = build_multi_user_app(tmp_path, monkeypatch)
    yield built
    teardown_multi_user_env()


def _create_as_admin(app) -> int:
    response = app.as_user(PRIMARY_ID).post(BASE, json=SIGNAL)
    assert response.status_code == 200, response.text
    return response.json()["item"]["id"]


def _writes(signal_id: int):
    return [
        ("POST", BASE, SIGNAL),
        ("POST", f"{BASE}/reassess", {"source_report_id": 1, "decision_profile": "balanced"}),
        ("PUT", f"{BASE}/{signal_id}/feedback", {"feedback_value": "useful"}),
        ("PATCH", f"{BASE}/{signal_id}/status", {"status": "invalidated"}),
    ]


def test_non_admin_gets_admin_required_on_every_shared_signal_write(app):
    signal_id = _create_as_admin(app)
    client = app.as_user(USER_A)

    for method, path, body in _writes(signal_id):
        response = client.request(method, path, json=body)
        assert response.status_code == 403, (method, path, response.text)
        assert response.json() == {"error": "ADMIN_REQUIRED", "message": "Administrator role required"}

    # nothing was written: still one signal, still active, no feedback
    listed = client.get(BASE).json()
    assert listed["total"] == 1 and listed["items"][0]["status"] == "active"
    assert client.get(f"{BASE}/{signal_id}/feedback").json()["feedback_value"] is None


def test_admin_writes_and_every_user_reads(app):
    signal_id = _create_as_admin(app)
    admin = app.as_user(PRIMARY_ID)

    assert admin.put(f"{BASE}/{signal_id}/feedback", json={"feedback_value": "useful"}).status_code == 200
    # the report does not exist: the request got past the admin guard and failed on its own merits
    assert admin.post(
        f"{BASE}/reassess", json={"source_report_id": 424242, "decision_profile": "balanced"},
    ).status_code == 404
    assert admin.patch(f"{BASE}/{signal_id}/status", json={"status": "invalidated"}).status_code == 200

    reader = app.as_user(USER_A)
    assert reader.get(f"{BASE}/{signal_id}").status_code == 200
    assert reader.get(f"{BASE}/{signal_id}/feedback").json()["feedback_value"] == "useful"
    assert reader.get(BASE).status_code == 200
