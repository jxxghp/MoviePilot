"""批量下载候选的缺集记账与覆盖判定规则。

本模块承载批量择优下载的纯决策规则：缺集字典（no_exists）的季/集
记账更新、整季候选的目标范围推导、允许集裁剪和下载去重键构造。
规则只操作传入数据，不做网络/下载器 I/O；下载执行顺序编排仍属于链层。
"""

from copy import deepcopy
from typing import Dict, List, Optional, Set

from app.domain.context import Context
from app.schemas.media import build_media_key, resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo


def update_no_exists_seasons(
        no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
        media_key: str,
        needed_seasons: List[int],
        current_seasons: List[int],
) -> List[int]:
    """
    更新缺失字典中的季数记账，返回剩余需要下载的季数。

    就地维护传入的 no_exists：已满足的季从记账中移除，媒体条目清空后整体删除。
    """
    # 剩余季数
    need = list(set(needed_seasons).difference(set(current_seasons)))
    # 清除已下载的季信息
    seas = deepcopy(no_exists.get(media_key))
    if seas:
        for season in list(seas):
            if season not in need:
                no_exists[media_key].pop(season)
            if not no_exists.get(media_key) and no_exists.get(media_key) is not None:
                no_exists.pop(media_key)
                break
    return need


def update_no_exists_episodes(
        no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
        media_key: str,
        season: int,
        needed_episodes: List[int],
        current_episodes: Set[int],
) -> List[int]:
    """
    更新缺失字典中的集数记账，返回剩余需要下载的集数。

    就地维护传入的 no_exists：仍有缺集时重写该季缺失信息，集齐后移除季条目，
    媒体条目清空后整体删除。
    """
    # 剩余集数
    need = list(set(needed_episodes).difference(set(current_episodes)))
    if need:
        not_exist = no_exists[media_key][season]
        no_exists[media_key][season] = NotExistMediaInfo(
            season=not_exist.season,
            episodes=need,
            total_episode=not_exist.total_episode,
            start_episode=not_exist.start_episode,
            require_complete_coverage=not_exist.require_complete_coverage
        )
    else:
        no_exists[media_key].pop(season)
        if not no_exists.get(media_key) and no_exists.get(media_key) is not None:
            no_exists.pop(media_key)
    return need


def get_season_episodes(
        no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
        media_key: str,
        season: int,
) -> int:
    """
    获取需要的季的集数；缺失信息不存在时返回 9999 表示不构成约束。
    """
    no_exist = no_exists.get(media_key)
    if not no_exist:
        return 9999
    season_info = no_exist.get(season)
    if not season_info:
        return 9999
    total = season_info.total_episode
    return int(total) if total is not None else 9999


def get_no_exist_media(
        no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
        media_key: str,
        season: int,
) -> Optional[NotExistMediaInfo]:
    """
    获取指定媒体和季的缺失信息。
    """
    if not no_exists:
        return None
    media = no_exists.get(media_key)
    if not media:
        return None
    return media.get(season)


def get_required_episodes(
        no_exists: Dict[str, Dict[int, NotExistMediaInfo]],
        media_key: str,
        season: int,
) -> Set[int]:
    """
    获取整季候选必须覆盖的目标集范围。
    """
    tv = get_no_exist_media(no_exists, media_key, season)
    if not tv:
        return set()
    if not tv.total_episode:
        return set()
    start = tv.start_episode or 1
    return set(range(start, tv.total_episode + 1))


def requires_complete_coverage(tv: Optional[NotExistMediaInfo]) -> bool:
    """
    判断当前缺失范围是否要求候选资源完整覆盖目标范围。
    """
    if not tv:
        return False
    return bool(tv.require_complete_coverage)


def apply_allowed_episodes(need_episodes: Set[int], context: Context) -> Set[int]:
    """
    根据候选携带的允许集裁剪 need_episodes，返回真正可下载的剧集集合。

    语义：allowed_episodes 为 None 表示调用方未约束，沿用 need_episodes；
    非空集合则与 need_episodes 取交集；空集合（显式拒绝）会被交集自然消解为空。
    调用方根据返回集合是否为空决定是否跳过当前候选。
    """
    effective = set(need_episodes)
    allowed = context.allowed_episodes
    if allowed is not None:
        effective &= set(allowed)
    return effective


def get_movie_download_key(context: Context) -> str:
    """
    获取电影下载去重键，确保失败候选不会阻断后续同名资源尝试。
    """
    if context.media_info is None:
        return ""
    return str(context.media_info.title_year)


def get_music_download_key(context: Context) -> str:
    """获取音乐下载去重键，同一订阅目标失败后仍可尝试后续候选。"""
    if context.media_info is None:
        return ""
    media_source, media_id = resolve_media_identity(media=context.media_info)
    return str(build_media_key(media_source, media_id) or context.media_info.title_year)
