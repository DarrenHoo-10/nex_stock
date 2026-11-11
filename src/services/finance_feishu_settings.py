"""Browser-safe view of the selected Feishu application's durable ownership."""
from urllib.parse import parse_qsl, urlsplit

from src.config import get_config
from src.services.finance_channel_ownership import channel_state
from src.services.finance_identity import setting


def public_settings_url():
    # This is an explicitly published browser address, never FINANCE_CORE_URL.
    value = setting("FINANCE_CORE_SETTINGS_URL")
    try:
        parts = urlsplit(value)
        loopback = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parts.hostname or parts.username or parts.password or parts.fragment
                or not (parts.scheme == "https" or parts.scheme == "http" and loopback)
                or parse_qsl(parts.query, keep_blank_values=True) not in ([], [("settings", "feishu")])):
            return None
        _ = parts.port
    except ValueError:
        return None
    return value


def settings_state():
    state = channel_state()
    current = str(getattr(get_config(), "feishu_app_id", "") or "").strip()
    return {"managed": state["managed"], "owner": state["owner"], "version": state["version"],
            "appId": state.get("appId"), "currentAppId": current or state.get("appId"),
            "appliesToCurrentApp": bool(state["managed"] and (not current or current == state.get("appId"))),
            "settingsUrl": public_settings_url()}
