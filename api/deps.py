# -*- coding: utf-8 -*-
"""
===================================
API 依赖注入模块
===================================

职责：
1. 提供数据库 Session 依赖
2. 提供配置依赖
3. 提供服务层依赖
4. 提供当前用户与管理员守卫依赖（多用户，docs/design/multi-user.md 第 2 节）
"""

from dataclasses import dataclass
from typing import Generator

from fastapi import Depends, Request
from sqlalchemy.orm import Session

from api.v1.errors import api_error
from src.auth import is_user_center_mode
from src.storage import DatabaseManager
from src.config import get_config, get_primary_user_id, Config
from src.services.system_config_service import SystemConfigService
from src.services.runtime_scheduler import RuntimeSchedulerService
from src.services.agent_chat_session_service import AgentChatSessionService


@dataclass(frozen=True)
class CurrentUser:
    """The user a request acts as."""

    user_id: str
    handle: str
    display_name: str
    role: str
    is_admin: bool


def get_current_user(request: Request) -> CurrentUser:
    """Resolve the acting user.

    * ``AUTH_MODE=user_center``: the session the auth middleware verified and attached to
      ``request.state.user_center_session`` (``user_id`` is the user-center ``userId``).
    * ``AUTH_MODE=local``: always the primary user (``PRIMARY_USER_ID``) with the admin
      role, so single-user and desktop deployments behave as before.
    """
    if is_user_center_mode():
        session = getattr(request.state, "user_center_session", None)
        if session is None or not session.user_id:
            raise api_error(401, "AUTH_REQUIRED", "Login required")
        return CurrentUser(
            user_id=session.user_id,
            handle=session.handle,
            display_name=session.display_name,
            role=session.role,
            is_admin=session.role == "admin",
        )
    primary = get_primary_user_id()
    return CurrentUser(user_id=primary, handle=primary, display_name=primary, role="admin", is_admin=True)


def require_admin(user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """Guard for process-level endpoints (system settings, scheduler, shared maintenance, global usage)."""
    if not user.is_admin:
        raise api_error(403, "ADMIN_REQUIRED", "Administrator role required")
    return user


def get_db() -> Generator[Session, None, None]:
    """
    获取数据库 Session 依赖
    
    使用 FastAPI 依赖注入机制，确保请求结束后自动关闭 Session
    
    Yields:
        Session: SQLAlchemy Session 对象
        
    Example:
        @router.get("/items")
        async def get_items(db: Session = Depends(get_db)):
            ...
    """
    db_manager = DatabaseManager.get_instance()
    session = db_manager.get_session()
    try:
        yield session
    finally:
        session.close()


def get_config_dep() -> Config:
    """
    获取配置依赖
    
    Returns:
        Config: 配置单例对象
    """
    return get_config()


def get_database_manager() -> DatabaseManager:
    """
    获取数据库管理器依赖
    
    Returns:
        DatabaseManager: 数据库管理器单例对象
    """
    return DatabaseManager.get_instance()


def get_agent_chat_session_service() -> AgentChatSessionService:
    """Build an Agent Chat session service for the current database manager."""
    return AgentChatSessionService(DatabaseManager.get_instance())


def get_system_config_service(request: Request) -> SystemConfigService:
    """Get app-lifecycle shared SystemConfigService instance."""
    service = getattr(request.app.state, "system_config_service", None)
    if service is None:
        service = SystemConfigService()
        request.app.state.system_config_service = service
    return service


def get_runtime_scheduler_service(request: Request) -> RuntimeSchedulerService:
    """Get app-lifecycle shared RuntimeSchedulerService instance."""
    service = getattr(request.app.state, "runtime_scheduler_service", None)
    if service is None:
        service = RuntimeSchedulerService()
        request.app.state.runtime_scheduler_service = service
    return service


def _daily_plan_unavailable():
    return api_error(503, "daily_plan_unavailable", "次日计划服务未就绪")


def get_board_anomaly_service():
    """Lazily build the board anomaly service; 503 while the module is unavailable."""
    try:
        from src.services.board_anomaly_service import BoardAnomalyService
    except ImportError as exc:
        raise _daily_plan_unavailable() from exc
    return BoardAnomalyService()


def get_daily_plan_service():
    """Lazily build the daily plan service; 503 while the module is unavailable."""
    try:
        from src.services.daily_plan_service import DailyPlanService
    except ImportError as exc:
        raise _daily_plan_unavailable() from exc
    return DailyPlanService()
