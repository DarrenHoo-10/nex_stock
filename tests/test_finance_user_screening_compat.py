"""Screening compatibility, conservative readiness and process ownership."""
import importlib.util
import os

import pytest
from fastapi import HTTPException

from src.services import finance_user_screening as screening


def test_process_identity_is_stable_and_confirmed_dead_process_is_distinct():
    marker = screening._process_marker(os.getpid())
    assert marker and marker == screening._process_marker(os.getpid())
    assert screening._process_marker(2 ** 30 - 1) is None


def test_screening_input_cannot_supply_credentials_owner_or_cache_path():
    for key in ("user_id", "api_key", "cache_path", "model", "requester_user_id"):
        with pytest.raises(HTTPException) as error:
            screening._request({"strategy": "dual_low", key: "synthetic"})
        assert error.value.status_code == 422
    assert screening._request({"strategy": "dual_low"})["max_results"] == 20


@pytest.mark.skipif(importlib.util.find_spec("src.services.user_config_service") is not None,
                    reason="P2 has actual personal configurations, covered by real worker tests")
def test_baseline_user_center_never_falls_back_to_system_model(monkeypatch):
    monkeypatch.setattr(screening, "is_user_center_mode", lambda: True)
    before = dict(os.environ)
    with pytest.raises(HTTPException) as error:
        screening._user_config("synthetic-ordinary-user")
    assert error.value.status_code == 503
    assert dict(os.environ) == before
