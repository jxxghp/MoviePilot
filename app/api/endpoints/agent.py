import asyncio
import copy
import hashlib
import json
import mimetypes
import shutil
import subprocess
import time
import uuid
from queue import Empty, Queue
from pathlib import Path
from threading import Lock
from typing import Any, AsyncIterator, Callable, Optional, Union

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import schemas
from app.agent import MoviePilotAgent, ReplyMode, StreamingHandler, agent_manager
from app.agent.llm.capability import AgentCapabilityManager
from app.agent.mcp import agent_mcp_manager
from app.chain.message import MessageChain
from app.chain.site import site_interaction_manager
from app.chain.skills import skills_interaction_manager
from app.chain.subscribe import subscribe_interaction_manager
from app.command import Command
from app.core.config import global_vars, settings
from app.core.event import Event, EventManager
from app.db import get_async_db
from app.db.agentchat_oper import AgentChatOper
from app.db.models import User
from app.db.models.agentchat import AgentChat
from app.db.user_oper import UserOper, get_current_active_user
from app.helper.agent import attach_web_agent_edit_queue, detach_web_agent_edit_queue
from app.helper.interaction import agent_interaction_manager, media_interaction_manager
from app.helper.locale import LocaleHelper
from app.log import logger
from app.schemas.types import EventType, MessageChannel

router = APIRouter()

WEB_AGENT_SESSION_PREFIX = "web-agent:"
WEB_AGENT_SOURCE = "web-agent"
WEB_AGENT_FILE_TTL_SECONDS = 6 * 60 * 60
WEB_AGENT_FILE_MAX_ITEMS = 256
WEB_AGENT_UPLOAD_MAX_BYTES = 32 * 1024 * 1024
WEB_AGENT_UPLOAD_CHUNK_SIZE = 1024 * 1024
WEB_AGENT_BROWSER_AUDIO_SUFFIXES = {".aac", ".m4a", ".mp3", ".mp4", ".wav", ".wave"}
WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS = 2.0
WEB_AGENT_TRADITIONAL_MAX_WAIT_SECONDS = 60.0
_WEB_AGENT_FILE_REGISTRY: dict[str, dict[str, Any]] = {}
_WEB_AGENT_NOTICE_QUEUES: dict[str, list[Queue[schemas.Notification]]] = {}
_WEB_AGENT_NOTICE_LOCK = Lock()
_WEB_AGENT_NOTICE_LISTENER_REGISTERED = False
_WEB_AGENT_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _ensure_superuser(user: User) -> None:
    """校验当前用户是否为超级管理员。"""
    if not getattr(user, "is_superuser", False):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Forbidden")


@router.get("/mcp/servers", summary="查询 Agent MCP 服务器配置", response_model=schemas.Response)
async def list_agent_mcp_servers(
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    查询 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    servers = agent_mcp_manager.get_servers()
    enabled_count = len([server for server in servers if server.enabled])
    return schemas.Response(
        success=True,
        data={
            "servers": [server.model_dump() for server in servers],
            "enabled_count": enabled_count,
            "total_count": len(servers),
        },
    )


@router.post("/mcp/servers", summary="保存 Agent MCP 服务器配置", response_model=schemas.Response)
async def save_agent_mcp_servers(
    request: schemas.AgentMcpServersSaveRequest,
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    保存 Agent 外部 MCP 服务器配置。
    """
    _ensure_superuser(current_user)
    success = await agent_mcp_manager.save_servers(request.servers)
    return schemas.Response(
        success=success,
        message="保存MCP配置成功" if success else "保存MCP配置失败",
    )


@router.post("/mcp/servers/test", summary="测试 Agent MCP 服务器", response_model=schemas.Response)
async def test_agent_mcp_server(
    request: schemas.AgentMcpServerTestRequest,
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    测试 Agent 外部 MCP 服务器连接并读取工具列表。
    """
    _ensure_superuser(current_user)
    try:
        result = await agent_mcp_manager.test_server(request.server)
        return schemas.Response(
            success=result.success,
            message=result.message,
            data=result.model_dump(),
        )
    except Exception as err:
        logger.warning(f"测试 Agent MCP 服务器失败: {err}")
        return schemas.Response(
            success=False,
            message=f"测试MCP服务器失败: {str(err)}",
            data={
                "success": False,
                "message": str(err),
                "tools": [],
                "tool_count": 0,
            },
        )


class _WebAgentStreamingHandler(StreamingHandler):
    """
    Web 前端专用流式处理器，将工具提示和文本统一回调给 SSE。
    """

    def __init__(self, on_emit: Callable[[str], None]) -> None:
        super().__init__()
        self._on_emit = on_emit

    def set_emit_callback(self, on_emit: Callable[[str], None]) -> None:
        """
        更新流式输出回调，复用 WebAgent 实例时指向当前 SSE 请求。

        :param on_emit: 当前请求的输出回调
        """
        self._on_emit = on_emit

    def emit(self, token: str) -> str:
        """追加 token 并同步通知 SSE 生产者。"""
        emitted = super().emit(token)
        if emitted:
            self._on_emit(emitted)
        return emitted

    def flush_pending_tool_summary(self) -> str:
        """输出延迟聚合的工具摘要。"""
        emitted = super().flush_pending_tool_summary()
        if emitted:
            self._on_emit(emitted)
        return ""

    async def start_streaming(
        self,
        channel: Optional[str] = None,
        source: Optional[str] = None,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        original_message_id: Optional[str] = None,
        original_chat_id: Optional[str] = None,
        title: str = "",
    ) -> None:
        """Web SSE 自身负责外发，不启动消息模块编辑循环。"""
        self._channel = channel
        self._source = source
        self._user_id = user_id
        self._username = username
        self._original_message_id = original_message_id
        self._original_chat_id = original_chat_id
        self._title = title
        self._streaming_enabled = True
        self._sent_text = ""
        self._message_response = None
        self._msg_start_offset = 0
        self._pending_tool_stats = {}

    async def stop_streaming(self) -> tuple[bool, str]:
        """停止 Web SSE 流式状态，保留缓冲区给 Agent 收口逻辑去重。"""
        if not self._streaming_enabled:
            return False, ""
        self._streaming_enabled = False
        self.flush_pending_tool_summary()
        with self._lock:
            self._sent_text = ""
            self._message_response = None
            self._msg_start_offset = 0
            self._pending_tool_stats = {}
        return False, ""

    @property
    def is_auto_flushing(self) -> bool:
        """让工具执行提示进入缓冲区，由 SSE 回调负责外发。"""
        return True


class _WebAgentMoviePilotAgent(MoviePilotAgent):
    """
    Web 前端专用 Agent，强制使用流式推理。
    """

    def __init__(
        self,
        *args: Any,
        notification_callback: Optional[Callable[[schemas.Notification], None]] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._notification_callback = notification_callback
        self.stream_handler = _WebAgentStreamingHandler(self._emit_output)

    def _should_stream(self) -> bool:
        """Web 面板需要实时输出，即使 Web 渠道本身不支持消息编辑。"""
        return True

    def set_notification_callback(
            self,
            notification_callback: Optional[Callable[[schemas.Notification], None]],
    ) -> None:
        """
        更新 Web SSE 通知回调，复用 Agent 实例时指向当前请求队列。

        :param notification_callback: 当前请求的 Web 通知回调
        """
        self._notification_callback = notification_callback

    def set_output_callback(self, output_callback: Optional[Callable[[str], None]]) -> None:
        """
        更新 Web SSE 输出回调，复用 Agent 实例时指向当前请求队列。

        :param output_callback: 当前请求的输出回调
        """
        self.output_callback = output_callback
        if output_callback and isinstance(self.stream_handler, _WebAgentStreamingHandler):
            self.stream_handler.set_emit_callback(self._emit_output)

    async def _is_system_admin_context(self) -> bool:
        """Web Agent 根据当前登录用户 ID 判断工具管理员上下文。"""
        if not self.user_id:
            return False
        try:
            user = await UserOper().async_get_by_id(int(self.user_id))
        except (TypeError, ValueError):
            return False
        except Exception as e:
            logger.error(f"检查 Web Agent 用户管理员身份失败: {e}")
            return False
        return bool(user and user.is_superuser)

    async def _build_tool_context(self, should_dispatch_reply: bool) -> dict[str, object]:
        """向工具上下文注入 Web SSE 通知回调。"""
        context = await super()._build_tool_context(should_dispatch_reply)
        context["notification_callback"] = self._notification_callback
        return context

    def _handle_stream_text(self, text: str) -> None:
        """文本输出交由 Web 流式处理器统一回调，避免重复增量。"""
        self.stream_handler.emit(text)


def _build_web_agent_session_id(user: User, session_id: Optional[str]) -> str:
    """
    构建前端 Agent 会话 ID。

    :param user: 当前登录用户
    :param session_id: 前端传入的会话标识
    :return: 可用于 Agent 记忆隔离的服务端会话 ID
    """
    seed = str(session_id or "").strip() or uuid.uuid4().hex
    if seed.startswith(WEB_AGENT_SESSION_PREFIX):
        return seed
    try:
        existing_chat = AgentChatOper().get(session_id=seed)
        if existing_chat and _can_access_agent_chat(existing_chat, user):
            return seed
    except Exception as e:
        logger.debug(f"读取WebAgent历史会话失败: {e}")
    user_part = user.name or str(user.id)
    digest = hashlib.sha256(f"{user_part}:{seed}".encode("utf-8")).hexdigest()
    return f"{WEB_AGENT_SESSION_PREFIX}{digest[:32]}"


def _can_access_agent_chat(chat: AgentChat, user: User) -> bool:
    """
    判断当前登录用户是否可以访问指定 Agent 会话。

    超级用户可查看所有渠道历史；普通用户仅能查看 user_id 或 username 匹配自己的会话。
    """
    if not chat or not user:
        return False
    if getattr(user, "is_superuser", False):
        return True
    user_id = str(user.id)
    username = str(user.name or "")
    return chat.user_id == user_id or (bool(username) and chat.username == username)


async def _get_accessible_agent_chat(
    oper: AgentChatOper, session_id: str, user: User
) -> Optional[AgentChat]:
    """
    读取当前用户可访问的 Agent 会话。
    """
    chat = await oper.async_get(session_id=session_id)
    if not chat or not _can_access_agent_chat(chat, user):
        return None
    return chat


def _apply_web_agent_display_event(event: dict, assistant_message: dict) -> None:
    """
    将 WebAgent SSE 事件同步应用到服务端展示消息快照。
    """
    event_type = event.get("type")
    if event_type == "delta":
        assistant_message["content"] += event.get("content") or ""
    elif event_type == "tool":
        for tool in assistant_message["tools"]:
            tool["status"] = "done"
        assistant_message["tools"].append(
            {
                "id": f"tool-{uuid.uuid4().hex}",
                "message": str(event.get("message") or "").strip(),
                "status": "running",
            }
        )
    elif event_type == "attachment" and event.get("attachment"):
        assistant_message["attachments"].append(event["attachment"])
    elif event_type == "choice" and event.get("choice"):
        assistant_message["choices"].append({**event["choice"], "status": "pending"})
    elif event_type == "message_update":
        target_message = event.get("target_message") or {}
        assistant_message["id"] = target_message.get("id") or assistant_message.get("id")
        assistant_message["content"] = target_message.get("content") or ""
        assistant_message["attachments"] = target_message.get("attachments") or []
        assistant_message["choices"] = target_message.get("choices") or []
        assistant_message["tools"] = target_message.get("tools") or []
        assistant_message["status"] = target_message.get("status") or "done"
    elif event_type == "error":
        assistant_message["status"] = "error"
        assistant_message["content"] = (
            assistant_message["content"]
            or event.get("message")
            or "智能助手响应失败"
        )
        for tool in assistant_message["tools"]:
            tool["status"] = "done"
    elif event_type == "done":
        if assistant_message.get("status") != "error":
            assistant_message["status"] = "done"
        for tool in assistant_message["tools"]:
            tool["status"] = "done"


def _save_web_agent_display_snapshot(
    *,
    session_id: str,
    current_user: User,
    messages: list[dict],
    client_session_id: Optional[str] = None,
) -> None:
    """
    保存 WebAgent 当前展示消息快照。
    """
    try:
        oper = AgentChatOper()
        existing_chat = oper.get(session_id=session_id)
        AgentChatOper().save_display_messages(
            session_id=session_id,
            user_id=(existing_chat.user_id if existing_chat else str(current_user.id)),
            username=(existing_chat.username if existing_chat else current_user.name),
            channel=(
                existing_chat.channel
                if existing_chat and existing_chat.channel
                else MessageChannel.WebAgent
            ),
            source=(
                existing_chat.source
                if existing_chat and existing_chat.source
                else WEB_AGENT_SOURCE
            ),
            original_chat_id=existing_chat.original_chat_id if existing_chat else None,
            client_session_id=(
                existing_chat.client_session_id
                if existing_chat and existing_chat.client_session_id
                else client_session_id
            ),
            messages=messages,
        )
    except Exception as e:
        logger.debug(f"保存WebAgent展示历史失败: {e}")


def _build_web_agent_sse(
        event_type: str,
        data: Optional[dict] = None,
        locale: Optional[str] = None,
) -> str:
    """
    构建 Web Agent SSE 消息。

    :param event_type: 前端事件类型
    :param data: 事件数据
    :param locale: 当前请求语言
    :return: 符合 SSE 格式的字符串
    """
    payload = {"type": event_type, **(data or {})}
    message = payload.get("message")
    if event_type == "error" and isinstance(message, str):
        payload["message_i18n"] = LocaleHelper.translate_text(
            message, locale=locale
        )
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _sanitize_web_agent_upload_name(
    filename: Optional[str], mime_type: Optional[str] = None
) -> str:
    """
    规范化 Web Agent 上传文件名，避免路径穿越和空文件名。

    :param filename: 浏览器上传的原始文件名
    :param mime_type: 浏览器上报的 MIME 类型
    :return: 可安全落盘的文件名
    """
    name = Path(filename or "attachment").name.strip()
    safe_name = "".join(
        char for char in name if char.isalnum() or char in (" ", ".", "_", "-")
    ).strip(" .")
    if not safe_name:
        safe_name = "attachment"
    if "." not in safe_name:
        suffix = mimetypes.guess_extension(mime_type or "") or ""
        safe_name = f"{safe_name}{suffix}"
    return safe_name


def _get_web_agent_upload_dir(user: User, session_id: Optional[str]) -> Path:
    """
    计算当前 Web Agent 会话的临时附件目录。

    :param user: 当前登录用户
    :param session_id: 前端会话标识
    :return: 已创建的临时附件目录
    """
    server_session_id = _build_web_agent_session_id(user, session_id)
    safe_session_id = server_session_id.replace(":", "_")
    upload_dir = settings.TEMP_PATH / "agent_uploads" / safe_session_id
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir


async def _save_web_agent_upload(upload_file: UploadFile, target_path: Path) -> int:
    """
    分块保存 Web Agent 上传文件，并限制单文件体积。

    :param upload_file: FastAPI 上传文件对象
    :param target_path: 目标落盘路径
    :return: 已写入的字节数
    """
    size = 0
    try:
        with target_path.open("wb") as output:
            while True:
                chunk = await upload_file.read(WEB_AGENT_UPLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > WEB_AGENT_UPLOAD_MAX_BYTES:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="附件超过 32MB，无法发送给智能助手",
                    )
                output.write(chunk)
    except Exception:
        target_path.unlink(missing_ok=True)
        raise
    finally:
        await upload_file.close()
    return size


def _cleanup_web_agent_file_registry() -> None:
    """清理过期或过量的 Web Agent 临时附件引用。"""
    now = time.time()
    expired_ids = [
        file_id
        for file_id, info in _WEB_AGENT_FILE_REGISTRY.items()
        if now - info.get("created_at", now) > WEB_AGENT_FILE_TTL_SECONDS
    ]
    for file_id in expired_ids:
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)

    overflow = len(_WEB_AGENT_FILE_REGISTRY) - WEB_AGENT_FILE_MAX_ITEMS
    if overflow <= 0:
        return
    sorted_items = sorted(
        _WEB_AGENT_FILE_REGISTRY.items(),
        key=lambda item: item[1].get("created_at", 0),
    )
    for file_id, _ in sorted_items[:overflow]:
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)


def _guess_web_agent_attachment_kind(
    mime_type: Optional[str], fallback: str = "file"
) -> str:
    """
    根据 MIME 类型推断前端附件展示方式。

    :param mime_type: 文件 MIME 类型
    :param fallback: 无法推断时使用的类型
    :return: image、audio 或 file
    """
    if mime_type and mime_type.startswith("image/"):
        return "image"
    if mime_type and mime_type.startswith("audio/"):
        return "audio"
    return fallback


def _build_web_agent_url_attachment(
    url: str,
    kind: str,
    name: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> dict:
    """
    构建远程或 data URL 附件事件。

    :param url: 前端可访问的附件地址
    :param kind: 附件展示类型
    :param name: 展示名称
    :param mime_type: MIME 类型
    :return: 前端附件描述
    """
    return {
        "kind": kind,
        "url": url,
        "download_url": url,
        "name": name,
        "mime_type": mime_type,
    }


def _build_web_agent_input_attachments(
    images: list[str],
    files: list[dict],
    audio_refs: list[str],
) -> list[dict]:
    """
    构造 WebAgent 用户输入附件展示记录。
    """
    attachments = []
    for index, image in enumerate(images or [], start=1):
        attachments.append(
            {
                "kind": "image",
                "url": image,
                "download_url": image,
                "name": f"image-{index}",
                "mime_type": "image/*",
            }
        )
    for index, file in enumerate(files or [], start=1):
        ref = file.get("ref") or file.get("url") or file.get("local_path") or ""
        mime_type = file.get("mime_type")
        attachments.append(
            {
                "kind": _guess_web_agent_attachment_kind(mime_type),
                "url": ref,
                "download_url": ref,
                "name": file.get("name") or f"attachment-{index}",
                "mime_type": mime_type,
                "size": file.get("size"),
                "local_path": file.get("local_path"),
            }
        )
    for index, audio_ref in enumerate(audio_refs or [], start=1):
        attachments.append(
            {
                "kind": "audio",
                "url": audio_ref,
                "download_url": audio_ref,
                "name": f"voice-{index}",
                "mime_type": "audio/*",
            }
        )
    return attachments


def _register_web_agent_file(
    file_path: Optional[str],
    file_name: Optional[str] = None,
    kind: Optional[str] = None,
    mime_type: Optional[str] = None,
) -> Optional[dict]:
    """
    注册 Web Agent 本地附件并返回前端可访问的短期下载地址。

    :param file_path: 本地文件路径
    :param file_name: 前端展示文件名
    :param kind: 附件展示类型
    :param mime_type: 已知 MIME 类型
    :return: 前端附件描述，文件不可访问时返回 None
    """
    if not file_path:
        return None
    try:
        resolved_path = Path(file_path).expanduser().resolve(strict=True)
    except OSError:
        return None
    if not resolved_path.is_file():
        return None

    _cleanup_web_agent_file_registry()
    file_id = uuid.uuid4().hex
    display_name = file_name or resolved_path.name
    resolved_mime_type = mime_type or mimetypes.guess_type(
        display_name or str(resolved_path)
    )[0]
    file_url = f"message/agent/file/{file_id}"
    _WEB_AGENT_FILE_REGISTRY[file_id] = {
        "path": resolved_path,
        "name": display_name,
        "mime_type": resolved_mime_type or "application/octet-stream",
        "created_at": time.time(),
    }
    return {
        "kind": kind or _guess_web_agent_attachment_kind(resolved_mime_type),
        "url": file_url,
        "download_url": file_url,
        "name": display_name,
        "mime_type": resolved_mime_type,
        "size": resolved_path.stat().st_size,
    }


def _get_web_agent_audio_mime_type(audio_path: Path) -> Optional[str]:
    """
    生成浏览器播放更友好的音频 MIME 类型。

    :param audio_path: 音频文件路径
    :return: 可用于 FileResponse/audio 标签的 MIME 类型
    """
    suffix = audio_path.suffix.lower()
    if suffix in {".wav", ".wave"}:
        return "audio/wav"
    if suffix == ".mp3":
        return "audio/mpeg"
    if suffix in {".m4a", ".mp4"}:
        return "audio/mp4"
    if suffix == ".aac":
        return "audio/aac"

    return mimetypes.guess_type(audio_path.name)[0]


def _prepare_web_agent_audio_attachment_path(voice_path: str) -> Path:
    """
    将 Agent 语音回复准备成 Web 面板可稳定播放的音频文件。

    部分 TTS provider 会生成 Opus/Ogg，桌面 Chromium 通常可播放，但 iOS/Safari
    兼容性不稳定；WebAgent 只在浏览器内播放，因此这里单独转成 WAV。
    """
    try:
        source_path = Path(voice_path).expanduser().resolve(strict=True)
    except OSError:
        return Path(voice_path)
    if source_path.suffix.lower() in WEB_AGENT_BROWSER_AUDIO_SUFFIXES:
        return source_path
    if not shutil.which("ffmpeg"):
        logger.warning("WebAgent 语音转 WAV 跳过：ffmpeg 不可用，path=%s", source_path)
        return source_path

    voice_dir = settings.TEMP_PATH / "voice"
    voice_dir.mkdir(parents=True, exist_ok=True)
    output_path = voice_dir / f"{source_path.stem}_web_{uuid.uuid4().hex[:8]}.wav"
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(source_path),
        "-ar",
        "24000",
        "-ac",
        "1",
        "-f",
        "wav",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not output_path.exists():
        logger.warning(
            "WebAgent 语音转 WAV 失败，将回退原文件: returncode=%s, stderr=%s",
            result.returncode,
            (result.stderr or "").strip()[:500],
        )
        return source_path
    return output_path


def _get_web_agent_registered_file(ref: str) -> Optional[dict[str, Any]]:
    """
    根据前端附件引用读取 WebAgent 临时文件登记信息。

    :param ref: message/agent/file/{file_id} 形式的短期引用
    :return: 文件登记信息，引用无效或过期时返回 None
    """
    normalized_ref = (ref or "").strip()
    prefix = "message/agent/file/"
    if not normalized_ref.startswith(prefix):
        return None

    _cleanup_web_agent_file_registry()
    file_id = normalized_ref[len(prefix):].split("/", 1)[0]
    return _WEB_AGENT_FILE_REGISTRY.get(file_id)


def _transcribe_web_agent_audio_refs(audio_refs: list[str]) -> Optional[str]:
    """
    转写 WebAgent 上传的本地录音附件。

    Web 面板上传后的音频已经保存在短期文件登记表里，不能再像第三方渠道那样
    走模块下载逻辑；这里直接读取临时文件并调用当前音频输入 provider。
    """
    if not audio_refs:
        return None
    if not AgentCapabilityManager.is_audio_input_available():
        logger.warning("WebAgent 音频输入能力未配置或未启用，跳过语音识别")
        return None

    transcripts = []
    for audio_ref in audio_refs:
        file_info = _get_web_agent_registered_file(audio_ref)
        if not file_info:
            logger.warning("WebAgent 语音引用不存在或已过期: ref=%s", audio_ref)
            continue

        file_path = Path(file_info["path"])
        try:
            content = file_path.read_bytes()
        except OSError as err:
            logger.warning("WebAgent 语音文件读取失败: ref=%s, error=%s", audio_ref, err)
            continue

        transcript = AgentCapabilityManager.transcribe_audio(
            content=content,
            filename=file_info.get("name") or file_path.name,
        )
        if transcript:
            transcripts.append(transcript)

    return "\n".join(transcripts).strip() if transcripts else None


def _merge_web_agent_prompt_with_transcript(prompt: str, transcript: Optional[str]) -> str:
    """合并用户输入文本和语音转写文本，避免重复发送相同内容。"""
    merged_parts = []
    seen_parts = set()
    for item in (prompt, transcript or ""):
        normalized = item.strip()
        if not normalized or normalized in seen_parts:
            continue
        seen_parts.add(normalized)
        merged_parts.append(normalized)
    return "\n".join(merged_parts).strip()


def _parse_web_agent_choice_callback(callback_data: str) -> Optional[tuple[str, int]]:
    """
    解析 Web Agent 按钮选择回调数据。

    :param callback_data: Agent 按钮携带的回调数据
    :return: 请求 ID 与选项序号，格式无效时返回 None
    """
    if not callback_data.startswith("agent_interaction:choice:"):
        return None
    try:
        _, _, request_id, option_index = callback_data.split(":", 3)
    except ValueError:
        return None
    if not request_id or not option_index.isdigit():
        return None
    return request_id, int(option_index)


def _normalize_web_agent_choice_button_rows(buttons: Optional[list[list[dict]]]) -> list[list[dict]]:
    """
    将消息渠道按钮二维结构转换为 Web 前端可渲染的按钮行。

    :param buttons: Notification 中的按钮行
    :return: Web 选择卡片按钮行
    """
    normalized_rows = []
    for row in buttons or []:
        normalized_row = []
        for button in row or []:
            text = str(button.get("text") or "").strip()
            callback_data = str(button.get("callback_data") or "").strip()
            if not text or not callback_data:
                continue
            description = str(button.get("description") or "").strip()
            normalized_row.append(
                {
                    "label": text,
                    "callback_data": callback_data,
                    **({"description": description} if description else {}),
                }
            )
        if normalized_row:
            normalized_rows.append(normalized_row)
    return normalized_rows


def _build_web_agent_choice_event(notification: schemas.Notification) -> Optional[dict]:
    """
    将带按钮通知转换为 Web Agent 选择卡片事件。

    :param notification: Agent 工具发出的按钮通知
    :return: 选择卡片事件，按钮为空时返回 None
    """
    button_rows = _normalize_web_agent_choice_button_rows(notification.buttons)
    buttons = [button for row in button_rows for button in row]
    if not buttons:
        return None

    choice_id = None
    parsed = _parse_web_agent_choice_callback(buttons[0]["callback_data"])
    if parsed:
        choice_id = parsed[0]

    return {
        "type": "choice",
        "choice": {
            "id": choice_id or uuid.uuid4().hex,
            "title": notification.title,
            "prompt": notification.text or "",
            "buttons": buttons,
            "button_rows": button_rows,
        },
    }


def _build_web_agent_choice_buttons_from_request(
    request,
) -> tuple[list[dict], list[list[dict]]]:
    """
    根据待处理交互请求重建可持久化的按钮列表与按钮行。

    :param request: 等待用户选择的交互请求
    :return: 平铺按钮列表与按行分组的按钮结构
    """
    buttons = [
        {
            "label": option.label,
            "callback_data": f"agent_interaction:choice:{request.request_id}:{index}",
            "description": option.description or option.label,
        }
        for index, option in enumerate(request.options, start=1)
    ]
    button_rows = [[button] for button in buttons]
    return buttons, button_rows


def _resolve_web_agent_choice_payload(callback_data: str, user_id: str) -> Optional[dict]:
    """
    解析并消费 Web Agent 按钮选择，生成前端反馈与下一条用户消息。

    :param callback_data: 前端点击的按钮回调数据
    :param user_id: 当前登录用户 ID
    :return: 可返回给前端的数据，选择无效时返回 None
    """
    parsed = _parse_web_agent_choice_callback(callback_data)
    if not parsed:
        return None

    request_id, option_index = parsed
    resolved = agent_interaction_manager.resolve(
        request_id=request_id,
        option_index=option_index,
        user_id=str(user_id),
    )
    if not resolved:
        return None

    request, option = resolved
    buttons, button_rows = _build_web_agent_choice_buttons_from_request(request)
    selected_description = option.description or option.label
    return {
        "message": option.value,
        "session_id": request.session_id,
        "display_message": selected_description,
        "choice_selection": {
            "choice_id": request.request_id,
            "title": request.title,
            "prompt": request.prompt,
            "buttons": buttons,
            "button_rows": button_rows,
            "selected_label": option.label,
            "selected_value": option.value,
            "selected_description": selected_description,
        },
        "feedback": {
            "request_id": request.request_id,
            "title": request.title,
            "prompt": request.prompt,
            "selected_label": option.label,
            "selected_value": option.value,
            "selected_description": selected_description,
            "buttons": buttons,
            "button_rows": button_rows,
        },
    }


def _build_web_agent_notification_events(
    notification: schemas.Notification,
) -> list[dict]:
    """
    将 Agent 工具通知转换为 Web SSE 事件。

    :param notification: 工具产生的通知消息
    :return: 前端可直接应用到当前助手消息的事件列表
    """
    events = []
    choice_event = _build_web_agent_choice_event(notification)
    if choice_event:
        events.append(choice_event)

    text_parts = [
        str(item).strip()
        for item in (notification.title, notification.text)
        if str(item or "").strip()
    ]
    if text_parts and not choice_event:
        events.append({"type": "delta", "content": "\n\n".join(text_parts)})

    if notification.image:
        image_ref = notification.image
        image_path = Path(image_ref).expanduser()
        attachment = None
        if not image_ref.startswith(("http://", "https://", "data:", "blob:")):
            attachment = _register_web_agent_file(
                image_ref, file_name=Path(image_ref).name, kind="image"
            )
        if not attachment:
            attachment = _build_web_agent_url_attachment(
                image_ref,
                kind="image",
                name=notification.title or image_path.name or "image",
            )
        events.append({"type": "attachment", "attachment": attachment})

    if notification.voice_path:
        audio_path = _prepare_web_agent_audio_attachment_path(notification.voice_path)
        attachment = _register_web_agent_file(
            str(audio_path),
            file_name=audio_path.name,
            kind="audio",
            mime_type=_get_web_agent_audio_mime_type(audio_path),
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    if notification.file_path:
        attachment = _register_web_agent_file(
            notification.file_path,
            file_name=notification.file_name or Path(notification.file_path).name,
        )
        if attachment:
            events.append({"type": "attachment", "attachment": attachment})

    return events


def _build_web_agent_display_message_from_events(
    events: list[dict],
) -> dict:
    """
    将传统消息事件聚合为前端展示消息快照。

    :param events: 已转换的 WebAgent SSE 事件列表
    :return: 可持久化的助手展示消息
    """
    message = MoviePilotAgent.build_display_message(
        role="assistant",
        status="streaming",
    )
    for event in events:
        _apply_web_agent_display_event(copy.deepcopy(event), message)
    _apply_web_agent_display_event({"type": "done"}, message)
    return message


def _is_web_agent_traditional_message(text: str) -> bool:
    """
    判断用户输入是否应走传统消息命令/交互链路。

    :param text: 前端输入文本
    :return: 需要交给 MessageChain 时返回 True
    """
    normalized = str(text or "").strip()
    return normalized.startswith("/") or normalized.startswith("CALLBACK:")


def _has_web_agent_traditional_interaction(user_id: str) -> bool:
    """
    判断当前用户是否存在待继续的传统交互会话。

    :param user_id: 当前登录用户 ID
    :return: 存在传统交互上下文时返回 True
    """
    return any(
        manager.get_by_user(user_id)
        for manager in (
            site_interaction_manager,
            subscribe_interaction_manager,
            skills_interaction_manager,
            media_interaction_manager,
        )
    )


def _extract_web_agent_notification_from_event_data(
    data: dict,
) -> Optional[schemas.Notification]:
    """
    从 NoticeMessage 事件数据中提取 WebAgent 通知。

    :param data: NoticeMessage 事件数据，兼容扁平字段和 message 包装格式
    :return: WebAgent 通知，不属于 WebAgent 或数据无效时返回 None
    """
    if not isinstance(data, dict):
        return None

    try:
        message = data.get("message")
        if isinstance(message, schemas.Notification):
            notification = message
        elif isinstance(message, dict):
            notification_data = copy.deepcopy(message)
            notification_data.pop("type", None)
            notification = schemas.Notification(**notification_data)
        else:
            notification_data = copy.deepcopy(data)
            notification_data.pop("type", None)
            notification_data.pop("current_time", None)
            notification = schemas.Notification(**notification_data)
    except Exception as err:
        logger.debug(f"解析WebAgent通知事件失败: {err}")
        return None

    channel = notification.channel
    channel_value = channel.value if isinstance(channel, MessageChannel) else channel
    if channel_value != MessageChannel.WebAgent.value:
        return None
    return notification


def _is_web_agent_notice_for_user(
    notification: schemas.Notification,
    user_id: str,
) -> bool:
    """
    判断 NoticeMessage 事件是否属于当前 WebAgent 用户。

    :param notification: NoticeMessage 中的通知消息
    :param user_id: 当前登录用户 ID
    :return: 可被本次 WebAgent 请求消费时返回 True
    """
    try:
        target_user = notification.userid
        return target_user is None or str(target_user) == str(user_id)
    except Exception:
        return False


def _get_web_agent_notice_user_id(notification: schemas.Notification) -> Optional[str]:
    """
    从 NoticeMessage 事件中解析 WebAgent 目标用户。

    :param notification: NoticeMessage 中的通知消息
    :return: 用户 ID 字符串，事件不属于 WebAgent 时返回 None
    """
    try:
        channel = notification.channel
        channel_value = channel.value if isinstance(channel, MessageChannel) else channel
        if channel_value != MessageChannel.WebAgent.value:
            return None
        user_id = notification.userid
        return str(user_id) if user_id is not None else None
    except Exception:
        return None


def _dispatch_web_agent_notice_event(event: Event) -> None:
    """
    将 WebAgent NoticeMessage 分发给正在等待的请求队列。

    :param event: NoticeMessage 广播事件
    """
    data = event.event_data if isinstance(event.event_data, dict) else {}
    notification = _extract_web_agent_notification_from_event_data(data)
    if not notification:
        return
    with _WEB_AGENT_NOTICE_LOCK:
        user_id = _get_web_agent_notice_user_id(notification)
        if user_id is None:
            queues = [
                notice_queue
                for user_queues in _WEB_AGENT_NOTICE_QUEUES.values()
                for notice_queue in user_queues
            ]
        else:
            queues = list(_WEB_AGENT_NOTICE_QUEUES.get(user_id) or [])
    for notice_queue in queues:
        notice_queue.put(notification)


def _ensure_web_agent_notice_listener() -> None:
    """
    确保 WebAgent NoticeMessage 全局监听器已注册。
    """
    global _WEB_AGENT_NOTICE_LISTENER_REGISTERED
    if _WEB_AGENT_NOTICE_LISTENER_REGISTERED:
        return
    with _WEB_AGENT_NOTICE_LOCK:
        if _WEB_AGENT_NOTICE_LISTENER_REGISTERED:
            return
        EventManager().add_event_listener(
            EventType.NoticeMessage,
            _dispatch_web_agent_notice_event,
        )
        _WEB_AGENT_NOTICE_LISTENER_REGISTERED = True


def _attach_web_agent_notice_queue(user_id: str, notice_queue: Queue[schemas.Notification]) -> None:
    """
    为当前 WebAgent 请求挂载通知收集队列。

    :param user_id: 当前用户 ID
    :param notice_queue: 用于接收通知事件的队列
    """
    _ensure_web_agent_notice_listener()
    with _WEB_AGENT_NOTICE_LOCK:
        _WEB_AGENT_NOTICE_QUEUES.setdefault(str(user_id), []).append(notice_queue)


def _detach_web_agent_notice_queue(user_id: str, notice_queue: Queue[schemas.Notification]) -> None:
    """
    移除当前 WebAgent 请求的通知收集队列。

    :param user_id: 当前用户 ID
    :param notice_queue: 需要移除的队列
    """
    with _WEB_AGENT_NOTICE_LOCK:
        queues = _WEB_AGENT_NOTICE_QUEUES.get(str(user_id))
        if not queues:
            return
        _WEB_AGENT_NOTICE_QUEUES[str(user_id)] = [
            item for item in queues if item is not notice_queue
        ]
        if not _WEB_AGENT_NOTICE_QUEUES[str(user_id)]:
            _WEB_AGENT_NOTICE_QUEUES.pop(str(user_id), None)


def _build_web_agent_command_items() -> list[dict]:
    """
    读取当前可用斜杠命令并转换为前端建议列表。

    :return: 按分类和命令名排序的命令列表
    """
    commands = Command().get_commands() or {}
    items = []
    for command, data in commands.items():
        if not command.startswith("/"):
            continue
        if data.get("show") is False:
            continue
        items.append(
            {
                "command": command,
                "description": data.get("description") or "",
                "category": data.get("category") or "其他",
                "type": data.get("type") or "",
                "pid": data.get("pid"),
            }
        )
    return sorted(items, key=lambda item: (item["category"], item["command"]))


def _extract_web_agent_slash_command(text: str) -> Optional[str]:
    """
    从 WebAgent 输入中提取斜杠命令名。

    :param text: 前端输入文本
    :return: 斜杠命令名，非命令输入返回 None
    """
    normalized = str(text or "").strip()
    if not normalized.startswith("/") or normalized.startswith("//"):
        return None
    command = normalized.split(maxsplit=1)[0].strip()
    return command or None


def _get_web_agent_unknown_command_message(text: str) -> Optional[str]:
    """
    判断 WebAgent 斜杠命令是否不存在。

    :param text: 前端输入文本
    :return: 命令不存在时返回错误提示，命令存在或非命令时返回 None
    """
    command = _extract_web_agent_slash_command(text)
    if not command:
        return None
    if Command().get(command):
        return None
    return f"命令不存在：{command}"


def _ensure_web_agent_command_allowed(current_user: User) -> Optional[str]:
    """
    校验当前 Web 用户是否可以执行传统斜杠命令。

    :param current_user: 当前登录用户
    :return: 无权限时返回错误提示，允许执行时返回 None
    """
    if getattr(current_user, "is_superuser", False):
        return None
    return "只有管理员才有权限执行此命令"


async def _collect_web_agent_traditional_events(
    *,
    text: str,
    current_user: User,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> list[dict]:
    """
    执行传统消息链路并收集本次 WebAgent 用户产生的通知事件。

    :param text: 需要交给传统消息链路处理的文本
    :param current_user: 当前登录用户
    :param original_message_id: WebAgent 原助手消息 ID
    :param original_chat_id: WebAgent 原聊天 ID
    :return: 可直接发送给前端的 SSE 事件列表
    """
    notice_queue: Queue[schemas.Notification] = Queue()
    edit_queue: Queue[dict] = Queue()
    user_id = str(current_user.id)

    _attach_web_agent_notice_queue(user_id, notice_queue)
    attach_web_agent_edit_queue(user_id, edit_queue)
    try:
        await run_in_threadpool(
            MessageChain().handle_message,
            channel=MessageChannel.WebAgent,
            source=WEB_AGENT_SOURCE,
            userid=user_id,
            username=current_user.name or user_id,
            text=text,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
        )

        events = []
        deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_MAX_WAIT_SECONDS
        idle_deadline: Optional[float] = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            drained_edit_event = False
            while True:
                try:
                    events.append(edit_queue.get_nowait())
                    drained_edit_event = True
                except Empty:
                    break
            if drained_edit_event:
                idle_deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS
                continue

            wait_until = idle_deadline or deadline
            timeout = max(0.05, min(0.25, wait_until - now, deadline - now))
            try:
                notification = await asyncio.to_thread(notice_queue.get, True, timeout)
            except Empty:
                if idle_deadline and time.monotonic() >= idle_deadline:
                    break
                continue

            if not _is_web_agent_notice_for_user(notification, user_id):
                continue
            events.extend(_build_web_agent_notification_events(notification))
            idle_deadline = time.monotonic() + WEB_AGENT_TRADITIONAL_IDLE_TIMEOUT_SECONDS
        return events
    finally:
        _detach_web_agent_notice_queue(user_id, notice_queue)
        detach_web_agent_edit_queue(user_id, edit_queue)


def _build_web_agent_traditional_callback_payload(
    callback_data: str,
    original_message_id: Optional[Union[str, int]] = None,
    original_chat_id: Optional[Union[str, int]] = None,
) -> dict:
    """
    构造传统消息链按钮回调的前端执行载荷。

    :param callback_data: 前端点击的传统按钮回调数据
    :param original_message_id: WebAgent 原助手消息 ID
    :param original_chat_id: WebAgent 原聊天 ID
    :return: 前端可继续发送到 /stream 的消息载荷
    """
    return {
        "message": f"CALLBACK:{callback_data}",
        "display_message": callback_data,
        "traditional": True,
        "original_message_id": original_message_id,
        "original_chat_id": original_chat_id,
    }


def _split_web_agent_output(text: str) -> list[dict]:
    """
    将 Agent 输出拆成普通文本与工具提示事件。

    :param text: 本次新增的 Agent 文本
    :return: 前端可直接渲染的事件片段
    """
    if not text:
        return []

    events = []

    def append_text(content: str) -> None:
        """将工具汇总行从普通文本中拆出，保留与消息渠道一致的展示文案。"""
        if not content:
            return
        lines = content.splitlines(keepends=True)
        buffer = ""
        for line in lines:
            stripped_line = line.strip()
            if stripped_line.startswith("（") and stripped_line.endswith("）"):
                if buffer:
                    events.append({"type": "delta", "content": buffer})
                    buffer = ""
                events.append(
                    {
                        "type": "tool",
                        "message": stripped_line,
                    }
                )
            else:
                buffer += line
        if buffer:
            events.append({"type": "delta", "content": buffer})

    marker = "⚙️ => "
    remaining = text
    while remaining:
        marker_index = remaining.find(marker)
        if marker_index < 0:
            append_text(remaining)
            break

        if marker_index > 0:
            append_text(remaining[:marker_index])

        after_marker = remaining[marker_index + len(marker):]
        line_end = after_marker.find("\n")
        if line_end < 0:
            message = after_marker.strip()
            remaining = ""
        else:
            message = after_marker[:line_end].strip()
            remaining = after_marker[line_end:].lstrip("\n")

        if message:
            events.append({"type": "tool", "message": f"{marker}{message}"})

    return events


@router.get("/file/{file_id}", summary="下载 Web 智能助手附件")
async def download_web_agent_file(file_id: str) -> FileResponse:
    """
    下载 Web 智能助手本轮生成的临时附件。

    :param file_id: 附件随机标识
    :return: 附件文件响应
    """
    _cleanup_web_agent_file_registry()
    file_info = _WEB_AGENT_FILE_REGISTRY.get(file_id)
    if not file_info:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    file_path = file_info["path"]
    if not file_path.exists() or not file_path.is_file():
        _WEB_AGENT_FILE_REGISTRY.pop(file_id, None)
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="附件不存在或已过期")

    return FileResponse(
        path=file_path,
        media_type=file_info.get("mime_type") or "application/octet-stream",
        filename=file_info.get("name") or file_path.name,
    )


@router.post("/upload", summary="上传 Web 智能助手附件", response_model=schemas.Response)
async def upload_web_agent_file(
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    上传 Web 智能助手对话附件。

    :param file: 浏览器选择的文件
    :param session_id: 前端会话标识
    :param current_user: 当前登录用户
    :return: Agent 可消费的附件描述
    """
    mime_type = file.content_type or mimetypes.guess_type(file.filename or "")[0]
    safe_name = _sanitize_web_agent_upload_name(file.filename, mime_type)
    upload_dir = _get_web_agent_upload_dir(current_user, session_id)
    target_path = upload_dir / f"{uuid.uuid4().hex[:8]}_{safe_name}"
    size = await _save_web_agent_upload(file, target_path)
    attachment = _register_web_agent_file(
        str(target_path),
        file_name=safe_name,
        kind=_guess_web_agent_attachment_kind(mime_type),
        mime_type=mime_type,
    )
    if not attachment:
        target_path.unlink(missing_ok=True)
        return schemas.Response(success=False, message="附件保存失败")

    attachment.update(
        {
            "ref": attachment["url"],
            "local_path": str(target_path),
            "status": "ready",
            "size": size,
        }
    )
    return schemas.Response(success=True, data=attachment)


@router.post("/callback", summary="Web 智能助手按钮回调", response_model=schemas.Response)
async def web_agent_callback(
    payload: schemas.AgentWebChoiceRequest,
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    接收 Web 智能助手选择卡片回调。

    :param payload: 按钮选择请求
    :param current_user: 当前登录用户
    :return: 下一条需要发送给 Agent 的用户消息与卡片反馈
    """
    if not _parse_web_agent_choice_callback(payload.callback_data):
        denied_message = _ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return schemas.Response(success=False, message=denied_message)
        return schemas.Response(
            success=True,
            data=_build_web_agent_traditional_callback_payload(
                payload.callback_data,
                original_message_id=payload.original_message_id,
                original_chat_id=payload.original_chat_id,
            ),
        )

    result = _resolve_web_agent_choice_payload(
        callback_data=payload.callback_data,
        user_id=str(current_user.id),
    )
    if not result:
        return schemas.Response(success=False, message="该选择已失效，请重新发起选择")
    return schemas.Response(success=True, data=result)


@router.get("/commands", summary="获取 Web 智能助手可用命令", response_model=schemas.Response)
async def list_web_agent_commands(
    current_user: User = Depends(get_current_active_user),
) -> schemas.Response:
    """
    获取当前 Web 智能助手可补全的斜杠命令。

    :param current_user: 当前登录用户
    :return: 可用命令列表
    """
    denied_message = _ensure_web_agent_command_allowed(current_user)
    if denied_message:
        return schemas.Response(success=False, message=denied_message)
    return schemas.Response(success=True, data=_build_web_agent_command_items())


@router.get("/sessions", summary="获取 Agent 历史会话", response_model=schemas.Response)
async def list_agent_chat_sessions(
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db),
    page: Optional[int] = 1,
    count: Optional[int] = 30,
) -> schemas.Response:
    """
    获取当前用户可访问的 Agent 历史会话列表。

    :param current_user: 当前登录用户
    :param db: 异步数据库会话
    :param page: 页码
    :param count: 每页数量
    :return: 会话摘要列表
    """
    user_id = None if current_user.is_superuser else str(current_user.id)
    username = None if current_user.is_superuser else current_user.name
    chats = await AgentChatOper(db).async_list_by_page(
        page=page,
        count=count,
        user_id=user_id,
        username=username,
    )
    return schemas.Response(
        success=True,
        data=[AgentChatOper.to_summary(chat) for chat in chats],
    )


@router.get("/sessions/{session_id}", summary="获取 Agent 历史会话详情", response_model=schemas.Response)
async def get_agent_chat_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db),
) -> schemas.Response:
    """
    获取一条 Agent 历史会话详情。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param db: 异步数据库会话
    :return: 会话详情
    """
    oper = AgentChatOper(db)
    chat = await _get_accessible_agent_chat(oper, session_id, current_user)
    server_session_id = session_id
    if not chat:
        server_session_id = _build_web_agent_session_id(current_user, session_id)
        if server_session_id != session_id:
            chat = await _get_accessible_agent_chat(oper, server_session_id, current_user)
    if not chat:
        if agent_manager.is_session_busy(server_session_id):
            return schemas.Response(
                success=True,
                data={
                    "session_id": server_session_id,
                    "client_session_id": session_id,
                    "messages": [],
                    "is_processing": True,
                },
            )
        return schemas.Response(success=False, message="会话不存在或无权访问")
    data = AgentChatOper.to_detail(chat)
    data["is_processing"] = agent_manager.is_session_busy(chat.session_id)
    return schemas.Response(success=True, data=data)


@router.put("/sessions/{session_id}/display", summary="保存 Agent 展示会话", response_model=schemas.Response)
async def save_agent_chat_display(
    session_id: str,
    payload: schemas.AgentChatDisplaySaveRequest,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db),
) -> schemas.Response:
    """
    保存前端聚合后的 Agent 展示消息。

    :param session_id: Agent 会话 ID
    :param payload: 展示消息保存请求
    :param current_user: 当前登录用户
    :param db: 异步数据库会话
    :return: 保存后的会话摘要
    """
    oper = AgentChatOper(db)
    existing_chat = await oper.async_get(session_id=session_id)
    if existing_chat and not _can_access_agent_chat(existing_chat, current_user):
        return schemas.Response(success=False, message="会话不存在或无权访问")

    messages = [
        message.model_dump(exclude_none=True)
        for message in payload.messages
    ]
    await run_in_threadpool(
        _save_web_agent_display_snapshot,
        session_id=session_id,
        current_user=current_user,
        messages=messages,
        client_session_id=existing_chat.client_session_id if existing_chat else session_id,
    )
    chat = await oper.async_get(session_id=session_id)
    if not chat:
        return schemas.Response(success=False, message="会话保存失败")
    return schemas.Response(success=True, data=AgentChatOper.to_summary(chat))


@router.delete("/sessions/{session_id}", summary="删除 Agent 历史会话", response_model=schemas.Response)
async def delete_agent_chat_session(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db),
) -> schemas.Response:
    """
    删除一条 Agent 历史会话。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param db: 异步数据库会话
    :return: 删除结果
    """
    oper = AgentChatOper(db)
    chat = await _get_accessible_agent_chat(oper, session_id, current_user)
    if not chat:
        return schemas.Response(success=False, message="会话不存在或无权访问")
    deleted = await oper.async_delete(session_id=session_id)
    return schemas.Response(success=deleted, message="删除成功" if deleted else "删除失败")


@router.post("/sessions/{session_id}/stop", summary="停止 Web 智能助手当前任务", response_model=schemas.Response)
async def stop_web_agent_session_task(
    session_id: str,
    current_user: User = Depends(get_current_active_user),
    db: AsyncSession = Depends(get_async_db),
) -> schemas.Response:
    """
    停止当前 Web 智能助手会话正在执行的任务。

    :param session_id: Agent 会话 ID
    :param current_user: 当前登录用户
    :param db: 异步数据库会话
    :return: 停止结果
    """
    server_session_id = _build_web_agent_session_id(current_user, session_id)
    chat = await _get_accessible_agent_chat(
        AgentChatOper(db), server_session_id, current_user
    )
    if not chat and server_session_id != session_id:
        chat = await _get_accessible_agent_chat(AgentChatOper(db), session_id, current_user)
    if chat and not _can_access_agent_chat(chat, current_user):
        return schemas.Response(success=False, message="会话不存在或无权访问")

    stopped = await agent_manager.stop_current_task(server_session_id)
    return schemas.Response(
        success=True,
        data={"stopped": stopped},
        message="已停止" if stopped else "当前没有正在执行的任务",
    )


@router.post("/stream", summary="Web智能助手流式对话")
async def web_agent_stream(
    payload: schemas.AgentWebChatRequest,
    request: Request,
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """
    Web 智能助手流式对话。

    :param payload: 对话请求
    :param request: 当前 HTTP 请求
    :param current_user: 当前登录用户
    :return: SSE 流式响应
    """
    prompt = payload.text.strip()
    locale = LocaleHelper.get_locale_from_request(request)
    display_prompt = (payload.display_text or payload.text).strip()
    is_traditional_message = (
        _is_web_agent_traditional_message(prompt)
        or _has_web_agent_traditional_interaction(str(current_user.id))
    )
    if is_traditional_message:
        denied_message = _ensure_web_agent_command_allowed(current_user)
        if denied_message:
            return StreamingResponse(
                iter([
                    _build_web_agent_sse(
                        "error",
                        {"message": denied_message},
                        locale=locale,
                    )
                ]),
                media_type="text/event-stream",
            )
        unknown_command_message = _get_web_agent_unknown_command_message(prompt)
        if unknown_command_message:
            return StreamingResponse(
                iter([
                    _build_web_agent_sse(
                        "error",
                        {"message": unknown_command_message},
                        locale=locale,
                    )
                ]),
                media_type="text/event-stream",
            )

        session_id = _build_web_agent_session_id(current_user, payload.session_id)
        user_attachments = _build_web_agent_input_attachments(
            images=payload.images or [],
            files=[
                file.model_dump(exclude_none=True)
                for file in (payload.files or [])
            ],
            audio_refs=payload.audio_refs or [],
        )
        display_messages = []
        if payload.echo_user:
            display_messages.append(
                MoviePilotAgent.build_display_message(
                    role="user",
                    content=display_prompt or prompt,
                    attachments=user_attachments,
                )
            )

        async def traditional_event_generator() -> AsyncIterator[str]:
            """
            生成传统消息链路的 WebAgent SSE 事件。
            """
            yield _build_web_agent_sse(
                "start",
                {"session_id": session_id},
                locale=locale,
            )
            events = await _collect_web_agent_traditional_events(
                text=prompt,
                current_user=current_user,
                original_message_id=payload.original_message_id,
                original_chat_id=payload.original_chat_id,
            )
            assistant_message = _build_web_agent_display_message_from_events(events)
            display_messages.append(assistant_message)
            for event in events:
                event_payload = copy.deepcopy(event)
                yield _build_web_agent_sse(
                    event_payload.pop("type"),
                    event_payload,
                    locale=locale,
                )
                if await request.is_disconnected():
                    break
            await run_in_threadpool(
                _save_web_agent_display_snapshot,
                session_id=session_id,
                current_user=current_user,
                messages=display_messages,
                client_session_id=payload.session_id or session_id,
            )
            yield _build_web_agent_sse("done", {}, locale=locale)

        return StreamingResponse(
            traditional_event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    if not settings.AI_AGENT_ENABLE:
        return StreamingResponse(
            iter([
                _build_web_agent_sse(
                    "error",
                    {"message": "智能助手未启用，请先在系统设置中开启。"},
                    locale=locale,
                )
            ]),
            media_type="text/event-stream",
        )

    transcript = _transcribe_web_agent_audio_refs(payload.audio_refs or [])
    prompt = _merge_web_agent_prompt_with_transcript(prompt, transcript)
    display_prompt = _merge_web_agent_prompt_with_transcript(display_prompt, transcript)
    has_audio_input = bool(transcript)
    if not prompt and payload.audio_refs and not payload.images and not payload.files:
        return StreamingResponse(
            iter([
                _build_web_agent_sse(
                    "error",
                    {"message": "语音识别失败，请稍后重试。"},
                    locale=locale,
                )
            ]),
            media_type="text/event-stream",
        )
    if not prompt and not payload.images and not payload.files and not payload.audio_refs:
        return StreamingResponse(
            iter([
                _build_web_agent_sse(
                    "error",
                    {"message": "请输入要发送给智能助手的内容或选择附件。"},
                    locale=locale,
                )
            ]),
            media_type="text/event-stream",
        )

    session_id = _build_web_agent_session_id(current_user, payload.session_id)
    MessageChain().bind_user_session(str(current_user.id), session_id)
    event_queue: asyncio.Queue = asyncio.Queue()
    last_output = ""
    user_attachments = _build_web_agent_input_attachments(
        images=payload.images or [],
        files=[
            file.model_dump(exclude_none=True)
            for file in (payload.files or [])
        ],
        audio_refs=payload.audio_refs or [],
    )
    display_messages = []
    if payload.echo_user:
        user_display_message = MoviePilotAgent.build_display_message(
            role="user",
            content=display_prompt or prompt,
            attachments=user_attachments,
        )
        if payload.choice_selection:
            user_display_message["choice_selection"] = payload.choice_selection
        display_messages.append(user_display_message)
    assistant_display_message = MoviePilotAgent.build_display_message(
        role="assistant",
        status="streaming",
    )
    display_messages.append(assistant_display_message)

    def output_callback(output: str) -> None:
        """
        接收 Agent 累积输出并转成增量事件。
        """
        nonlocal last_output
        delta = output[len(last_output):] if output.startswith(last_output) else output
        last_output = output
        for item in _split_web_agent_output(delta):
            _apply_web_agent_display_event(item, assistant_display_message)
            event_queue.put_nowait(item)

    def notification_callback(notification: schemas.Notification) -> None:
        """
        接收 Agent 工具主动发送的 Web 通知。
        """
        for item in _build_web_agent_notification_events(notification):
            _apply_web_agent_display_event(item, assistant_display_message)
            event_queue.put_nowait(item)

    async def event_generator() -> AsyncIterator[str]:
        """
        生成前端 Agent SSE 事件。
        """
        audio_ref_set = set(payload.audio_refs or [])
        files = [
            file.model_dump(exclude_none=True)
            for file in (payload.files or [])
            if file.ref not in audio_ref_set
        ]
        for audio_ref in payload.audio_refs or []:
            files.append({"ref": audio_ref, "mime_type": "audio/*"})

        async def run_agent() -> None:
            """后台执行 Agent，并将结果写入事件队列。"""
            try:
                await agent_manager.process_message(
                    session_id=session_id,
                    user_id=str(current_user.id),
                    message=prompt,
                    images=payload.images or [],
                    files=files or None,
                    has_audio_input=has_audio_input,
                    channel=MessageChannel.WebAgent.value,
                    source=WEB_AGENT_SOURCE,
                    username=current_user.name,
                    reply_mode=ReplyMode.CAPTURE_ONLY,
                    allow_message_tools=True,
                    output_callback=output_callback,
                    notification_callback=notification_callback,
                    agent_factory=_WebAgentMoviePilotAgent,
                    wait_for_completion=True,
                )
            except Exception as err:
                logger.error(f"Web智能助手执行失败: {str(err)}")
                error_event = {
                    "type": "error",
                    "message": f"智能助手执行失败: {str(err)}",
                }
                _apply_web_agent_display_event(error_event, assistant_display_message)
                await event_queue.put(error_event)
            finally:
                done_event = {"type": "done"}
                _apply_web_agent_display_event(done_event, assistant_display_message)
                await run_in_threadpool(
                    _save_web_agent_display_snapshot,
                    session_id=session_id,
                    current_user=current_user,
                    messages=display_messages,
                    client_session_id=payload.session_id or session_id,
                )
                await event_queue.put(done_event)

        task = asyncio.create_task(run_agent())
        _WEB_AGENT_BACKGROUND_TASKS.add(task)
        task.add_done_callback(_WEB_AGENT_BACKGROUND_TASKS.discard)
        try:
            yield _build_web_agent_sse(
                "start",
                {"session_id": session_id},
                locale=locale,
            )
            disconnected = False
            while not global_vars.is_system_stopped:
                if await request.is_disconnected():
                    disconnected = True
                    break
                event = await event_queue.get()
                yield _build_web_agent_sse(
                    event.pop("type"),
                    event,
                    locale=locale,
                )
                if task.done() and event_queue.empty():
                    break
        except asyncio.CancelledError:
            disconnected = True
            return
        finally:
            if not task.done() and not disconnected:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            # 客户端退到后台导致 SSE 断开时，保留后台 Agent 继续执行；完成后会保存展示快照，
            # 前端恢复可见时可通过会话详情接口拉取最终状态。

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
