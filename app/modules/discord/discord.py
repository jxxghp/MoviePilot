"""
Discord 机器人连接、频道路由、消息卡片和输入状态生命周期管理。
"""

import asyncio
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import discord
from discord import app_commands

from app.application.messaging.ingress import async_forward_message_to_host
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.runtime.execution import run_in_threadpool
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.types import MessageType

# 仅这些通知类型将正文拆为卡片字段，其他类型保留原始文本。
PARSE_FIELD_TYPES = {
    # 下载资源通知。
    MessageType.Download,
    # 整理入库通知。
    MessageType.Organize,
    # 订阅状态通知。
    MessageType.Subscribe,
    # 手动处理通知。
    MessageType.Manual,
}


def _is_regular_file(path: Path) -> bool:
    """
    判断路径是否仍指向可发送的普通文件。
    """
    return path.exists() and path.is_file()


class Discord:
    """
    管理 Discord 机器人专用事件循环、交互命令和通知发送。
    """

    # Discord 应用命令同步最多保留 100 条。
    _MAX_SLASH_COMMANDS = 100
    # 客户端关闭和每轮线程等待的超时时间，单位秒。
    _client_close_timeout_seconds = 10
    _thread_join_timeout_seconds = 5

    def __init__(
        self,
        DISCORD_BOT_TOKEN: Optional[str] = None,
        DISCORD_GUILD_ID: Optional[Union[str, int]] = None,
        DISCORD_CHANNEL_ID: Optional[Union[str, int]] = None,
        **kwargs: Any,
    ) -> None:
        """
        创建客户端和专用事件循环，注册回调并启动后台线程。

        完整配置会启动网络连接；stop 负责停止客户端、输入状态任务和事件循环线程。

        :param DISCORD_BOT_TOKEN: 机器人令牌；缺失时不初始化客户端
        :param DISCORD_GUILD_ID: 可选服务器标识，限定命令同步与频道回退范围
        :param DISCORD_CHANNEL_ID: 可选默认通知频道标识
        :param kwargs: name 指定通知配置名称，其他字段当前忽略
        """
        logger.debug(
            f"[Discord] 初始化 Discord 实例: name={kwargs.get('name')}, "
            f"GUILD_ID={DISCORD_GUILD_ID}, CHANNEL_ID={DISCORD_CHANNEL_ID}, "
            f"TOKEN={'已配置' if DISCORD_BOT_TOKEN else '未配置'}"
        )
        if not DISCORD_BOT_TOKEN:
            logger.error("Discord Bot Token 未配置！")
            return

        self._token = DISCORD_BOT_TOKEN
        self._guild_id = self._to_int(DISCORD_GUILD_ID)
        self._channel_id = self._to_int(DISCORD_CHANNEL_ID)
        self._config_name = kwargs.get("name")
        logger.debug(
            f"[Discord] 解析后的 ID: _guild_id={self._guild_id}, _channel_id={self._channel_id}"
        )
        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.guilds = True

        self._client: discord.Client = discord.Client(
            intents=intents, proxy=get_runtime_setting('PROXY_HOST')
        )
        self._tree: Optional[app_commands.CommandTree] = app_commands.CommandTree(self._client)
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._stop_requested = threading.Event()
        self._ready_event = threading.Event()
        self._user_dm_cache: Dict[str, discord.DMChannel] = {}
        # 保存用户最近发言频道，避免回复被误发到默认频道。
        self._user_chat_mapping: Dict[
            str, str
        ] = {}
        self._broadcast_channel = None
        self._bot_user_id: Optional[int] = None
        self._typing_tasks: Dict[str, asyncio.Task[None]] = {}
        self._typing_stop_events: Dict[str, asyncio.Event] = {}
        self._typing_lifecycle_lock = asyncio.Lock()
        self._typing_accepting = True
        self._typing_interval_seconds = 5
        self._typing_initial_delay_seconds = 1
        self._typing_max_duration_seconds = 10 * 60
        self._typing_stop_timeout_seconds = 1
        self._registered_commands: Optional[Dict[str, dict[str, Any]]] = None

        self._register_events()
        self._start()

    @staticmethod
    def _to_int(val: Optional[Union[str, int]]) -> Optional[int]:
        """
        解析频道或服务器标识，空值和非法数字文本返回 None。
        """
        try:
            return int(val) if val is not None and str(val).strip() else None
        except ValueError:
            return None

    def _register_events(self) -> None:
        """
        注册登录、普通消息和按钮交互回调，并维护原频道回复路由。
        """
        async def on_ready() -> None:
            """
            标记登录就绪，并同步此前缓存的斜杠命令。
            """
            self._bot_user_id = self._client.user.id if self._client.user else None
            self._ready_event.set()
            logger.info(f"Discord Bot 已登录：{self._client.user}")
            if self._registered_commands is not None:
                try:
                    await self._sync_registered_commands()
                except Exception as err:
                    logger.error(f"同步 Discord 斜杠命令失败：{err}")

        self._client.event(on_ready)

        async def on_message(message: discord.Message) -> None:
            """
            忽略机器人消息，将允许处理的消息及附件转交统一入口。
            """
            if message.author.bot:
                return
            if not self._should_process_message(message):
                return

            # 记录交互发生的频道，供后续回复使用。
            self._update_user_chat_mapping(
                str(message.author.id), str(message.channel.id)
            )

            cleaned_text = self._clean_bot_mention(message.content or "")
            username = (
                message.author.display_name
                or message.author.global_name
                or message.author.name
            )
            payload = {
                "type": "message",
                "userid": str(message.author.id),
                "username": username,
                "user_tag": str(message.author),
                "text": cleaned_text,
                "message_id": str(message.id),
                "chat_id": str(message.channel.id),
                "channel_type": "dm"
                if isinstance(message.channel, discord.DMChannel)
                else "guild",
            }
            if message.attachments:
                payload["attachments"] = [
                    {
                        "id": str(attachment.id),
                        "filename": attachment.filename,
                        "content_type": attachment.content_type,
                        "url": attachment.url,
                        "proxy_url": attachment.proxy_url,
                        "size": attachment.size,
                        "height": attachment.height,
                        "width": attachment.width,
                    }
                    for attachment in message.attachments
                ]
            await self._post_to_ds(payload)

        self._client.event(on_message)

        async def on_interaction(interaction: discord.Interaction) -> None:
            """
            确认按钮交互，记录原频道并转交回调载荷。
            """
            if interaction.type == discord.InteractionType.component:
                data = interaction.data or {}
                callback_data = data.get("custom_id")
                if not callback_data:
                    return
                try:
                    await interaction.response.defer(ephemeral=True)
                except Exception as e:
                    logger.error(f"处理 Discord 交互响应失败：{e}")

                # 记录交互发生的频道，供后续回复使用。
                if interaction.user and interaction.channel:
                    self._update_user_chat_mapping(
                        str(interaction.user.id), str(interaction.channel.id)
                    )

                username = (
                    (
                        interaction.user.display_name
                        or interaction.user.global_name
                        or interaction.user.name
                    )
                    if interaction.user
                    else None
                )
                payload = {
                    "type": "interaction",
                    "userid": str(interaction.user.id) if interaction.user else None,
                    "username": username,
                    "user_tag": str(interaction.user) if interaction.user else None,
                    "callback_data": callback_data,
                    "message_id": str(interaction.message.id)
                    if interaction.message
                    else None,
                    "chat_id": str(interaction.channel.id)
                    if interaction.channel
                    else None,
                }
                await self._post_to_ds(payload)

        self._client.event(on_interaction)

    def _start(self) -> None:
        """
        启动并持有 Discord 客户端专用事件循环线程。
        """
        if self._thread:
            return

        def runner() -> None:
            """
            在线程内运行客户端，并在退出前回收本实例拥有的异步任务。
            """
            loop = self._loop
            client = self._client
            asyncio.set_event_loop(loop)
            start_task: Optional[asyncio.Task[None]] = None
            try:
                if not self._stop_requested.is_set():
                    start_task = loop.create_task(client.start(self._token))
                    loop.run_until_complete(start_task)
            except asyncio.CancelledError:
                if not self._stop_requested.is_set():
                    logger.error("Discord Bot 启动任务被意外取消")
            except Exception as err:
                if not self._stop_requested.is_set():
                    logger.error(f"Discord Bot 启动失败：{err}")
            finally:
                self._ready_event.clear()
                if start_task and not start_task.done():
                    start_task.cancel()
                    try:
                        loop.run_until_complete(start_task)
                    except asyncio.CancelledError:
                        pass
                try:
                    loop.run_until_complete(self._stop_all_typing_tasks())
                except Exception as err:
                    logger.debug(f"Discord typing 任务收尾失败：{err}")
                try:
                    loop.run_until_complete(client.close())
                except Exception as err:
                    logger.debug(f"Discord Bot 关闭失败：{err}")
                finally:
                    asyncio.set_event_loop(None)
                    loop.close()

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()

    def stop(self) -> bool:
        """
        停止 Discord 客户端，并返回事件循环线程是否已经终止。
        """
        if not self._client or not self._loop or not self._thread:
            return True
        self._stop_requested.set()
        loop = self._loop
        thread = self._thread
        if loop.is_running():
            try:
                asyncio.run_coroutine_threadsafe(
                    self._stop_all_typing_tasks(), loop
                ).result(timeout=5)
            except Exception as err:
                logger.error(f"停止 Discord typing 状态失败：{err}")
            try:
                asyncio.run_coroutine_threadsafe(
                    self._client.close(), loop
                ).result(timeout=self._client_close_timeout_seconds)
            except Exception as err:
                logger.error(f"关闭 Discord Bot 失败：{err}")
        self._ready_event.clear()
        if thread is not threading.current_thread():
            thread.join(timeout=self._thread_join_timeout_seconds)
        if thread.is_alive():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception as err:
                logger.error(f"停止 Discord 事件循环失败：{err}")
            if thread is not threading.current_thread():
                thread.join(timeout=self._thread_join_timeout_seconds)
        if thread.is_alive():
            logger.error("Discord Bot 线程未在超时内停止")
            return False
        return True

    def get_state(self) -> bool:
        """
        检查登录就绪标记和客户端是否同时可用。

        :return: 已设置就绪标记且客户端存在时返回 True
        """
        return self._ready_event.is_set() and self._client is not None

    def register_commands(self, commands: Dict[str, dict[str, Any]]) -> bool:
        """
        注册 Discord 斜杠命令。

        :param commands: 命令字典，键为斜杠命令，值包含描述和分类等元数据
        :return: 是否成功提交同步任务
        """
        self._registered_commands = dict(commands or {})
        return self._schedule_command_sync()

    def delete_commands(self) -> bool:
        """
        清理 Discord 斜杠命令。

        :return: 是否成功提交同步任务
        """
        self._registered_commands = {}
        return self._schedule_command_sync()

    def _schedule_command_sync(self) -> bool:
        """
        在 Discord 事件循环中提交命令同步任务。
        """
        if not self._tree or not self._loop:
            return False
        if not self.get_state():
            logger.debug("Discord Bot 未就绪，斜杠命令将在登录后同步")
            return True
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._sync_registered_commands(), self._loop
            )
            return bool(future.result(timeout=30))
        except Exception as err:
            logger.error(f"同步 Discord 斜杠命令失败：{err}")
            return False

    async def _sync_registered_commands(self) -> bool:
        """
        将当前命令集合同步到 Discord 应用命令树。
        """
        if not self._tree or not self._client:
            return False
        if not self._client.is_ready():
            await self._client.wait_until_ready()

        guild = discord.Object(id=self._guild_id) if self._guild_id else None
        self._tree.clear_commands(guild=guild)

        commands = self._registered_commands or {}
        registered_count = 0
        seen_names = set()
        for command_text, command_data in commands.items():
            if registered_count >= self._MAX_SLASH_COMMANDS:
                logger.warning(
                    f"Discord 斜杠命令数量超过 {self._MAX_SLASH_COMMANDS} 个，后续命令已跳过"
                )
                break
            command_name = self._normalize_slash_command_name(command_text)
            if not command_name or command_name in seen_names:
                logger.warning(f"跳过无效或重复的 Discord 斜杠命令：{command_text}")
                continue
            seen_names.add(command_name)
            description = self._normalize_slash_command_description(
                command_data.get("description") if isinstance(command_data, dict) else None,
                command_name,
            )
            self._tree.add_command(
                self._build_slash_command(command_text, command_name, description),
                guild=guild,
                override=True,
            )
            registered_count += 1

        synced_commands = await self._tree.sync(guild=guild)
        logger.info(f"Discord 斜杠命令已同步：{len(synced_commands)} 个")
        return True

    @staticmethod
    def _normalize_slash_command_name(command_text: str) -> str:
        """
        转换为 Discord 允许的斜杠命令名称。
        """
        command_name = str(command_text or "").strip().lstrip("/").lower()
        if not re.fullmatch(r"[a-z0-9_-]{1,32}", command_name):
            return ""
        return command_name

    @staticmethod
    def _normalize_slash_command_description(
        description: Optional[str],
        fallback: str,
    ) -> str:
        """
        整理 Discord 斜杠命令描述，满足长度要求。
        """
        normalized = str(description or fallback or "MoviePilot").strip()
        return normalized[:100] or "MoviePilot"

    def _build_slash_command(
        self,
        command_text: str,
        command_name: str,
        description: str,
    ) -> app_commands.Command:
        """
        构建 Discord 斜杠命令对象。
        """

        async def _callback(
            interaction: discord.Interaction,
            args: Optional[str] = None,
        ) -> None:
            """
            把 Discord 斜杠命令及参数交给统一命令处理流程。
            """
            await self._handle_slash_command(interaction, command_text, args)

        _callback.__name__ = f"moviepilot_{command_name}"
        _callback = app_commands.describe(args="命令参数")(_callback)
        return app_commands.Command(
            name=command_name,
            description=description,
            callback=_callback,
        )

    async def _handle_slash_command(
        self,
        interaction: discord.Interaction,
        command_text: str,
        args: Optional[str] = None,
    ) -> None:
        """
        处理 Discord 斜杠命令回调，并转发到统一消息入口。
        """
        try:
            await interaction.response.defer(ephemeral=True, thinking=True)
        except Exception as err:
            logger.debug(f"延迟响应 Discord 斜杠命令失败：{err}")

        userid = str(interaction.user.id) if interaction.user else None
        chat_id = str(interaction.channel.id) if interaction.channel else None
        username = None
        if interaction.user:
            username = (
                getattr(interaction.user, "display_name", None)
                or getattr(interaction.user, "global_name", None)
                or getattr(interaction.user, "name", None)
            )
        if userid and chat_id:
            self._update_user_chat_mapping(userid, chat_id)

        arg_text = str(args or "").strip()
        payload = {
            "type": "message",
            "userid": userid,
            "username": username,
            "user_tag": str(interaction.user) if interaction.user else None,
            "text": f"{command_text} {arg_text}".strip(),
            "message_id": str(interaction.id),
            "chat_id": chat_id,
            "channel_type": "dm"
            if isinstance(interaction.channel, discord.DMChannel)
            else "guild",
        }
        await self._post_to_ds(payload)

        try:
            if interaction.response.is_done():
                await interaction.followup.send("命令已提交，请稍等...", ephemeral=True)
            else:
                await interaction.response.send_message(
                    "命令已提交，请稍等...",
                    ephemeral=True,
                )
        except Exception as err:
            logger.debug(f"发送 Discord 斜杠命令确认失败：{err}")

    def send_msg(
        self,
        title: str,
        text: Optional[str] = None,
        image: Optional[str] = None,
        userid: Optional[str] = None,
        link: Optional[str] = None,
        buttons: Optional[List[List[dict[str, str]]]] = None,
        original_message_id: Optional[Union[int, str]] = None,
        original_chat_id: Optional[str] = None,
        mtype: Optional["MessageType"] = None,
    ) -> Union[bool, Tuple[bool, Optional[Dict[str, str]]]]:
        """
        在专用事件循环中发送或编辑通知，最多等待 30 秒。

        超时只结束当前等待，不会自动撤销已提交到事件循环的发送任务。

        :param title: 消息标题，可与正文择一提供
        :param text: 消息正文
        :param image: 可选图片地址
        :param userid: 用于上下文频道路由或私聊回退的用户标识
        :param link: 可选详情链接
        :param buttons: 可选按钮行
        :param original_message_id: 原消息标识；与原频道同时提供才执行编辑
        :param original_chat_id: 原频道标识，也可单独用于定向发送
        :param mtype: 通知类型，决定是否将正文解析为字段卡片
        :return: 内部发送完成时返回 (成功标记, 消息标识字典或 None)；入口校验、超时或调度失败返回 False
        """
        logger.debug(
            f"[Discord] send_msg 被调用: userid={userid}, title={title[:50] if title else None}..."
        )
        logger.debug(
            f"[Discord] get_state() = {self.get_state()}, "
            f"_ready_event.is_set() = {self._ready_event.is_set()}, "
            f"_client = {self._client is not None}"
        )
        if not self.get_state():
            logger.warning("[Discord] get_state() 返回 False，Bot 未就绪，无法发送消息")
            return False
        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            logger.debug("[Discord] 准备异步发送消息...")
            future = asyncio.run_coroutine_threadsafe(
                self._send_message(
                    title=title,
                    text=text,
                    image=image,
                    userid=userid,
                    link=link,
                    buttons=buttons,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                    mtype=mtype,
                ),
                self._loop,
            )
            result = future.result(timeout=30)
            logger.debug(f"[Discord] 异步发送完成，结果: {result}")
            return result
        except Exception as err:
            logger.error(f"发送 Discord 消息失败：{err}")
            return False

    def send_file(
        self,
        file_path: str,
        title: Optional[str] = None,
        text: Optional[str] = None,
        userid: Optional[str] = None,
        file_name: Optional[str] = None,
        original_chat_id: Optional[str] = None,
    ) -> Union[bool, Tuple[bool, Optional[Dict[str, str]]]]:
        """
        在专用事件循环中上传本地文件，最多等待 30 秒。

        :param file_path: 本地普通文件路径
        :param title: 可选标题，加入文件附言
        :param text: 可选正文，加入文件附言
        :param userid: 用于上下文频道路由或私聊回退的用户标识
        :param file_name: 可选上传文件名，默认使用本地名称
        :param original_chat_id: 优先接收文件的频道标识
        :return: 内部发送完成时返回 (成功标记, 消息标识字典或 None)；入口校验、超时或调度失败返回 False
        """
        if not self.get_state():
            return False
        if not file_path:
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_file(
                    file_path=file_path,
                    title=title,
                    text=text,
                    userid=userid,
                    file_name=file_name,
                    original_chat_id=original_chat_id,
                ),
                self._loop,
            )
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 文件失败：{err}")
            return False

    def send_medias_msg(
        self,
        medias: List[MediaInfo],
        userid: Optional[str] = None,
        title: Optional[str] = None,
        buttons: Optional[List[List[dict[str, str]]]] = None,
        original_message_id: Optional[Union[int, str]] = None,
        original_chat_id: Optional[str] = None,
    ) -> Optional[bool]:
        """
        在专用事件循环中发送或编辑媒体候选列表。

        :param medias: 待展示媒体列表，仅发送前 10 张卡片
        :param userid: 用于上下文频道路由或私聊回退的用户标识
        :param title: 可选列表标题，省略时使用默认标题
        :param buttons: 可选自定义按钮；为空时按输入列表生成选择按钮
        :param original_message_id: 原消息标识，与原频道同时提供才执行编辑
        :param original_chat_id: 原消息频道，也可单独指定发送频道
        :return: 发送成功返回 True；未就绪、空列表、30 秒等待超时或失败返回 False
        """
        if not self.get_state() or not medias:
            return False
        title = title or "媒体列表"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_list_message(
                    embeds=self._build_media_embeds(medias, title),
                    userid=userid,
                    buttons=self._build_default_buttons(len(medias))
                    if not buttons
                    else buttons,
                    fallback_buttons=buttons,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                ),
                self._loop,
            )
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 媒体列表失败：{err}")
            return False

    def send_torrents_msg(
        self,
        torrents: List[Context],
        userid: Optional[str] = None,
        title: Optional[str] = None,
        buttons: Optional[List[List[dict[str, str]]]] = None,
        original_message_id: Optional[Union[int, str]] = None,
        original_chat_id: Optional[str] = None,
    ) -> Optional[bool]:
        """
        在专用事件循环中发送或编辑种子候选列表。

        :param torrents: 待展示种子列表，仅发送前 10 张卡片
        :param userid: 用于上下文频道路由或私聊回退的用户标识
        :param title: 可选列表标题，省略时使用默认标题
        :param buttons: 可选自定义按钮；为空时按输入列表生成选择按钮
        :param original_message_id: 原消息标识，与原频道同时提供才执行编辑
        :param original_chat_id: 原消息频道，也可单独指定发送频道
        :return: 发送成功返回 True；未就绪、空列表、30 秒等待超时或失败返回 False
        """
        if not self.get_state() or not torrents:
            return False
        title = title or "种子列表"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_list_message(
                    embeds=self._build_torrent_embeds(torrents, title),
                    userid=userid,
                    buttons=self._build_default_buttons(len(torrents))
                    if not buttons
                    else buttons,
                    fallback_buttons=buttons,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id,
                ),
                self._loop,
            )
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 种子列表失败：{err}")
            return False

    def start_typing(
        self,
        userid: Optional[str] = None,
        chat_id: Optional[str] = None,
        max_duration_seconds: Optional[float] = None,
        initial_delay_seconds: Optional[float] = None,
    ) -> bool:
        """
        为会话持续发送正在输入状态，直到停止或达到最长时限。

        每 5 秒续发；替换已有任务前等待其结束，避免同一会话并行续发。

        :param userid: 没有频道标识时用于定位会话的用户标识
        :param chat_id: 优先定位会话的频道标识
        :param max_duration_seconds: 最长续发秒数；未提供或为 0 时使用默认 600 秒
        :param initial_delay_seconds: 首发延迟秒数，默认 1 秒；负值按 0 处理
        :return: 成功建立唯一续发任务返回 True；未就绪、无法定位或等待失败返回 False
        """
        if not self.get_state():
            return False
        typing_key = self._typing_key(userid=userid, chat_id=chat_id)
        if not typing_key:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._start_typing_task(
                    typing_key=typing_key,
                    userid=userid,
                    chat_id=chat_id,
                    max_duration_seconds=max_duration_seconds,
                    initial_delay_seconds=initial_delay_seconds,
                ),
                self._loop,
            )
            return future.result(timeout=10)
        except Exception as err:
            logger.error(f"发送 Discord typing 状态失败：{err}")
            return False

    def stop_typing(
        self,
        userid: Optional[str] = None,
        chat_id: Optional[str] = None,
    ) -> bool:
        """
        请求停止会话的正在输入续发任务。

        :param userid: 无频道标识时使用的用户标识
        :param chat_id: 优先使用的会话频道标识
        :return: 找到既有任务并请求停止时返回 True；不代表任务已完成退出
        """
        typing_key = self._typing_key(userid=userid, chat_id=chat_id)
        if not typing_key or not self._loop:
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._stop_typing_task(typing_key), self._loop
            )
            return future.result(timeout=5)
        except Exception as err:
            logger.error(f"停止 Discord typing 状态失败：{err}")
            return False

    @staticmethod
    def _typing_key(userid: Optional[str] = None, chat_id: Optional[str] = None) -> str:
        """
        优先按频道维度管理 正在输入状态，缺失时退回用户维度。
        """
        if chat_id:
            return f"chat:{chat_id}"
        if userid:
            return f"user:{userid}"
        return ""

    async def _start_typing_task(
        self,
        typing_key: str,
        userid: Optional[str] = None,
        chat_id: Optional[str] = None,
        max_duration_seconds: Optional[float] = None,
        initial_delay_seconds: Optional[float] = None,
    ) -> bool:
        """
        为会话启动唯一的 Discord 输入状态续发任务。

        :return: 是否取得该会话的唯一 输入状态任务
        """
        if not self._typing_accepting:
            logger.debug("Discord client 已停止，拒绝启动 typing 任务")
            return False
        channel = await self._resolve_channel(userid=userid, chat_id=chat_id)
        if not channel:
            return False
        async with self._typing_lifecycle_lock:
            if not self._typing_accepting:
                logger.debug("Discord client 已停止，拒绝启动 typing 任务")
                return False
            found, terminal = await self._stop_typing_task_locked(typing_key)
            if found and not terminal:
                logger.warning(
                    f"Discord typing 旧任务尚未结束，拒绝并行启动: key={typing_key}"
                )
                return False

            stop_event = asyncio.Event()
            max_duration = max_duration_seconds or self._typing_max_duration_seconds
            initial_delay = (
                self._typing_initial_delay_seconds
                if initial_delay_seconds is None
                else max(initial_delay_seconds, 0)
            )

            async def _typing_worker() -> None:
                """
                延迟首发并定期续发当前会话的 正在输入状态。
                """
                started_at = self._loop.time()
                try:
                    # Discord 输入状态触发后也会在客户端自然保留一段时间，
                    # 先给短响应一个取消窗口，避免回复后残留输入状态。
                    if initial_delay:
                        try:
                            await asyncio.wait_for(
                                stop_event.wait(),
                                timeout=initial_delay,
                            )
                            return
                        except asyncio.TimeoutError:
                            pass
                    while not stop_event.is_set():
                        if self._loop.time() - started_at >= max_duration:
                            logger.warning(
                                f"Discord typing状态超过最大续期，自动停止: key={typing_key}"
                            )
                            break
                        try:
                            await channel.trigger_typing()
                        except Exception as err:
                            logger.debug(f"触发 Discord typing 状态失败：{err}")
                        try:
                            await asyncio.wait_for(
                                stop_event.wait(),
                                timeout=self._typing_interval_seconds,
                            )
                        except asyncio.TimeoutError:
                            pass
                finally:
                    current_task = asyncio.current_task()
                    if self._typing_tasks.get(typing_key) is current_task:
                        self._typing_tasks.pop(typing_key, None)
                        self._typing_stop_events.pop(typing_key, None)

            task = asyncio.create_task(
                _typing_worker(),
                name=f"discord.typing.{typing_key}",
            )
            self._typing_stop_events[typing_key] = stop_event
            self._typing_tasks[typing_key] = task
            return True

    async def _stop_typing_task(self, typing_key: str) -> bool:
        """
        请求停止会话输入状态任务，并保留尚未结束的任务引用。

        :return: 是否找到并请求停止了既有任务
        """
        async with self._typing_lifecycle_lock:
            found, _ = await self._stop_typing_task_locked(typing_key)
            return found

    async def _stop_typing_task_locked(self, typing_key: str) -> tuple[bool, bool]:
        """
        在 生命周期锁内停止单个任务。

        :return: 是否找到任务，以及 任务是否已进入终态
        """
        stop_event = self._typing_stop_events.get(typing_key)
        task = self._typing_tasks.get(typing_key)
        found = bool(stop_event or task)
        if stop_event:
            stop_event.set()
        if task and task is not asyncio.current_task() and not task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(task),
                    timeout=self._typing_stop_timeout_seconds,
                )
            except asyncio.TimeoutError:
                return found, False
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
            except Exception as err:
                logger.debug(f"Discord typing 任务异常结束：{err}")
        terminal = task is None or task.done()
        if terminal:
            if self._typing_tasks.get(typing_key) is task:
                self._typing_tasks.pop(typing_key, None)
            if self._typing_stop_events.get(typing_key) is stop_event:
                self._typing_stop_events.pop(typing_key, None)
        return found, terminal

    async def _stop_all_typing_tasks(self) -> None:
        """
        封住新增输入状态任务，并在统一预算内停止、取消和回收现有任务。
        """
        async with self._typing_lifecycle_lock:
            self._typing_accepting = False
            typing_keys = set(self._typing_tasks) | set(self._typing_stop_events)
            owners = {
                typing_key: self._typing_tasks.get(typing_key)
                for typing_key in typing_keys
            }
            for typing_key in typing_keys:
                stop_event = self._typing_stop_events.get(typing_key)
                if stop_event:
                    stop_event.set()

            current_task = asyncio.current_task()
            active_tasks = {
                task
                for task in owners.values()
                if task and task is not current_task and not task.done()
            }
            settled_tasks = {task for task in owners.values() if task and task.done()}
            if active_tasks:
                done, pending = await asyncio.wait(
                    active_tasks,
                    timeout=self._typing_stop_timeout_seconds,
                )
                settled_tasks.update(done)
                for task in pending:
                    task.cancel()
                if pending:
                    done, _ = await asyncio.wait(
                        pending,
                        timeout=self._typing_stop_timeout_seconds,
                    )
                    settled_tasks.update(done)

            for task in settled_tasks:
                if task.cancelled():
                    continue
                error = task.exception()
                if error:
                    logger.debug(f"Discord typing 任务异常结束：{error}")

            for typing_key, owned_task in owners.items():
                if owned_task is None or owned_task.done():
                    if self._typing_tasks.get(typing_key) is owned_task:
                        self._typing_tasks.pop(typing_key, None)
                    self._typing_stop_events.pop(typing_key, None)

    def delete_msg(
        self, message_id: Union[str, int], chat_id: Optional[str] = None
    ) -> Optional[bool]:
        """
        在线程专用事件循环中删除消息，最多等待 15 秒。

        :param message_id: Discord 数字消息标识
        :param chat_id: 消息所在频道标识；未提供时按默认频道策略回退
        :return: 删除成功返回 True，未就绪、超时或失败返回 False
        """
        if not self.get_state():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._delete_message(message_id=message_id, chat_id=chat_id), self._loop
            )
            return future.result(timeout=15)
        except Exception as err:
            logger.error(f"删除 Discord 消息失败：{err}")
            return False

    async def _send_message(
        self,
        title: str,
        text: Optional[str],
        image: Optional[str],
        userid: Optional[str],
        link: Optional[str],
        buttons: Optional[List[List[dict[str, str]]]],
        original_message_id: Optional[Union[int, str]],
        original_chat_id: Optional[str],
        mtype: Optional["MessageType"] = None,
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """
        解析接收频道并异步发送或编辑单条嵌入消息。

        :return: 成功标记及消息、频道标识字典；无法发送时返回 (False, None)
        """
        logger.debug(
            f"[Discord] _send_message: userid={userid}, original_chat_id={original_chat_id}"
        )
        channel = await self._resolve_channel(userid=userid, chat_id=original_chat_id)
        logger.debug(
            f"[Discord] _resolve_channel 返回: {channel}, type={type(channel)}"
        )
        if not channel:
            logger.error("未找到可用的 Discord 频道或私聊")
            return False, None

        embed = self._build_embed(
            title=title, text=text, image=image, link=link, mtype=mtype
        )
        view = self._build_view(buttons=buttons, link=link)
        content = None

        if original_message_id and original_chat_id:
            logger.debug(f"[Discord] 编辑现有消息: message_id={original_message_id}")
            success = await self._edit_message(
                chat_id=original_chat_id,
                message_id=original_message_id,
                content=content,
                embed=embed,
                view=view,
            )
            return (
                success,
                {
                    "message_id": str(original_message_id),
                    "chat_id": str(original_chat_id),
                }
                if success and original_message_id and original_chat_id
                else None,
            )

        logger.debug(f"[Discord] 发送新消息到频道: {channel}")
        try:
            sent_message = await channel.send(content=content, embed=embed, view=view)
            logger.debug("[Discord] 消息发送成功")
            return (
                True,
                {
                    "message_id": str(sent_message.id),
                    "chat_id": str(channel.id),
                }
                if sent_message and getattr(channel, "id", None) is not None
                else None,
            )
        except Exception as e:
            logger.error(f"[Discord] 发送消息到频道失败: {e}")
            return False, None

    async def _send_file(
        self,
        file_path: str,
        title: Optional[str],
        text: Optional[str],
        userid: Optional[str],
        file_name: Optional[str],
        original_chat_id: Optional[str],
    ) -> Tuple[bool, Optional[Dict[str, str]]]:
        """
        在线程池检查本地文件后，上传到解析出的接收频道。

        标题和正文合并为附言，超过 1900 字符时截断并追加省略号。

        :return: 成功标记及消息、频道标识字典；无法发送时返回 (False, None)
        """
        channel = await self._resolve_channel(userid=userid, chat_id=original_chat_id)
        if not channel:
            logger.error("未找到可用的 Discord 频道或私聊")
            return False, None

        local_file = Path(file_path)
        if not await run_in_threadpool(
                _is_regular_file,
                local_file,
        ):
            logger.error(f"Discord发送文件失败，文件不存在: {local_file}")
            return False, None

        content_parts = [part for part in [title, text] if part]
        content = "\n".join(content_parts) if content_parts else None
        if content and len(content) > 1900:
            content = content[:1900] + "..."

        try:
            discord_file = discord.File(
                str(local_file), filename=file_name or local_file.name
            )
            sent_message = await channel.send(content=content, file=discord_file)
            return (
                True,
                {
                    "message_id": str(sent_message.id),
                    "chat_id": str(channel.id),
                },
            )
        except Exception as err:
            logger.error(f"Discord发送文件失败: {err}")
            return False, None

    async def _send_list_message(
        self,
        embeds: List[discord.Embed],
        userid: Optional[str],
        buttons: Optional[List[List[dict[str, str]]]],
        fallback_buttons: Optional[List[List[dict[str, str]]]],
        original_message_id: Optional[Union[int, str]],
        original_chat_id: Optional[str],
    ) -> bool:
        """
        发送前 10 张列表卡片；提供原消息和频道时改为编辑。

        :return: 发送或编辑成功返回 True，无法解析频道时返回 False；SDK 发送异常由同步入口处理
        """
        channel = await self._resolve_channel(userid=userid, chat_id=original_chat_id)
        if not channel:
            logger.error("未找到可用的 Discord 频道或私聊")
            return False

        view = self._build_view(buttons=buttons if buttons else fallback_buttons)
        # Discord 单条消息最多允许 10 张嵌入卡片。
        embeds = embeds[:10] if embeds else []

        if original_message_id and original_chat_id:
            return await self._edit_message(
                chat_id=original_chat_id,
                message_id=original_message_id,
                content=None,
                embed=None,
                view=view,
                embeds=embeds,
            )

        await channel.send(
            embed=embeds[0] if len(embeds) == 1 else None,
            embeds=embeds if len(embeds) > 1 else None,
            view=view,
        )
        return True

    async def _edit_message(
        self,
        chat_id: Union[str, int],
        message_id: Union[str, int],
        content: Optional[str],
        embed: Optional[discord.Embed],
        view: Optional[discord.ui.View],
        embeds: Optional[List[discord.Embed]] = None,
    ) -> bool:
        """
        读取原消息并更新正文、卡片与按钮，失败时记录日志。

        :return: 编辑成功返回 True；频道不可用或操作失败返回 False
        """
        channel = await self._resolve_channel(chat_id=str(chat_id))
        if not channel:
            logger.error(f"未找到要编辑的 Discord 频道：{chat_id}")
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            kwargs: Dict[str, Any] = {"content": content, "view": view}
            if embeds:
                if len(embeds) == 1:
                    kwargs["embed"] = embeds[0]
                else:
                    kwargs["embeds"] = embeds
            elif embed:
                kwargs["embed"] = embed
            await message.edit(**kwargs)
            return True
        except Exception as err:
            logger.error(f"编辑 Discord 消息失败：{err}")
            return False

    async def _delete_message(
        self, message_id: Union[str, int], chat_id: Optional[str]
    ) -> bool:
        """
        读取并删除指定频道消息，失败时记录日志。

        :return: 删除成功返回 True；频道不可用或操作失败返回 False
        """
        channel = await self._resolve_channel(chat_id=chat_id)
        if not channel:
            logger.error("删除 Discord 消息时未找到频道")
            return False
        try:
            message = await channel.fetch_message(int(message_id))
            await message.delete()
            return True
        except Exception as err:
            logger.error(f"删除 Discord 消息失败：{err}")
            return False

    @staticmethod
    def _build_embed(
        title: str,
        text: Optional[str],
        image: Optional[str],
        link: Optional[str],
        mtype: Optional["MessageType"] = None,
    ) -> discord.Embed:
        """
        把通知正文转换为 Discord 嵌入消息。

        仅下载、整理、订阅和手动通知解析“字段：值”；书名括号、URL 和时间中的冒号
        保留为普通正文，避免错误切分。未提供详情链接时使用项目主页。
        """
        fields: List[Dict[str, Union[str, bool]]] = []
        desc_lines: List[str] = []
        should_parse_fields = mtype in PARSE_FIELD_TYPES if mtype else False

        def _collect_spans(s: str, left: str, right: str) -> List[Tuple[int, int]]:
            """
            收集成对括号区间，供正文字段解析排除标题中的冒号。
            """
            spans: List[Tuple[int, int]] = []
            start = 0
            while True:
                l_idx = s.find(left, start)
                if l_idx == -1:
                    break
                r_idx = s.find(right, l_idx + 1)
                if r_idx == -1:
                    break
                spans.append((l_idx, r_idx))
                start = r_idx + 1
            return spans

        def _find_colon_index(s: str, m: re.Match[str]) -> Optional[int]:
            """
            返回匹配字段中首个中英文冒号的位置。
            """
            segment = s[m.start(): m.end()]
            for i, ch in enumerate(segment):
                if ch in (":", "："):
                    return m.start() + i
            return None

        if text:
            # 处理上游未反序列化的 "\n" 等转义换行，避免被当成普通字符
            if "\\n" in text or "\\r" in text:
                text = (
                    text.replace("\\r\\n", "\n")
                    .replace("\\n", "\n")
                    .replace("\\r", "\n")
                )
            if not should_parse_fields:
                desc_lines.append(text.strip())
            else:
                # 匹配形如 "字段：值" 的片段，字段名不允许包含常见分隔符；
                # 下一个字段需以顿号/逗号/分号等分隔开，且不能是 URL 协议开头，避免值里出现 URL 的":" 被误拆
                # 字段名允许表情等 Unicode 字符，但排除空白/分隔符/冒号
                name_re = r"[^\s:：，,。；;、]+"
                pair_pattern = re.compile(
                    rf"({name_re})[：:](.*?)(?=(?:[，,。；;、]+\s*(?!https?://|ftp://|ftps://|magnet:){name_re}[：:])|$)",
                    re.IGNORECASE,
                )
                for line in text.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    matches = list(pair_pattern.finditer(line))
                    if matches:
                        book_spans = _collect_spans(line, "《", "》") + _collect_spans(
                            line, "【", "】"
                        )
                        if book_spans:
                            has_book_colon = False
                            for m in matches:
                                colon_idx = _find_colon_index(line, m)
                                if colon_idx is not None and any(
                                    l < colon_idx < r for l, r in book_spans
                                ):
                                    has_book_colon = True
                                    break
                            if has_book_colon:
                                desc_lines.append(line)
                                continue
                        # 若整行只是 URL/时间等自然包含":"的内容，则不当作字段
                        url_like_names = {"http", "https", "ftp", "ftps", "magnet"}
                        if all(
                            m.group(1).lower() in url_like_names or m.group(1).isdigit()
                            for m in matches
                        ):
                            desc_lines.append(line)
                            continue
                        last_end = 0
                        for m in matches:
                            # 追加匹配前的非空文本到描述
                            prefix = line[last_end: m.start()].strip(" ，,;；。、")
                            # 仅当前缀不全是分隔符/空白时才记录
                            if prefix and prefix.strip(" ，,;；。、"):
                                desc_lines.append(prefix)
                            name = m.group(1).strip()
                            value = m.group(2).strip(" ，,;；。、\t") or "-"
                            if name:
                                fields.append(
                                    {"name": name, "value": value, "inline": False}
                                )
                            last_end = m.end()
                        # 匹配末尾后的文本
                        suffix = line[last_end:].strip(" ，,;；。、")
                        if suffix and suffix.strip(" ，,;；。、"):
                            desc_lines.append(suffix)
                    else:
                        desc_lines.append(line)
        description = "\n".join(desc_lines).strip()
        if not description and not fields and text:
            description = text.strip()
        embed = discord.Embed(
            title=title,
            url=link or "https://github.com/jxxghp/MoviePilot",
            description=description if description else None,
            color=0xE67E22,
        )
        for field in fields:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        if image:
            embed.set_image(url=image)
        return embed

    @staticmethod
    def _build_media_embeds(medias: List[MediaInfo], title: str) -> List[discord.Embed]:
        """
        生成前 10 个媒体的详情卡片，首张附带列表标题。
        """
        embeds: List[discord.Embed] = []
        for index, media in enumerate(medias[:10], start=1):
            overview = media.get_overview_string(80)
            desc_parts = [
                f"{media.type.value if media.type else ''} | {media.vote_star}"
                if media.vote_star
                else (media.type.value if media.type else ""),
                overview,
            ]
            embed = discord.Embed(
                title=f"{index}. {media.title_year}",
                url=media.detail_link or discord.Embed.Empty,
                description="\n".join([p for p in desc_parts if p]),
                color=0x5865F2,
            )
            if media.get_poster_image():
                embed.set_thumbnail(url=media.get_poster_image())
            embeds.append(embed)
        if embeds:
            embeds[0].set_author(name=title)
        return embeds

    @staticmethod
    def _build_torrent_embeds(
        torrents: List[Context], title: str
    ) -> List[discord.Embed]:
        """
        生成前 10 个种子的站点、大小、流量优惠和资源卡片。
        """
        embeds: List[discord.Embed] = []
        for index, context in enumerate(torrents[:10], start=1):
            torrent = context.torrent_info
            if torrent is None:
                continue
            meta = MetaInfo(torrent.title or "", torrent.description)
            title_text = f"{meta.season_episode} {meta.resource_term} {meta.video_term} {meta.release_group}"
            title_text = re.sub(r"\s+", " ", title_text).strip()
            detail = [
                f"{torrent.site_name} | {size_tools.format_compact_size(torrent.size)} | {torrent.volume_factor} | {torrent.seeders}↑",
                meta.resource_term,
                meta.video_term,
            ]
            embed = discord.Embed(
                title=f"{index}. {title_text or torrent.title}",
                url=torrent.page_url or discord.Embed.Empty,
                description="\n".join([d for d in detail if d]),
                color=0x00A86B,
            )
            poster = getattr(torrent, "poster", None)
            if poster:
                embed.set_thumbnail(url=poster)
            embeds.append(embed)
        if embeds:
            embeds[0].set_author(name=title)
        return embeds

    @staticmethod
    def _build_default_buttons(count: int) -> List[List[dict[str, str]]]:
        """
        为列表生成数字选择按钮，最多 5 行、每行 5 个。
        """
        buttons: List[List[dict[str, str]]] = []
        max_rows = 5
        max_per_row = 5
        capped = min(count, max_rows * max_per_row)
        for idx in range(1, capped + 1):
            row_idx = (idx - 1) // max_per_row
            if len(buttons) <= row_idx:
                buttons.append([])
            buttons[row_idx].append({"text": f"选择 {idx}", "callback_data": str(idx)})
        if count > capped:
            logger.warn(f"按钮数量超过 Discord 限制，仅展示前 {capped} 个")
        return buttons

    @staticmethod
    def _build_view(
        buttons: Optional[List[List[dict[str, str]]]], link: Optional[str] = None
    ) -> Optional[discord.ui.View]:
        """
        构建无本地超时的按钮视图，最多保留前 5 行、每行 5 个按钮。

        自定义按钮优先于详情链接；回调标识截为 99 字符，回调按钮标题截为 80 字符。
        """
        has_buttons = buttons and any(buttons)
        if not has_buttons and not link:
            return None

        view = discord.ui.View(timeout=None)
        if buttons:
            for row_index, button_row in enumerate(buttons[:5]):
                for button in button_row[:5]:
                    if "url" in button:
                        btn = discord.ui.Button(
                            label=button.get("text", "链接"),
                            url=button["url"],
                            style=discord.ButtonStyle.link,
                        )
                    else:
                        custom_id = (
                            button.get("callback_data")
                            or button.get("text")
                            or f"btn-{row_index}"
                        )[:99]
                        btn = discord.ui.Button(
                            label=button.get("text", "选择")[:80],
                            custom_id=custom_id,
                            style=discord.ButtonStyle.primary,
                        )
                    view.add_item(btn)
        elif link:
            view.add_item(
                discord.ui.Button(
                    label="查看详情", url=link, style=discord.ButtonStyle.link
                )
            )
        return view

    async def _resolve_channel(
        self, userid: Optional[str] = None, chat_id: Optional[str] = None
    ) -> Optional[Union[discord.abc.GuildChannel, discord.abc.PrivateChannel, discord.Thread]]:
        """
        按回复上下文和配置优先级解析发送频道。

        显式频道用于原会话回复；只指定用户时必须使用私聊，失败不会回落公共频道。
        未指定接收人时才选择配置或自动发现的广播频道，并缓存成功结果。

        :param userid: 定向用户标识，仅发送至该用户私聊
        :param chat_id: 显式频道标识，优先用于原频道回复
        :return: 首个可用频道或私聊对象；均不可用时返回 None
        """
        logger.debug(
            f"[Discord] _resolve_channel: userid={userid}, chat_id={chat_id}, "
            f"_channel_id={self._channel_id}, _guild_id={self._guild_id}"
        )

        if chat_id:
            logger.debug(f"[Discord] 尝试通过 chat_id={chat_id} 获取原始频道")
            channel = self._client.get_channel(int(chat_id))
            if channel:
                logger.debug(f"[Discord] 通过 get_channel 找到频道: {channel}")
                return channel
            try:
                channel = await self._client.fetch_channel(int(chat_id))
                logger.debug(f"[Discord] 通过 fetch_channel 找到频道: {channel}")
                return channel
            except Exception as err:
                logger.warn(f"通过 chat_id 获取 Discord 频道失败：{err}")
                return None

        if userid:
            return await self._get_dm_channel(str(userid))

        if self._broadcast_channel:
            logger.debug(f"[Discord] 使用缓存的广播频道: {self._broadcast_channel}")
            return self._broadcast_channel
        if self._channel_id:
            logger.debug(
                f"[Discord] 尝试通过配置的 _channel_id={self._channel_id} 获取频道"
            )
            channel = self._client.get_channel(self._channel_id)
            if not channel:
                try:
                    channel = await self._client.fetch_channel(self._channel_id)
                except Exception as err:
                    logger.warn(f"通过配置的频道ID获取 Discord 频道失败：{err}")
                    channel = None
            self._broadcast_channel = channel
            if channel:
                logger.debug(f"[Discord] 通过配置的频道ID找到频道: {channel}")
                return channel

        logger.debug("[Discord] 尝试在 Guild 中寻找可用频道")
        target_guilds = []
        if self._guild_id:
            guild = self._client.get_guild(self._guild_id)
            if guild:
                target_guilds.append(guild)
        else:
            target_guilds = list(self._client.guilds)
        logger.debug(f"[Discord] 目标 Guilds 数量: {len(target_guilds)}")

        for guild in target_guilds:
            for channel in guild.text_channels:
                if guild.me and channel.permissions_for(guild.me).send_messages:
                    logger.debug(f"[Discord] 在 Guild 中找到可用频道: {channel}")
                    self._broadcast_channel = channel
                    return channel

        return None

    async def _get_dm_channel(self, userid: str) -> Optional[discord.DMChannel]:
        """
        获取或创建用户私聊频道，并缓存成功结果。

        :param userid: Discord 数字用户标识
        :return: 私聊频道对象；用户查询或创建失败时返回 None
        """
        logger.debug(f"[Discord] _get_dm_channel: userid={userid}")
        if userid in self._user_dm_cache:
            logger.debug(
                f"[Discord] 从缓存获取私聊频道: {self._user_dm_cache.get(userid)}"
            )
            return self._user_dm_cache.get(userid)
        try:
            logger.debug(f"[Discord] 尝试获取/创建用户 {userid} 的私聊频道")
            user_obj = self._client.get_user(int(userid))
            logger.debug(f"[Discord] get_user 结果: {user_obj}")
            if not user_obj:
                user_obj = await self._client.fetch_user(int(userid))
                logger.debug(f"[Discord] fetch_user 结果: {user_obj}")
            if not user_obj:
                logger.debug(f"[Discord] 无法找到用户 {userid}")
                return None
            dm = user_obj.dm_channel
            logger.debug(f"[Discord] 用户现有 dm_channel: {dm}")
            if not dm:
                dm = await user_obj.create_dm()
                logger.debug(f"[Discord] 创建新的 dm_channel: {dm}")
            if dm:
                self._user_dm_cache[userid] = dm
            return dm
        except Exception as err:
            logger.error(f"获取 Discord 私聊失败：{err}")
            return None

    def _update_user_chat_mapping(self, userid: str, chat_id: str) -> None:
        """
        记录用户最近发言的频道，供后续回复路由使用。

        :param userid: 实际发言用户标识
        :param chat_id: 本次发言所在频道标识
        """
        if userid and chat_id:
            self._user_chat_mapping[userid] = chat_id
            logger.debug(
                f"[Discord] 更新用户频道映射: userid={userid} -> chat_id={chat_id}"
            )

    def _get_user_chat_id(self, userid: str) -> Optional[str]:
        """
        读取用户最近发言的频道标识。

        :param userid: 实际发言用户标识
        :return: 缓存的频道标识；没有记录时返回 None
        """
        return self._user_chat_mapping.get(userid)

    def _should_process_message(self, message: discord.Message) -> bool:
        """
        接受私聊消息，以及群内提及机器人或以斜杠开头的消息。
        """
        if isinstance(message.channel, discord.DMChannel):
            return True
        content = message.content or ""
        # 仅处理 @Bot 或斜杠命令
        if self._client.user and self._client.user.mentioned_in(message):
            return True
        if content.startswith("/"):
            return True
        return False

    def _clean_bot_mention(self, content: str) -> str:
        """
        去除发给当前机器人的提及前缀，保留实际命令正文。
        """
        if not content:
            return ""
        if self._bot_user_id:
            mention_pattern = rf"<@!?{self._bot_user_id}>"
            content = re.sub(mention_pattern, "", content).strip()
        return content

    async def _post_to_ds(self, payload: Dict[str, Any]) -> None:
        """
        把 Discord 事件异步转交统一消息入口。
        """
        if not await async_forward_message_to_host(
            payload,
            self._config_name,
            timeout=10,
        ):
            logger.error("转发 Discord 消息失败")
