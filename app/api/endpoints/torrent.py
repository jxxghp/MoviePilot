from typing import Optional

from fastapi import Depends

from app.schemas.cache import TorrentCacheData as _SchemaTorrentCacheData
from app.schemas.cache import TorrentReidentifyData as _SchemaTorrentReidentifyData
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.chain.media import MediaChain
from app.chain.torrents import TorrentsChain
from app.runtime.config import settings
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.db.models import User
from app.api.deps import get_current_active_superuser, get_current_active_superuser_async
from app.schemas.types import (
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
    MusicTargetEntityType,
)
from app.foundation.crypto import HashUtils
from app.domain.media import is_music_media_source, normalize_music_type
from app.schemas.media import resolve_media_identity

router = ResponseAPIRouter()


@router.get(
    "/cache",
    summary="获取种子缓存",
    response_model=_SchemaResponse[_SchemaTorrentCacheData],
)
async def torrents_cache(_: User = Depends(get_current_active_superuser_async)):
    """
    获取当前种子缓存数据
    """
    torrents_chain = TorrentsChain()

    # 获取spider和rss两种缓存
    if settings.SUBSCRIBE_MODE == "rss":
        cache_info = await torrents_chain.async_get_torrents("rss")
    else:
        cache_info = await torrents_chain.async_get_torrents("spider")

    # 统计信息
    torrent_count = sum(len(torrents) for torrents in cache_info.values())

    # 转换为前端需要的格式
    torrent_data = []
    for domain, contexts in cache_info.items():
        for context in contexts:
            torrent_hash = HashUtils.md5(
                f"{context.torrent_info.title}{context.torrent_info.description}"
            )
            media_source, media_id = resolve_media_identity(media=context.media_info)
            torrent_data.append(
                {
                    "hash": torrent_hash,
                    "domain": domain,
                    "title": context.torrent_info.title,
                    "description": context.torrent_info.description,
                    "size": context.torrent_info.size,
                    "pubdate": context.torrent_info.pubdate,
                    "site_name": context.torrent_info.site_name,
                    "media_name": context.media_info.title
                    if context.media_info
                    else "",
                    "media_year": context.media_info.year if context.media_info else "",
                    "media_type": context.media_info.type if context.media_info else "",
                    "media_source": media_source,
                    "media_id": media_id,
                    "music_type": getattr(context.media_info, "music_type", None),
                    "season_episode": context.meta_info.season_episode
                    if context.meta_info
                    else "",
                    "resource_term": context.meta_info.resource_term
                    if context.meta_info
                    else "",
                    "enclosure": context.torrent_info.enclosure,
                    "page_url": context.torrent_info.page_url,
                    "poster_path": context.media_info.get_poster_image()
                    if context.media_info
                    else "",
                    "backdrop_path": context.media_info.get_backdrop_image()
                    if context.media_info
                    else "",
                }
            )

    return _SchemaResponse(
        success=True,
        data={"count": torrent_count, "sites": len(cache_info), "data": torrent_data},
    )


@router.delete(
    "/cache/{domain}/{torrent_hash}",
    summary="删除指定种子缓存",
    response_model=_SchemaResponse[None],
)
async def delete_cache(
    domain: str,
    torrent_hash: str,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    删除指定的种子缓存
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    :param _: 当前用户，必须是超级用户
    """

    torrents_chain = TorrentsChain()

    try:
        # 获取当前缓存
        cache_data = await torrents_chain.async_get_torrents()

        if domain not in cache_data:
            return _SchemaResponse(success=False, message=f"站点 {domain} 缓存不存在")

        # 查找并删除指定种子
        original_count = len(cache_data[domain])
        cache_data[domain] = [
            context
            for context in cache_data[domain]
            if HashUtils.md5(
                f"{context.torrent_info.title}{context.torrent_info.description}"
            )
            != torrent_hash
        ]

        if len(cache_data[domain]) == original_count:
            return _SchemaResponse(success=False, message="未找到指定的种子")

        # 保存更新后的缓存：影视与音乐分别回写各自存储文件
        video_cache, music_cache = torrents_chain.split_cache_contexts(cache_data)
        video_file, music_file = torrents_chain.cache_files()
        await torrents_chain.async_save_cache(video_cache, video_file)
        await torrents_chain.async_save_cache(music_cache, music_file)

        return _SchemaResponse(success=True, message="种子删除成功")
    except Exception as e:
        return _SchemaResponse(success=False, message=f"删除失败：{str(e)}")


@router.delete("/cache", summary="清理种子缓存", response_model=_SchemaResponse[None])
async def clear_cache(_: User = Depends(get_current_active_superuser_async)):
    """
    清理所有种子缓存
    """
    torrents_chain = TorrentsChain()

    try:
        await torrents_chain.async_clear_torrents()
        return _SchemaResponse(success=True, message="种子缓存清理完成")
    except Exception as e:
        return _SchemaResponse(success=False, message=f"清理失败：{str(e)}")


@router.post("/cache/refresh", summary="刷新种子缓存", response_model=_SchemaResponse[None])
def refresh_cache(_: User = Depends(get_current_active_superuser)):
    """
    刷新种子缓存
    """
    from app.chain.torrents import TorrentsChain

    torrents_chain = TorrentsChain()

    try:
        result = torrents_chain.refresh()

        # 统计刷新结果
        total_count = sum(len(torrents) for torrents in result.values())
        sites_count = len(result)

        return _SchemaResponse(
            success=True,
            message=f"缓存刷新完成，共刷新 {sites_count} 个站点，{total_count} 个种子",
        )
    except Exception as e:
        return _SchemaResponse(success=False, message=f"刷新失败：{str(e)}")


@router.post(
    "/cache/reidentify/{domain}/{torrent_hash}",
    summary="重新识别种子",
    response_model=_SchemaResponse[_SchemaTorrentReidentifyData],
)
async def reidentify_cache(
    domain: str,
    torrent_hash: str,
    media_source: Optional[MediaSource] = None,
    media_id: Optional[str] = None,
    music_type: Optional[MusicTargetEntityType] = None,
    _: User = Depends(get_current_active_superuser_async),
):
    """
    重新识别指定的种子
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    :param media_source: 媒体数据源
    :param media_id: 数据源原生 ID
    :param music_type: 音乐实体类型，仅支持单曲或专辑
    :param _: 当前用户，必须是超级用户
    """

    torrents_chain = TorrentsChain()
    media_chain = MediaChain()

    try:
        # 获取当前缓存
        cache_data = await torrents_chain.async_get_torrents()

        if domain not in cache_data:
            return _SchemaResponse(success=False, message=f"站点 {domain} 缓存不存在")

        # 查找指定种子
        target_context = None
        for context in cache_data[domain]:
            if (
                HashUtils.md5(
                    f"{context.torrent_info.title}{context.torrent_info.description}"
                )
                == torrent_hash
            ):
                target_context = context
                break

        if not target_context:
            return _SchemaResponse(success=False, message="未找到指定的种子")

        existing_music_type = normalize_music_type(
            getattr(target_context.media_info, "music_type", None),
            allow_artist=False,
        )
        normalized_music_type = normalize_music_type(
            music_type,
            allow_artist=False,
        )
        if music_type is not None and not normalized_music_type:
            return _SchemaResponse(
                success=False,
                message="音乐实体类型无效，仅支持 recording 或 album",
            )
        is_music = (
            getattr(target_context.media_info, "type", None) == MediaType.MUSIC
            or isinstance(target_context.meta_info, MetaMusic)
            or target_context.torrent_info.category
            in (MediaType.MUSIC, MediaType.MUSIC.value, "music")
            or is_music_media_source(media_source)
            or normalized_music_type is not None
        )
        if is_music and media_source and not is_music_media_source(media_source):
            return _SchemaResponse(
                success=False,
                message="音乐重新识别只能使用音乐元数据源",
            )
        if is_music and not normalized_music_type:
            normalized_music_type = existing_music_type or MUSIC_ENTITY_RECORDING

        # 重识别沿用原媒体域；音乐标题必须使用 MetaMusic，避免误入影视模块。
        if is_music:
            meta = (
                target_context.meta_info
                if isinstance(target_context.meta_info, MetaMusic)
                else MetaMusic.parse_query(target_context.torrent_info.title)
            )
        else:
            meta = MetaInfo(
                title=target_context.torrent_info.title,
                subtitle=target_context.torrent_info.description,
            )

        has_explicit_id = media_source is not None or media_id is not None
        if has_explicit_id and (not media_source or not media_id):
            return _SchemaResponse(
                success=False,
                message="媒体来源和媒体 ID 必须同时提供",
            )
        if has_explicit_id:
            # 手动指定媒体身份时执行精确识别。
            mediainfo = await media_chain.async_recognize_media(
                meta=meta,
                media_source=media_source,
                media_id=media_id,
                mtype=MediaType.MUSIC if is_music else None,
                music_type=normalized_music_type,
            )
        else:
            # 未指定 ID 时按标题识别，请求级来源仍用于约束本次识别。
            mediainfo = await media_chain.async_recognize_by_meta(
                meta,
                media_source=media_source,
                mtype=MediaType.MUSIC if is_music else None,
                music_type=normalized_music_type,
            )

        if not mediainfo:
            # 失败占位仍保留原媒体域，避免音乐缓存被误写进影视缓存文件。
            mediainfo = (
                MusicInfo(
                    music_type=normalized_music_type or MUSIC_ENTITY_RECORDING
                )
                if is_music
                else MediaInfo()
            )
        else:
            # 清理多余数据
            mediainfo.clear()

        # 更新上下文中的媒体信息
        target_context.media_info = mediainfo

        # 保存更新后的缓存：影视与音乐分别回写各自存储文件
        video_cache, music_cache = torrents_chain.split_cache_contexts(cache_data)
        video_file, music_file = torrents_chain.cache_files()
        await torrents_chain.async_save_cache(video_cache, video_file)
        await torrents_chain.async_save_cache(music_cache, music_file)

        return _SchemaResponse(
            success=True,
            message="重新识别完成",
            data={
                "media_name": mediainfo.title if mediainfo else "",
                "media_year": mediainfo.year if mediainfo else "",
                "media_type": mediainfo.type.value
                if mediainfo and mediainfo.type
                else "",
                "media_source": getattr(mediainfo, "media_source", None),
                "media_id": getattr(mediainfo, "media_id", None),
                "music_type": getattr(mediainfo, "music_type", None),
            },
        )
    except Exception as e:
        return _SchemaResponse(success=False, message=f"重新识别失败：{str(e)}")
