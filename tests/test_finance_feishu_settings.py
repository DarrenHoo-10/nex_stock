"""Browser ownership uses the real durable state and existing auth middleware."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api.middlewares import auth
from api.v1.endpoints import finance_assistant
from src.services import finance_channel_ownership as channel, finance_feishu_settings as settings
from src.services.finance_identity import FinanceIdentityStore


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    config = SimpleNamespace(database_path=str(tmp_path / "stock.db"), feishu_app_id="selected-app")
    values = {"FINANCE_FEISHU_MANAGED": "true", "FINANCE_FEISHU_APP_ID": "selected-app",
              "FINANCE_CORE_URL": "http://private-core.internal", "FINANCE_SERVICE_TOKEN": "private-token",
              "FINANCE_CORE_SETTINGS_URL": "https://nexbot.example.test/?settings=feishu"}
    monkeypatch.setattr(channel, "get_config", lambda: config)
    monkeypatch.setattr(settings, "get_config", lambda: config)
    monkeypatch.setattr(channel, "setting", lambda key: values.get(key, ""))
    monkeypatch.setattr(settings, "setting", lambda key: values.get(key, ""))
    store = FinanceIdentityStore(tmp_path / "ownership.db")
    monkeypatch.setattr(channel, "FinanceIdentityStore", lambda path: store)
    monkeypatch.setattr(auth, "is_user_center_mode", lambda: False)
    monkeypatch.setattr(auth, "is_auth_enabled", lambda: True)
    monkeypatch.setattr(auth, "verify_session", lambda cookie: cookie == "synthetic-login")
    app = FastAPI()
    app.include_router(finance_assistant.router, prefix="/api/v1/finance/assistant")
    auth.add_auth_middleware(app)
    return SimpleNamespace(client=TestClient(app), config=config, values=values)


def test_browser_read_requires_session_and_reports_real_cas_transitions(fixture):
    f = fixture
    path = "/api/v1/finance/assistant/feishu-settings"
    assert f.client.get(path, headers={"Authorization": "Bearer private-token"}).status_code == 401
    f.client.cookies.set(auth.COOKIE_NAME, "synthetic-login")
    local = f.client.get(path).json()
    assert local["owner"] == "stock" and local["appliesToCurrentApp"]
    for owner in ("paused", "core"):
        channel.transition(channel.channel_state()["version"], owner)
        response = f.client.get(path)
        assert response.status_code == 200
        assert response.json()["owner"] == owner
        assert response.json()["settingsUrl"] == "https://nexbot.example.test/?settings=feishu"
        assert "tenantId" not in response.json()
        assert "private-core" not in response.text and "private-token" not in response.text
        with pytest.raises(HTTPException):
            channel.require_stock_registration()
    f.config.feishu_app_id = "unrelated-app"
    other = f.client.get(path).json()
    assert other["currentAppId"] == "unrelated-app" and not other["appliesToCurrentApp"]
    channel.require_stock_registration()
    with pytest.raises(HTTPException):
        channel.require_stock_registration("selected-app")


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://private.internal/settings",
                                 "https://user:secret@nexbot.example.test/", "https://nexbot.example.test/?token=private",
                                 "https://nexbot.example.test/#token=private", "https://nexbot.example.test:bad/", ""])
def test_invalid_public_url_is_omitted_without_reusing_private_transport(fixture, url):
    fixture.values["FINANCE_CORE_SETTINGS_URL"] = url
    assert settings.settings_state()["settingsUrl"] is None


def test_unmanaged_local_deployment_keeps_original_controls(fixture):
    fixture.values["FINANCE_FEISHU_MANAGED"] = "false"
    result = settings.settings_state()
    assert result["managed"] is False and result["owner"] == "stock"
    assert result["appliesToCurrentApp"] is False
