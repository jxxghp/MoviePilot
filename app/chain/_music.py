import copy
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Tuple, cast

from app.application.classification.reference import (
    subscription_classification_override,
)
from app.application.configuration import get_configured_system_config
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.subscription.contract import (
    SubscriptionRepository,
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
)
from app.application.subscription.execution import SubscriptionExecutionContext
from app.application.subscription.mutation import SubscriptionActor
from app.application.subscription.sitebudget import SubscriptionSearchCancelled
from app.application.torrent.download import TorrentHelper
from app.chain._contracts import MusicSubscribeMixinHost
from app.chain.download import DownloadChain
from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.domain.context import Context, MediaInfo, MusicInfo
from app.domain.media import MUSIC_SUBSCRIBABLE_TYPES
from app.domain.meta.metamusic import MetaMusic
from app.runtime.log import logger
from app.schemas.category import ClassificationSelection
from app.schemas.common import JsonData
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaType,
    SystemConfigKey,
)

if TYPE_CHECKING:
    from app.application.subscription.mutation import SyncSubscriptionMutationScope


def _normalize_music_total_tracks(value: Any) -> Optional[int]:
    """将专辑曲目总数归一为正整数，无效或未知值返回 None。"""
    try:
        total_tracks = int(value or 0)
    except (TypeError, ValueError):
        return None
    return total_tracks if total_tracks > 0 else None


def _music_info_from_subscribe(subscribe: SubscriptionSnapshot) -> MusicInfo:
    """从订阅行恢复不依赖远端请求的最小音乐目标，保留专辑完成判断所需字段。"""
    year_text = str(subscribe.year or "")[:4]
    music_type = getattr(subscribe, "music_type", None)
    # 音乐订阅的 description 由标准 MusicInfo.overview 生成，首段固定为艺术家。
    artist_text = str(getattr(subscribe, "description", None) or "") \
        .split(" · ", maxsplit=1)[0].strip()
    artists = [
        artist.strip() for artist in artist_text.split(" / ") if artist.strip()
    ]
    return MusicInfo(
        media_source=subscribe.media_source,
        media_id=str(subscribe.media_id) if subscribe.media_id is not None else None,
        music_type=music_type,
        title=subscribe.name,
        artists=artists,
        album=subscribe.name if music_type == MUSIC_ENTITY_ALBUM else None,
        year=int(year_text) if year_text.isdigit() else None,
        total_tracks=getattr(subscribe, "total_tracks", None)
        if music_type == MUSIC_ENTITY_ALBUM else None,
        cover_url=getattr(subscribe, "poster", None) or getattr(subscribe, "backdrop", None),
    )


def _music_subscribe_classification_override(
        subscribe: SubscriptionSnapshot,
) -> Optional[ClassificationSelection]:
    """把订阅手工分类转换为规则执行层使用的生效分类覆盖。"""
    return subscription_classification_override(
        category_id=getattr(subscribe, "media_category_id", None),
        path_snapshot=getattr(subscribe, "media_category", None),
        media_type=MediaType.MUSIC,
    )


def _apply_music_subscription_classification(
        media: MusicInfo,
        subscribe: SubscriptionSnapshot,
) -> MusicInfo:
    """在音乐订阅域内应用手工分类，避免 mixin 反向依赖订阅具体实现。"""
    override = _music_subscribe_classification_override(subscribe)
    if override is None:
        return media
    finalized = MediaChain()._finalize_recognition_result(
        media,
        effective_override=override,
    )
    return finalized or media


def _finalize_music_subscribe_recognition(
        media_chain: MediaChain,
        subscribe: SubscriptionSnapshot,
        mediainfo: Optional[MusicInfo],
) -> Optional[MusicInfo]:
    """统一收口音乐订阅的远端结果、持久化快照与空结果分类。"""
    return cast(
        Optional[MusicInfo],
        media_chain._finalize_recognition_result(
            mediainfo,
            effective_override=_music_subscribe_classification_override(subscribe),
        ),
    )


class MusicSubscribeMixin:
    __mixin_host_protocol__ = MusicSubscribeMixinHost
    subscription_repository: SubscriptionRepository
    sync_subscription_mutation_scope: "SyncSubscriptionMutationScope"
    _SubscribeChain__candidate_contract_changed: Callable[
        [SubscriptionSnapshot, SubscriptionSnapshot], bool
    ]
    """
    音乐订阅功能域 mixin：单曲/专辑目标识别、实体快照同步、候选筛选、
    择优下载与完成推进。

    该域方法通过 self 复用 SubscribeChain 主体的 get_sub_sites / get_params /
    filter_torrents / check_and_handle_existing_media / finish_subscribe_or_not /
    get_subscribe_source_keyword 等编排能力，因此仅作为 mixin 混入 SubscribeChain，
    不独立成链。订阅元数据与媒体键由 Application 共享契约提供，避免 mixin 与
    SubscribeChain 主体形成双向模块依赖。
    """

    @staticmethod
    def _validate_music_subscribe_target(
            mediainfo: MediaInfo,
            requested_music_type: Optional[str] = None,
    ) -> Optional[str]:
        """校验音乐订阅实体一致性，并确保专辑具备可验证的曲目总数。"""
        if mediainfo.type != MediaType.MUSIC:
            return "识别结果不是音乐"
        music_type = getattr(mediainfo, "music_type", None)
        if requested_music_type and requested_music_type not in MUSIC_SUBSCRIBABLE_TYPES:
            return "音乐订阅仅支持单曲或专辑"
        if music_type not in MUSIC_SUBSCRIBABLE_TYPES:
            return "音乐订阅仅支持单曲或专辑"
        if requested_music_type and requested_music_type != music_type:
            return f"音乐订阅类型不匹配：请求 {requested_music_type}，识别为 {music_type}"
        if music_type == MUSIC_ENTITY_ALBUM \
                and _normalize_music_total_tracks(getattr(mediainfo, "total_tracks", None)) is None:
            return "专辑总曲目数未知，无法校验整张专辑资源"
        return None

    @staticmethod
    def _ensure_music_subscribe_entity(
            subscribe: SubscriptionSnapshot,
            mediainfo: Optional[MusicInfo],
    ) -> Optional[MusicInfo]:
        """保持已持久化的单曲/专辑实体边界，拒绝远端详情把订阅类型改写。"""
        if not mediainfo:
            return None
        expected_type = getattr(subscribe, "music_type", None)
        actual_type = getattr(mediainfo, "music_type", None)
        if expected_type and expected_type not in MUSIC_SUBSCRIBABLE_TYPES:
            logger.warning(f"音乐订阅 {subscribe.name} 的实体类型无效：{expected_type}")
            return None
        if actual_type not in MUSIC_SUBSCRIBABLE_TYPES:
            logger.warning(
                f"音乐订阅 {subscribe.name} 识别为不可订阅实体：{actual_type}"
            )
            if expected_type in MUSIC_SUBSCRIBABLE_TYPES:
                return _music_info_from_subscribe(subscribe)
            return None
        if expected_type and actual_type != expected_type:
            logger.warning(
                f"音乐订阅 {subscribe.name} 实体不匹配："
                f"订阅为 {expected_type}，远端识别为 {actual_type}，使用订阅快照"
            )
            return _music_info_from_subscribe(subscribe)
        if actual_type == MUSIC_ENTITY_ALBUM:
            remote_total = _normalize_music_total_tracks(getattr(mediainfo, "total_tracks", None))
            stored_total = _normalize_music_total_tracks(getattr(subscribe, "total_tracks", None))
            resolved_total = remote_total or stored_total
            if resolved_total is not None and mediainfo.total_tracks != resolved_total:
                # 识别模块结果可能来自共享缓存，补齐订阅快照时不得原地修改。
                mediainfo = copy.copy(mediainfo)
                mediainfo.total_tracks = resolved_total
        return mediainfo

    @staticmethod
    def _recognize_music_subscribe(subscribe: SubscriptionSnapshot) -> Optional[MusicInfo]:
        """按订阅身份恢复音乐目标，远端暂不可用时使用已持久化的稳定快照。"""
        media_chain = MediaChain()
        mediainfo: Optional[MusicInfo]
        if subscribe.media_source and subscribe.media_id:
            # 与影视共用统一识别入口，按媒体源和原生 ID 恢复音乐详情
            recognized = media_chain.recognize_media(
                media_source=subscribe.media_source,
                media_id=str(subscribe.media_id),
                mtype=MediaType.MUSIC,
                music_type=getattr(subscribe, "music_type", None),
            )
            if recognized:
                mediainfo = MusicSubscribeMixin._ensure_music_subscribe_entity(
                    subscribe,
                    recognized,
                )
            elif getattr(subscribe, "music_type", None) in {
                MUSIC_ENTITY_RECORDING,
                MUSIC_ENTITY_ALBUM,
            }:
                mediainfo = _music_info_from_subscribe(subscribe)
            else:
                # 旧订阅没有保存实体类型时不能猜测为单曲，否则可能误把专辑按单曲完成。
                mediainfo = None
        elif getattr(subscribe, "music_type", None) == MUSIC_ENTITY_ALBUM:
            # 缺少远端 ID 的专辑不能退化为单曲识别，使用已保存专辑快照更可靠。
            mediainfo = _music_info_from_subscribe(subscribe)
        else:
            # 旧订阅没有实体类型时只允许走 Recording 识别，不能从全局混合搜索中猜成专辑或艺术家。
            recognized = media_chain.recognize_media(
                meta=build_subscribe_meta(subscribe),
                mtype=MediaType.MUSIC,
                media_source=subscribe.media_source,
                music_type=MUSIC_ENTITY_RECORDING,
            )
            mediainfo = MusicSubscribeMixin._ensure_music_subscribe_entity(
                subscribe,
                recognized,
            )
        return _finalize_music_subscribe_recognition(
            media_chain,
            subscribe,
            mediainfo,
        )

    @staticmethod
    async def _async_recognize_music_subscribe(subscribe: SubscriptionSnapshot) -> Optional[MusicInfo]:
        """异步按订阅身份恢复音乐目标，远端暂不可用时使用已持久化的稳定快照。"""
        media_chain = MediaChain()
        mediainfo: Optional[MusicInfo]
        if subscribe.media_source and subscribe.media_id:
            # 与影视共用统一识别入口，按媒体源和原生 ID 恢复音乐详情
            recognized = await media_chain.async_recognize_media(
                media_source=subscribe.media_source,
                media_id=str(subscribe.media_id),
                mtype=MediaType.MUSIC,
                music_type=getattr(subscribe, "music_type", None),
            )
            if recognized:
                mediainfo = MusicSubscribeMixin._ensure_music_subscribe_entity(
                    subscribe,
                    recognized,
                )
            elif getattr(subscribe, "music_type", None) in {
                MUSIC_ENTITY_RECORDING,
                MUSIC_ENTITY_ALBUM,
            }:
                mediainfo = _music_info_from_subscribe(subscribe)
            else:
                mediainfo = None
        elif getattr(subscribe, "music_type", None) == MUSIC_ENTITY_ALBUM:
            mediainfo = _music_info_from_subscribe(subscribe)
        else:
            recognized = await media_chain.async_recognize_media(
                meta=build_subscribe_meta(subscribe),
                mtype=MediaType.MUSIC,
                media_source=subscribe.media_source,
                music_type=MUSIC_ENTITY_RECORDING,
            )
            mediainfo = MusicSubscribeMixin._ensure_music_subscribe_entity(
                subscribe,
                recognized,
            )
        return _finalize_music_subscribe_recognition(
            media_chain,
            subscribe,
            mediainfo,
        )

    def _sync_music_subscribe_target(
            self,
            subscribe: SubscriptionSnapshot,
            mediainfo: MusicInfo,
    ) -> SubscriptionSnapshot:
        """把远端识别得到的专辑类型和总曲目数同步到订阅，供搜索失败与完成历史复用。"""
        update_data = {}
        if mediainfo.music_type and getattr(subscribe, "music_type", None) != mediainfo.music_type:
            update_data["music_type"] = mediainfo.music_type
        if mediainfo.music_type == MUSIC_ENTITY_ALBUM:
            # 远端详情可能暂时不返回曲目数；已确认的订阅快照不能因此被清空。
            total_tracks = _normalize_music_total_tracks(mediainfo.total_tracks) \
                or _normalize_music_total_tracks(getattr(subscribe, "total_tracks", None))
        else:
            total_tracks = None
        if getattr(subscribe, "total_tracks", None) != total_tracks:
            update_data["total_tracks"] = total_tracks
        if not update_data:
            return subscribe
        return self._update_music_subscription(
            subscribe,
            update_data,
            scene="music_target",
        )

    def _update_music_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        payload: Mapping[str, JsonData],
        *,
        scene: str,
    ) -> SubscriptionSnapshot:
        """经显式同步事务作用域更新音乐订阅并返回提交后的快照。"""
        with self.sync_subscription_mutation_scope() as mutation:
            change = mutation.update(
                subscribe.id,
                dict(payload),
                SubscriptionActor(name="chain", is_superuser=True),
                existing=subscribe,
                scene=scene,
            )
        return change.snapshot if change else subscribe

    @staticmethod
    def _is_music_download_complete(
            subscribe: SubscriptionSnapshot,
            mediainfo: MusicInfo,
            downloads: Optional[List[Context]],
    ) -> bool:
        """判断音乐下载是否满足订阅完成条件；专辑必须由下载层确认整专曲目覆盖。"""
        if not downloads:
            return False
        music_type = getattr(subscribe, "music_type", None) or mediainfo.music_type
        if music_type != MUSIC_ENTITY_ALBUM:
            return True
        return any(context.confirmed_full_coverage for context in downloads)

    def _prepare_music_subscribe(
            self,
            subscribe: SubscriptionSnapshot,
    ) -> Optional[Tuple[SubscriptionSnapshot, MusicInfo, MetaMusic]]:
        """识别音乐订阅目标、同步实体快照，并在搜索前处理已完整入库的目标。"""
        mediainfo = self._recognize_music_subscribe(subscribe)
        if not mediainfo:
            logger.warning(
                f"未识别到音乐订阅目标：{subscribe.name}，"
                f"媒体源：{subscribe.media_source}，媒体ID：{subscribe.media_id}"
            )
            return None
        validation_error = self._validate_music_subscribe_target(
            mediainfo,
            getattr(subscribe, "music_type", None),
        )
        if validation_error:
            logger.warning(f"音乐订阅 {subscribe.name} 无法继续：{validation_error}")
            return None
        subscribe = self._sync_music_subscribe_target(subscribe, mediainfo)
        meta = MetaMusic.from_music_info(mediainfo)
        exists, _ = self.check_and_handle_existing_media(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=subscribe_media_key(subscribe),
        )
        if exists:
            return None
        return subscribe, mediainfo, meta

    def _filter_music_subscribe_contexts(
            self,
            subscribe: SubscriptionSnapshot,
            mediainfo: MusicInfo,
            contexts: List[Context],
    ) -> List[Context]:
        """按站点、音乐实体、订阅参数和优先级规则筛选并绑定下载上下文。"""
        sites = self.get_sub_sites(subscribe)
        default_rule_key = SystemConfigKey.BestVersionFilterRuleGroups \
            if subscribe.best_version else SystemConfigKey.SubscribeFilterRuleGroups
        rule_groups = subscribe.filter_groups or get_configured_system_config().get(default_rule_key) or []
        torrent_helper = TorrentHelper()
        matched: List[Context] = []
        for source_context in contexts or []:
            source_torrent = source_context.torrent_info
            if not source_torrent or source_torrent.category not in (MediaType.MUSIC, MediaType.MUSIC.value):
                continue
            # 过滤模块会就地写入 pri_order；RSS 缓存会被多个订阅复用，必须隔离候选副本。
            torrent = copy.copy(source_torrent)
            if sites and torrent.site not in sites:
                continue
            if not SearchChain.matches_music_resource(
                    mediainfo,
                    torrent.title,
                    torrent.description,
            ):
                continue
            if not torrent_helper.filter_torrent(torrent, self.get_params(subscribe)):
                continue
            filtered = self.filter_torrents(
                rule_groups=rule_groups,
                torrent_list=[torrent],
                mediainfo=mediainfo,
            )
            if filtered is not None:
                if not filtered:
                    continue
                torrent = filtered[0]

            context = copy.copy(source_context)
            context.torrent_info = torrent
            meta = MetaMusic.from_music_info(mediainfo)
            meta.org_string = torrent.title
            meta.apply_audio_quality(f"{torrent.title} {torrent.description or ''}", overwrite=True)
            if subscribe.best_version:
                # 用户规则组可用格式、码率等内置规则定义洗版顺序；未命中规则
                # 优先级时再回退到规范化音质分数，确保零配置也能自动升级。
                music_priority = torrent.pri_order or meta.audio_quality_score
                if music_priority <= (subscribe.current_priority or 0):
                    logger.info(
                        f"{torrent.title} 音质优先级 {music_priority} "
                        f"未高于当前版本 {subscribe.current_priority or 0}"
                    )
                    continue
                torrent.pri_order = music_priority
            context.meta_info = meta
            context.media_info = mediainfo
            context.match_source = str(mediainfo.media_source or "title")
            context.candidate_recognized = False
            context.media_info_is_target = True
            context.media_info = _apply_music_subscription_classification(
                context.media_info,
                subscribe,
            )
            matched.append(context)
        return matched

    def _download_music_subscribe(
            self,
            subscribe: SubscriptionSnapshot,
            mediainfo: MusicInfo,
            contexts: List[Context],
            execution_context: Optional[SubscriptionExecutionContext] = None,
    ) -> None:
        """批量择优下载音乐候选，并按单曲或整专完成语义推进订阅。"""
        if not contexts:
            return
        self._ensure_music_execution_active(execution_context)
        repository = self.subscription_repository
        current_subscribe = repository.get(subscribe.id)
        if current_subscribe is None:
            logger.info(f"音乐订阅 {subscribe.id} 已删除，放弃本轮下载提交")
            return
        if current_subscribe.state == "S":
            logger.info(f"音乐订阅 {current_subscribe.name} 已暂停，放弃本轮下载提交")
            return
        if self._SubscribeChain__candidate_contract_changed(subscribe, current_subscribe):
            logger.info(f"音乐订阅 {current_subscribe.name} 的筛选或媒体身份已变化，放弃旧候选并等待下一轮")
            return
        subscribe = current_subscribe
        governance = None
        if execution_context:
            governance = SubscriptionDownloadGovernance(
                cancelled=execution_context.should_stop,
                mark_started=execution_context.mark_download_started,
            )
            execution_context.report_phase("preparing")
        self._ensure_music_execution_active(execution_context)
        downloads, _ = DownloadChain().batch_download(
            contexts=contexts,
            username=subscribe.username,
            save_path=subscribe.save_path,
            downloader=subscribe.downloader,
            source=self.get_subscribe_source_keyword(subscribe),
            custom_words=subscribe.custom_words,
            governance=governance,
        )
        successful = [
            context for context in downloads or []
            if context and context.meta_info and context.torrent_info
        ]
        quality_downloads = successful
        if getattr(subscribe, "music_type", None) == MUSIC_ENTITY_ALBUM:
            quality_downloads = [
                context for context in successful
                if context.confirmed_full_coverage
            ]
        current_subscribe = None
        if subscribe.best_version and quality_downloads:
            best_context = max(quality_downloads, key=lambda item: item.torrent_info.pri_order)
            best_meta = best_context.meta_info
            quality_data = {
                "current_priority": best_context.torrent_info.pri_order,
                "current_audio_format": best_meta.audio_format,
                "current_bitrate": best_meta.bitrate,
                "current_bit_depth": best_meta.bit_depth,
                "current_sample_rate": best_meta.sample_rate,
            }
            current_subscribe = self._update_music_subscription(
                subscribe,
                quality_data,
                scene="music_download",
            )
        if current_subscribe is None:
            current_subscribe = repository.get(subscribe.id)
        if current_subscribe:
            self.finish_subscribe_or_not(
                subscribe=current_subscribe,
                meta=MetaMusic.from_music_info(mediainfo),
                mediainfo=mediainfo,
                downloads=downloads,
            )

    def _search_music_subscribe(
            self,
            subscribe: SubscriptionSnapshot,
            execution_context: Optional[SubscriptionExecutionContext] = None,
    ) -> None:
        """复用站点标题搜索、订阅过滤和批量下载完成单个音乐订阅。"""
        self._ensure_music_execution_active(execution_context)
        target = self._prepare_music_subscribe(subscribe)
        if not target:
            return
        subscribe, mediainfo, _ = target
        self._ensure_music_execution_active(execution_context)

        sites = self.get_sub_sites(subscribe)
        default_rule_key = SystemConfigKey.BestVersionFilterRuleGroups \
            if subscribe.best_version else SystemConfigKey.SubscribeFilterRuleGroups
        rule_groups = subscribe.filter_groups or get_configured_system_config().get(default_rule_key) or []
        keywords = [subscribe.keyword] if subscribe.keyword else SearchChain.music_site_keywords(mediainfo)
        if not keywords:
            keywords = [subscribe.name]

        searchchain = SearchChain()
        contexts: List[Context] = []
        if execution_context:
            execution_context.report_phase("searching")
        for keyword in keywords:
            self._ensure_music_execution_active(execution_context)
            contexts = searchchain.search_by_title(
                title=keyword,
                sites=sites,
                mtype=MediaType.MUSIC,
                rule_groups=rule_groups,
            )
            self._ensure_music_execution_active(execution_context)
            contexts = self._filter_music_subscribe_contexts(
                subscribe=subscribe,
                mediainfo=mediainfo,
                contexts=contexts,
            )
            if contexts:
                break

        if not contexts:
            logger.warning(f"音乐订阅 {subscribe.keyword or subscribe.name} 未搜索到符合条件的资源")
            return

        self._download_music_subscribe(
            subscribe,
            mediainfo,
            contexts,
            execution_context=execution_context,
        )

    def _match_music_subscribe(
            self,
            subscribe: SubscriptionSnapshot,
            contexts: List[Context],
            execution_context: Optional[SubscriptionExecutionContext] = None,
    ) -> None:
        """直接匹配本轮 RSS 缓存中的音乐资源，避免再次调用站点搜索接口。"""
        self._ensure_music_execution_active(execution_context)
        target = self._prepare_music_subscribe(subscribe)
        if not target:
            return
        subscribe, mediainfo, _ = target
        if execution_context:
            execution_context.report_phase("matching")
        self._ensure_music_execution_active(execution_context)
        matched = self._filter_music_subscribe_contexts(
            subscribe=subscribe,
            mediainfo=mediainfo,
            contexts=contexts,
        )
        if not matched:
            logger.info(f"音乐订阅 {subscribe.name} 未匹配到符合条件的 RSS 资源")
            return
        self._download_music_subscribe(
            subscribe,
            mediainfo,
            matched,
            execution_context=execution_context,
        )

    @staticmethod
    def _ensure_music_execution_active(
            execution_context: Optional[SubscriptionExecutionContext],
    ) -> None:
        """在音乐搜索、匹配和提交前的安全边界执行取消与 TTL 检查。"""
        if execution_context is None:
            return
        if execution_context.is_cancel_requested():
            raise SubscriptionSearchCancelled("订阅搜索已取消")
        if execution_context.is_expired():
            raise TimeoutError("订阅执行已超过协作截止时间")
