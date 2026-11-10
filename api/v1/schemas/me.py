# -*- coding: utf-8 -*-
"""Schemas for the current-user endpoint."""

from typing import Any, List, Literal, Optional

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
