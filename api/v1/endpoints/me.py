# -*- coding: utf-8 -*-
"""Current-user endpoints (docs/design/multi-user.md section 10): identity, personal settings, own usage."""

from fastapi import APIRouter, Depends, Query

from api.deps import CurrentUser, get_current_user, get_database_manager
from api.v1.endpoints.usage import build_usage_dashboard
from api.v1.errors import api_error
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.me import (
    MeResponse,
    MeSettingsResponse,
    MeSettingsUpdateRequest,
    TestModelResponse,
)
from api.v1.schemas.usage import UsageDashboardResponse
from src.auth import get_auth_mode
from src.services.me_settings_service import MeSettingsService, run_model_test
from src.services.user_config_service import user_model_key_configured
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
}


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
