# -*- coding: utf-8 -*-
"""Schemas for the current-user endpoint."""

from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class MeResponse(BaseModel):
    """The signed-in user and what the client needs to decide which entry points to show."""

    user_id: str = Field(..., description="User-center userId; PRIMARY_USER_ID in local auth mode")
    handle: str = Field("", description="User handle (latest login snapshot)")
    display_name: str = Field("", description="Display name (latest login snapshot)")
    role: str = Field(..., description="User-center role: admin or user")
    is_admin: bool = Field(..., description="Whether the user may use process-level endpoints")
    auth_mode: Literal["local", "user_center"] = Field(..., description="Active AUTH_MODE")
    model_key_configured: bool = Field(
        ...,
        description="Whether the user's effective configuration has a usable model (channel / API key); "
                    "false means analysis and Agent chat need the user's own model key first",
    )


class SettingFieldItem(BaseModel):
    """One personal setting. Secret values are always masked; plaintext is never returned."""

    key: str
    title: str = ""
    description: str = ""
    group: str
    data_type: str = "string"
    ui_control: str = "text"
    options: List[Any] = Field(default_factory=list)
    is_secret: bool = False
    value: str = Field("", description="Own value or the inherited default; ****** for a configured secret")
    source: Literal["user", "system_default", "unset"] = Field(
        ..., description="user = the user's own value, system_default = inherited from the system layer, unset"
    )
    inherits: bool = Field(
        ..., description="Whether the key falls back to the system default while the user has none "
                         "(false for model / search keys, push channels and other secrets)",
    )
    configured: bool = False
    editable: bool = Field(..., description="False for the primary user: their settings are the system (.env) settings")
    updated_at: Optional[str] = None


class SettingGroupItem(BaseModel):
    id: str
    title: str
    fields: List[SettingFieldItem]


class MeSettingsResponse(BaseModel):
    user_id: str
    is_primary: bool
    managed_in_system_settings: bool = Field(
        ..., description="True for the primary user: values are shown read-only and edited in the system settings"
    )
    groups: List[SettingGroupItem]


class SettingUpdateItem(BaseModel):
    key: str = Field(..., min_length=1, max_length=128)
    value: str = Field(..., max_length=16384, description="Empty (or ******) on a secret keeps the stored value")


class MeSettingsUpdateRequest(BaseModel):
    items: List[SettingUpdateItem] = Field(..., min_length=1, max_length=200)


class TestModelResponse(BaseModel):
    success: bool
    model: str = ""
    latency_ms: Optional[int] = None
    error_code: Optional[str] = None
    # Generic failure category (auth_failed / unreachable / timeout / bad_response) and the HTTP status at most;
    # nothing of the upstream error text or body is returned.
    error_category: Optional[str] = None
    http_status: Optional[int] = None
    message: str = ""


class ChannelFieldInfo(BaseModel):
    name: str
    label: str
    kind: str
    required: bool = False
    secret: bool = False


class ChannelTypeInfo(BaseModel):
    channel_type: str
    label: str
    fields: List[ChannelFieldInfo]


class NotificationChannelItem(BaseModel):
    """One push channel. Secret config values are always ``******``; plaintext is never returned."""

    id: Optional[int] = Field(None, description="Channel id; null for the primary user's read-only .env channels")
    channel_type: str
    name: str = ""
    enabled: bool = True
    source: Literal["user", "env"] = Field("user", description="env = a system (.env) channel of the primary user")
    editable: bool = True
    readable: bool = Field(True, description="False when the stored secrets cannot be decrypted (re-enter the config)")
    config: Dict[str, Any] = Field(default_factory=dict, description="Field values; secret fields are masked")
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class NotificationChannelsResponse(BaseModel):
    user_id: str
    is_primary: bool
    managed_in_system_settings: bool = Field(
        ..., description="True for the primary user: the channels are the .env ones, edited in the system settings"
    )
    note: str = ""
    max_channels: int = Field(0, description="Per-user channel limit (NOTIFICATION_CHANNELS_MAX_PER_USER)")
    channels: List[NotificationChannelItem]
    channel_types: List[ChannelTypeInfo] = Field(default_factory=list)


class NotificationChannelCreateRequest(BaseModel):
    channel_type: str = Field(..., min_length=1, max_length=32)
    name: Optional[str] = Field(None, max_length=64)
    enabled: bool = True
    config: Dict[str, Any] = Field(default_factory=dict)


class NotificationChannelUpdateRequest(BaseModel):
    name: Optional[str] = Field(None, max_length=64)
    enabled: Optional[bool] = None
    config: Optional[Dict[str, Any]] = Field(
        None, description="Partial config; an empty or masked secret keeps the stored value"
    )


class NotificationChannelTestResponse(BaseModel):
    success: bool
    status: Literal["sent", "failed", "blocked"]
    error_category: Optional[str] = Field(
        None, description="send_failed / target_not_allowed / unreadable - never an upstream message or body"
    )
    # No timing is returned (an open, closed and filtered target would differ by their latency).


class ScheduleWindow(BaseModel):
    enabled: bool
    default_time: str


class DailyPlanSchedule(ScheduleWindow):
    times: List[str]
    earliest_time: str = Field(..., description="Earliest allowed plan time (system data-ready time)")


class PreopenSchedule(ScheduleWindow):
    time: str
    earliest_time: str = Field(..., description="Earliest allowed start of the pre-open push (final overnight US session)")
    latest_time: str = Field(..., description="Latest allowed start of the pre-open push (before market open)")


class MeScheduleResponse(BaseModel):
    user_id: str
    is_primary: bool
    managed_in_system_settings: bool
    daily_plan: DailyPlanSchedule
    preopen_push: PreopenSchedule
