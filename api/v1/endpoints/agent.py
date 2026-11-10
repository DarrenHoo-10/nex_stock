# -*- coding: utf-8 -*-
"""
Agent API endpoints.
"""

import asyncio
import json
import logging
import threading
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from api.deps import CurrentUser, get_agent_chat_session_service, get_current_user
from api.v1.schemas.assistant_notes import RecallStatus
from api.v1.schemas.system_config import AgentBackendStatusResponse
from src.config import get_agent_reasoning_effort, get_config, get_effective_agent_primary_model, get_primary_user_id
from src.agent.conversation import is_bot_session_id
from src.services.agent_chat_session_service import AgentChatSessionService
from src.services.user_config_service import get_user_config, require_usable_model
from src.llm.generation_params import REASONING_EFFORT_VALUES
from src.services.agent_model_service import (
    AgentChatOverrideError,
    apply_agent_chat_overrides,
    list_agent_model_deployments,
    resolve_model_context_window,
)
from src.storage import ChatSessionNotFound, ChatSessionStateConflict

# Tool name -> Chinese display name mapping
TOOL_DISPLAY_NAMES: Dict[str, str] = {
    "get_realtime_quote":         "获取实时行情",
    "get_daily_history":          "获取历史K线",
    "get_chip_distribution":      "分析筹码分布",
    "get_analysis_context":       "获取分析上下文",
    "get_stock_info":             "获取股票基本面",
    "search_stock_news":          "搜索股票新闻",
    "search_comprehensive_intel": "搜索综合情报",
    "search_market_news":         "搜索题材新闻",
    "analyze_trend":              "分析技术趋势",
    "calculate_ma":               "计算均线系统",
    "get_volume_analysis":        "分析量能变化",
    "analyze_pattern":            "识别K线形态",
    "get_market_indices":         "获取市场指数",
    "get_sector_rankings":        "分析行业板块",
    "get_skill_backtest_summary": "获取技能回测概览",
    "get_strategy_backtest_summary": "获取策略回测概览",
    "get_stock_backtest_summary": "获取个股回测数据",
}

logger = logging.getLogger(__name__)

# Chat SSE: a keep-alive comment every 15 s while no event arrives; the stream still ends with
# "分析超时" after 300 s without any progress event (Codex keeps its own deadline).
_STREAM_HEARTBEAT_SECONDS = 15.0
_STREAM_IDLE_TIMEOUT_SECONDS = 300.0

router = APIRouter()


def _config_for(user: Any, *, need_model: bool = False):
    """Configuration of the acting user: ``get_config()`` for the primary user (and callers without a
    user context), the user's own read-only view for everybody else.

    ``need_model=True`` is for routes that call a model: a non-primary user without a model of their own
    gets 400 ``model_key_required`` before anything reaches a provider.
    """
    if not isinstance(user, CurrentUser) or user.user_id == get_primary_user_id():
        return get_config()
    config = get_user_config(user.user_id)
    return require_usable_model(config) if need_model else config


# Keyed by (user_id, request_id): a user can only cancel their own streams, and two users may reuse
# the same client-chosen request id.
_ACTIVE_CODEX_STREAMS: Dict[tuple, threading.Event] = {}
_ACTIVE_CODEX_STREAMS_LOCK = threading.Lock()

class ChatRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    message: str
    session_id: Optional[str] = None
    session_generation: Optional[str] = Field(default=None, min_length=1, max_length=36)
    request_id: Optional[str] = Field(default=None, min_length=1, max_length=64)
    skills: Optional[List[str]] = Field(
        default=None,
        validation_alias=AliasChoices("skills", "strategies"),
    )
    context: Optional[Dict[str, Any]] = None  # Previous analysis context for data reuse
    # Per-request Agent model (a ``route_model`` from GET /agent/models) and reasoning effort
    # (low|medium|high|xhigh|max, or "none" to send none). Blank = configured AGENT_LITELLM_MODEL / AGENT_REASONING_EFFORT.
    model: Optional[str] = Field(default=None, max_length=200)
    reasoning_effort: Optional[str] = Field(default=None, max_length=16)

    @property
    def effective_skills(self) -> Optional[List[str]]:
        """Return skill ids from the unified request shape."""
        return self.skills


def _build_agent_chat_context(request: ChatRequest, config, skills: Optional[List[str]]) -> Dict[str, Any]:
    """Build the shared context contract for regular and streaming Agent Chat."""
    context = dict(request.context or {})
    context.pop("skills", None)
    context.pop("strategies", None)
    if skills is not None:
        context["skills"] = skills
    report_language = context.get("report_language")
    if report_language is None or (isinstance(report_language, str) and not report_language.strip()):
        context["report_language"] = config.report_language
    return context


class ActiveStockContextResponse(BaseModel):
    stock_code: str
    stock_name: Optional[str]
    canonical_id: str
    asset_type: str


class SessionStateResponse(BaseModel):
    selected_skill_ids: Optional[List[str]]


class ChatResponse(BaseModel):
    success: bool
    content: str
    session_id: str
    error: Optional[str] = None
    active_stock_context: Optional[ActiveStockContextResponse]
    session_state_version: int
    session_generation: str
    session_state: SessionStateResponse


def _public_chat_state(*, active_stock_context, session_state_version, session_generation, selected_skill_ids):
    """One whitelist for detail, accepted and non-stream committed state."""
    stock = None if active_stock_context is None else {
        key: active_stock_context[key] for key in ("stock_code", "stock_name", "canonical_id", "asset_type")
    }
    return {
        "active_stock_context": stock,
        "session_state_version": session_state_version,
        "session_generation": session_generation,
        "session_state": {"selected_skill_ids": selected_skill_ids},
    }


def _session_not_found() -> HTTPException:
    """404 for a missing session and for one that belongs to another user: never distinguished."""
    return HTTPException(
        status_code=404,
        detail={"error": ChatSessionNotFound.code, "message": "会话不存在"},
    )


def _read_session_id(session_service: AgentChatSessionService, session_id: str, user_id: str) -> str:
    """Session id to read for ``user_id``: another user's session reads as one that does not exist.

    The missing-id response of the read / delete / context / compact endpoints is an empty one (not a 404), so
    a foreign id is served from a fresh id that cannot exist, giving exactly that response and no existence signal.
    """
    try:
        session_service.require_session_access(session_id, user_id)
    except ChatSessionNotFound:
        return f"missing-{uuid.uuid4().hex}"
    return session_id


def _chat_session_id(request: "ChatRequest", user: CurrentUser) -> str:
    """Session id of a chat request (a new one when none is given).

    The chat bots own the ``{platform}_...`` / ``feishu:...`` id namespaces and act as the primary user; any other
    user creating such an id would pre-empt the session the bot needs later.
    """
    if not request.session_id:
        return str(uuid.uuid4())
    if user.user_id != get_primary_user_id() and is_bot_session_id(request.session_id):
        raise HTTPException(
            status_code=400,
            detail={"error": "validation_error", "message": "session_id 使用了保留前缀，请换一个"},
        )
    return request.session_id


def _prepare_chat_request(request, config, session_id, session_service, user_id):
    """Prepare once against the session snapshot, then commit exactly one turn.

    ``user_id`` is the authenticated user: the session is created for them or must already be theirs.
    """
    resolved = session_service.prepare_session_turn(
        config, session_id, request.message, request.effective_skills,
        context=_build_agent_chat_context(request, config, None),
        expected_generation=request.session_generation,
        user_id=user_id,
    )
    if resolved.clarification is not None:
        accepted = session_service.commit_user_turn(resolved)
        return None, None, accepted, resolved.clarification
    executor = _build_executor(config, resolved.skill_selection.effective_skill_ids)
    turn = executor.prepare_turn(
        message=request.message, session_id=session_id, context=resolved.effective_context,
        selected_skill_ids=resolved.skill_selection.selected_skill_ids_update,
        resolved_turn=resolved, session_service=session_service,
    )
    return executor, turn, turn.accepted_snapshot, None


def _clarification_result(session_service, accepted, content, backend_id):
    from src.agent.executor import AgentResult

    session_service.db.save_conversation_message(
        accepted["session_id"], "assistant", content, accepted_turn=accepted,
    )
    return AgentResult(success=True, content=content, backend=backend_id)


def _accepted_state_payload(accepted):
    return _public_chat_state(
        active_stock_context=accepted["active_stock_context"],
        session_state_version=accepted["session_state_version"],
        session_generation=accepted["session_generation"],
        selected_skill_ids=accepted["selected_skill_ids"],
    )


class SkillInfo(BaseModel):
    id: str
    name: str
    description: str

class SkillsResponse(BaseModel):
    skills: List[SkillInfo]
    default_skill_id: str = ""


class StrategiesResponse(BaseModel):
    strategies: List[SkillInfo]
    default_strategy_id: str = ""


class AgentModelDeployment(BaseModel):
    deployment_id: str
    model: str
    provider: str
    source: str
    api_base: Optional[str] = None
    deployment_name: Optional[str] = None
    route_model: Optional[str] = None
    is_primary: bool = False
    is_fallback: bool = False


class AgentModelsResponse(BaseModel):
    models: List[AgentModelDeployment]
    default_model: Optional[str] = None
    default_reasoning_effort: str = ""
    reasoning_effort_options: List[str] = Field(default_factory=lambda: list(REASONING_EFFORT_VALUES))


@router.get("/models", response_model=AgentModelsResponse)
async def get_agent_models(user: CurrentUser = Depends(get_current_user)):
    """Get configured Agent model deployments for frontend selection (the user's own, never the system's)."""
    config = _config_for(user)
    from src.agent.agent_backend import AgentBackendConfigError, resolve_agent_backend_id

    try:
        selected_backend = resolve_agent_backend_id(config)
    except AgentBackendConfigError:
        return AgentModelsResponse(models=[])
    if selected_backend == "codex_app_server":
        return AgentModelsResponse(models=[])
    return AgentModelsResponse(
        models=[AgentModelDeployment(**item) for item in list_agent_model_deployments(config)],
        default_model=get_effective_agent_primary_model(config) or None,
        default_reasoning_effort=get_agent_reasoning_effort(config),
    )


def _apply_request_overrides(request: ChatRequest, config, backend_id: str):
    """Config for this request with its model / reasoning effort applied (400 when not offered)."""
    model = getattr(request, "model", None)
    reasoning_effort = getattr(request, "reasoning_effort", None)
    if not (model or "").strip() and not (reasoning_effort or "").strip():
        return config
    if backend_id == "codex_app_server":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "capability_unsupported",
                "message": "Codex Agent does not support choosing the model or reasoning effort per request",
            },
        )
    try:
        return apply_agent_chat_overrides(config, model=model, reasoning_effort=reasoning_effort)
    except AgentChatOverrideError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": str(exc)}) from exc


@router.get("/status", response_model=AgentBackendStatusResponse)
async def get_agent_status(user: CurrentUser = Depends(get_current_user)):
    """Return the current effective Chat backend status for the Chat page."""
    config = _config_for(user)
    payload = await asyncio.to_thread(_get_agent_chat_status, config)
    response = _agent_status_response(payload)
    response.recall = await asyncio.to_thread(_get_recall_status, config, user)
    return response


def _agent_status_response(payload: Dict[str, Any]) -> AgentBackendStatusResponse:
    return AgentBackendStatusResponse(
        backend=payload["backend"],
        available=payload["available"],
        experimental=payload["experimental"],
        version=payload.get("version"),
        error_code=payload.get("error_code"),
        message=payload.get("message"),
    )


def _get_recall_status(config, user: Any = None) -> Optional[RecallStatus]:
    """Long-term recall health of the acting user's memory bank; a weak dependency, so never fail the status call."""
    try:
        from src.agent.recall import get_recall_service, get_user_recall_service

        if isinstance(user, CurrentUser) and user.user_id != get_primary_user_id():
            health = get_user_recall_service(user.user_id, config).health()
        else:
            health = get_recall_service(config).health()
        return RecallStatus(
            provider=health.provider_id,
            status=health.status,
            message=health.message,
            latency_ms=health.latency_ms,
            pending_retain=health.pending_retain,
            pending_forget=health.pending_forget,
        )
    except Exception as exc:
        logger.warning("Recall status unavailable: %s", exc)
        return None


def _build_skills_response(config) -> SkillsResponse:
    from src.agent.factory import get_skill_manager
    from src.agent.skills.defaults import get_primary_default_skill_id

    skill_manager = get_skill_manager(config)
    available_skills = sorted(
        [
            skill
            for skill in skill_manager.list_skills()
            if getattr(skill, "user_invocable", True)
        ],
        key=lambda skill: (
            int(getattr(skill, "default_priority", 100)),
            skill.display_name,
            skill.name,
        ),
    )
    skills = [
        SkillInfo(id=skill.name, name=skill.display_name, description=skill.description)
        for skill in available_skills
    ]
    return SkillsResponse(
        skills=skills,
        default_skill_id=get_primary_default_skill_id(available_skills),
    )


@router.get("/skills", response_model=SkillsResponse)
async def get_skills():
    """
    Get available agent strategy skills.
    """
    return _build_skills_response(get_config())


@router.get("/strategies", response_model=StrategiesResponse, include_in_schema=False)
async def get_strategies():
    """Compatibility alias for legacy clients."""
    payload = _build_skills_response(get_config())
    return StrategiesResponse(
        strategies=payload.skills,
        default_strategy_id=payload.default_skill_id,
    )

@router.post("/chat", response_model=ChatResponse)
async def agent_chat(
    request: ChatRequest,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Chat with the AI Agent without progress events.

    Codex Agent callers must use ``/chat/stream``, which provides progress
    events and request cancellation. The default LiteLLM Agent keeps this
    endpoint's existing behavior.
    """
    config = _config_for(user, need_model=True)
    backend_id = _select_agent_chat_backend(config)
    if backend_id == "codex_app_server":
        raise HTTPException(
            status_code=400,
            detail={
                "error": "capability_unsupported",
                "message": "Codex Agent requires the Chat interface with progress and stop support",
            },
        )
    
    config = _apply_request_overrides(request, config, backend_id)
    session_id = _chat_session_id(request, user)

    try:
        executor, turn, accepted, clarification = await asyncio.to_thread(
            _prepare_chat_request, request, config, session_id, session_service, user.user_id,
        )
        if clarification is not None:
            result = await asyncio.to_thread(_clarification_result, session_service, accepted, clarification, backend_id)
        else:
            result = await asyncio.to_thread(executor.execute_turn, turn)

        return ChatResponse(
            success=result.success,
            content=result.content,
            session_id=session_id,
            error=result.error,
            **_accepted_state_payload(accepted),
        )
    except ChatSessionNotFound as exc:
        raise _session_not_found() from exc
    except ChatSessionStateConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": str(exc)}) from exc
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Agent chat API failed: {e}")
        logger.exception("Agent chat error details:")
        raise HTTPException(status_code=500, detail=str(e))


class SessionItem(BaseModel):
    session_id: str
    title: str
    message_count: int
    created_at: Optional[str] = None
    last_active: Optional[str] = None

class SessionsResponse(BaseModel):
    sessions: List[SessionItem]

class SessionMessagesResponse(BaseModel):
    session_id: str
    messages: List[Dict[str, Any]]
    session_state: SessionStateResponse
    active_stock_context: Optional[ActiveStockContextResponse]
    session_state_version: int
    session_generation: Optional[str]


@router.get("/chat/sessions", response_model=SessionsResponse)
async def list_chat_sessions(
    limit: int = 50,
    user_id: Optional[str] = None,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """获取当前用户的聊天会话列表（只返回本人创建的会话）

    Args:
        limit: Maximum number of sessions to return.
        user_id: Optional platform-prefixed session id prefix (legacy query-parameter name, kept for
            compatibility): narrows the caller's OWN sessions to those whose session_id starts with
            this prefix, e.g. ``telegram_12345``.  It never selects another user's sessions.
    """
    sessions = session_service.list_sessions(limit, user_id, user_id=user.user_id)
    return SessionsResponse(sessions=sessions)


@router.get("/chat/sessions/{session_id}", response_model=SessionMessagesResponse)
async def get_chat_session_messages(
    session_id: str,
    limit: int = 100,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """获取单个会话的完整消息（别人的会话与不存在的会话响应完全相同：空消息）"""
    detail = session_service.get_session_detail(
        _read_session_id(session_service, session_id, user.user_id),
        limit,
        user.user_id,
    )
    return SessionMessagesResponse(
        session_id=session_id,
        messages=detail.messages,
        **_public_chat_state(
            active_stock_context=detail.active_stock_context,
            session_state_version=detail.session_state_version,
            session_generation=detail.session_generation,
            selected_skill_ids=detail.selected_skill_ids,
        ),
    )


@router.delete("/chat/sessions/{session_id}")
async def delete_chat_session(
    session_id: str,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """删除指定会话（别人的会话与不存在的会话响应完全相同：deleted 为 0，且不会删除别人的会话）"""
    try:
        session_service.require_session_access(session_id, user.user_id)
    except ChatSessionNotFound:
        return {"deleted": 0}
    return {"deleted": session_service.delete_session(session_id, user.user_id)}


class ChatContextSummaryInfo(BaseModel):
    covered_message_count: int
    estimated_tokens: int
    updated_at: Optional[str] = None


class ChatContextInfoResponse(BaseModel):
    session_id: str
    model: Optional[str] = None
    context_window: Optional[int] = None
    message_count: int
    history_tokens: int
    history_message_count: int
    compression_enabled: bool
    trigger_tokens: int
    protected_turns: int
    compactable_message_count: int
    summary: Optional[ChatContextSummaryInfo] = None


class ChatCompactResponse(BaseModel):
    status: str
    summarized_message_count: int
    tokens_before: int
    tokens_after: int
    context: ChatContextInfoResponse


def _context_info_response(
    session_id: str, config, model: Optional[str], read_session_id: Optional[str] = None,
) -> ChatContextInfoResponse:
    from src.agent.chat_context import describe_chat_context

    info = describe_chat_context(read_session_id or session_id, config)
    effective_model = (model or "").strip() or get_effective_agent_primary_model(config) or None
    return ChatContextInfoResponse(
        session_id=session_id,
        model=effective_model,
        context_window=resolve_model_context_window(config, effective_model),
        message_count=info.message_count,
        history_tokens=info.history_tokens,
        history_message_count=info.history_message_count,
        compression_enabled=info.compression_enabled,
        trigger_tokens=info.trigger_tokens,
        protected_turns=info.protected_turns,
        compactable_message_count=info.compactable_message_count,
        summary=ChatContextSummaryInfo(**info.summary) if info.summary else None,
    )


@router.get("/chat/sessions/{session_id}/context", response_model=ChatContextInfoResponse)
async def get_chat_session_context(
    session_id: str,
    model: Optional[str] = None,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """Conversation context of a session: history tokens, compression state and the model's context window.

    Read-only; calls no model. ``model`` (optional) only selects which model's context window is reported.
    """
    read_session_id = _read_session_id(session_service, session_id, user.user_id)
    config = _config_for(user)
    return await asyncio.to_thread(_context_info_response, session_id, config, model, read_session_id)


class ChatCompactRequest(BaseModel):
    model: Optional[str] = Field(default=None, max_length=200)


@router.post("/chat/sessions/{session_id}/compact", response_model=ChatCompactResponse)
async def compact_chat_session(
    session_id: str,
    request: Optional[ChatCompactRequest] = None,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """Manually fold the session's older messages into its rolling summary (the last protected turns stay verbatim).

    Uses the same summary prompt as automatic compression and works even when
    ``AGENT_CONTEXT_COMPRESSION_ENABLED`` is off; later turns then carry the summary plus newer messages.
    """
    read_session_id = _read_session_id(session_service, session_id, user.user_id)
    config = _config_for(user, need_model=True)
    backend_id = _select_agent_chat_backend(config)
    model = (request.model if request else None) or None
    config = _apply_request_overrides(ChatRequest(message="", model=model), config, backend_id)
    if backend_id == "codex_app_server":
        raise HTTPException(
            status_code=400,
            detail={"error": "capability_unsupported", "message": "Codex Agent does not support manual compaction"},
        )

    def run():
        from src.agent.chat_context import compact_chat_history
        from src.agent.llm_adapter import LLMToolAdapter

        return compact_chat_history(read_session_id, LLMToolAdapter(config), config)

    try:
        result = await asyncio.to_thread(run)
    except ChatSessionStateConflict as exc:
        raise HTTPException(status_code=409, detail={"error": exc.code, "message": str(exc)}) from exc
    if result.status == "failed":
        raise HTTPException(
            status_code=502,
            detail={"error": "compaction_failed", "message": "摘要模型没有返回结果，会话未改动，请稍后重试"},
        )
    context = await asyncio.to_thread(_context_info_response, session_id, config, model, read_session_id)
    return ChatCompactResponse(
        status=result.status,
        summarized_message_count=result.summarized_message_count,
        tokens_before=result.tokens_before,
        tokens_after=result.tokens_after,
        context=context,
    )


class SendChatRequest(BaseModel):
    """Request body for sending chat content to notification channels."""

    content: str = Field(..., min_length=1, max_length=50000)
    title: Optional[str] = None


@router.post("/chat/send")
async def send_chat_to_notification(request: SendChatRequest, user: CurrentUser = Depends(get_current_user)):
    """
    Send chat session content to configured notification channels.
    Uses run_in_executor to avoid blocking the event loop.
    """
    from src.notification import NotificationService

    if user.user_id != get_primary_user_id():
        # Notification channels are the primary user's (.env) until per-user channels exist (P3):
        # another user's chat must never be pushed to them.
        return {
            "success": False,
            "error": "no_channels",
            "message": "未配置通知渠道，请先在设置中配置",
        }
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(
        None,
        lambda: NotificationService().send(request.content),
    )
    if not success:
        return {
            "success": False,
            "error": "no_channels",
            "message": "未配置通知渠道，请先在设置中配置",
        }
    return {"success": True}


def _build_executor(config, skills: Optional[List[str]] = None):
    """Build and return the backend-neutral Chat executor (sync helper)."""
    from src.agent.factory import build_agent_chat_executor

    return build_agent_chat_executor(config, skills=skills)


def _get_agent_chat_status(config) -> Dict[str, Any]:
    from src.services.agent_backend_status_service import AgentBackendStatusService

    return AgentBackendStatusService(config=config).get_status()


def _select_agent_chat_backend(config) -> str:
    """Select the runtime backend without repeating the compatibility probe."""
    from src.services.agent_backend_status_service import evaluate_agent_backend_config

    evaluation = evaluate_agent_backend_config(config)
    if not evaluation["available"]:
        raise HTTPException(
            status_code=400,
            detail={
                "error": evaluation["error_code"],
                "message": evaluation["message"],
            },
        )
    return evaluation["backend"]


async def _run_research_in_background(
    agent,
    question: str,
    context: Optional[Dict[str, Any]],
    *,
    timeout: int,
):
    """Run deep research off the event loop with an internal overall timeout."""
    return await asyncio.to_thread(
        agent.research,
        question,
        context,
        timeout_seconds=timeout,
    )


# ============================================================
# Deep research endpoint
# ============================================================

class ResearchRequest(BaseModel):
    question: str
    stock_code: Optional[str] = None

class ResearchResponse(BaseModel):
    success: bool
    content: str
    sources: List[str] = Field(default_factory=list)
    token_usage: int = 0
    error: Optional[str] = None


@router.post("/research", response_model=ResearchResponse)
async def agent_research(request: ResearchRequest, user: CurrentUser = Depends(get_current_user)):
    """Run a deep-research query via the ResearchAgent.

    Similar to the ``/research`` bot command but exposed as a REST endpoint. Runs with the caller's own
    model / search keys (400 ``model_key_required`` for a non-primary user without a model).
    """
    config = _config_for(user, need_model=True)
    if not config.is_agent_available():
        raise HTTPException(status_code=400, detail="Agent mode is not enabled")

    question = request.question
    context: Optional[Dict[str, Any]] = None
    if request.stock_code:
        question = f"[Stock: {request.stock_code}] {question}"
        context = {"stock_code": request.stock_code}

    try:
        from src.agent.research import ResearchAgent
        from src.agent.factory import get_tool_registry
        from src.agent.llm_adapter import LLMToolAdapter

        registry = get_tool_registry()
        llm_adapter = LLMToolAdapter(config)
        budget = getattr(config, "agent_deep_research_budget", 30000)

        agent = ResearchAgent(
            tool_registry=registry,
            llm_adapter=llm_adapter,
            token_budget=budget,
            **({"user_id": user.user_id} if isinstance(user, CurrentUser) else {}),
        )

        research_timeout = getattr(config, "agent_deep_research_timeout", 180)

        result = await _run_research_in_background(
            agent,
            question,
            context,
            timeout=research_timeout,
        )
        if getattr(result, "timed_out", False):
            logger.warning("Agent research API timed out after %ss", research_timeout)
            return ResearchResponse(
                success=False,
                content="",
                sources=[],
                token_usage=0,
                error=f"Deep research timed out after {research_timeout}s",
            )

        return ResearchResponse(
            success=result.success,
            content=result.report,
            sources=[f"Sub-question {i+1}: {q}" for i, q in enumerate(result.sub_questions)],
            token_usage=result.total_tokens,
            error=result.error if not result.success else None,
        )
    except Exception as e:
        logger.error("Agent research API failed: %s", e)
        logger.exception("Agent research error details:")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/chat/stream")
async def agent_chat_stream(
    request: ChatRequest,
    session_service: AgentChatSessionService = Depends(get_agent_chat_session_service),
    user: CurrentUser = Depends(get_current_user),
):
    """
    Chat with the AI Agent, streaming progress via SSE.
    Each SSE event is a JSON object with a 'type' field:
      - thinking: AI is deciding next action
      - stage_start: an agent or orchestrator stage has begun
      - stage_done: an agent or orchestrator stage finished
      - tool_start: a tool call has begun
      - tool_done: a tool call finished
      - generating: final answer being generated
      - pipeline_timeout: analysis stopped because the stage/pipeline budget expired
      - pipeline_budget_skipped: analysis stopped before an unstarted stage
        because the remaining budget was too low for useful work
      - done: analysis complete, contains 'content' and 'success'
      - error: error occurred, contains 'message'
    """
    config = _config_for(user, need_model=True)
    backend_id = _select_agent_chat_backend(config)
    config = _apply_request_overrides(request, config, backend_id)

    session_id = _chat_session_id(request, user)
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    cancel_event = threading.Event()
    request_id = request.request_id or str(uuid.uuid4())

    stream_key = (user.user_id, request_id)

    if backend_id == "codex_app_server":
        with _ACTIVE_CODEX_STREAMS_LOCK:
            if stream_key in _ACTIVE_CODEX_STREAMS:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "request_conflict",
                        "message": "This Agent request is already running",
                    },
                )
            _ACTIVE_CODEX_STREAMS[stream_key] = cancel_event

    def progress_callback(event: dict):
        if backend_id == "codex_app_server" and cancel_event.is_set():
            return
        # Enrich tool events with display names
        if event.get("type") in ("tool_start", "tool_done"):
            tool = event.get("tool", "")
            event["display_name"] = TOOL_DISPLAY_NAMES.get(tool, tool)
        asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    def run_sync(executor, turn):
        try:
            execute_kwargs = {
                "progress_callback": progress_callback,
            }
            if backend_id == "codex_app_server":
                execute_kwargs["cancel_event"] = cancel_event
            result = executor.execute_turn(
                turn,
                **execute_kwargs,
            )
            event = {
                "type": "done",
                "success": result.success,
                "content": result.content,
                "error": result.error,
                "total_steps": result.total_steps,
                "session_id": session_id,
            }
            event.update({
                "backend": getattr(result, "backend", "") or backend_id,
                "error_code": getattr(result, "error_code", None),
                "request_id": request_id,
            })
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)
        except Exception as exc:
            logger.error("Agent stream error: %s", exc)
            event = {
                "type": "error",
                "message": "Agent Chat failed" if backend_id == "codex_app_server" else str(exc),
                "error_code": getattr(exc, "code", "unknown_backend_error"),
                "backend": backend_id,
                "request_id": request_id,
                "session_id": session_id,
            }
            asyncio.run_coroutine_threadsafe(queue.put(event), loop)

    async def event_generator():
        fut = None
        try:
            try:
                executor, turn, accepted, clarification = await asyncio.to_thread(
                    _prepare_chat_request, request, config, session_id, session_service, user.user_id,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error("Agent request preparation failed: %s", exc, exc_info=True)
                known = isinstance(exc, (ChatSessionStateConflict, ChatSessionNotFound))
                event = {
                    "type": "error",
                    "message": str(exc) if known else "Agent request was not accepted",
                    "error_code": exc.code if known else "request_not_accepted",
                    "backend": backend_id,
                    "request_id": request_id,
                    "session_id": session_id,
                }
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                return

            accepted_event = {
                "type": "accepted",
                "backend": backend_id,
                "request_id": request_id,
                "session_id": session_id,
                **_accepted_state_payload(accepted),
            }
            yield "data: " + json.dumps(accepted_event, ensure_ascii=False) + "\n\n"

            # Backend execution starts only after the accepted event has been
            # yielded, so Web state and server persistence share one commit point.
            if clarification is not None:
                result = await asyncio.to_thread(_clarification_result, session_service, accepted, clarification, backend_id)
                event = {"type": "done", "success": result.success, "content": result.content,
                         "error": None, "total_steps": 0, "session_id": session_id,
                         "backend": backend_id, "request_id": request_id, "error_code": None}
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                return
            fut = loop.run_in_executor(None, run_sync, executor, turn)
            idle_seconds = 0.0
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=_STREAM_HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    idle_seconds += _STREAM_HEARTBEAT_SECONDS
                    # Codex owns one authoritative backend deadline; a second API
                    # timeout would race it and could emit a terminal event before
                    # process cleanup finishes.
                    if backend_id != "codex_app_server" and idle_seconds >= _STREAM_IDLE_TIMEOUT_SECONDS:
                        event = {"type": "error", "message": "分析超时"}
                        yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                        break
                    # SSE comment: keeps proxies (Cloudflare drops a response after
                    # ~100 s without bytes) from closing the stream during one long
                    # model call; clients ignore lines that start with ':'.
                    yield ": keepalive\n\n"
                    continue
                idle_seconds = 0.0
                yield "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"
                if event.get("type") in ("done", "error"):
                    break
        finally:
            if backend_id == "codex_app_server" and (fut is None or not fut.done()):
                cancel_event.set()
            try:
                if backend_id == "codex_app_server" and fut is not None:
                    while not fut.done():
                        try:
                            await asyncio.shield(fut)
                        except asyncio.CancelledError:
                            # Client disconnect cancellation must not abandon the
                            # owned Codex/tool worker before it actually exits.
                            cancel_event.set()
                    if not fut.cancelled():
                        fut.result()
                elif fut is not None:
                    await asyncio.wait_for(fut, timeout=5.0)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                # Cleanup taking longer than 5s is treated as an expected timeout; no warning.
                logger.debug("agent executor cleanup timed out after 5s for session %s", session_id)
            except Exception as exc:
                logger.warning("agent executor cleanup error (ignored): %s", exc, exc_info=True)
            finally:
                if backend_id == "codex_app_server":
                    with _ACTIVE_CODEX_STREAMS_LOCK:
                        if _ACTIVE_CODEX_STREAMS.get(stream_key) is cancel_event:
                            _ACTIVE_CODEX_STREAMS.pop(stream_key, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.post("/chat/stream/{request_id}/cancel")
async def cancel_agent_chat_stream(request_id: str, user: CurrentUser = Depends(get_current_user)):
    """Signal cancellation while the original Codex SSE remains open (own streams only)."""
    with _ACTIVE_CODEX_STREAMS_LOCK:
        cancel_event = _ACTIVE_CODEX_STREAMS.get((user.user_id, request_id))
    if cancel_event is None:
        raise HTTPException(
            status_code=404,
            detail={
                "error": "request_not_active",
                "message": "This Agent request is no longer running",
            },
        )
    cancel_event.set()
    return {"accepted": True, "request_id": request_id}
