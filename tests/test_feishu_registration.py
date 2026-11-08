# -*- coding: utf-8 -*-
"""Scan-to-create Feishu bot: HTTP boundary is mocked, session logic and config saving are real."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

from src.config import Config
from src.core.config_manager import ConfigManager
from src.services import feishu_registration_service as reg
from src.services.system_config_service import SystemConfigService


def _resp(body, status=200):
    mock = MagicMock()
    mock.status_code = status
    mock.json.return_value = body
    return mock


INIT = {"nonce": "n", "supported_auth_methods": ["client_secret"]}
BEGIN = {
    "device_code": "dc",
    "verification_uri_complete": "https://open.feishu.cn/page/launcher?user_code=AB",
    "interval": 5,
    "expires_in": 3600,
}


class RegistrationSessionTests(unittest.TestCase):
    def setUp(self):
        self.svc = reg.FeishuRegistrationService()

    def _start(self):
        with patch.object(reg.requests, "post", side_effect=[_resp(INIT), _resp(BEGIN)]) as post:
            out = self.svc.start()
        return out, post

    def test_start_returns_url_and_hides_device_code(self):
        out, post = self._start()
        self.assertEqual(out["verification_url"], BEGIN["verification_uri_complete"])
        self.assertNotIn("device_code", out)
        self.assertEqual(post.call_args_list[1].kwargs["data"]["archetype"], "PersonalAgent")

    def test_start_rejects_unsupported_endpoint(self):
        with patch.object(reg.requests, "post", return_value=_resp({"supported_auth_methods": []})):
            with self.assertRaises(reg.RegistrationError):
                self.svc.start()

    def test_poll_pending_then_success(self):
        out, _ = self._start()
        sid = out["session_id"]
        with patch.object(reg.requests, "post", return_value=_resp({"error": "authorization_pending"}, 400)):
            self.assertEqual(self.svc.poll(sid)["status"], "pending")
        self.svc._sessions[sid].next_poll_at = 0
        ok = {"client_id": "cli_1", "client_secret": "s3", "user_info": {"open_id": "ou_1", "tenant_brand": "feishu"}}
        with patch.object(reg.requests, "post", return_value=_resp(ok)):
            res = self.svc.poll(sid)
        self.assertEqual((res["status"], res["app_id"], res["open_id"], res["domain"]), ("success", "cli_1", "ou_1", "feishu"))

    def test_poll_is_throttled_to_interval(self):
        out, _ = self._start()
        with patch.object(reg.requests, "post", return_value=_resp({"error": "authorization_pending"}, 400)) as post:
            self.svc.poll(out["session_id"])
            self.svc.poll(out["session_id"])
        self.assertEqual(post.call_count, 1)

    def test_slow_down_denied_and_unknown(self):
        out, _ = self._start()
        sid = out["session_id"]
        with patch.object(reg.requests, "post", return_value=_resp({"error": "slow_down"}, 400)):
            self.assertEqual(self.svc.poll(sid)["interval"], 10)
        self.svc._sessions[sid].next_poll_at = 0
        with patch.object(reg.requests, "post", return_value=_resp({"error": "access_denied"}, 400)):
            self.assertEqual(self.svc.poll(sid)["status"], "denied")
        with self.assertRaises(reg.RegistrationSessionNotFound):
            self.svc.poll("missing")

    def test_network_failure_keeps_session_pending(self):
        out, _ = self._start()
        with patch.object(reg.requests, "post", side_effect=reg.requests.ConnectionError("x")):
            self.assertEqual(self.svc.poll(out["session_id"])["status"], "pending")


class ApplyRegistrationTests(unittest.TestCase):
    def setUp(self):
        # Windows keeps the sqlite file open after Config reload, so cleanup may fail there.
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.env_path = Path(self.tmp.name) / ".env"
        self.env_path.write_text("STOCK_LIST=600519\nFEISHU_APP_ID=cli_old\n", encoding="utf-8")
        self._env = {k: os.environ.get(k) for k in ("ENV_FILE", "DATABASE_PATH")}
        os.environ["ENV_FILE"] = str(self.env_path)
        os.environ["DATABASE_PATH"] = str(Path(self.tmp.name) / "t.db")
        Config.reset_instance()
        self.service = SystemConfigService(manager=ConfigManager(env_path=self.env_path))

    def tearDown(self):
        # A config reload exports saved keys into os.environ; don't leak them into other tests.
        for key in [k for k in os.environ if k.startswith("FEISHU_")]:
            os.environ.pop(key, None)
        for key, value in self._env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        Config.reset_instance()
        self.tmp.cleanup()

    def test_saves_credentials_target_and_clears_old_binding(self):
        result = {"app_id": "cli_new", "app_secret": "sec", "open_id": "ou_9", "domain": "feishu"}
        with patch.object(reg, "_start_stream", return_value=True), \
                patch("src.services.feishu_binding_service.unbind_chat") as unbind:
            out = reg.apply_registration(result, self.service)
        saved = ConfigManager(env_path=self.env_path).read_config_map()
        self.assertEqual(saved["FEISHU_APP_ID"], "cli_new")
        self.assertEqual(saved["FEISHU_APP_SECRET"], "sec")
        self.assertEqual(saved["FEISHU_STREAM_ENABLED"], "true")
        self.assertEqual((saved["FEISHU_CHAT_ID"], saved["FEISHU_RECEIVE_ID_TYPE"]), ("ou_9", "open_id"))
        unbind.assert_called_once()
        self.assertFalse(out["restart_required"])

    def test_restart_required_when_stream_cannot_start(self):
        result = {"app_id": "cli_new", "app_secret": "sec", "open_id": "", "domain": "lark"}
        with patch.object(reg, "_start_stream", return_value=False):
            out = reg.apply_registration(result, self.service)
        self.assertTrue(out["restart_required"])
        self.assertEqual(ConfigManager(env_path=self.env_path).read_config_map()["FEISHU_DOMAIN"], "lark")


if __name__ == "__main__":
    unittest.main()
