# -*- coding: utf-8 -*-
"""Current-user endpoints (docs/design/multi-user.md section 10): identity, personal settings, own usage."""

from fastapi import APIRouter, Depends, Query

from api.deps import CurrentUser, get_current_user, get_database_manager
from api.v1.endpoints.usage import build_usage_dashboard
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.me import (
    MeResponse,
    MeScheduleResponse,
    MeSettingsResponse,
    MeSettingsUpdateRequest,
    NotificationChannelCreateRequest,
    NotificationChannelItem,
    NotificationChannelsResponse,
    NotificationChannelTestResponse,
    NotificationChannelUpdateRequest,
    TestModelResponse,
)
from api.v1.schemas.usage import UsageDashboardResponse
from src.auth import get_auth_mode
from src.services.me_settings_service import MeSettingsService, run_model_test
from src.services.user_config_service import user_model_key_configured
from src.services.user_notification_service import ChannelError, UserNotificationChannelService
from src.services.user_schedule_service import describe_schedule
from src.services.user_settings_service import UserSettingError
from src.storage import DatabaseManager

router = APIRouter()

# UserSettingError.code -> HTTP status; the code itself is returned as the stable ``error`` value.
_SETTING_ERROR_STATUS = {
    "primary_user_uses_env": 403,
    "scope_not_user": 400,
    "managed_elsewhere": 400,
    "invalid_key": 400,
    "invalid_value": 400,
    "unsafe_url": 400,
    "invalid_user_id": 400,
    "plan_time_too_early": 400,
    "preopen_time_too_late": 400,
    "preopen_time_too_early": 400,
}


# ChannelError.code -> HTTP status. A foreign channel id answers exactly like a missing one (404 channel_not_found).
_CHANNEL_ERROR_STATUS = {
    "primary_user_uses_env": 403,
    "channel_not_found": 404,
    "invalid_channel_type": 400,
    "invalid_config": 400,
    "unsafe_target": 400,
    "channel_limit_reached": 400,
    "rate_limited": 429,
    "secret_store_unavailable": 503,
}


def _channel_error(exc: ChannelError):
    return api_error(
        _CHANNEL_ERROR_STATUS.get(exc.code, 400),
        exc.code,
        exc.message,
        detail={"issues": exc.issues} if exc.issues else None,
    )


def _setting_error(exc: UserSettingError):
    return api_error(
        _SETTING_ERROR_STATUS.get(exc.code, 400),
        exc.code,
        exc.message,
        detail={"issues": exc.issues} if exc.issues else None,
    )


@router.get(
    "",
    response_model=MeResponse,
    responses={401: {"model": ErrorResponse}},
    summary="Get the current user",
    description="Returns who the request acts as, whether they are an administrator, the auth mode "
                "and whether a usable model key is configured for them.",
)
def get_me(user: CurrentUser = Depends(get_current_user)) -> MeResponse:
    return MeResponse(
        user_id=user.user_id,
        handle=user.handle,
        display_name=user.display_name,
        role=user.role,
        is_admin=user.is_admin,
        auth_mode=get_auth_mode(),
        model_key_configured=user_model_key_configured(user.user_id),
    )


@router.get(
    "/settings",
    response_model=MeSettingsResponse,
    summary="Get the personal settings",
    description="User-scope settings grouped as model / search / data-source credentials / Agent / push / position "
                "rules / report preferences. Each field carries its effective value, where it comes from "
                "(user / system_default / unset) and whether it inherits the system default; secrets are masked, "
                "and an inherited system default is reported as source / configured only (its value is empty). "
                "The primary user's settings are the system (.env) settings: returned read-only.",
)
def get_my_settings(user: CurrentUser = Depends(get_current_user)) -> MeSettingsResponse:
    return MeSettingsResponse(**MeSettingsService().describe(user.user_id))


@router.put(
    "/settings",
    response_model=MeSettingsResponse,
    responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="Update personal settings",
    description="Partial update validated with the system settings rules. An empty (or masked) secret keeps the "
                "stored value; system-scope keys are rejected (scope_not_user); URL values must be public https "
                "(unsafe_url). Nothing is stored when any item is invalid. The primary user is rejected "
                "(primary_user_uses_env): edit the system settings instead.",
)
def update_my_settings(
    request: MeSettingsUpdateRequest,
    user: CurrentUser = Depends(get_current_user),
) -> MeSettingsResponse:
    service = MeSettingsService()
    try:
        service.update(user.user_id, [item.model_dump() for item in request.items])
    except UserSettingError as exc:
        raise _setting_error(exc) from exc
    return MeSettingsResponse(**service.describe(user.user_id))


@router.delete(
    "/settings/{key}",
    response_model=MeSettingsResponse,
    responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="Reset one personal setting",
    description="Drops the user's own value: the system default applies again (or nothing, for model / search "
                "keys and other secrets).",
)
def reset_my_setting(key: str, user: CurrentUser = Depends(get_current_user)) -> MeSettingsResponse:
    service = MeSettingsService()
    try:
        service.reset(user.user_id, key)
    except UserSettingError as exc:
        raise _setting_error(exc) from exc
    return MeSettingsResponse(**service.describe(user.user_id))


@router.post(
    "/settings/test-model",
    response_model=TestModelResponse,
    responses={400: {"model": ErrorResponse}},
    summary="Test the personal model configuration",
    description="One minimal completion with the user's own model key and endpoint. 400 model_key_required when "
                "the user has no model of their own (the system key is never used for personal accounts); "
                "a provider failure is reported in the body with success=false.",
)
def test_my_model(user: CurrentUser = Depends(get_current_user)) -> TestModelResponse:
    return TestModelResponse(**run_model_test(user.user_id))


@router.get(
    "/usage",
    response_model=UsageDashboardResponse,
    summary="Own LLM usage",
    description="Token usage of the signed-in user only, in the same shape as /usage/dashboard "
                "(which stays administrator-only and covers every user).",
)
def get_my_usage(
    period: str = Query("month", description="'today' | 'month' | 'all'"),
    limit: int = Query(50, ge=1, le=200, description="Recent call records to include"),
    user: CurrentUser = Depends(get_current_user),
    db_manager: DatabaseManager = Depends(get_database_manager),
) -> UsageDashboardResponse:
    return build_usage_dashboard(db_manager, period, limit, user_id=user.user_id)


@router.get(
    "/schedule",
    response_model=MeScheduleResponse,
    summary="Effective push schedule",
    description="The user's effective next-day plan / pre-open push switches and times, plus the allowed range: the "
                "earliest plan time (the system's board data is ready) and the latest pre-open start (before market "
                "open). Values are saved through PUT /me/settings (DAILY_PLAN_ENABLED / DAILY_PLAN_TIME / "
                "PREOPEN_PUSH_ENABLED / PREOPEN_PUSH_TIME); a time outside the range is rejected there with "
                "plan_time_too_early / preopen_time_too_early / preopen_time_too_late.",
)
def get_my_schedule(user: CurrentUser = Depends(get_current_user)) -> MeScheduleResponse:
    return MeScheduleResponse(**describe_schedule(user.user_id))


@router.get(
    "/notification-channels",
    response_model=NotificationChannelsResponse,
    summary="List personal push channels",
    description="Channels of the signed-in user, secrets masked. The primary user's channels are the .env channels: "
                "listed read-only by type (no secrets) and managed in the system settings.",
)
def list_my_notification_channels(user: CurrentUser = Depends(get_current_user)) -> NotificationChannelsResponse:
    return NotificationChannelsResponse(**UserNotificationChannelService().list(user.user_id))


@router.post(
    "/notification-channels",
    response_model=NotificationChannelItem,
    status_code=201,
    responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}},
    summary="Add a personal push channel",
    description="Types: feishu, dingtalk, wecom, telegram, pushplus, serverchan3, email, custom. Webhook URLs must be "
                "public https URLs and the SMTP host a public host on port 465 / 587 / 25 (unsafe_target). At most "
                "NOTIFICATION_CHANNELS_MAX_PER_USER channels (channel_limit_reached). The primary user is rejected "
                "(primary_user_uses_env).",
)
def create_my_notification_channel(
    request: NotificationChannelCreateRequest, user: CurrentUser = Depends(get_current_user),
) -> NotificationChannelItem:
    try:
        item = UserNotificationChannelService().create(
            user.user_id, request.channel_type, request.config, name=request.name, enabled=request.enabled,
        )
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return NotificationChannelItem(**item)


@router.put(
    "/notification-channels/{channel_id}",
    response_model=NotificationChannelItem,
    responses={400: {"model": ErrorResponse}, 403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Update a personal push channel",
    description="Partial update. An empty or masked secret keeps the stored value. Another user's id answers like a "
                "missing one (404 channel_not_found).",
)
def update_my_notification_channel(
    channel_id: int, request: NotificationChannelUpdateRequest, user: CurrentUser = Depends(get_current_user),
) -> NotificationChannelItem:
    try:
        item = UserNotificationChannelService().update(
            user.user_id, channel_id, config=request.config, name=request.name, enabled=request.enabled,
        )
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return NotificationChannelItem(**item)


@router.delete(
    "/notification-channels/{channel_id}",
    response_model=NotificationChannelsResponse,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}},
    summary="Delete a personal push channel",
    description="Returns the remaining channels. Another user's id answers like a missing one (404 channel_not_found).",
)
def delete_my_notification_channel(
    channel_id: int, user: CurrentUser = Depends(get_current_user),
) -> NotificationChannelsResponse:
    service = UserNotificationChannelService()
    try:
        service.delete(user.user_id, channel_id)
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return NotificationChannelsResponse(**service.list(user.user_id))


@router.post(
    "/notification-channels/{channel_id}/test",
    response_model=NotificationChannelTestResponse,
    responses={403: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 429: {"model": ErrorResponse}},
    summary="Send a test message through one channel",
    description="Sends a fixed test message. The result is a status and a generic error category only (send_failed / "
                "target_not_allowed / unreadable); nothing of the upstream response is returned and no timing is "
                "reported. At most one test per channel every few seconds and one running test per user "
                "(rate_limited); the send has a hard wall-clock deadline (USER_PUSH_SEND_DEADLINE_SECONDS).",
)
def test_my_notification_channel(
    channel_id: int, user: CurrentUser = Depends(get_current_user),
) -> NotificationChannelTestResponse:
    try:
        result = UserNotificationChannelService().test(user.user_id, channel_id)
    except ChannelError as exc:
        raise _channel_error(exc) from exc
    return NotificationChannelTestResponse(**result)
