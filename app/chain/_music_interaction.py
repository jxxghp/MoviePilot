"""消息渠道音乐交互的解析、候选展示及类型转换。"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Protocol, Tuple

from app.chain.media import MediaChain
from app.domain import title as title_rules
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.foundation import url as url_tools
from app.schemas.message import Message
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    NotificationChannel,
)

if TYPE_CHECKING:
    from app.application.messaging.media import PendingMediaInteraction
    from app.domain.meta.metabase import MetaBase


class MusicInteractionHost(Protocol):
    """声明音乐候选展示复用的消息交互链能力。"""

    def _page_items(
        self,
        items: List[Any],
        page: int,
        page_size: int,
    ) -> Tuple[List[Any], int, int]:
        """返回交互候选的当前页、页码及总页数。"""
        ...

    def _page_size(self, channel: Optional[NotificationChannel]) -> int:
        """按消息渠道能力返回候选列表的分页大小。"""
        ...

    def _supports_interactive_buttons(
        self,
        channel: Optional[NotificationChannel],
    ) -> bool:
        """判断消息渠道是否支持按钮与回调。"""
        ...

    def _create_media_buttons(
        self,
        channel: NotificationChannel,
        request: PendingMediaInteraction,
        items: List[MediaInfo | MusicInfo],
        total: int,
        total_pages: int,
    ) -> List[List[Dict[str, str]]]:
        """为候选生成可交互按钮。"""
        ...

    def post_message(self, message: Message) -> None:
        """将渠道通知交给宿主消息发送实现。"""
        ...


def resolve_music_action(text: str) -> Tuple[Optional[str], str]:
    """解析音乐交互前缀，避免把普通聊天误判为音乐搜索。"""
    normalized = (text or "").strip()
    action_names = {
        "搜索": "MusicSearch",
        "下载": "MusicReSearch",
        "订阅": "MusicSubscribe",
        "洗版": "MusicReSubscribe",
    }
    match = re.match(
        r"^(?:音乐\s*(?P<music_first>搜索|下载|订阅|洗版)|"
        r"(?P<action_first>搜索|下载|订阅|洗版)\s*音乐)\s*[:：]?\s*(?P<content>.*)$",
        normalized,
    )
    if match:
        action = match.group("music_first") or match.group("action_first")
        return action_names[action], match.group("content").strip()

    match = re.match(
        r"^搜(?:索)?(?:歌曲|歌)\s*[:：]?\s*(?P<content>.*)$",
        normalized,
    )
    if match:
        return "MusicSearch", match.group("content").strip()

    match = re.match(r"^音乐\s*[:：]\s*(?P<content>.*)$", normalized)
    if match:
        return "MusicSearch", match.group("content").strip()
    if normalized.startswith("音乐 "):
        return "MusicSearch", normalized[len("音乐 "):].strip()
    return None, normalized


def resolve_media_action(text: str) -> Tuple[Optional[str], str]:
    """归类影视或音乐搜索、订阅及普通聊天输入。"""
    music_action, music_content = resolve_music_action(text)
    if music_action:
        return music_action, music_content
    if text.startswith("订阅"):
        return "Subscribe", re.sub(r"订阅[:：\s]*", "", text)
    if text.startswith("洗版"):
        return "ReSubscribe", re.sub(r"洗版[:：\s]*", "", text)
    if text.startswith("搜索") or text.startswith("下载"):
        return "ReSearch", re.sub(r"(搜索|下载)[:：\s]*", "", text)
    if url_tools.is_link(text):
        return None, text
    if not title_rules.is_media_title_like(text):
        return None, text
    return "Search", text


def search_music_candidates(
    query: str,
) -> Tuple[MetaMusic, str, List[MusicInfo]]:
    """用音乐目录搜索单曲与专辑候选，并返回交互展示标题。"""
    meta = MetaMusic.parse_query(query)
    candidates = MediaChain().search_music(
        query=query,
        limit=20,
        music_types=(MUSIC_ENTITY_RECORDING, MUSIC_ENTITY_ALBUM),
    )
    return meta, meta.title or query, candidates


def format_music_candidate(index: int, media: MusicInfo) -> str:
    """格式化音乐单曲或专辑候选，明确展示实体类型和艺术家。"""
    entity = "专辑" if media.music_type == MUSIC_ENTITY_ALBUM else "单曲"
    title = media.title_year or media.title or "未命名音乐"
    details = [f"{index}. [{entity}] {title}"]
    artist = media.artist or media.album_artist
    if artist:
        details.append(artist)
    if media.music_type != MUSIC_ENTITY_ALBUM and media.album:
        details.append(f"专辑：{media.album}")
    return " — ".join(details)


def media_exists_retry_prompt(
    mediainfo: MediaInfo | MusicInfo,
    meta: Optional[MetaBase],
    keyword: str,
    *,
    subscription: bool,
) -> Tuple[str, str]:
    """返回媒体库已存在时适用于影视或音乐的重试提示。"""
    media_label = mediainfo.title_year
    if not isinstance(mediainfo, MusicInfo) and meta:
        media_label += meta.sea or ""
    if isinstance(mediainfo, MusicInfo):
        retry_hint = f"音乐 洗版 {keyword}" if subscription else f"音乐 下载 {keyword}"
    else:
        retry_hint = "洗版 XXX" if subscription else "搜索 名称 或 下载 名称"
    return media_label, retry_hint


def selected_music_type(mediainfo: MediaInfo | MusicInfo) -> Optional[str]:
    """返回已选音乐实体类型，影视媒体不携带音乐类型。"""
    return mediainfo.music_type if isinstance(mediainfo, MusicInfo) else None


def post_music_candidates(
    host: MusicInteractionHost,
    request: PendingMediaInteraction,
    channel: NotificationChannel,
    source: str,
    userid: int | str,
    original_message_id: int | str | None = None,
    original_chat_id: Optional[str] = None,
) -> None:
    """按渠道能力以文字和按钮展示音乐候选，支持纯文本回复编号。"""
    page_items, page, total_pages = host._page_items(
        items=request.items,
        page=request.page,
        page_size=host._page_size(channel),
    )
    request.page = page
    total = len(request.items)
    title = f"【音乐：{request.title}】共找到{total}条相关信息"
    if host._supports_interactive_buttons(channel):
        title += "，请选择操作"
        buttons = host._create_media_buttons(
            channel=channel,
            request=request,
            items=page_items,
            total=total,
            total_pages=total_pages,
        )
    else:
        page_size = host._page_size(channel)
        title += (
            "，请回复对应数字选择"
            if total <= page_size
            else "，请回复对应数字选择（p: 上一页 n: 下一页）"
        )
        buttons = None
    text = "\n".join(
        format_music_candidate(index, media)
        for index, media in enumerate(page_items, start=1)
        if isinstance(media, MusicInfo)
    )
    host.post_message(
        Message(
            channel=channel,
            source=source,
            title=title,
            text=text,
            userid=userid,
            buttons=buttons,
            original_message_id=original_message_id,
            original_chat_id=original_chat_id,
            save_history=False,
        )
    )
