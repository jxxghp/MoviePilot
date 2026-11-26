"""
种子缓存相关的应用用例。
"""

import copy
from collections.abc import Awaitable, Callable
from typing import Any, Optional

from app.domain.context import Context, MediaInfo, MusicInfo
from app.domain.media import is_music_media_source, normalize_music_type
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.foundation.crypto import HashUtils
from app.runtime.cache import async_cache_mutation
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource, MediaType, MusicTargetEntityType


class TorrentCacheRecognitionService:
    """
    执行种子缓存条目的媒体重新识别用例。
    """

    def __init__(self, torrents_chain: Any, media_chain: Any,
                 reset_download_failure: Optional[Callable[[list[Context]], Awaitable[None]]] = None) -> None:
        """
        初始化缓存和媒体识别依赖。

        :param torrents_chain: 提供缓存读取、分类与持久化的种子链
        :param media_chain: 执行元数据识别的媒体链
        :param reset_download_failure: 缓存成功保存且识别出媒体身份后，清除该资源的下载失败退避
        """
        self.torrents_chain = torrents_chain
        self.media_chain = media_chain
        self.reset_download_failure = reset_download_failure

    async def execute(
        self,
        domain: str,
        torrent_hash: str,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        music_type: Optional[MusicTargetEntityType] = None,
    ) -> tuple[bool, str, Optional[dict[str, object]]]:
        """
        重新识别缓存条目并持久化影视、音乐分离后的缓存。

        返回值保持端点原有的成功标识、用户消息和响应数据三元组，便于旧插件
        继续消费原始 HTTP 响应结构。成功识别并保存后只清除本资源的自动下载失败记录，
        不主动提交下载，也不影响其他资源和重复提交保护。
        """
        cache_data = await self.torrents_chain.async_get_torrents()
        if domain not in cache_data:
            return False, f"站点 {domain} 缓存不存在", None

        target_context = next(
            (
                context
                for context in cache_data[domain]
                if HashUtils.md5(
                    f"{context.torrent_info.title}{context.torrent_info.description}"
                )
                == torrent_hash
            ),
            None,
        )
        if not target_context:
            return False, "未找到指定的种子", None

        existing_music_type = normalize_music_type(
            getattr(target_context.media_info, "music_type", None), allow_artist=False
        )
        normalized_music_type = normalize_music_type(music_type, allow_artist=False)
        if music_type is not None and not normalized_music_type:
            return False, "音乐实体类型无效，仅支持 recording 或 album", None

        is_music = (
            getattr(target_context.media_info, "type", None) == MediaType.MUSIC
            or isinstance(target_context.meta_info, MetaMusic)
            or target_context.torrent_info.category
            in (MediaType.MUSIC, MediaType.MUSIC.value, "music")
            or is_music_media_source(media_source)
            or normalized_music_type is not None
        )
        if is_music and media_source and not is_music_media_source(media_source):
            return False, "音乐重新识别只能使用音乐元数据源", None
        if is_music and not normalized_music_type:
            normalized_music_type = existing_music_type or MUSIC_ENTITY_RECORDING

        meta = (
            target_context.meta_info
            if is_music and isinstance(target_context.meta_info, MetaMusic)
            else MetaMusic.parse_query(target_context.torrent_info.title)
            if is_music
            else MetaInfo(
                title=target_context.torrent_info.title,
                subtitle=target_context.torrent_info.description,
            )
        )
        has_explicit_id = media_source is not None or media_id is not None
        if has_explicit_id and (not media_source or not media_id):
            return False, "媒体来源和媒体 ID 必须同时提供", None
        if has_explicit_id:
            mediainfo = await self.media_chain.async_recognize_media(
                meta=meta,
                media_source=media_source,
                media_id=media_id,
                mtype=MediaType.MUSIC if is_music else None,
                music_type=normalized_music_type,
            )
        else:
            mediainfo = await self.media_chain.async_recognize_by_meta(
                meta,
                media_source=media_source,
                mtype=MediaType.MUSIC if is_music else None,
                music_type=normalized_music_type,
            )

        if not mediainfo:
            mediainfo = (
                MusicInfo(music_type=normalized_music_type or MUSIC_ENTITY_RECORDING)
                if is_music
                else MediaInfo()
            )
        else:
            mediainfo.clear()
        media_source_value, recognized_id = resolve_media_identity(media=mediainfo)
        recognized = bool(media_source_value and recognized_id)
        async with async_cache_mutation("torrent-snapshots"):
            # 远端识别期间不占写锁；提交时重读当前快照，仅修改本次目标。
            latest = await self.torrents_chain.async_get_torrents()
            current = next((context for context in latest.get(domain, [])
                            if HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}")
                            == torrent_hash), None)
            if current is None:
                return False, "种子缓存已刷新，目标条目不存在", None
            original = copy.copy(current)
            current.meta_info = meta
            current.media_info = mediainfo
            current.match_source = str(media_source_value) if recognized else "unknown"
            current.candidate_recognized = recognized
            current.media_info_is_target = False
            current.media_recognize_fail_count = 0 if recognized else 1
            video_cache, music_cache = self.torrents_chain.split_cache_contexts(latest)
            video_file, music_file = self.torrents_chain.cache_files()
            # 同一个资源不会跨分类移动；只写其所属缓存，避免无关文件失败或部分提交。
            is_music_cache = current.torrent_info.category in (MediaType.MUSIC, MediaType.MUSIC.value)
            cache, filename = (music_cache, music_file) if is_music_cache else (video_cache, video_file)
            try:
                await self.torrents_chain.async_save_cache(cache, filename, strict=True)
            except Exception:
                return False, "种子缓存保存失败，请稍后重试", None
        if recognized and self.reset_download_failure is not None:
            try:
                await self.reset_download_failure([original, current])
            except Exception as err:
                logger.error(f"重新识别后清除资源下载失败记录失败：{err}")
                return False, "重新识别已保存，但清除下载失败记录失败，请重试", None
        return True, "重新识别完成", {
            "media_name": mediainfo.title if mediainfo else "",
            "media_year": mediainfo.year if mediainfo else "",
            "media_type": mediainfo.type.value if mediainfo and mediainfo.type else "",
            "media_source": getattr(mediainfo, "media_source", None),
            "media_id": getattr(mediainfo, "media_id", None),
            "music_type": getattr(mediainfo, "music_type", None),
        }
