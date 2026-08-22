from typing import Optional

from fastapi import Depends

from app.schemas.cache import TorrentCacheData as _SchemaTorrentCacheData
from app.schemas.cache import TorrentReidentifyData as _SchemaTorrentReidentifyData
from app.schemas.response import Response as _SchemaResponse
from app.api.response import ResponseAPIRouter
from app.application.orchestration.media import MediaChain
from app.application.orchestration.torrents import TorrentsChain
from app.application.configuration import get_api_runtime_config_snapshot
from app.api.deps import (
    get_current_active_superuser,
    get_current_active_superuser_async,
)
from app.schemas.types import (
    MediaSource,
    MusicTargetEntityType,
)
from app.foundation.crypto import HashUtils
from app.schemas.media import resolve_media_identity
from app.application.torrent_cache import TorrentCacheRecognitionService

router = ResponseAPIRouter()


@router.get(
    "/cache",
    summary="获取种子缓存",
    response_model=_SchemaResponse[_SchemaTorrentCacheData],
)
async def torrents_cache(_: object = Depends(get_current_active_superuser_async)):
    """
    获取当前种子缓存数据
    """
    torrents_chain = TorrentsChain()

    # 获取spider和rss两种缓存
    if get_api_runtime_config_snapshot().subscribe_mode == "rss":
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
    _: object = Depends(get_current_active_superuser_async),
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
async def clear_cache(_: object = Depends(get_current_active_superuser_async)):
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
def refresh_cache(_: object = Depends(get_current_active_superuser)):
    """
    刷新种子缓存
    """
    from app.application.orchestration.torrents import TorrentsChain

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
    _: object = Depends(get_current_active_superuser_async),
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

    try:
        service = TorrentCacheRecognitionService(TorrentsChain(), MediaChain())
        success, message, data = await service.execute(
            domain=domain,
            torrent_hash=torrent_hash,
            media_source=media_source,
            media_id=media_id,
            music_type=music_type,
        )
        return _SchemaResponse(
            success=success,
            message=message,
            data=data,
        )
    except Exception as e:
        return _SchemaResponse(success=False, message=f"重新识别失败：{str(e)}")
