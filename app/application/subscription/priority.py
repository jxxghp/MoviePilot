"""订阅洗版优先级与缺失集计算的领域规则。

本模块是订阅按集事实（note / episode_priority）、洗版准入基线
（current_priority）和目标范围推导的唯一算法来源；链层只保留薄委托，
编排逻辑不得内联复制这些规则。所有函数均为纯计算：不读全局配置、
不做 I/O，仅操作传入的订阅快照、上下文与元数据对象。
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional, Set, Union, cast

from app.application.subscription.contract import SubscriptionSnapshot, subscribe_media_keys
from app.domain.context import Context
from app.domain.meta.metabase import MetaBase
from app.schemas.common import JsonData
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MediaType


def normalize_episode_priority(
    episode_priority: Optional[Mapping[str, int]],
) -> Dict[str, int]:
    """
    归一化按集洗版优先级状态。
    """
    if not isinstance(episode_priority, dict):
        return {}

    normalized = {}
    for episode, priority in episode_priority.items():
        try:
            normalized[str(int(episode))] = int(priority)
        except (TypeError, ValueError):
            continue
    return normalized


def is_full_best_version_enabled(subscribe: SubscriptionSnapshot) -> bool:
    """
    判断当前订阅是否启用了电视剧全集洗版。
    """
    return bool(subscribe.best_version_full) and bool(subscribe.best_version) and subscribe.type == MediaType.TV.value


def get_episode_priority(subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None) -> Dict[str, int]:
    """
    获取订阅按集洗版优先级状态。
    """
    episode_priority = normalize_episode_priority(subscribe.episode_priority)
    if episode_priority:
        return episode_priority

    if (
        subscribe.best_version
        and not is_full_best_version_enabled(subscribe)
        and subscribe.type == MediaType.TV.value
        and subscribe.current_priority is not None
    ):
        target_episodes = get_best_version_target_episodes(subscribe, total_episode=total_episode)
        return {str(episode): int(subscribe.current_priority) for episode in target_episodes}
    return {}


def get_best_version_target_episodes(subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None) -> List[int]:
    """
    获取洗版订阅目标剧集范围。
    """
    if subscribe.type != MediaType.TV.value:
        return []

    start_episode = subscribe.start_episode or 1
    total_episode = total_episode or subscribe.total_episode or 0
    if total_episode < start_episode:
        return []
    return list(range(start_episode, total_episode + 1))


def get_downloaded_best_version_episodes(
    subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
) -> List[int]:
    """
    获取洗版订阅目标范围内已下载到任意版本的剧集。

    分集洗版的完成态要求 priority==100，但订阅目标满足查询有时只需要确认
    目标集是否已下载过任意版本，因此这里按 note 与 episode_priority>0 统计。
    """
    if subscribe.type != MediaType.TV.value:
        return []

    start_episode = subscribe.start_episode or 1
    total_episode = total_episode or subscribe.total_episode or 0
    if total_episode < start_episode:
        return []
    target_episodes = set(range(start_episode, total_episode + 1))
    downloaded = set()
    for episode in subscribe.note or []:
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        if episode_number in target_episodes:
            downloaded.add(episode_number)
    for episode_key, priority in get_episode_priority(subscribe, total_episode=total_episode).items():
        if not str(episode_key).isdigit():
            continue
        try:
            if float(priority) > 0:
                episode_number = int(episode_key)
                if episode_number in target_episodes:
                    downloaded.add(episode_number)
        except (TypeError, ValueError):
            continue
    return sorted(downloaded)


def get_pending_best_version_episodes_with_priority(
    subscribe: SubscriptionSnapshot,
    episode_priority: Optional[Mapping[str, int]] = None,
    total_episode: Optional[int] = None,
) -> List[int]:
    """
    使用指定按集优先级状态获取当前仍需继续洗版的剧集。
    """
    target_episodes = get_best_version_target_episodes(subscribe, total_episode=total_episode)
    if not target_episodes:
        return []

    if episode_priority is None:
        normalized = get_episode_priority(subscribe, total_episode=total_episode)
    else:
        normalized = normalize_episode_priority(episode_priority)
    return [episode for episode in target_episodes if normalized.get(str(episode)) != 100]


def get_pending_best_version_episodes(
    subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
) -> List[int]:
    """
    获取当前仍需继续洗版的剧集。
    """
    return get_pending_best_version_episodes_with_priority(subscribe, total_episode=total_episode)


def compute_lack_episode(
    subscribe: SubscriptionSnapshot,
    no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
) -> int:
    """
    计算订阅范围内尚未下载到任何版本的集数。

    普通电视剧订阅以媒体库缺失结果为准；调用方没有缺失结果时按空缺失处理，
    避免入口级刷新失败把未知状态写成异常。洗版电视剧订阅按 note 与
    episode_priority>0 判断是否已有任意版本落点，priority<100 仍表示已下载过任意版本。
    """
    if subscribe.type != MediaType.TV.value:
        return 0

    if not subscribe.best_version:
        no_exists = no_exists or {}
        left_seasons = (
            next(
                (
                    no_exists.get(media_key)
                    for media_key in subscribe_media_keys(subscribe)
                    if no_exists.get(media_key) is not None
                ),
                {},
            )
            or {}
        )
        for season_info in left_seasons.values():
            if season_info.season != subscribe.season:
                continue
            left_episodes = season_info.episodes
            if not left_episodes:
                return season_info.total_episode or 0
            return len(left_episodes)
        return 0

    total_episode = subscribe.total_episode or 0
    if not total_episode:
        return 0
    start_episode = subscribe.start_episode or 1
    if total_episode < start_episode:
        return 0

    target_episodes = set(range(start_episode, total_episode + 1))
    downloaded: Set[int] = set()
    for episode in subscribe.note or []:
        try:
            episode_number = int(episode)
        except (TypeError, ValueError):
            continue
        if episode_number in target_episodes:
            downloaded.add(episode_number)
    for episode_key, priority in get_episode_priority(subscribe).items():
        try:
            if float(priority) <= 0:
                continue
            episode_number = int(episode_key)
        except (TypeError, ValueError):
            continue
        if episode_number in target_episodes:
            downloaded.add(episode_number)
    return len(target_episodes - downloaded)


def get_best_version_current_priority(
    subscribe: SubscriptionSnapshot,
    episode_priority: Optional[Mapping[str, int]] = None,
) -> int:
    """
    获取洗版订阅当前优先级状态。
    """
    if not subscribe.best_version or subscribe.type != MediaType.TV.value:
        return subscribe.current_priority or 0
    if is_full_best_version_enabled(subscribe):
        return subscribe.current_priority or 0

    target_episodes = get_best_version_target_episodes(subscribe)
    if not target_episodes:
        return subscribe.current_priority or 0

    if episode_priority is None:
        normalized = get_episode_priority(subscribe)
    else:
        normalized = normalize_episode_priority(episode_priority)
    return min(
        (normalized.get(str(episode), 0) for episode in target_episodes),
        default=0,
    )


def prepare_subscribe_progress_fields(
    subscribe: SubscriptionSnapshot,
    no_exists: Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]] = None,
    touch_last_update: Optional[bool] = False,
) -> Dict[str, JsonData]:
    """
    准备订阅进度持久化字段。

    该方法只返回待写字段，不主动写库。普通电视剧的 no_exists 为空时表示当前缺失结果为空；
    洗版电视剧按 note 与 episode_priority 计算未下载过任何版本的目标集数量。
    """
    update_data: Dict[str, JsonData] = {}
    if subscribe.type == MediaType.TV.value:
        if no_exists is None and not subscribe.best_version:
            no_exists = {}
        update_data["lack_episode"] = compute_lack_episode(subscribe, no_exists=no_exists)
        if subscribe.best_version and not is_full_best_version_enabled(subscribe):
            update_data["current_priority"] = get_best_version_current_priority(subscribe)
    if update_data and touch_last_update:
        update_data["last_update"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return update_data


def prepare_best_version_total_expansion_fields(
    subscribe: SubscriptionSnapshot,
    total_episode: int,
) -> Dict[str, JsonData]:
    """
    准备洗版电视剧总集数扩展后需要写库的字段。

    该方法基于新总集数构造临时不可变快照继续计算；实际数据库写入由调用方统一执行。
    """
    update_data: Dict[str, JsonData] = {"total_episode": total_episode}
    old_total_episode = subscribe.total_episode or 0
    working_episode_priority = subscribe.episode_priority
    working_current_priority = subscribe.current_priority
    if subscribe.best_version and subscribe.type == MediaType.TV.value:
        episode_priority = get_episode_priority(
            subscribe,
            total_episode=old_total_episode,
        )
        if (
            not is_full_best_version_enabled(subscribe)
            and not episode_priority
            and subscribe.current_priority is not None
        ):
            episode_priority = {
                str(episode): int(subscribe.current_priority)
                for episode in get_best_version_target_episodes(
                    subscribe,
                    total_episode=old_total_episode,
                )
            }
        update_data["episode_priority"] = episode_priority
        working_episode_priority = episode_priority
        if is_full_best_version_enabled(subscribe):
            update_data["current_priority"] = 0
            working_current_priority = 0

    working_snapshot = replace(
        subscribe,
        total_episode=total_episode,
        episode_priority=working_episode_priority,
        current_priority=working_current_priority,
    )
    update_data.update(prepare_subscribe_progress_fields(subscribe=working_snapshot, no_exists={}))
    return update_data


def prepare_best_version_total_change_fields(
    subscribe: SubscriptionSnapshot,
    total_episode: int,
    old_total_episode: int,
) -> Dict[str, JsonData]:
    """
    准备洗版电视剧总集数变化后需要写库的字段。

    总集数变化会改变目标范围，按集优先级只保留新范围内的目标集，避免范围外
    旧状态继续参与完成集、缺失集和当前优先级计算。
    """
    update_data: Dict[str, JsonData] = {"total_episode": total_episode}
    target_episodes = set(
        get_best_version_target_episodes(
            subscribe,
            total_episode=total_episode,
        )
    )
    episode_priority = get_episode_priority(
        subscribe,
        total_episode=old_total_episode,
    )
    filtered_priority = {
        str(episode): priority for episode, priority in episode_priority.items() if int(episode) in target_episodes
    }
    working_snapshot = replace(
        subscribe,
        total_episode=total_episode,
        episode_priority=filtered_priority,
    )
    if is_full_best_version_enabled(subscribe):
        current_priority = 0 if total_episode > old_total_episode else subscribe.current_priority
    else:
        current_priority = (
            0
            if not target_episodes
            else get_best_version_current_priority(
                working_snapshot,
                episode_priority=filtered_priority,
            )
        )
    update_data["episode_priority"] = filtered_priority
    update_data["current_priority"] = current_priority
    working_snapshot = replace(working_snapshot, current_priority=current_priority)
    update_data.update(prepare_subscribe_progress_fields(subscribe=working_snapshot, no_exists={}))
    return update_data


def prepare_total_episode_change_fields(
    subscribe: SubscriptionSnapshot,
    total_episode: int,
    old_total_episode: int,
) -> Dict[str, JsonData]:
    """
    准备已有订阅总集数持久化字段。
    """
    if subscribe.best_version and subscribe.type == MediaType.TV.value:
        return prepare_best_version_total_change_fields(
            subscribe=subscribe,
            total_episode=total_episode,
            old_total_episode=old_total_episode,
        )

    return {
        "total_episode": total_episode,
        "lack_episode": max(
            (subscribe.lack_episode or 0) + (total_episode - old_total_episode),
            0,
        ),
    }


def is_best_version_complete(subscribe: SubscriptionSnapshot) -> bool:
    """
    判断洗版订阅是否已完成。
    """
    if not subscribe.best_version:
        return False
    if subscribe.type != MediaType.TV.value:
        return bool(subscribe.current_priority == 100)
    if is_full_best_version_enabled(subscribe):
        return bool(subscribe.current_priority == 100)

    target_episodes = get_best_version_target_episodes(subscribe)
    if not target_episodes:
        return bool(subscribe.current_priority == 100)

    episode_priority = get_episode_priority(subscribe)
    return all(episode_priority.get(str(episode)) == 100 for episode in target_episodes)


def is_best_version_complete_with_priority(
    subscribe: SubscriptionSnapshot,
    episode_priority: Optional[Mapping[str, int]] = None,
) -> bool:
    """
    使用指定按集优先级状态判断洗版是否已完成。
    """
    if not subscribe.best_version:
        return False
    if subscribe.type != MediaType.TV.value:
        return bool(subscribe.current_priority == 100)
    if is_full_best_version_enabled(subscribe):
        return bool(subscribe.current_priority == 100)

    target_episodes = get_best_version_target_episodes(subscribe)
    if not target_episodes:
        return bool(subscribe.current_priority == 100)

    return not get_pending_best_version_episodes_with_priority(subscribe, episode_priority)


def get_downloaded_episodes(downloads: Optional[List[Context]]) -> List[int]:
    """
    获取本次下载实际涉及的剧集。
    """
    if not downloads:
        return []

    downloaded_episodes = set()
    for context in downloads:
        selected_episodes = getattr(context, "selected_episodes", None)
        if selected_episodes is None:
            selected_episodes = context.meta_info.episode_list if context.meta_info else []
        for episode in selected_episodes or []:
            try:
                downloaded_episodes.add(int(episode))
            except (TypeError, ValueError):
                continue
    return sorted(downloaded_episodes)


def get_best_version_completed_episodes(subscribe: SubscriptionSnapshot) -> List[int]:
    """
    获取已完成洗版的剧集。
    """
    episode_priority = get_episode_priority(subscribe)
    target_episodes = set(get_best_version_target_episodes(subscribe))
    return sorted(
        int(episode)
        for episode, priority in episode_priority.items()
        if str(episode).isdigit() and int(episode) in target_episodes and priority == 100
    )


def get_best_version_interested_episodes(
    subscribe: SubscriptionSnapshot,
    context: Context,
    priority: int,
) -> List[int]:
    """
    获取当前资源中仍值得继续洗版的剧集。
    """
    if subscribe.type != MediaType.TV.value:
        return []

    target_episodes = set(get_best_version_target_episodes(subscribe))
    if not target_episodes:
        return []

    selected_episodes = getattr(context, "selected_episodes", None)
    if selected_episodes is None:
        selected_episodes = context.meta_info.episode_list if context.meta_info else []
    if not selected_episodes:
        episode_priority = get_episode_priority(subscribe)
        return sorted(
            [
                episode
                for episode in target_episodes
                if (known_priority := episode_priority.get(str(episode))) is None or priority > known_priority
            ]
        )

    episode_priority = get_episode_priority(subscribe)
    interested = []
    for episode in selected_episodes:
        try:
            episode_num = int(episode)
        except (TypeError, ValueError):
            continue
        if episode_num not in target_episodes:
            continue
        current_priority = episode_priority.get(str(episode_num))
        if current_priority is None or priority > current_priority:
            interested.append(episode_num)
    return sorted(set(interested))


def prepare_best_version_tv_candidate(
    subscribe: SubscriptionSnapshot,
    context: Context,
    priority: int,
) -> bool:
    """
    校验电视剧洗版候选，并为分集模式设置允许下载的剧集范围。

    全集模式按当前准入基线筛选；分集模式设置能严格提升质量的目标集范围。
    """
    if is_full_best_version_enabled(subscribe):
        try:
            return int(priority or 0) > int(subscribe.current_priority or 0)
        except (TypeError, ValueError):
            return False

    interested_episodes = get_best_version_interested_episodes(
        subscribe=subscribe,
        context=context,
        priority=priority,
    )
    if not interested_episodes:
        return False
    context.allowed_episodes = set(interested_episodes)
    return True


def is_full_season_resource(meta: MetaBase, subscribe: SubscriptionSnapshot) -> bool:
    """
    判断候选资源是否覆盖订阅目标全集范围。
    """
    season_list = meta.season_list or [1]
    if len(season_list) != 1:
        return False
    if subscribe.season is not None and season_list[0] != subscribe.season:
        return False

    episodes = meta.episode_list
    if not episodes:
        # 资源未标出单集时按整季包处理，后续下载前仍会解析种子文件确认完整性。
        return True

    target_episodes = set(get_best_version_target_episodes(subscribe))
    if not target_episodes:
        return False
    return target_episodes.issubset(set(episodes))


def is_full_season_best_version_resource(meta: MetaBase, subscribe: SubscriptionSnapshot) -> bool:
    """
    判断候选资源是否符合全集洗版资源约束。
    """
    if not is_full_best_version_enabled(subscribe):
        return True

    return is_full_season_resource(meta=meta, subscribe=subscribe)


def should_prefer_full_pack_for_episode_best_version(
    subscribe: SubscriptionSnapshot,
    priority: int,
) -> bool:
    """
    判断分集洗版是否应优先下载整包。

    整包优先级必须严格高于每个目标集；否则交回按集路径，只下载能提升质量的集。
    """
    if subscribe.type != MediaType.TV.value or is_full_best_version_enabled(subscribe):
        return False

    target_episodes = get_best_version_target_episodes(subscribe)
    if not target_episodes:
        return False

    try:
        resource_priority = int(priority or 0)
    except (TypeError, ValueError):
        resource_priority = 0

    episode_priority = get_episode_priority(subscribe)
    return all(resource_priority > episode_priority.get(str(episode), 0) for episode in target_episodes)


def build_full_pack_first_no_exists(
    subscribe: SubscriptionSnapshot,
    mediakey: Union[int, str],
) -> Optional[Dict[Union[int, str], Dict[int, NotExistMediaInfo]]]:
    """
    构造分集洗版优先全集时使用的整季缺失范围。
    """
    if not subscribe.best_version or is_full_best_version_enabled(subscribe) or subscribe.type != MediaType.TV.value:
        return None

    target_episodes = get_best_version_target_episodes(subscribe)
    if not target_episodes:
        return None

    season_map: Dict[Optional[int], NotExistMediaInfo] = {
        subscribe.season: NotExistMediaInfo(
            season=subscribe.season,
            episodes=[],
            total_episode=subscribe.total_episode,
            start_episode=subscribe.start_episode or 1,
            require_complete_coverage=True,
        )
    }
    return {mediakey: cast(Dict[int, NotExistMediaInfo], season_map)}
