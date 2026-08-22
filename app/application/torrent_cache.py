"""种子缓存相关的应用用例。"""

from typing import Any, Optional

from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.foundation.crypto import HashUtils
from app.domain.media import is_music_media_source, normalize_music_type
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource, MediaType, MusicTargetEntityType


class TorrentCacheRecognitionService:
    """执行种子缓存条目的媒体重新识别用例。"""

    def __init__(self, torrents_chain: Any, media_chain: Any):
        """初始化缓存和媒体识别依赖。"""
        self.torrents_chain = torrents_chain
        self.media_chain = media_chain

    async def execute(
        self,
        domain: str,
        torrent_hash: str,
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        music_type: Optional[MusicTargetEntityType] = None,
    ) -> tuple[bool, str, Optional[dict]]:
        """重新识别缓存条目并持久化影视、音乐分离后的缓存。

        返回值保持端点原有的成功标识、用户消息和响应数据三元组，便于旧插件
        继续消费原始 HTTP 响应结构。
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
        target_context.media_info = mediainfo

        video_cache, music_cache = self.torrents_chain.split_cache_contexts(cache_data)
        video_file, music_file = self.torrents_chain.cache_files()
        await self.torrents_chain.async_save_cache(video_cache, video_file)
        await self.torrents_chain.async_save_cache(music_cache, music_file)
        return True, "重新识别完成", {
            "media_name": mediainfo.title if mediainfo else "",
            "media_year": mediainfo.year if mediainfo else "",
            "media_type": mediainfo.type.value if mediainfo and mediainfo.type else "",
            "media_source": getattr(mediainfo, "media_source", None),
            "media_id": getattr(mediainfo, "media_id", None),
            "music_type": getattr(mediainfo, "music_type", None),
        }
