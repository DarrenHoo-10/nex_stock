# -*- coding: utf-8 -*-
"""Schemas for the current-user endpoint."""

from typing import Literal

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
