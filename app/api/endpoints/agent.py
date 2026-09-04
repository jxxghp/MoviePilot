import json
import mimetypes
import uuid
from typing import Any, AsyncIterator, Optional
from urllib.parse import unquote

from fastapi import (
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse

from app.agent.mcp import agent_mcp_manager
from app.api.dependencies.agent import (
    get_agent_chat_persistence,
    get_agent_chat_service,
)
from app.api.dependencies.auth import get_current_active_user
from app.api.presentation.sse import build_sse_response
from app.api.principal import ApiPrincipal
from app.api.response import (
    CompatibleCountParam,
    CompatiblePageParam,
    ResponseAPIRouter,
)
from app.application import agent as agent_application
from app.application.messaging import agent as web_agent_application
from app.application.messaging.agent import (
    parse_agent_choice_callback,
)
from app.application.messaging.chat import (
    AgentChatPersistenceService,
    AgentChatService,
    get_configured_agent_chat_persistence,
    get_configured_agent_chat_service,
)
from app.runtime.events import eventmanager
from app.runtime.localization import LocaleHelper
from app.runtime.log import logger
from app.schemas.agent import AgentChatDisplaySaveRequest as _SchemaAgentChatDisplaySaveRequest
from app.schemas.agent import AgentChatSessionDetail as _SchemaAgentChatSessionDetail
from app.schemas.agent import AgentChatSessionSummary as _SchemaAgentChatSessionSummary
from app.schemas.agent import AgentChatUploadAttachment as _SchemaAgentChatUploadAttachment
from app.schemas.agent import AgentCommandRunData as _SchemaAgentCommandRunData
from app.schemas.agent import AgentCommandRunRequest as _SchemaAgentCommandRunRequest
from app.schemas.agent import AgentMcpServerListData as _SchemaAgentMcpServerListData
from app.schemas.agent import AgentMcpServersSaveRequest as _SchemaAgentMcpServersSaveRequest
from app.schemas.agent import AgentMcpServerTestRequest as _SchemaAgentMcpServerTestRequest
from app.schemas.agent import AgentMcpServerTestResult as _SchemaAgentMcpServerTestResult
from app.schemas.agent import AgentSessionStopData as _SchemaAgentSessionStopData
from app.schemas.agent import AgentWebCallbackData as _SchemaAgentWebCallbackData
from app.schemas.agent import AgentWebCommandInfo as _SchemaAgentWebCommandInfo
from app.schemas.message import AgentWebChatRequest as _SchemaAgentWebChatRequest
from app.schemas.message import AgentWebChoiceRequest as _SchemaAgentWebChoiceRequest
from app.schemas.response import Response as _SchemaResponse
from app.schemas.types import NotificationChannel

router = ResponseAPIRouter()


def _ensure_superuser(user: ApiPrincipal) -> None:
    """校验当前用户是否为超级管理员。"""
    if not getattr(user, "is_superuser", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


def _parse_agent_channel_header(value: Optional[str]) -> Optional[NotificationChannel]:
    """解析 Agent API 的 ASCII 渠道名，并兼容旧版本直接传递的枚举值。"""
    if not value:
        return None
    try:
        return NotificationChannel[value]
    except KeyError:
        try:
            return NotificationChannel(value)
        except ValueError:
            try:
                return NotificationChannel(unquote(value))
            except ValueError:
                return None


@router.get(
    "/mcp/servers",
    summary="查询 Agent MCP 服务器配置",
    response_model=_SchemaResponse[_SchemaAgentMcpServerListData],
)
async def list_agent_mcp_servers(
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    查询 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    servers = agent_mcp_manager.get_servers()
    enabled_count = len([server for server in servers if server.enabled])
    return _SchemaResponse(
        success=True,
        data={
            "servers": [server.model_dump() for server in servers],
            "enabled_count": enabled_count,
            "total_count": len(servers),
        },
    )


@router.post(
    "/mcp/servers",
    summary="保存 Agent MCP 服务器配置",
    response_model=_SchemaResponse[None],
)
async def save_agent_mcp_servers(
    request: _SchemaAgentMcpServersSaveRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    保存 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    success = await agent_mcp_manager.save_servers(request.servers)
    return _SchemaResponse(
        success=success,
        message="保存MCP配置成功" if success else "保存MCP配置失败",
    )


@router.post(
    "/mcp/servers/test",
    summary="测试 Agent MCP 服务器",
    response_model=_SchemaResponse[_SchemaAgentMcpServerTestResult],
)
async def test_agent_mcp_server(
    request: _SchemaAgentMcpServerTestRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    测试 Agent 外部 MCP 服务器连接并读取工具列表。
    """
    _ensure_superuser(current_user)
    try:
        result = await agent_mcp_manager.test_server(request.server)
        return _SchemaResponse(
            success=result.success,
            message=result.message,
            data=result.model_dump(),
        )
    except Exception as err:
        logger.warning(f"测试 Agent MCP 服务器失败: {err}", exc_info=True)
        return _SchemaResponse(
            success=False,
            message="MCP 服务测试失败，请检查服务配置后重试",
            data={
                "success": False,
                "message": "MCP 服务测试失败，请检查服务配置后重试",
                "tools": [],
                "tool_count": 0,
            },
        )


def _build_web_agent_sse(
    event_type: str,
    data: Optional[dict[str, Any]] = None,
    locale: Optional[str] = None,
) -> str:
    """
    构建 Web Agent SSE 消息。

    :param event_type: 前端事件类型
    :param data: 事件数据
    :param locale: 当前请求语言
    :return: 符合 SSE 格式的字符串
    """
    if event_type == "interaction-protected":
        return f"event: interaction-protected\ndata: {json.dumps(data or {}, ensure_ascii=False)}\n\n"
    payload = {"type": event_type, **(data or {})}
    message = payload.get("message")
    if event_type == "error" and isinstance(message, str):
        payload["message_i18n"] = LocaleHelper.translate_text(message, locale=locale)
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


@router.get(
    "/file/{file_id}",
    summary="下载 Web 智能助手附件",
    response_model=None,
    response_class=FileResponse,
    responses={
        200: {
            "description": "Agent 附件文件",
            "content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}},
        }
    },
)
async def download_web_agent_file(file_id: str) -> FileResponse:
    """
    下载 Web 智能助手本轮生成的临时附件。

    :param file_id: 附件随机标识
    :return: 附件文件响应
    """
    file_info = web_agent_application.get_web_agent_registered_file(f"message/agent/file/{file_id}")
    if not file_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    file_path = file_info["path"]
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    return FileResponse(
        path=file_path,
        media_type=file_info.get("mime_type") or "application/octet-stream",
        filename=file_info.get("name") or file_path.name,
    )


@router.post(
    "/upload",
    summary="上传 Web 智能助手附件",
    response_model=_SchemaResponse[_SchemaAgentChatUploadAttachment],
)
async def upload_web_agent_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    上传 Web 智能助手对话附件。

    :param file: 浏览器选择的文件
    :param session_id: 前端会话标识
    :param current_user: 当前登录用户
    :return: Agent 可消费的附件描述
    """
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    safe_name = web_agent_application.sanitize_web_agent_upload_name(file.filename, mime_type)
    upload_dir = await web_agent_application.get_web_agent_upload_dir(current_user, session_id, service)
    target_path = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    try:
        size = await web_agent_application.save_web_agent_upload(file, target_path)
    except web_agent_application.WebAgentUploadTooLargeError as err:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=str(err),
        ) from err
    attachment = web_agent_application.register_web_agent_file(
        str(target_path),
        file_name=safe_name,
        kind=web_agent_application.guess_web_agent_attachment_kind(mime_type),
        mime_type=mime_type,
    )
    if not attachment:
        target_path.unlink(missing_ok=True)
        return _SchemaResponse(success=False, message="附件保存失败")

    attachment.update(
        {
            "ref": attachment["url"],
            "local_path": str(target_path),
            "status": "ready",
            "size": size,
        }
    )
    return _SchemaResponse(success=True, data=attachment)


@router.post(
    "/callback",
    summary="Web 智能助手按钮回调",
    response_model=_SchemaResponse[_SchemaAgentWebCallbackData],
)
async def web_agent_callback(
    payload: _SchemaAgentWebChoiceRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
) -> _SchemaResponse:
    """
    接收 Web 智能助手选择卡片回调。

    :param payload: 按钮选择请求
    :param current_user: 当前登录用户
    :return: 下一条需要发送给 Agent 的用户消息与卡片反馈
    """
    if not parse_agent_choice_callback(payload.callback_data):
        denied_message = web_agent_application.ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return _SchemaResponse(success=False, message=denied_message)
        return _SchemaResponse(
            success=True,
            data=web_agent_application.build_web_agent_traditional_callback_payload(
                payload.callback_data,
                original_message_id=payload.original_message_id,
                original_chat_id=payload.original_chat_id,
            ),
        )

    result = web_agent_application.resolve_web_agent_choice_payload(
        callback_data=payload.callback_data,
        user_id=str(current_user.id),
    )
    if not result:
        return _SchemaResponse(success=False, message="该选择已失效，请重新发起选择")
    return _SchemaResponse(success=True, data=result)


@router.get(
    "/commands",
    summary="获取 Web 智能助手可用命令",
    response_model=_SchemaResponse[list[_SchemaAgentWebCommandInfo]],
)
async def list_web_agent_commands(
    current_user: ApiPrincipal = Depends(get_current_active_user),
    page: CompatiblePageParam = None,
    count: CompatibleCountParam = None,
) -> _SchemaResponse:
    """
    获取当前 Web 智能助手可补全的斜杠命令。

    :param current_user: 当前登录用户
    :return: 可用命令列表
    """
    denied_message = web_agent_application.ensure_web_agent_command_allowed(current_user)
    if denied_message:
        return _SchemaResponse(success=False, message=denied_message)
    return _SchemaResponse(
        success=True,
        data=web_agent_application.build_web_agent_command_items(),
    )


@router.post(  # type: ignore[misc]
    "/commands/run",
    summary="执行 Agent 斜杠命令",
    response_model=_SchemaResponse[_SchemaAgentCommandRunData],
)
async def run_agent_command(
    payload: _SchemaAgentCommandRunRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    agent_channel: Optional[str] = Header(None, alias="X-MoviePilot-Agent-Channel"),
    agent_source: Optional[str] = Header(None, alias="X-MoviePilot-Agent-Source"),
) -> _SchemaResponse[Any]:
    """以当前认证用户和宿主透传渠道触发已注册命令。"""
    _ensure_superuser(current_user)
    channel = _parse_agent_channel_header(agent_channel)
    agent_source = unquote(agent_source) if agent_source else None
    try:
        data = web_agent_application.dispatch_command(
            payload.command,
            user_id=str(current_user.id),
            channel=channel,
            source=agent_source,
            publish_event=eventmanager.send_event,
        )
    except ValueError as error:
        return _SchemaResponse(success=False, message=str(error))
    return _SchemaResponse(success=True, message=data.get("message"), data=data)


@router.get(
    "/sessions",
    summary="获取 Agent 历史会话",
    response_model=_SchemaResponse[list[_SchemaAgentChatSessionSummary]],
)
async def list_agent_chat_sessions(
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
    page: int = 1,
    count: int = 30,
) -> _SchemaResponse:
    """
    获取当前用户可访问的 Agent 历史会话列表。

    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :param page: 页码
    :param count: 每页数量
    :return: 会话摘要列表
    """
    chats = await service.list(
        current_user,
        page=page,
        count=count,
    )
    return _SchemaResponse(success=True, data=chats)


@router.get(
    "/sessions/{session_id}",
    summary="获取 Agent 历史会话详情",
    response_model=_SchemaResponse[_SchemaAgentChatSessionDetail],
)
async def get_agent_chat_session(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    获取一条 Agent 历史会话详情。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 会话详情
    """
    chat = await web_agent_application.get_accessible_agent_chat(service, session_id, current_user)
    server_session_id = session_id
    if not chat:
        server_session_id = await web_agent_application.build_web_agent_session_id_async(
            current_user,
            session_id,
            service,
        )
        if server_session_id != session_id:
            chat = await web_agent_application.get_accessible_agent_chat(
                service,
                server_session_id,
                current_user,
            )
    if not chat:
        manager = agent_application.get_running_agent_manager()
        if manager and manager.is_session_busy(server_session_id):
            return _SchemaResponse(
                success=True,
                data={
                    "session_id": server_session_id,
                    "client_session_id": session_id,
                    "messages": [],
                    "is_processing": True,
                },
            )
        return _SchemaResponse(success=False, message="会话不存在或无权访问")
    data = service.to_detail(chat).model_dump()
    manager = agent_application.get_running_agent_manager()
    data["is_processing"] = bool(manager and manager.is_session_busy(chat.session_id))
    return _SchemaResponse(success=True, data=data)


@router.put(
    "/sessions/{session_id}/display",
    summary="保存 Agent 展示会话",
    response_model=_SchemaResponse[_SchemaAgentChatSessionSummary],
)
async def save_agent_chat_display(
    session_id: str,
    payload: _SchemaAgentChatDisplaySaveRequest,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
    persistence: AgentChatPersistenceService = Depends(get_agent_chat_persistence),
) -> _SchemaResponse:
    """
    保存前端聚合后的 Agent 展示消息。

    :param session_id: Agent 会话 ID
    :param payload: 展示消息保存请求
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 保存后的会话摘要
    """
    existing_chat = await service.get_accessible(session_id, current_user)
    if existing_chat is None:
        unrestricted_chat = await service.get(session_id)
    else:
        unrestricted_chat = existing_chat
    if unrestricted_chat and existing_chat is None:
        return _SchemaResponse(success=False, message="会话不存在或无权访问")

    messages = [message.model_dump(exclude_none=True) for message in payload.messages]
    await web_agent_application.save_web_agent_display_snapshot(
        session_id=session_id,
        current_user=current_user,
        messages=messages,
        client_session_id=existing_chat.client_session_id if existing_chat else session_id,
        service=service,
        persistence=persistence,
    )
    # 写入由独立 worker 事务完成，使用组合根登记的短会话服务复读，避免请求会话
    # 的 identity map 返回写入前的 ORM 快照。
    chat_service = get_configured_agent_chat_service()
    chat = await chat_service.get_accessible(session_id, current_user)
    if not chat:
        return _SchemaResponse(success=False, message="会话保存失败")
    return _SchemaResponse(success=True, data=chat_service.to_summary(chat))


@router.delete(
    "/sessions/{session_id}",
    summary="删除 Agent 历史会话",
    response_model=_SchemaResponse[None],
)
async def delete_agent_chat_session(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    删除一条 Agent 历史会话。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 删除结果
    """
    chat = await web_agent_application.get_accessible_agent_chat(service, session_id, current_user)
    if not chat:
        return _SchemaResponse(success=False, message="会话不存在或无权访问")
    deleted = await service.delete(session_id, current_user)
    return _SchemaResponse(success=deleted, message="删除成功" if deleted else "删除失败")


@router.post(
    "/sessions/{session_id}/stop",
    summary="停止 Web 智能助手当前任务",
    response_model=_SchemaResponse[_SchemaAgentSessionStopData],
)
async def stop_web_agent_session_task(
    session_id: str,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: AgentChatService = Depends(get_agent_chat_service),
) -> _SchemaResponse:
    """
    停止当前 Web 智能助手会话正在执行的任务。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param service: Agent 会话应用服务
    :return: 停止结果
    """
    server_session_id = await web_agent_application.build_web_agent_session_id_async(
        current_user,
        session_id,
        service,
    )
    chat = await web_agent_application.get_accessible_agent_chat(
        service,
        server_session_id,
        current_user,
    )
    if not chat and server_session_id != session_id:
        chat = await web_agent_application.get_accessible_agent_chat(service, session_id, current_user)
    if chat and not web_agent_application.can_access_agent_chat(chat, current_user):
        return _SchemaResponse(success=False, message="会话不存在或无权访问")

    manager = agent_application.get_running_agent_manager()
    stopped = await manager.stop_current_task(server_session_id) if manager else False
    return _SchemaResponse(
        success=True,
        data={"stopped": stopped},
        message="已停止" if stopped else "当前没有正在执行的任务",
    )


async def _web_agent_stream_impl(
    payload: _SchemaAgentWebChatRequest,
    request: Request,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    service: Optional[AgentChatService] = None,
    persistence: Optional[AgentChatPersistenceService] = None,
) -> StreamingResponse:
    """把 FastAPI 对话请求映射为 Application 事件流并完成 SSE framing。"""
    if not isinstance(service, AgentChatService):
        service = get_configured_agent_chat_service()
    if not isinstance(persistence, AgentChatPersistenceService):
        persistence = get_configured_agent_chat_persistence()

    async def is_disconnected() -> bool:
        """读取当前 HTTP 连接状态；直接函数调用时按未断线处理。"""
        callback = getattr(request, "is_disconnected", None)
        return bool(await callback()) if callable(callback) else False

    result = await web_agent_application.build_web_agent_stream(
        web_agent_application.WebAgentStreamCommand(
            text=payload.text,
            display_text=payload.display_text,
            session_id=payload.session_id,
            images=list(payload.images or []),
            audio_refs=list(payload.audio_refs or []),
            files=[file.model_dump(exclude_none=True) for file in (payload.files or [])],
            choice_selection=payload.choice_selection,
            original_message_id=payload.original_message_id,
            original_chat_id=payload.original_chat_id,
            echo_user=payload.echo_user,
        ),
        current_user=current_user,
        is_disconnected=is_disconnected,
        protected_transport_supported=(getattr(request, "headers", {}).get("X-MoviePilot-Agent-Interaction") == "1"),
        service=service,
        persistence=persistence,
    )
    locale = LocaleHelper.get_locale_from_request(request)

    async def sse_events() -> AsyncIterator[str]:
        """把 Application 事件编码为浏览器可消费的 SSE 帧。"""
        async for event in result.events:
            event_payload = dict(event)
            event_type = str(event_payload.pop("type", ""))
            if event_type == "heartbeat":
                yield ": heartbeat\n\n"
                continue
            yield _build_web_agent_sse(
                event_type,
                event_payload,
                locale=locale,
            )

    return build_sse_response(
        sse_events(),
        headers=({"X-MoviePilot-Agent-Control": result.control} if result.control else None),
    )


@router.post(
    "/stream",
    summary="Web智能助手流式对话",
    response_model=None,
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Agent SSE 事件流",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        }
    },
)
async def web_agent_stream(
    payload: _SchemaAgentWebChatRequest,
    request: Request,
    current_user: ApiPrincipal = Depends(get_current_active_user),
    persistence: AgentChatPersistenceService = Depends(get_agent_chat_persistence),
) -> StreamingResponse:
    """Web 智能助手流式对话的稳定公开路由入口。"""
    return await _web_agent_stream_impl(
        payload,
        request,
        current_user,
        persistence=persistence,
    )
