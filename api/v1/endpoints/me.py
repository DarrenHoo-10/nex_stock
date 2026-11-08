# -*- coding: utf-8 -*-
"""Current-user endpoint (docs/design/multi-user.md section 10)."""

from fastapi import APIRouter, Depends

from api.deps import CurrentUser, get_current_user
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.me import MeResponse
from src.auth import get_auth_mode
from src.services.user_config_service import user_model_key_configured

router = APIRouter()


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
