import asyncio
import re
import threading
from typing import Optional, List, Dict, Any, Tuple, Union

import discord
from discord import app_commands
import httpx

from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.schemas.types import NotificationType
from app.utils.string import StringUtils

# Discord embed 字段解析白名单
# 只有这些消息类型会使用复杂的字段解析逻辑
PARSE_FIELD_TYPES = {
    NotificationType.Download,      # 资源下载
    NotificationType.Organize,      # 整理入库
    NotificationType.Subscribe,     # 订阅
    NotificationType.Manual,        # 手动处理
}


class Discord:
    """
    Discord Bot 通知与交互实现（基于 discord.py 2.6.4）
    """

    def __init__(self, DISCORD_BOT_TOKEN: Optional[str] = None,
                 DISCORD_GUILD_ID: Optional[Union[str, int]] = None,
                 DISCORD_CHANNEL_ID: Optional[Union[str, int]] = None,
                 **kwargs):
        if not DISCORD_BOT_TOKEN:
            logger.error("Discord Bot Token 未配置！")
            return

        self._token = DISCORD_BOT_TOKEN
        self._guild_id = self._to_int(DISCORD_GUILD_ID)
        self._channel_id = self._to_int(DISCORD_CHANNEL_ID)
        base_ds_url = f"http://127.0.0.1:{settings.PORT}/api/v1/message/"
        self._ds_url = f"{base_ds_url}?token={settings.API_TOKEN}"
        if kwargs.get("name"):
            self._ds_url = f"{self._ds_url}&source={kwargs.get('name')}"

        intents = discord.Intents.default()
        intents.message_content = True
        intents.messages = True
        intents.guilds = True

        self._client: Optional[discord.Client] = discord.Client(
            intents=intents,
            proxy=settings.PROXY_HOST
        )
        self._tree: Optional[app_commands.CommandTree] = None
        self._loop: asyncio.AbstractEventLoop = asyncio.new_event_loop()
        self._thread: Optional[threading.Thread] = None
        self._ready_event = threading.Event()
        self._user_dm_cache: Dict[str, discord.DMChannel] = {}
        self._broadcast_channel = None
        self._bot_user_id: Optional[int] = None

        self._register_events()
        self._start()

    @staticmethod
    def _to_int(val: Optional[Union[str, int]]) -> Optional[int]:
        try:
            return int(val) if val is not None and str(val).strip() else None
        except ValueError:
            return None

    def _register_events(self):
        @self._client.event
        async def on_ready():
            self._bot_user_id = self._client.user.id if self._client.user else None
            self._ready_event.set()
            logger.info(f"Discord Bot 已登录：{self._client.user}")

        @self._client.event
        async def on_message(message: discord.Message):
            if message.author.bot:
                return
            if not self._should_process_message(message):
                return

            cleaned_text = self._clean_bot_mention(message.content or "")
            username = message.author.display_name or message.author.global_name or message.author.name
            payload = {
                "type": "message",
                "userid": str(message.author.id),
                "username": username,
                "user_tag": str(message.author),
                "text": cleaned_text,
                "message_id": str(message.id),
                "chat_id": str(message.channel.id),
                "channel_type": "dm" if isinstance(message.channel, discord.DMChannel) else "guild"
            }
            await self._post_to_ds(payload)

        @self._client.event
        async def on_interaction(interaction: discord.Interaction):
            if interaction.type == discord.InteractionType.component:
                data = interaction.data or {}
                callback_data = data.get("custom_id")
                if not callback_data:
                    return
                try:
                    await interaction.response.defer(ephemeral=True)
                except Exception as e:
                    logger.error(f"处理 Discord 交互响应失败：{e}")

                username = (interaction.user.display_name or interaction.user.global_name or interaction.user.name) \
                    if interaction.user else None
                payload = {
                    "type": "interaction",
                    "userid": str(interaction.user.id) if interaction.user else None,
                    "username": username,
                    "user_tag": str(interaction.user) if interaction.user else None,
                    "callback_data": callback_data,
                    "message_id": str(interaction.message.id) if interaction.message else None,
                    "chat_id": str(interaction.channel.id) if interaction.channel else None
                }
                await self._post_to_ds(payload)

    def _start(self):
        if self._thread:
            return

        def runner():
            asyncio.set_event_loop(self._loop)
            try:
                self._loop.create_task(self._client.start(self._token))
                self._loop.run_forever()
            except Exception as err:
                logger.error(f"Discord Bot 启动失败：{err}")
            finally:
                try:
                    self._loop.run_until_complete(self._client.close())
                except Exception as err:
                    logger.debug(f"Discord Bot 关闭失败：{err}")

        self._thread = threading.Thread(target=runner, daemon=True)
        self._thread.start()

    def stop(self):
        if not self._client or not self._loop or not self._thread:
            return
        try:
            asyncio.run_coroutine_threadsafe(self._client.close(), self._loop).result(timeout=10)
        except Exception as err:
            logger.error(f"关闭 Discord Bot 失败：{err}")
        finally:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception as err:
                logger.error(f"停止 Discord 事件循环失败：{err}")
            self._ready_event.clear()

    def get_state(self) -> bool:
        return self._ready_event.is_set() and self._client is not None

    def send_msg(self, title: str, text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None,
                 buttons: Optional[List[List[dict]]] = None,
                 original_message_id: Optional[Union[int, str]] = None,
                 original_chat_id: Optional[str] = None,
                 mtype: Optional['NotificationType'] = None) -> Optional[bool]:
        if not self.get_state():
            return False
        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return False

        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_message(title=title, text=text, image=image, userid=userid,
                                   link=link, buttons=buttons,
                                   original_message_id=original_message_id,
                                   original_chat_id=original_chat_id,
                                   mtype=mtype),
                self._loop)
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 消息失败：{err}")
            return False

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None, title: Optional[str] = None,
                        buttons: Optional[List[List[dict]]] = None,
                        original_message_id: Optional[Union[int, str]] = None,
                        original_chat_id: Optional[str] = None) -> Optional[bool]:
        if not self.get_state() or not medias:
            return False
        title = title or "媒体列表"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_list_message(
                    embeds=self._build_media_embeds(medias, title),
                    userid=userid,
                    buttons=self._build_default_buttons(len(medias)) if not buttons else buttons,
                    fallback_buttons=buttons,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id
                ),
                self._loop
            )
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 媒体列表失败：{err}")
            return False

    def send_torrents_msg(self, torrents: List[Context], userid: Optional[str] = None, title: Optional[str] = None,
                          buttons: Optional[List[List[dict]]] = None,
                          original_message_id: Optional[Union[int, str]] = None,
                          original_chat_id: Optional[str] = None) -> Optional[bool]:
        if not self.get_state() or not torrents:
            return False
        title = title or "种子列表"
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._send_list_message(
                    embeds=self._build_torrent_embeds(torrents, title),
                    userid=userid,
                    buttons=self._build_default_buttons(len(torrents)) if not buttons else buttons,
                    fallback_buttons=buttons,
                    original_message_id=original_message_id,
                    original_chat_id=original_chat_id
                ),
                self._loop
            )
            return future.result(timeout=30)
        except Exception as err:
            logger.error(f"发送 Discord 种子列表失败：{err}")
            return False

    def delete_msg(self, message_id: Union[str, int], chat_id: Optional[str] = None) -> Optional[bool]:
        if not self.get_state():
            return False
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._delete_message(message_id=message_id, chat_id=chat_id),
                self._loop
            )
            return future.result(timeout=15)
        except Exception as err:
            logger.error(f"删除 Discord 消息失败：{err}")
            return False

    async def _send_message(self, title: str, text: Optional[str], image: Optional[str],
                            userid: Optional[str], link: Optional[str],
                            buttons: Optional[List[List[dict]]],
                            original_message_id: Optional[Union[int, str]],
                            original_chat_id: Optional[str],
                            mtype: Optional['NotificationType'] = None) -> bool:
        channel = await self._resolve_channel(userid=userid, chat_id=original_chat_id)
        if not channel:
            logger.error("未找到可用的 Discord 频道或私聊")
            return False

        embed = self._build_embed(title=title, text=text, image=image, link=link, mtype=mtype)
        view = self._build_view(buttons=buttons, link=link)
        content = None

        if original_message_id and original_chat_id:
            return await self._edit_message(chat_id=original_chat_id, message_id=original_message_id,
                                            content=content, embed=embed, view=view)

        await channel.send(content=content, embed=embed, view=view)
        return True

    async def _send_list_message(self, embeds: List[discord.Embed],
                                 userid: Optional[str],
                                 buttons: Optional[List[List[dict]]],
                                 fallback_buttons: Optional[List[List[dict]]],
                                 original_message_id: Optional[Union[int, str]],
                                 original_chat_id: Optional[str]) -> bool:
        channel = await self._resolve_channel(userid=userid, chat_id=original_chat_id)
        if not channel:
            logger.error("未找到可用的 Discord 频道或私聊")
            return False

        view = self._build_view(buttons=buttons if buttons else fallback_buttons)
        embeds = embeds[:10] if embeds else []  # Discord 单条消息最多 10 个 embed

        if original_message_id and original_chat_id:
            return await self._edit_message(chat_id=original_chat_id, message_id=original_message_id,
                                            content=None, embed=None, view=view, embeds=embeds)

        await channel.send(embed=embeds[0] if len(embeds) == 1 else None,
                           embeds=embeds if len(embeds) > 1 else None,
                           view=view)
        return True

    async def _edit_message(self, chat_id: Union[str, int], message_id: Union[str, int],
                            content: Optional[str], embed: Optional[discord.Embed],
                            view: Optional[discord.ui.View], embeds: Optional[List[discord.Embed]] = None) -> bool:
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

    async def _delete_message(self, message_id: Union[str, int], chat_id: Optional[str]) -> bool:
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
    def _build_embed(title: str, text: Optional[str], image: Optional[str],
                     link: Optional[str], mtype: Optional['NotificationType'] = None) -> discord.Embed:
        fields: List[Dict[str, str]] = []
        desc_lines: List[str] = []
        should_parse_fields = mtype in PARSE_FIELD_TYPES if mtype else False
        def _collect_spans(s: str, left: str, right: str) -> List[Tuple[int, int]]:
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

        def _find_colon_index(s: str, m: re.Match) -> Optional[int]:
            segment = s[m.start():m.end()]
            for i, ch in enumerate(segment):
                if ch in (":", "："):
                    return m.start() + i
            return None

        if text:
            # 处理上游未反序列化的 "\n" 等转义换行，避免被当成普通字符
            if "\\n" in text or "\\r" in text:
                text = text.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\r", "\n")
            if not should_parse_fields:
                desc_lines.append(text.strip())
            else:
                # 匹配形如 "字段：值" 的片段，字段名不允许包含常见分隔符；
                # 下一个字段需以顿号/逗号/分号等分隔开，且不能是 URL 协议开头，避免值里出现 URL 的":" 被误拆
                name_re = r"[A-Za-z0-9\u4e00-\u9fa5_\-&]+"
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
                        book_spans = _collect_spans(line, "《", "》") + _collect_spans(line, "【", "】")
                        if book_spans:
                            has_book_colon = False
                            for m in matches:
                                colon_idx = _find_colon_index(line, m)
                                if colon_idx is not None and any(l < colon_idx < r for l, r in book_spans):
                                    has_book_colon = True
                                    break
                            if has_book_colon:
                                desc_lines.append(line)
                                continue
                        # 若整行只是 URL/时间等自然包含":"的内容，则不当作字段
                        url_like_names = {"http", "https", "ftp", "ftps", "magnet"}
                        if all(m.group(1).lower() in url_like_names or m.group(1).isdigit() for m in matches):
                            desc_lines.append(line)
                            continue
                        last_end = 0
                        for m in matches:
                            # 追加匹配前的非空文本到描述
                            prefix = line[last_end:m.start()].strip(" ，,;；。、")
                            # 仅当前缀不全是分隔符/空白时才记录
                            if prefix and prefix.strip(" ，,;；。、"):
                                desc_lines.append(prefix)
                            name = m.group(1).strip()
                            value = m.group(2).strip(" ，,;；。、\t") or "-"
                            if name:
                                fields.append({"name": name, "value": value, "inline": False})
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
            color=0xE67E22
        )
        for field in fields:
            embed.add_field(name=field["name"], value=field["value"], inline=False)
        if image:
            embed.set_image(url=image)
        return embed

    @staticmethod
    def _build_media_embeds(medias: List[MediaInfo], title: str) -> List[discord.Embed]:
        embeds: List[discord.Embed] = []
        for index, media in enumerate(medias[:10], start=1):
            overview = media.get_overview_string(80)
            desc_parts = [
                f"{media.type.value} | {media.vote_star}" if media.vote_star else media.type.value,
                overview
            ]
            embed = discord.Embed(
                title=f"{index}. {media.title_year}",
                url=media.detail_link or discord.Embed.Empty,
                description="\n".join([p for p in desc_parts if p]),
                color=0x5865F2
            )
            if media.get_poster_image():
                embed.set_thumbnail(url=media.get_poster_image())
            embeds.append(embed)
        if embeds:
            embeds[0].set_author(name=title)
        return embeds

    @staticmethod
    def _build_torrent_embeds(torrents: List[Context], title: str) -> List[discord.Embed]:
        embeds: List[discord.Embed] = []
        for index, context in enumerate(torrents[:10], start=1):
            torrent = context.torrent_info
            meta = MetaInfo(torrent.title, torrent.description)
            title_text = f"{meta.season_episode} {meta.resource_term} {meta.video_term} {meta.release_group}"
            title_text = re.sub(r"\s+", " ", title_text).strip()
            detail = [
                f"{torrent.site_name} | {StringUtils.str_filesize(torrent.size)} | {torrent.volume_factor} | {torrent.seeders}↑",
                meta.resource_term,
                meta.video_term
            ]
            embed = discord.Embed(
                title=f"{index}. {title_text or torrent.title}",
                url=torrent.page_url or discord.Embed.Empty,
                description="\n".join([d for d in detail if d]),
                color=0x00A86B
            )
            poster = getattr(torrent, "poster", None)
            if poster:
                embed.set_thumbnail(url=poster)
            embeds.append(embed)
        if embeds:
            embeds[0].set_author(name=title)
        return embeds

    @staticmethod
    def _build_default_buttons(count: int) -> List[List[dict]]:
        buttons: List[List[dict]] = []
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
    def _build_view(buttons: Optional[List[List[dict]]], link: Optional[str] = None) -> Optional[discord.ui.View]:
        has_buttons = buttons and any(buttons)
        if not has_buttons and not link:
            return None

        view = discord.ui.View(timeout=None)
        if buttons:
            for row_index, button_row in enumerate(buttons[:5]):
                for button in button_row[:5]:
                    if "url" in button:
                        btn = discord.ui.Button(label=button.get("text", "链接"),
                                                url=button["url"],
                                                style=discord.ButtonStyle.link)
                    else:
                        custom_id = (button.get("callback_data") or button.get("text") or f"btn-{row_index}")[:99]
                        btn = discord.ui.Button(label=button.get("text", "选择")[:80],
                                                custom_id=custom_id,
                                                style=discord.ButtonStyle.primary)
                    view.add_item(btn)
        elif link:
            view.add_item(discord.ui.Button(label="查看详情", url=link, style=discord.ButtonStyle.link))
        return view

    async def _resolve_channel(self, userid: Optional[str] = None, chat_id: Optional[str] = None):
        # 优先使用明确的聊天 ID
        if chat_id:
            channel = self._client.get_channel(int(chat_id))
            if channel:
                return channel
            try:
                return await self._client.fetch_channel(int(chat_id))
            except Exception as err:
                logger.warn(f"通过 chat_id 获取 Discord 频道失败：{err}")

        # 私聊
        if userid:
            dm = await self._get_dm_channel(str(userid))
            if dm:
                return dm

        # 配置的广播频道
        if self._broadcast_channel:
            return self._broadcast_channel
        if self._channel_id:
            channel = self._client.get_channel(self._channel_id)
            if not channel:
                try:
                    channel = await self._client.fetch_channel(self._channel_id)
                except Exception as err:
                    logger.warn(f"通过配置的频道ID获取 Discord 频道失败：{err}")
                    channel = None
            self._broadcast_channel = channel
            if channel:
                return channel

        # 按 Guild 寻找一个可用文本频道
        target_guilds = []
        if self._guild_id:
            guild = self._client.get_guild(self._guild_id)
            if guild:
                target_guilds.append(guild)
        else:
            target_guilds = list(self._client.guilds)

        for guild in target_guilds:
            for channel in guild.text_channels:
                if guild.me and channel.permissions_for(guild.me).send_messages:
                    self._broadcast_channel = channel
                    return channel
        return None

    async def _get_dm_channel(self, userid: str) -> Optional[discord.DMChannel]:
        if userid in self._user_dm_cache:
            return self._user_dm_cache.get(userid)
        try:
            user_obj = self._client.get_user(int(userid)) or await self._client.fetch_user(int(userid))
            if not user_obj:
                return None
            dm = user_obj.dm_channel or await user_obj.create_dm()
            if dm:
                self._user_dm_cache[userid] = dm
            return dm
        except Exception as err:
            logger.error(f"获取 Discord 私聊失败：{err}")
            return None

    def _should_process_message(self, message: discord.Message) -> bool:
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
        if not content:
            return ""
        if self._bot_user_id:
            mention_pattern = rf"<@!?{self._bot_user_id}>"
            content = re.sub(mention_pattern, "", content).strip()
        return content

    async def _post_to_ds(self, payload: Dict[str, Any]) -> None:
        try:
            proxy = None
            if settings.PROXY:
                proxy = settings.PROXY.get("https") or settings.PROXY.get("http")
            async with httpx.AsyncClient(timeout=10, verify=False, proxy=proxy) as client:
                await client.post(self._ds_url, json=payload)
        except Exception as err:
            logger.error(f"转发 Discord 消息失败：{err}")
