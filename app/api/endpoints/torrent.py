from typing import Optional

from fastapi import APIRouter, Depends

from app import schemas
from app.chain.media import MediaChain
from app.chain.torrents import TorrentsChain
from app.core.config import settings
from app.core.context import MediaInfo
from app.core.metainfo import MetaInfo
from app.db.models import User
from app.db.user_oper import get_current_active_superuser
from app.utils.crypto import HashUtils

router = APIRouter()


@router.get("/cache", summary="获取种子缓存", response_model=schemas.Response)
def torrents_cache(_: User = Depends(get_current_active_superuser)):
    """
    获取当前种子缓存数据
    """
    torrents_chain = TorrentsChain()

    # 获取spider和rss两种缓存
    if settings.SUBSCRIBE_MODE == "rss":
        cache_info = torrents_chain.get_torrents("rss")
    else:
        cache_info = torrents_chain.get_torrents("spider")

    # 统计信息
    torrent_count = sum(len(torrents) for torrents in cache_info.values())

    # 转换为前端需要的格式
    torrent_data = []
    for domain, contexts in cache_info.items():
        for context in contexts:
            torrent_hash = HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}")
            torrent_data.append({
                "hash": torrent_hash,
                "domain": domain,
                "title": context.torrent_info.title,
                "description": context.torrent_info.description,
                "size": context.torrent_info.size,
                "pubdate": context.torrent_info.pubdate,
                "site_name": context.torrent_info.site_name,
                "media_name": context.media_info.title if context.media_info else "",
                "media_year": context.media_info.year if context.media_info else "",
                "media_type": context.media_info.type if context.media_info else "",
                "season_episode": context.meta_info.season_episode if context.meta_info else "",
                "resource_term": context.meta_info.resource_term if context.meta_info else "",
                "enclosure": context.torrent_info.enclosure,
                "page_url": context.torrent_info.page_url,
                "poster_path": context.media_info.get_poster_image() if context.media_info else "",
                "backdrop_path": context.media_info.get_backdrop_image() if context.media_info else ""
            })

    return schemas.Response(success=True, data={
        "count": torrent_count,
        "sites": len(cache_info),
        "data": torrent_data
    })


@router.delete("/cache/{domain}/{torrent_hash}", summary="删除指定种子缓存",
               response_model=schemas.Response)
def delete_cache(domain: str, torrent_hash: str, _: User = Depends(get_current_active_superuser)):
    """
    删除指定的种子缓存
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    :param _: 当前用户，必须是超级用户
    """

    torrents_chain = TorrentsChain()

    try:
        # 获取当前缓存
        cache_data = torrents_chain.get_torrents()

        if domain not in cache_data:
            return schemas.Response(success=False, message=f"站点 {domain} 缓存不存在")

        # 查找并删除指定种子
        original_count = len(cache_data[domain])
        cache_data[domain] = [
            context for context in cache_data[domain]
            if HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}") != torrent_hash
        ]

        if len(cache_data[domain]) == original_count:
            return schemas.Response(success=False, message="未找到指定的种子")

        # 保存更新后的缓存
        torrents_chain.save_cache(cache_data, torrents_chain.cache_file)

        return schemas.Response(success=True, message="种子删除成功")
    except Exception as e:
        return schemas.Response(success=False, message=f"删除失败：{str(e)}")


@router.delete("/cache", summary="清理种子缓存", response_model=schemas.Response)
def clear_cache(_: User = Depends(get_current_active_superuser)):
    """
    清理所有种子缓存
    """
    torrents_chain = TorrentsChain()

    try:
        torrents_chain.clear_torrents()
        return schemas.Response(success=True, message="种子缓存清理完成")
    except Exception as e:
        return schemas.Response(success=False, message=f"清理失败：{str(e)}")


@router.post("/cache/refresh", summary="刷新种子缓存", response_model=schemas.Response)
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

        return schemas.Response(success=True, message=f"缓存刷新完成，共刷新 {sites_count} 个站点，{total_count} 个种子")
    except Exception as e:
        return schemas.Response(success=False, message=f"刷新失败：{str(e)}")


@router.post("/cache/reidentify/{domain}/{torrent_hash}", summary="重新识别种子", response_model=schemas.Response)
def reidentify_cache(domain: str, torrent_hash: str,
                     tmdbid: Optional[int] = None, doubanid: Optional[str] = None,
                     _: User = Depends(get_current_active_superuser)):
    """
    重新识别指定的种子
    :param domain: 站点域名
    :param torrent_hash: 种子hash（使用title+description的md5）
    :param tmdbid: 手动指定的TMDB ID
    :param doubanid: 手动指定的豆瓣ID
    :param _: 当前用户，必须是超级用户
    """

    torrents_chain = TorrentsChain()
    media_chain = MediaChain()

    try:
        # 获取当前缓存
        cache_data = torrents_chain.get_torrents()

        if domain not in cache_data:
            return schemas.Response(success=False, message=f"站点 {domain} 缓存不存在")

        # 查找指定种子
        target_context = None
        for context in cache_data[domain]:
            if HashUtils.md5(f"{context.torrent_info.title}{context.torrent_info.description}") == torrent_hash:
                target_context = context
                break

        if not target_context:
            return schemas.Response(success=False, message="未找到指定的种子")

        # 重新识别
        meta = MetaInfo(title=target_context.torrent_info.title,
                        subtitle=target_context.torrent_info.description)
        if tmdbid or doubanid:
            # 手动指定媒体信息
            mediainfo = MediaChain().recognize_media(meta=meta, tmdbid=tmdbid, doubanid=doubanid)
        else:
            # 自动重新识别
            mediainfo = media_chain.recognize_by_meta(meta)

        if not mediainfo:
            # 创建空的媒体信息
            mediainfo = MediaInfo()
        else:
            # 清理多余数据
            mediainfo.clear()

        # 更新上下文中的媒体信息
        target_context.media_info = mediainfo

        # 保存更新后的缓存
        torrents_chain.save_cache(cache_data, TorrentsChain().cache_file)

        return schemas.Response(success=True, message="重新识别完成", data={
            "media_name": mediainfo.title if mediainfo else "",
            "media_year": mediainfo.year if mediainfo else "",
            "media_type": mediainfo.type.value if mediainfo and mediainfo.type else ""
        })
    except Exception as e:
        return schemas.Response(success=False, message=f"重新识别失败：{str(e)}")
