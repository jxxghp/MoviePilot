"""
订阅入库事实同步。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from app.application.subscription.contract import SubscriptionIdentity, SubscriptionSnapshot
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.domain.context import MediaInfo
from app.domain.meta.metabase import MetaBase
from app.runtime.events import Event
from app.runtime.log import logger
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType


class SubscribeLibraryOwner(_SubscribeOwnerBase):
    """
    整理入库后把交付事实同步到订阅的入库集号与缺失集数。
    """

    def _sync_library_progress_after_transfer(self, event: Event) -> None:
        """
        整理成功后同步订阅的入库事实，使订阅卡片与日历立即反映媒体库状态。

        整理成功才是媒体真正进入媒体库的时刻，此前订阅行的缺失集数只在搜索、
        刷新与检查轮次里按媒体库查询重算，因此刚入库的剧集会在日历上长时间
        显示为未入库。这里把本次交付的集号补写为订阅的入库事实，并同步收缩
        缺失集数，使入库状态在整理完成后立即可见。

        :param event: 整理完成事件
        """
        event_data = event.event_data if isinstance(event.event_data, dict) else {}
        mediainfo = event_data.get("mediainfo")
        meta = event_data.get("meta")
        if not isinstance(mediainfo, MediaInfo) or not isinstance(meta, MetaBase):
            return
        media_source, media_id = resolve_media_identity(media=mediainfo)
        if not media_source or not media_id:
            return
        if mediainfo.type == MediaType.MOVIE:
            try:
                self._apply_movie_library_progress(
                    media_source=media_source,
                    media_id=media_id,
                )
            except Exception as error:
                logger.warning(f"整理入库后同步电影订阅状态失败：{mediainfo.title_year}，{error}")
            return
        if mediainfo.type != MediaType.TV:
            return
        episodes = [episode for episode in meta.episode_list if episode > 0]
        if not episodes:
            return
        try:
            self._apply_library_progress(
                media_source=media_source,
                media_id=media_id,
                season=meta.begin_season,
                episodes=episodes,
            )
        except Exception as error:
            logger.warning(f"整理入库后同步订阅入库状态失败：{mediainfo.title_year}，{error}")

    def _apply_movie_library_progress(
        self,
        *,
        media_source: MediaSource,
        media_id: str,
    ) -> None:
        """
        电影整理入库后将订阅记录为已入库，保持日历与卡片状态立即可见。

        :param media_source: 整理媒体的来源身份
        :param media_id: 整理媒体的来源原生标识
        """
        subscribe = self.subscription_repository.get_by(
            SubscriptionIdentity(
                media_source=media_source,
                media_id=media_id,
                type=MediaType.MOVIE.value,
            )
        )
        if not subscribe:
            return
        recorded = {int(episode) for episode in subscribe.note or [] if str(episode).isdigit()}
        if 1 in recorded:
            return
        update_data = {
            "note": [1],
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._SubscribeChain__apply_subscribe_update(subscribe, update_data, scene="transfer")
        logger.info(f"电影订阅 {subscribe.name} 入库事实已同步：[1]")

    def _apply_library_progress(
        self,
        *,
        media_source: MediaSource,
        media_id: str,
        season: Optional[int],
        episodes: List[int],
    ) -> None:
        """
        按媒体身份定位订阅，并把本次交付的集号补写为入库事实。

        :param media_source: 整理媒体的来源身份
        :param media_id: 整理媒体的来源原生标识
        :param season: 本次交付的季号；为空表示不限定季
        :param episodes: 本次交付的集号
        """
        target_season = season if season is not None else 1
        subscribe = self.subscription_repository.get_by(
            SubscriptionIdentity(
                media_source=media_source,
                media_id=media_id,
                type=MediaType.TV.value,
                season=target_season,
            )
        )
        if not subscribe and target_season == 1:
            subscribe = self.subscription_repository.get_by(
                SubscriptionIdentity(
                    media_source=media_source,
                    media_id=media_id,
                    type=MediaType.TV.value,
                    season=None,
                )
            )
        if not subscribe:
            return
        if subscribe.season is not None and season is not None and subscribe.season != season:
            # 身份相同但季号不同的订阅不共享入库事实，避免把别季剧集写进本订阅。
            return
        update_data = self._build_library_progress_fields(subscribe=subscribe, episodes=episodes)
        if not update_data:
            return
        self._SubscribeChain__apply_subscribe_update(subscribe, update_data, scene="transfer")
        logger.info(
            f"订阅 {subscribe.name} 第{subscribe.season}季 入库事实已同步："
            f"已入库 {len(update_data.get('note') or [])} 集，"
            f"缺失 {update_data.get('lack_episode', subscribe.lack_episode)} 集"
        )

    @staticmethod
    def _target_episodes(subscribe: SubscriptionSnapshot) -> Set[int]:
        """
        计算订阅目标范围内的集号集合。

        :param subscribe: 订阅快照
        :return: 目标范围内的集号；总集数未知时为空集合
        """
        total_episode = subscribe.total_episode or 0
        if not total_episode:
            return set()
        start_episode = subscribe.start_episode or 1
        if total_episode < start_episode:
            return set()
        return set(range(start_episode, total_episode + 1))

    @classmethod
    def _recorded_episodes(cls, subscribe: SubscriptionSnapshot) -> Set[int]:
        """
        计算订阅已记录的入库集号。

        :param subscribe: 订阅快照
        :return: 已记录的集号集合
        """
        return {
            int(episode)
            for episode in subscribe.note or []
            if str(episode).isdigit()
        }

    @classmethod
    def _downloaded_episodes(cls, subscribe: SubscriptionSnapshot) -> Set[int]:
        """
        计算订阅已拥有任意版本的集号，含洗版订阅的按集优先级事实。

        :param subscribe: 订阅快照
        :return: 已有落点的集号集合
        """
        downloaded = cls._recorded_episodes(subscribe)
        for episode, priority in (subscribe.episode_priority or {}).items():
            if not str(episode).isdigit():
                continue
            try:
                if int(priority) > 0:
                    downloaded.add(int(episode))
            except (TypeError, ValueError):
                continue
        return downloaded

    @classmethod
    def _build_library_progress_fields(
        cls,
        subscribe: SubscriptionSnapshot,
        episodes: List[int],
    ) -> Dict[str, Any]:
        """
        准备入库事实同步的持久化字段。

        缺失集数按本次新增的入库集号同步收缩，保持缺失集数与入库集号来自同一
        份事实；客户端据此计算的已入库集数才不会与逐集入库明细互相矛盾。

        :param subscribe: 订阅快照
        :param episodes: 本次交付的集号
        :return: 待写入字段；没有新增入库事实时为空字典
        """
        target = cls._target_episodes(subscribe)
        if not target:
            return {}
        recorded = cls._recorded_episodes(subscribe)
        downloaded = cls._downloaded_episodes(subscribe)
        accepted = {
            episode for episode in episodes if episode in target and episode not in downloaded
        }
        if not accepted:
            return {}
        update_data: Dict[str, Any] = {
            "note": sorted(recorded | accepted),
            "last_update": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        }
        lack_episode = subscribe.lack_episode or 0
        if lack_episode:
            update_data["lack_episode"] = max(lack_episode - len(accepted), 0)
        return update_data
