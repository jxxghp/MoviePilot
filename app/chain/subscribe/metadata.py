"""
订阅单条元数据刷新与完成对账协作者。
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional, TypeVar, cast

from app.application.classification.reference import (
    subscription_classification_override,
)
from app.application.subscription.contract import (
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
)
from app.application.subscription.facts import FreshFactLease
from app.chain.media import MediaChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaSource, MediaType

SubscriptionMediaT = TypeVar("SubscriptionMediaT", MediaInfo, MusicInfo)


@dataclass(frozen=True, slots=True)
class SubscriptionSearchTarget:
    """
    所有媒体订阅进入同一搜索编排前的目标和缺失范围快照。

    :param subscribe: 准备阶段刷新后的订阅持久化快照
    :param meta: 用于资源检索和匹配的标题解析元数据
    :param media: 已识别并应用订阅分类覆盖的媒体目标
    :param missing: 按媒体键和季号组织的缺集范围，音乐目标使用空映射
    :param media_key: 用于查询缺失范围与完成对账的媒体键，无法确定时为 None
    """

    subscribe: SubscriptionSnapshot
    meta: MetaBase
    media: MediaInfo | MusicInfo
    missing: dict[str, dict[int, NotExistMediaInfo]]
    media_key: Optional[str | int]


def prepare_search_target(owner: _SubscribeOwnerBase, subscribe: SubscriptionSnapshot,
                          media_chain: MediaChain, ensure_active: Callable[[], None]) -> Optional[SubscriptionSearchTarget]:
    """
    在准备阶段保留实体差异，之后统一复用搜索、预算和交付流程。
    """
    if subscribe.type == MediaType.MUSIC.value:
        target = owner._prepare_music_subscribe(subscribe)
        if not target:
            return None
        current, media, meta = target
        ensure_active()
        return SubscriptionSearchTarget(current, meta, media, {}, subscribe_media_key(current))
    try:
        meta = build_subscribe_meta(subscribe)
    except ValueError:
        logger.error(f"订阅《{subscribe.name}》的媒体类型不受支持，暂时无法搜索")
        return None
    media = media_chain.recognize_media(
        meta=meta, mtype=meta.type, **subscribe_recognize_kwargs(subscribe),
        episode_group=subscribe.episode_group, cache=False,
    )
    if not media:
        logger.warning(f"未识别到媒体信息，标题：{subscribe.name}，媒体来源：{subscribe.media_source}，媒体 ID：{subscribe.media_id}")
        return None
    ensure_active()
    media = apply_subscription_classification(media, subscribe)
    key = subscribe_media_key(subscribe)
    exists, missing = owner.check_and_handle_existing_media(
        subscribe=subscribe, meta=meta, mediainfo=media, mediakey=key,
    )
    return None if exists else SubscriptionSearchTarget(subscribe, meta, media, missing, key)


def apply_subscription_classification(
    media: SubscriptionMediaT,
    subscribe: SubscriptionSnapshot,
) -> SubscriptionMediaT:
    """
    把稳定订阅分类覆盖应用到媒体副本，并兼容缺少新字段的旧快照。
    """
    override = subscription_classification_override(
        category_id=getattr(subscribe, "media_category_id", None),
        path_snapshot=getattr(subscribe, "media_category", None),
        media_type=media.type,
    )
    if override is None:
        return media
    finalized = MediaChain()._finalize_recognition_result(
        media,
        effective_override=override,
    )
    return finalized or media


class SubscribeMetadataOwner(_SubscribeOwnerBase):
    """
    刷新一条订阅的新鲜媒体事实、持久字段与可选完成状态。
    """

    def _retry_subscribe_media_identity(
        self,
        subscribe: SubscriptionSnapshot,
        meta: MetaBase,
        media_chain_factory: Callable[[], Any],
    ) -> Optional[SubscriptionSnapshot]:
        """
        在元数据巡检中重试豆瓣或 Bangumi 到 TMDB 的订阅身份转换。

        该补偿复用既有的 TMDB 元数据巡检周期。没有匹配结果、外部识别失败或目标身份
        已被其他订阅占用时保留原订阅，避免转换失败影响当前订阅的后续搜索。

        :param subscribe: 当前订阅快照
        :param meta: 当前订阅的媒体类型和标题元数据
        :param media_chain_factory: 媒体链构造入口
        :return: 转换成功并完成持久化后的订阅快照；不适用或失败时返回 None
        """
        if meta.type not in (MediaType.MOVIE, MediaType.TV):
            return None
        is_tmdb_recognize_source = getattr(self, "_SubscribeChain__is_tmdb_recognize_source", None)
        if is_tmdb_recognize_source is None or not is_tmdb_recognize_source():
            return None
        source, media_id = resolve_media_identity(media=subscribe)
        if source not in (MediaSource.Douban, MediaSource.Bangumi) or not media_id:
            return None

        media_chain = media_chain_factory()
        try:
            tmdb_info = media_chain.convert_media_identity(
                target_source=MediaSource.TMDB,
                media_source=source,
                media_id=media_id,
                mtype=meta.type,
                season=subscribe.season,
            )
            tmdb_id = tmdb_info.get("id") if isinstance(tmdb_info, dict) else None
            if not tmdb_id:
                logger.info(
                    f"订阅身份巡检未找到 TMDB 对应条目：{source}:{media_id}，"
                    f"暂时沿用原身份：{subscribe.name}"
                )
                return None
            converted = media_chain.recognize_media(
                meta=meta,
                mtype=meta.type,
                media_source=MediaSource.TMDB,
                media_id=str(tmdb_id),
                episode_group=subscribe.episode_group,
                cache=False,
            )
        except Exception as err:
            logger.warning(
                f"订阅身份巡检将 {source}:{media_id} 转换为 TMDB 失败，"
                f"暂时沿用原身份：{subscribe.name}，原因：{err}"
            )
            return None

        if not converted:
            logger.info(
                f"订阅身份巡检未识别到 TMDB 媒体：{source}:{media_id}，"
                f"暂时沿用原身份：{subscribe.name}"
            )
            return None

        target_season = subscribe.season
        projected_season = tmdb_info.get("season") if isinstance(tmdb_info, dict) else None
        if target_season is None and isinstance(projected_season, int) and projected_season > 0:
            target_season = projected_season
        update_data: dict[str, Any] = {
            "name": converted.title,
            "year": str(converted.year) if converted.year is not None else None,
            "vote": converted.vote_average,
            "poster": converted.get_poster_image(),
            "backdrop": converted.get_backdrop_image(),
            "description": converted.overview,
            "media_source": MediaSource.TMDB,
            "media_id": str(tmdb_id),
        }
        if target_season != subscribe.season:
            update_data["season"] = target_season
        if meta.type == MediaType.TV and target_season is not None:
            episodes = (converted.seasons or {}).get(target_season) or []
            if episodes:
                update_data["total_episode"] = len(episodes)

        try:
            updated = self._SubscribeChain__apply_subscribe_update(
                subscribe,
                update_data,
                scene="identity_refresh",
            )
        except Exception as err:
            logger.warning(
                f"订阅身份巡检写入 TMDB 身份失败：{subscribe.name}，"
                f"暂时沿用原身份，原因：{err}"
            )
            return None
        logger.info(
            f"订阅身份巡检已将 {source}:{media_id} 转换为 TMDB {tmdb_id}：{converted.title_year}"
        )
        return cast(SubscriptionSnapshot, updated)

    def _check_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        fresh_fact_lease: FreshFactLease,
        *,
        reconcile_completion: bool,
        media_chain_factory: Callable[[], Any],
    ) -> Optional[SubscriptionSnapshot]:
        """
        刷新单条订阅元数据，并按需复用同一事实执行完成对账。
        """
        try:
            meta = build_subscribe_meta(subscribe)
        except ValueError:
            logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}")
            return None
        converted_subscribe = self._retry_subscribe_media_identity(
            subscribe,
            meta,
            media_chain_factory,
        )
        if converted_subscribe is not None:
            subscribe = converted_subscribe
            try:
                meta = build_subscribe_meta(subscribe)
            except ValueError:
                logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}")
                return None
        if meta.type == MediaType.MUSIC:
            mediainfo = self._recognize_music_subscribe(subscribe)
        else:
            mediainfo = fresh_fact_lease.get_or_load(
                subscribe,
                lambda: media_chain_factory().recognize_media(
                    meta=meta,
                    mtype=meta.type,
                    **subscribe_recognize_kwargs(subscribe),
                    episode_group=subscribe.episode_group,
                    cache=False,
                ),
            )
        if not mediainfo:
            logger.warning(
                f"未识别到媒体信息，标题：{subscribe.name}，"
                f"媒体来源：{subscribe.media_source}，媒体 ID：{subscribe.media_id}"
            )
            return None
        episodes = (
            mediainfo.seasons.get(subscribe.season) or []
            if meta.type == MediaType.TV
            else []
        )
        progress_update: dict[str, Any]
        if (
            subscribe.type == MediaType.TV.value
            and not subscribe.manual_total_episode
            and episodes
        ):
            current_total_episode = len(episodes)
            total_episode = self._SubscribeChain__apply_episodes_refresh(
                current_total_episode,
                season=subscribe.season,
                mediainfo=mediainfo,
                media_source=subscribe.media_source,
                media_id=subscribe.media_id,
                subscribe_id=subscribe.id,
                scene="refresh",
            )
            old_total_episode = subscribe.total_episode or 0
            if total_episode and total_episode < old_total_episode:
                total_episode = self._SubscribeChain__resolve_total_episode_decrease(
                    subscribe=subscribe,
                    candidate_total=total_episode,
                    meta=meta,
                    mediainfo=mediainfo,
                    mediakey=subscribe_media_key(subscribe),
                )
            if total_episode and total_episode != old_total_episode:
                progress_update = self._SubscribeChain__prepare_total_episode_change_fields(
                    subscribe=subscribe,
                    total_episode=total_episode,
                    old_total_episode=old_total_episode,
                )
            else:
                total_episode = subscribe.total_episode
                progress_update = {"lack_episode": subscribe.lack_episode}
                if subscribe.best_version:
                    progress_update = self._SubscribeChain__prepare_subscribe_progress_fields(
                        subscribe=subscribe,
                        no_exists={},
                    )
            logger.info(
                f"订阅 {subscribe.name} 总集数变化，更新总集数为{total_episode}，"
                f"缺失集数为{progress_update.get('lack_episode', subscribe.lack_episode)} ..."
            )
        else:
            total_episode = subscribe.total_episode
            progress_update = {"lack_episode": subscribe.lack_episode}
            if subscribe.best_version and subscribe.type == MediaType.TV.value:
                progress_update = self._SubscribeChain__prepare_subscribe_progress_fields(
                    subscribe=subscribe,
                    no_exists={},
                )
        update_data = {
            "name": mediainfo.title,
            "year": str(mediainfo.year) if mediainfo.year is not None else None,
            "vote": mediainfo.vote_average,
            "poster": mediainfo.get_poster_image(),
            "backdrop": mediainfo.get_backdrop_image(),
            "description": mediainfo.overview,
            "media_source": resolve_media_identity(media=mediainfo)[0],
            "media_id": resolve_media_identity(media=mediainfo)[1],
            "total_episode": total_episode,
        }
        if meta.type == MediaType.MUSIC:
            music_type = getattr(mediainfo, "music_type", None)
            update_data.update(
                {
                    "music_type": music_type,
                    "total_tracks": getattr(mediainfo, "total_tracks", None)
                    if music_type == MUSIC_ENTITY_ALBUM
                    else None,
                }
            )
        update_data.update(progress_update)
        updated = self._SubscribeChain__apply_subscribe_update(
            subscribe,
            update_data,
            scene="metadata_refresh",
        )
        if reconcile_completion and updated.state in self.get_states_for_search("R"):
            self.reconcile_subscription_completion(
                subscribe=updated,
                meta=meta,
                mediainfo=mediainfo,
            )
        return cast(SubscriptionSnapshot, updated)
