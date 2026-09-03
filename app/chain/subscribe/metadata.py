"""订阅单条元数据刷新与完成对账协作者。"""

from collections.abc import Callable
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
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import MUSIC_ENTITY_ALBUM, MediaType

SubscriptionMediaT = TypeVar("SubscriptionMediaT", MediaInfo, MusicInfo)


def apply_subscription_classification(
    media: SubscriptionMediaT,
    subscribe: SubscriptionSnapshot,
) -> SubscriptionMediaT:
    """把稳定订阅分类覆盖应用到媒体副本，并兼容缺少新字段的旧快照。"""
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
    """刷新一条订阅的新鲜媒体事实、持久字段与可选完成状态。"""

    def _check_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        fresh_fact_lease: FreshFactLease,
        *,
        reconcile_completion: bool,
        media_chain_factory: Callable[[], Any],
    ) -> Optional[SubscriptionSnapshot]:
        """刷新单条订阅元数据，并按需复用同一事实执行完成对账。"""
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
