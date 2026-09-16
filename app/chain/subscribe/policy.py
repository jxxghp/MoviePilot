"""订阅优先级、剧集范围与来源编码策略"""

import json
from typing import Any, Callable, Dict, List, Optional, Tuple, Union, cast

from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.subscription import priority as _priority
from app.application.subscription.contract import (
    SubscriptionSnapshot,
)
from app.application.subscription.execution import SubscriptionExecutionContext
from app.chain.download import DownloadChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.domain.context import (
    Context,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.schemas.mediaserver import NotExistMediaInfo as _SchemaNotExistMediaInfo
from app.schemas.types import (
    MediaType,
)


class _SubscribePriorityPolicyOwner(_SubscribeOwnerBase):
    """订阅优先级、剧集范围与来源编码策略 owner。"""

    @staticmethod
    def _SubscribeChain__normalize_episode_priority(
        episode_priority: Optional[dict[str, int]],
    ) -> Dict[str, int]:
        """归一化按集洗版优先级状态。"""
        result: Dict[str, int] = _priority.normalize_episode_priority(episode_priority)
        return result

    @classmethod
    def _SubscribeChain__get_episode_priority(
        cls, subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
    ) -> Dict[str, int]:
        """获取订阅按集洗版优先级状态。"""
        result: Dict[str, int] = _priority.get_episode_priority(subscribe, total_episode=total_episode)
        return result

    @classmethod
    def get_episode_priority(cls, subscribe: SubscriptionSnapshot) -> Dict[str, int]:
        """对外暴露按集洗版优先级状态。"""
        result: Dict[str, int] = _priority.get_episode_priority(subscribe)
        return result

    @classmethod
    def _SubscribeChain__get_best_version_target_episodes(
        cls, subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
    ) -> List[int]:
        """获取洗版订阅目标剧集范围。"""
        result: List[int] = _priority.get_best_version_target_episodes(subscribe, total_episode=total_episode)
        return result

    @classmethod
    def _SubscribeChain__get_downloaded_best_version_episodes(
        cls, subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
    ) -> List[int]:
        """获取洗版订阅目标范围内已下载到任意版本的剧集。"""
        result: List[int] = _priority.get_downloaded_best_version_episodes(
            subscribe,
            total_episode=total_episode,
        )
        return result

    @classmethod
    def _SubscribeChain__get_pending_best_version_episodes_with_priority(
        cls,
        subscribe: SubscriptionSnapshot,
        episode_priority: Optional[dict[str, int]] = None,
        total_episode: Optional[int] = None,
    ) -> List[int]:
        """使用指定按集优先级状态获取当前仍需继续洗版的剧集。"""
        result: List[int] = _priority.get_pending_best_version_episodes_with_priority(
            subscribe,
            episode_priority=episode_priority,
            total_episode=total_episode,
        )
        return result

    @classmethod
    def _get_pending_best_version_episodes(
        cls, subscribe: SubscriptionSnapshot, total_episode: Optional[int] = None
    ) -> List[int]:
        """获取当前仍需继续洗版的剧集。"""
        result: List[int] = _priority.get_pending_best_version_episodes(
            subscribe,
            total_episode=total_episode,
        )
        return result

    @classmethod
    def compute_lack_episode(
        cls,
        subscribe: SubscriptionSnapshot,
        no_exists: Optional[Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]] = None,
    ) -> int:
        """计算订阅范围内尚未下载到任何版本的集数。"""
        result: int = _priority.compute_lack_episode(subscribe, no_exists=no_exists)
        return result

    @classmethod
    def get_best_version_current_priority(
        cls,
        subscribe: SubscriptionSnapshot,
        episode_priority: Optional[dict[str, int]] = None,
    ) -> int:
        """获取洗版订阅当前优先级状态。"""
        result: int = _priority.get_best_version_current_priority(
            subscribe,
            episode_priority=episode_priority,
        )
        return result

    @classmethod
    def _SubscribeChain__prepare_best_version_total_expansion_fields(
        cls,
        subscribe: SubscriptionSnapshot,
        total_episode: int,
    ) -> Dict[str, Any]:
        """准备洗版电视剧总集数扩展后需要写库的字段。"""
        result: Dict[str, Any] = _priority.prepare_best_version_total_expansion_fields(subscribe, total_episode)
        return result

    @classmethod
    def _SubscribeChain__prepare_best_version_total_change_fields(
        cls,
        subscribe: SubscriptionSnapshot,
        total_episode: int,
        old_total_episode: int,
    ) -> Dict[str, Any]:
        """准备洗版电视剧总集数变化后需要写库的字段。"""
        result: Dict[str, Any] = _priority.prepare_best_version_total_change_fields(
            subscribe,
            total_episode,
            old_total_episode,
        )
        return result

    @classmethod
    def _SubscribeChain__prepare_total_episode_change_fields(
        cls,
        subscribe: SubscriptionSnapshot,
        total_episode: int,
        old_total_episode: int,
    ) -> Dict[str, Any]:
        """准备已有订阅总集数持久化字段，并同步内存对象上的总集数快照。"""
        result: Dict[str, Any] = _priority.prepare_total_episode_change_fields(
            subscribe,
            total_episode,
            old_total_episode,
        )
        return result

    @classmethod
    def _SubscribeChain__is_best_version_complete(cls, subscribe: SubscriptionSnapshot) -> bool:
        """判断洗版订阅是否已完成。"""
        result: bool = _priority.is_best_version_complete(subscribe)
        return result

    @classmethod
    def is_best_version_complete(cls, subscribe: SubscriptionSnapshot) -> bool:
        """对外暴露洗版完成判断。"""
        result: bool = _priority.is_best_version_complete(subscribe)
        return result

    @classmethod
    def _SubscribeChain__is_best_version_complete_with_priority(
        cls,
        subscribe: SubscriptionSnapshot,
        episode_priority: Optional[dict[str, int]] = None,
    ) -> bool:
        """使用指定按集优先级状态判断洗版是否已完成。"""
        result: bool = _priority.is_best_version_complete_with_priority(
            subscribe,
            episode_priority=episode_priority,
        )
        return result


class SubscribePolicyOwner(_SubscribePriorityPolicyOwner):
    """订阅提交前复核与下载治理策略 owner。"""

    @staticmethod
    def _SubscribeChain__get_downloaded_episodes(downloads: Optional[List[Context]]) -> List[int]:
        """获取本次下载实际涉及的剧集。"""
        result: List[int] = _priority.get_downloaded_episodes(downloads)
        return result

    @classmethod
    def _SubscribeChain__get_best_version_completed_episodes(cls, subscribe: SubscriptionSnapshot) -> List[int]:
        """获取已完成洗版的剧集。"""
        result: List[int] = _priority.get_best_version_completed_episodes(subscribe)
        return result

    @classmethod
    def _SubscribeChain__get_best_version_interested_episodes(
        cls,
        subscribe: SubscriptionSnapshot,
        context: Context,
        priority: int,
    ) -> List[int]:
        """获取当前资源中仍值得继续洗版的剧集。"""
        result: List[int] = _priority.get_best_version_interested_episodes(
            subscribe,
            context,
            priority,
        )
        return result

    @classmethod
    def _SubscribeChain__prepare_best_version_tv_candidate(
        cls,
        subscribe: SubscriptionSnapshot,
        context: Context,
        priority: int,
    ) -> bool:
        """校验电视剧洗版候选，并为分集模式设置允许下载的剧集范围。"""
        result: bool = _priority.prepare_best_version_tv_candidate(subscribe, context, priority)
        return result

    @classmethod
    def _SubscribeChain__is_full_best_version_enabled(cls, subscribe: SubscriptionSnapshot) -> bool:
        """判断当前订阅是否启用了电视剧全集洗版。"""
        result: bool = _priority.is_full_best_version_enabled(subscribe)
        return result

    @classmethod
    def _SubscribeChain__is_full_season_resource(cls, meta: MetaBase, subscribe: SubscriptionSnapshot) -> bool:
        """判断候选资源是否覆盖订阅目标全集范围。"""
        result: bool = _priority.is_full_season_resource(meta, subscribe)
        return result

    @classmethod
    def _SubscribeChain__is_full_season_best_version_resource(
        cls, meta: MetaBase, subscribe: SubscriptionSnapshot
    ) -> bool:
        """判断候选资源是否符合全集洗版资源约束。"""
        result: bool = _priority.is_full_season_best_version_resource(meta, subscribe)
        return result

    @classmethod
    def _SubscribeChain__should_prefer_full_pack_for_episode_best_version(
        cls,
        subscribe: SubscriptionSnapshot,
        priority: int,
    ) -> bool:
        """判断分集洗版是否应优先下载整包。"""
        result: bool = _priority.should_prefer_full_pack_for_episode_best_version(subscribe, priority)
        return result

    @classmethod
    def _SubscribeChain__build_full_pack_first_no_exists(
        cls,
        subscribe: SubscriptionSnapshot,
        mediakey: Union[int, str],
    ) -> Optional[Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]]:
        """构造分集洗版优先全集时使用的整季缺失范围。"""
        result: Optional[Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]] = (
            _priority.build_full_pack_first_no_exists(subscribe, mediakey)
        )
        return result

    @staticmethod
    def _SubscribeChain__load_current_subscription(
        loader: Callable[[int], Optional[SubscriptionSnapshot]],
        subscription_id: int,
    ) -> Optional[SubscriptionSnapshot]:
        """通过显式可调用边界读取提交前的最新订阅快照。"""
        return loader(subscription_id)

    def _SubscribeChain__download_best_version_with_full_pack_first(
        self,
        contexts: List[Context],
        no_exists: Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]],
        subscribe: SubscriptionSnapshot,
        mediakey: Union[int, str],
        username: Optional[str] = None,
        save_path: Optional[str] = None,
        downloader: Optional[str] = None,
        source: Optional[str] = None,
        execution_context: Optional[SubscriptionExecutionContext] = None,
    ) -> Tuple[List[Context], Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]]:
        """
        TV 分集洗版先尝试覆盖目标范围的全集资源，失败后回退到按集下载。
        """
        governance: Optional[SubscriptionDownloadGovernance] = None
        repository = getattr(self, "subscription_repository", None)
        get_current = getattr(repository, "get", None)
        if callable(get_current):
            current_get = cast(
                Callable[[int], Optional[SubscriptionSnapshot]],
                get_current,
            )
            current = self._SubscribeChain__load_current_subscription(
                current_get,
                subscribe.id,
            )
            if current is None:
                logger.info(f"订阅 {subscribe.id} 已删除，放弃本轮下载提交")
                return [], no_exists
            if current.state == "S":
                logger.info(f"订阅 {current.name} 已暂停，放弃本轮下载提交")
                return [], no_exists
            if self._SubscribeChain__candidate_contract_changed(subscribe, current):
                logger.info(f"订阅 {current.name} 的筛选或媒体身份已变化，放弃旧候选并等待下一轮")
                return [], no_exists
            if not contexts or not contexts[0].meta_info or not contexts[0].media_info:
                return [], no_exists

            exists, fresh_no_exists = self.check_and_handle_existing_media(
                subscribe=current,
                meta=contexts[0].meta_info,
                mediainfo=contexts[0].media_info,
                mediakey=mediakey,
            )
            if exists:
                return [], fresh_no_exists
            contexts = self._SubscribeChain__revalidate_download_contexts(current, contexts)
            if not contexts:
                return [], fresh_no_exists
            subscribe = current
            no_exists = fresh_no_exists
            username = current.username
            save_path = current.save_path
            downloader = current.downloader
            source = self.get_subscribe_source_keyword(current)
            governance = SubscriptionDownloadGovernance(
                cancelled=execution_context.should_stop if execution_context else None,
                mark_started=execution_context.mark_download_started if execution_context else None,
            )
        full_pack_no_exists = self._SubscribeChain__build_full_pack_first_no_exists(
            subscribe=subscribe, mediakey=mediakey
        )
        full_season_contexts = (
            [
                context
                for context in contexts
                if context.media_info.type == MediaType.TV
                and self._SubscribeChain__is_full_season_resource(meta=context.meta_info, subscribe=subscribe)
            ]
            if full_pack_no_exists
            else []
        )
        target_episodes = self._SubscribeChain__get_best_version_target_episodes(subscribe)
        target_range = f"{target_episodes[0]}-{target_episodes[-1]}" if target_episodes else "empty"
        episode_priority_gate = self._SubscribeChain__get_episode_priority(subscribe)
        full_pack_contexts = []
        for context in full_season_contexts:
            candidate_priority = context.torrent_info.pri_order
            accepted = self._SubscribeChain__should_prefer_full_pack_for_episode_best_version(
                subscribe=subscribe,
                priority=candidate_priority,
            )
            logger.info(
                f"{subscribe.name} 整包候选优先级判断：candidate_priority={candidate_priority}，"
                f"episode_priority={episode_priority_gate}，target_range={target_range}，"
                f"decision={'accept' if accepted else 'reject'}"
            )
            if accepted:
                full_pack_contexts.append(context)

        if full_season_contexts and not full_pack_contexts:
            logger.info(f"{subscribe.name} 全集候选优先级未高于全部目标集，回退到分集洗版")

        if full_pack_contexts:
            logger.info(f"{subscribe.name} 分集洗版优先尝试全集资源，共匹配到 {len(full_pack_contexts)} 个候选")
            downloads, lefts = DownloadChain().batch_download(
                contexts=full_pack_contexts,
                no_exists=full_pack_no_exists,
                username=username,
                save_path=save_path,
                downloader=downloader,
                source=source,
                custom_words=subscribe.custom_words,
                governance=governance,
            )
            if downloads:
                return downloads, lefts
            logger.info(f"{subscribe.name} 未下载到全集资源，回退到分集洗版")

        result: Tuple[List[Context], Dict[Union[int, str], Dict[int, _SchemaNotExistMediaInfo]]] = (
            DownloadChain().batch_download(
                contexts=contexts,
                no_exists=no_exists,
                username=username,
                save_path=save_path,
                downloader=downloader,
                source=source,
                custom_words=subscribe.custom_words,
                governance=governance,
            )
        )
        return result

    @staticmethod
    def _SubscribeChain__candidate_contract_changed(
        prepared: SubscriptionSnapshot,
        current: SubscriptionSnapshot,
    ) -> bool:
        """检测会让准备阶段候选失效的订阅身份和筛选字段变化。"""
        fields = (
            "type",
            "media_source",
            "media_id",
            "music_type",
            "season",
            "episode_group",
            "keyword",
            "sites",
            "include",
            "exclude",
            "quality",
            "resolution",
            "effect",
            "audio_quality",
            "audio_format",
            "min_bitrate",
            "min_bit_depth",
            "min_sample_rate",
            "filter_groups",
            "custom_words",
        )
        return any(getattr(prepared, field) != getattr(current, field) for field in fields)

    def _SubscribeChain__revalidate_download_contexts(
        self,
        subscribe: SubscriptionSnapshot,
        contexts: List[Context],
    ) -> List[Context]:
        """按当前洗版模式和优先级重新过滤准备阶段候选。"""
        if not subscribe.best_version:
            return contexts
        accepted: List[Context] = []
        for context in contexts:
            media = context.media_info
            meta = context.meta_info
            torrent = context.torrent_info
            if not media or not meta or not torrent:
                continue
            if media.type == MediaType.TV:
                if self._SubscribeChain__is_full_best_version_enabled(subscribe) \
                        and not self._SubscribeChain__is_full_season_resource(meta, subscribe):
                    continue
                if not self._is_episode_range_covered(meta, subscribe):
                    continue
                if not self._SubscribeChain__prepare_best_version_tv_candidate(
                    subscribe=subscribe,
                    context=context,
                    priority=torrent.pri_order,
                ):
                    continue
            elif subscribe.current_priority and torrent.pri_order <= subscribe.current_priority:
                continue
            accepted.append(context)
        return accepted

    @classmethod
    def _is_episode_range_covered(cls, meta: MetaBase, subscribe: SubscriptionSnapshot) -> bool:
        """
        判断种子是否覆盖当前仍需洗版的剧集范围。
        """
        episodes = meta.episode_list
        if not episodes:
            # 没有剧集信息，表示该种子为合集
            return True

        pending_episodes = cls._get_pending_best_version_episodes(subscribe)
        if not pending_episodes:
            return True

        return bool(set(episodes).intersection(set(pending_episodes)))

    @staticmethod
    def get_states_for_search(state: str) -> str:
        """
        根据给定的状态返回实际需要搜索的状态列表，支持多个状态用逗号分隔
        :param state: 订阅状态
            N: New（新建，未处理）
            R: Resolved（订阅中）
            P: Pending（待定，信息待进一步更新，允许搜索，不允许完成）
            S: Suspended（暂停，订阅不参与任何动作，暂时停止处理）
        :return: 需要查询的状态列表（多个状态用逗号分隔）
        """
        # 如果状态是 R 或 P，则视为一起搜索，返回 R,P 作为查询条件
        if state in ["R", "P"]:
            return "R,P"
        return state

    @staticmethod
    def get_subscribe_source_keyword(subscribe: SubscriptionSnapshot) -> str:
        """
        构造用于订阅来源的关键字字符串

        :param subscribe: 订阅快照
        :return str: 格式化的订阅来源关键字字符串，格式为 "Subscribe|{...}"
        """
        source_keyword = {
            "id": subscribe.id,
            "name": subscribe.name,
            "year": subscribe.year,
            "type": subscribe.type,
            "season": subscribe.season,
            "episode_group": subscribe.episode_group,
            "media_source": subscribe.media_source,
            "media_id": subscribe.media_id,
            "music_type": getattr(subscribe, "music_type", None),
        }
        return f"Subscribe|{json.dumps(source_keyword, ensure_ascii=False)}"

    @staticmethod
    def parse_subscribe_source_keyword(source_keyword_str: str) -> Optional[dict[str, Any]]:
        """
        解析订阅来源关键字字符串

        :param source_keyword_str: 订阅来源关键字字符串，格式为 "Subscribe|{...}"
        :return Dict: 如果解析失败则返回None
        """
        if not source_keyword_str or not source_keyword_str.startswith("Subscribe|"):
            return None

        try:
            # 分割字符串获取JSON部分
            json_part = source_keyword_str.split("|", 1)[1]
            # 解析JSON字符串
            source_keyword = json.loads(json_part)
            return cast(dict[str, Any], source_keyword)
        except (IndexError, json.JSONDecodeError, TypeError) as e:
            logger.error(f"解析订阅来源关键字失败: {e}")
            return None
