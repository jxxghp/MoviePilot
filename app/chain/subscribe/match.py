"""订阅候选准备、身份复核与资源匹配"""

import copy
from datetime import datetime
from typing import Any, Callable, Dict, List, Literal, Optional

from app.application.configuration import get_configured_system_config
from app.application.subscription.candidates import CandidateIndex
from app.application.subscription.contract import (
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
)
from app.application.subscription.execution import (
    SubscriptionExecutionAdmission,
    SubscriptionExecutionContext,
    SubscriptionExecutionLease,
)
from app.application.subscription.facts import FreshFactLease
from app.application.subscription.observability import MatchExecutionSummary
from app.application.subscription.sitebudget import SubscriptionSearchCancelled
from app.application.torrent.download import TorrentHelper
from app.chain.media import MediaChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.chain.subscribe.metadata import apply_subscription_classification
from app.domain.context import (
    Context,
    MediaInfo,
    TorrentInfo,
)
from app.domain.meta.metabase import MetaBase
from app.domain.meta.words import WordsMatcher
from app.domain.metainfo import MetaInfo
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.media import resolve_media_identity
from app.schemas.types import (
    MediaType,
    SystemConfigKey,
)

_MatchOutcome = Literal["completed", "skipped", "failed"]


def _report_match_progress(
    progress_callback: Optional[Callable[..., None]],
    summary: MatchExecutionSummary,
    *,
    value: float,
    text: str,
    current: Optional[int] = None,
) -> None:
    """发布 Match 进度，并始终携带真实的完成、跳过和失败计数。"""
    if progress_callback:
        progress_callback(
            value=value,
            text=text,
            data=summary.as_data(current=current),
        )


def _match_stop_reason(context: SubscriptionExecutionContext) -> Optional[str]:
    """区分 Match 安全停止来自 TTL 还是取消请求。"""
    if context.is_expired():
        return "ttl_timeout"
    if context.is_cancel_requested():
        return "cancelled"
    return None


def _match_result_reason(
    outcome: Optional[str],
    context: SubscriptionExecutionContext,
) -> Optional[str]:
    """为跳过结果补充停止边界或业务原因。"""
    if outcome != "skipped":
        return None
    return _match_stop_reason(context) or "business_skip"


def _release_match_admission(
    admission: SubscriptionExecutionAdmission,
    lease: SubscriptionExecutionLease,
    subscription_id: int,
    summary: MatchExecutionSummary,
) -> None:
    """结束当前订阅资源检查，并记录未能恢复的状态。"""
    if admission.release(lease):
        return
    summary.release_failures += 1
    logger.error(f"订阅 {subscription_id} 的搜索状态没有正常恢复，系统稍后会继续检查")


def _report_match_finished(
    progress_callback: Optional[Callable[..., None]],
    summary: MatchExecutionSummary,
) -> None:
    """按实际计数发布 Match 最终进度。"""
    if not progress_callback:
        return
    final_text = "订阅资源检查完成"
    if summary.finished < summary.total:
        final_text = "订阅资源检查已停止，部分订阅这次未检查"
    elif summary.failed:
        final_text = "订阅资源检查结束，部分订阅没有完成"
    elif summary.skipped:
        final_text = "订阅资源检查完成，部分订阅这次未检查"
    _report_match_progress(progress_callback, summary, value=100, text=final_text)


def _is_title_match_allowed(
    mediainfo: MediaInfo,
    torrent_meta: MetaBase,
    torrent_info: TorrentInfo,
) -> bool:
    """判断资源是否可以通过标题兜底匹配订阅目标。"""
    if not TorrentHelper.match_torrent(
        mediainfo=mediainfo,
        torrent_meta=torrent_meta,
        torrent=torrent_info,
    ):
        return False
    if not TorrentHelper.requires_identity_disambiguation(
        mediainfo=mediainfo,
        torrent_meta=torrent_meta,
    ):
        return True
    logger.debug(
        f"{torrent_info.site_name} - {torrent_info.title} 仅通过无年份别名命中且候选媒体身份无法确认，已跳过"
    )
    return False


def _get_media_id_match_source(mediainfo: Optional[MediaInfo]) -> str:
    """返回候选自身识别命中的明确媒体 ID 类型。"""
    media_source, media_id = resolve_media_identity(media=mediainfo)
    if media_source and media_id:
        return str(media_source)
    return "unknown"


def _reconcile_candidate_media(
    target_mediainfo: MediaInfo,
    candidate_mediainfo: MediaInfo,
    torrent_meta: MetaBase,
    torrent_info: TorrentInfo,
    context: Context,
) -> Optional[MediaInfo]:
    """
    在推断媒体 ID 冲突时，以订阅目标对候选标题做严格复核。

    标题携带明确媒体身份或目标缺少同来源 ID 时保持严格拒绝；只有普通
    标题推断产生冲突且标题、别名、类型和年份复核通过时才回填订阅目标。
    """
    candidate_source, candidate_id = resolve_media_identity(media=candidate_mediainfo)
    target_source, target_id = resolve_media_identity(media=target_mediainfo)
    conflicts = []
    if candidate_source and candidate_id and (candidate_source != target_source or candidate_id != target_id):
        conflicts.append(
            (
                str(candidate_source),
                candidate_id,
                f"{target_source}:{target_id}" if target_source and target_id else None,
            )
        )
    if not conflicts:
        return candidate_mediainfo

    conflict_text = "、".join(
        f"{source} {candidate_id} != {target_id if target_id is not None else '缺失'}"
        for source, candidate_id, target_id in conflicts
    )
    if any(target_id is None for _, _, target_id in conflicts):
        logger.debug(
            f"{torrent_info.site_name} - {torrent_info.title} 候选媒体ID与订阅目标不可比较：{conflict_text}"
        )
        return None

    explicit_source, explicit_id = resolve_media_identity(media=torrent_meta)
    if explicit_source and explicit_id:
        logger.debug(
            f"{torrent_info.site_name} - {torrent_info.title} 标题含明确媒体ID "
            f"{explicit_source}:{explicit_id}，保持严格校验：{conflict_text}"
        )
        return None

    if not TorrentHelper.match_torrent(
        mediainfo=target_mediainfo,
        torrent_meta=torrent_meta,
        torrent=torrent_info,
    ):
        logger.debug(
            f"{torrent_info.site_name} - {torrent_info.title} 候选媒体ID冲突且标题复核失败：{conflict_text}"
        )
        return None

    evidence_matched, evidence = TorrentHelper.match_same_work_evidence(
        target_mediainfo=target_mediainfo,
        candidate_mediainfo=candidate_mediainfo,
        torrent_meta=torrent_meta,
    )
    if not evidence_matched:
        logger.debug(
            f"{torrent_info.site_name} - {torrent_info.title} 候选媒体ID冲突且缺少同作品证据："
            f"{evidence}；{conflict_text}"
        )
        return None

    context.media_info = target_mediainfo
    context.match_source = "title"
    context.candidate_recognized = False
    context.media_info_is_target = True
    logger.debug(
        f"{target_mediainfo.title_year} 候选媒体ID冲突（{conflict_text}），"
        f"经标题或别名及{evidence}复核匹配到订阅目标："
        f"{torrent_info.site_name} - {torrent_info.title}"
    )
    return target_mediainfo


def _prepare_subscription_match(
    owner: Any,
    subscribe: SubscriptionSnapshot,
    candidate_index: CandidateIndex,
    fresh_fact_lease: FreshFactLease,
) -> Optional[
    tuple[MetaBase, MediaInfo, Dict[str, List[Context]], List[str], List[int]]
]:
    """为单个订阅路由候选，并在本轮新鲜事实租约中加载媒体信息。"""
    try:
        meta = build_subscribe_meta(subscribe)
    except ValueError:
        logger.error(f"订阅《{subscribe.name}》的媒体类型不受支持，暂时无法检查资源")
        return None
    domains = owner.site_repository.get_domains_by_ids(subscribe.sites) if subscribe.sites else []
    sub_sites = owner.get_sub_sites(subscribe)
    routed_torrents = candidate_index.route_for_match(
        subscribe,
        domains=set(domains) if domains else None,
        site_ids=set(sub_sites) if sub_sites else None,
    )
    if not routed_torrents:
        logger.debug(f"订阅 {subscribe.name} 本轮没有可能相关的资源，跳过资源匹配准备")
        return None
    mediainfo = fresh_fact_lease.get_or_load(
        subscribe,
        lambda: MediaChain().recognize_media(
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
    mediainfo = apply_subscription_classification(mediainfo, subscribe)
    return meta, mediainfo, routed_torrents, domains, sub_sites


def _prepare_match_torrents(
    torrents: Dict[str, List[Context]],
) -> Dict[str, List[Context]]:
    """预识别待匹配资源，并保留原上下文供后续订阅复用。"""
    processed_torrents: Dict[str, List[Context]] = {}
    for domain, contexts in torrents.items():
        if runtime_stop_state.is_system_stopped:
            break
        processed_torrents[domain] = []
        for context in contexts:
            if runtime_stop_state.is_system_stopped:
                break
            if context.torrent_info and getattr(context.torrent_info, "category", None) in (
                MediaType.MUSIC,
                MediaType.MUSIC.value,
            ):
                # 音乐 RSS 使用订阅目标做实体匹配，不应进入影视识别并累计失败次数。
                processed_torrents[domain].append(context)
                continue
            if (
                not context.media_info or not resolve_media_identity(media=context.media_info)[1]
            ) and context.media_recognize_fail_count < 3:
                logger.debug(
                    f"尝试重新识别种子：{context.torrent_info.title}，当前失败次数："
                    f"{context.media_recognize_fail_count}/3"
                )
                re_mediainfo = MediaChain().recognize_by_meta(
                    context.meta_info,
                    obtain_images=False,
                )
                if re_mediainfo:
                    re_mediainfo.clear()
                    context.media_info = re_mediainfo
                    context.match_source = _get_media_id_match_source(re_mediainfo)
                    context.candidate_recognized = bool(resolve_media_identity(media=re_mediainfo)[1])
                    context.media_info_is_target = False
                    context.media_recognize_fail_count = 0
                    logger.debug(f"种子 {context.torrent_info.title} 重新识别成功")
                else:
                    context.media_recognize_fail_count += 1
                    logger.debug(
                        f"种子 {context.torrent_info.title} 媒体识别失败，失败次数："
                        f"{context.media_recognize_fail_count}/3"
                    )
            elif context.media_recognize_fail_count >= 3:
                logger.debug(f"种子 {context.torrent_info.title} 已达到最大识别失败次数(3次)，跳过识别")
            processed_torrents[domain].append(context)
    return processed_torrents


class SubscribeMatchOwner(_SubscribeOwnerBase):
    """订阅候选准备、身份复核与资源匹配 owner。"""

    def _prepare_match_torrents(
        self,
        torrents: Dict[str, List[Context]],
    ) -> Dict[str, List[Context]]:
        """保留既有实例入口，并委托模块级实现执行资源预识别。"""
        return _prepare_match_torrents(torrents)

    def match(
        self,
        torrents: Dict[str, List[Context]],
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        从缓存中匹配订阅，并自动下载。

        该入口保持订阅刷新、定时任务和插件调用的稳定签名，具体匹配流程由内部阶段执行。
        """
        if not torrents:
            logger.warn("当前没有可检查的订阅资源")
            if progress_callback:
                progress_callback(value=100, text="当前没有可检查的订阅资源")
            return
        if progress_callback:
            progress_callback(value=0, text="正在整理订阅资源 ...")
        return self._run_match(
            torrents=torrents,
            progress_callback=progress_callback,
        )

    def _run_match(
        self,
        torrents: Dict[str, List[Context]],
        progress_callback: Optional[Callable[..., None]] = None,
    ) -> None:
        """
        从缓存中匹配订阅，并自动下载

        :param torrents: 按站点分组的资源上下文
        :param progress_callback: 订阅匹配进度更新回调
        """
        if not torrents:
            logger.warn("当前没有可检查的订阅资源")
            if progress_callback:
                progress_callback(value=100, text="当前没有可检查的订阅资源")
            return

        if progress_callback:
            progress_callback(value=0, text="正在整理订阅资源 ...")

        lock_acquired = False
        summary = MatchExecutionSummary.from_candidates(torrents)
        summary_started = False
        try:
            lock_acquired = self._acquire_run_lock("match", progress_callback)
            if not lock_acquired:
                return

            processed_torrents = self._prepare_match_torrents(torrents)
            candidate_index, fresh_fact_lease = CandidateIndex(processed_torrents), FreshFactLease()

            # 所有订阅
            subscribes = self.subscription_repository.list(self.get_states_for_search("R"))
            total_num = len(subscribes)
            summary.total = total_num
            logger.info(summary.start_log())
            summary_started = True
            if progress_callback:
                _report_match_progress(
                    progress_callback,
                    summary,
                    value=20,
                    text=f"资源整理完成，开始检查 {total_num} 个订阅 ...",
                )
            try:
                for index, listed_subscribe in enumerate(subscribes, start=1):
                    if runtime_stop_state.is_system_stopped:
                        break
                    if progress_callback:
                        _report_match_progress(
                            progress_callback,
                            summary,
                            value=20 + ((index - 1) / total_num * 80 if total_num else 80),
                            text=(f"正在检查订阅（{index}/{total_num}）{listed_subscribe.name} ..."),
                            current=listed_subscribe.id,
                        )
                    outcome: Optional[str] = "skipped"
                    reason: Optional[str] = "not_eligible"
                    lease = self._subscription_execution_admission.try_acquire(
                        subscription_id=listed_subscribe.id,
                        operation="match",
                        ttl_seconds=self._SUBSCRIPTION_EXECUTION_TTL,
                    )
                    if lease is None:
                        reason = "admission_conflict"
                        logger.debug(f"订阅 {listed_subscribe.name} 正在处理中，本次不再重复检查资源")
                    else:
                        current_subscribe = None
                        execution_context = SubscriptionExecutionContext(
                            lease=lease,
                            admission=self._subscription_execution_admission,
                            cancel_requested=lambda: runtime_stop_state.is_system_stopped,
                        )
                        try:
                            current_subscribe = self.subscription_repository.get(listed_subscribe.id)
                            if current_subscribe is None:
                                logger.debug(f"订阅 {listed_subscribe.id} 已删除，本次不再检查资源")
                            elif current_subscribe.state not in {"R", "P"}:
                                logger.debug(f"订阅 {current_subscribe.name} 当前不需要检查资源，本次跳过")
                            elif execution_context.should_stop():
                                reason = _match_stop_reason(execution_context)
                                logger.debug(f"订阅 {current_subscribe.name} 的资源检查已停止")
                            else:
                                outcome = self._match_subscription(
                                    subscribe=current_subscribe,
                                    processed_torrents=processed_torrents,
                                    candidate_index=candidate_index,
                                    fresh_fact_lease=fresh_fact_lease,
                                    execution_context=execution_context,
                                )
                                reason = _match_result_reason(outcome, execution_context)
                        except SubscriptionSearchCancelled as err:
                            outcome = "skipped"
                            reason = _match_stop_reason(execution_context) or "cancelled"
                            logger.debug(f"订阅 {listed_subscribe.name} 的资源检查已停止：{str(err)}")
                        except Exception as err:
                            outcome = "failed"
                            reason = "error"
                            subscribe_name = (
                                current_subscribe.name if current_subscribe is not None else listed_subscribe.name
                            )
                            logger.error(f"订阅 {subscribe_name} 检查资源时出错：{str(err)}", exc_info=True)
                        finally:
                            _release_match_admission(
                                self._subscription_execution_admission,
                                lease,
                                listed_subscribe.id,
                                summary,
                            )
                    summary.record(outcome, reason)
                    _report_match_progress(
                        progress_callback,
                        summary,
                        value=20 + (summary.finished / total_num * 80 if total_num else 80),
                        text=(f"已检查订阅（{index}/{total_num}）"),
                    )
            finally:
                processed_torrents.clear()
                del processed_torrents
                subscribes.clear()
                del subscribes
                _report_match_finished(progress_callback, summary)
        finally:
            if summary_started:
                logger.info(summary.finish_log())
            if lock_acquired:
                self._match_lock.release()
                logger.debug(f"订阅资源检查已结束：{datetime.now()}")

    def _match_subscription(
        self,
        *,
        subscribe: SubscriptionSnapshot,
        processed_torrents: Dict[str, List[Context]],
        candidate_index: CandidateIndex,
        fresh_fact_lease: FreshFactLease,
        execution_context: SubscriptionExecutionContext,
    ) -> _MatchOutcome:
        """在订阅准入边界调用单订阅 Match 执行。"""
        return self._execute_match(
            subscribe=subscribe,
            processed_torrents=processed_torrents,
            candidate_index=candidate_index,
            fresh_fact_lease=fresh_fact_lease,
            execution_context=execution_context,
        )

    def _execute_match(
        self,
        *,
        subscribe: SubscriptionSnapshot,
        processed_torrents: Dict[str, List[Context]],
        candidate_index: CandidateIndex,
        fresh_fact_lease: FreshFactLease,
        execution_context: SubscriptionExecutionContext,
    ) -> _MatchOutcome:
        """在单条订阅准入期间完成匹配、下载和完成判定。"""
        if execution_context.should_stop():
            return "skipped"
        logger.debug(f"开始匹配订阅，标题：{subscribe.name} ...")
        if subscribe.type == MediaType.MUSIC.value:
            music_contexts = [context for contexts in processed_torrents.values() for context in contexts]
            execution_context.report_phase("matching")
            if not execution_context.should_stop():
                self._match_music_subscribe(
                    subscribe,
                    music_contexts,
                    execution_context=execution_context,
                )
                return "completed"
            return "skipped"
        mediakey = subscribe_media_key(subscribe)
        prepared = _prepare_subscription_match(
            self, subscribe, candidate_index, fresh_fact_lease
        )
        if prepared is None:
            return "completed"
        if execution_context.should_stop():
            return "skipped"
        meta, mediainfo, routed_torrents, domains, sub_sites = prepared

        # 如果媒体已存在或已下载完毕，跳过当前订阅处理
        exist_flag, no_exists = self.check_and_handle_existing_media(
            subscribe=subscribe, meta=meta, mediainfo=mediainfo, mediakey=mediakey
        )
        if exist_flag:
            return "completed"

        # 匹配前聚合多来源别名；清理大字段时保留匹配所需的标题合集。
        mediainfo = MediaChain().supplement_media_info(mediainfo) or mediainfo
        auxiliary_names = list(getattr(mediainfo, "names", None) or [])
        mediainfo.clear()
        mediainfo.names = auxiliary_names

        # 订阅识别词
        if subscribe.custom_words:
            custom_words_list = subscribe.custom_words.split("\n")
        else:
            custom_words_list = None

        # 遍历预识别后的种子
        _match_context = []
        torrenthelper = TorrentHelper()
        systemconfig = get_configured_system_config()
        wordsmatcher = WordsMatcher()
        execution_context.report_phase("matching")
        for domain, contexts in routed_torrents.items():
            if runtime_stop_state.is_system_stopped or execution_context.should_stop():
                break
            if domains and domain not in domains:
                continue
            logger.debug(f"开始匹配站点：{domain}，共缓存了 {len(contexts)} 个种子...")
            for context in contexts:
                if runtime_stop_state.is_system_stopped or execution_context.should_stop():
                    break
                # 提取信息
                _context = copy.copy(context)
                torrent_meta = _context.meta_info
                torrent_mediainfo = _context.media_info
                torrent_info = _context.torrent_info

                # 不在订阅站点范围的不处理
                if sub_sites and torrent_info.site not in sub_sites:
                    logger.debug(f"{torrent_info.site_name} - {torrent_info.title} 不符合订阅站点要求")
                    continue

                # 有自定义识别词时，需要判断是否需要重新识别
                if custom_words_list:
                    # 使用org_string，应用一次后理论上不能再次应用
                    _, apply_words = wordsmatcher.prepare(
                        torrent_meta.org_string, custom_words=custom_words_list
                    )
                    if apply_words:
                        logger.debug(
                            f"{torrent_info.site_name} - {torrent_info.title} 因订阅存在自定义识别词，重新识别元数据..."
                        )
                        # 重新识别元数据
                        torrent_meta = MetaInfo(
                            title=torrent_info.title,
                            subtitle=torrent_info.description,
                            custom_words=custom_words_list,
                        )
                        # 更新元数据缓存
                        _context.meta_info = torrent_meta
                        # 重新识别媒体信息
                        torrent_mediainfo = MediaChain().recognize_by_meta(
                            torrent_meta,
                            episode_group=subscribe.episode_group,
                            obtain_images=False,
                        )
                        if torrent_mediainfo:
                            # 清理多余信息
                            torrent_mediainfo.clear()
                            # 更新种子缓存
                            _context.media_info = torrent_mediainfo
                            _context.match_source = _get_media_id_match_source(
                                torrent_mediainfo
                            )
                            _context.candidate_recognized = bool(
                                resolve_media_identity(media=torrent_mediainfo)[1]
                            )
                            _context.media_info_is_target = False

                # 如果仍然没有识别到媒体信息，尝试标题匹配
                if not torrent_mediainfo or not resolve_media_identity(media=torrent_mediainfo)[1]:
                    logger.debug(
                        f"{torrent_info.site_name} - {torrent_info.title} 重新识别失败，尝试通过标题匹配..."
                    )
                    if not _is_title_match_allowed(
                        mediainfo=mediainfo,
                        torrent_meta=torrent_meta,
                        torrent_info=torrent_info,
                    ):
                        continue
                    # 匹配成功
                    logger.debug(
                        f"{mediainfo.title_year} 通过标题匹配到可选资源：{torrent_info.site_name} - {torrent_info.title}"
                    )
                    torrent_mediainfo = mediainfo
                    # 更新种子缓存
                    _context.media_info = mediainfo
                    _context.match_source = "title"
                    _context.candidate_recognized = False
                    _context.media_info_is_target = True

                # 直接比对媒体信息
                if torrent_mediainfo and resolve_media_identity(media=torrent_mediainfo)[1]:
                    if torrent_mediainfo.type != mediainfo.type:
                        continue
                    torrent_mediainfo = self._SubscribeChain__reconcile_candidate_media(
                        target_mediainfo=mediainfo,
                        candidate_mediainfo=torrent_mediainfo,
                        torrent_meta=torrent_meta,
                        torrent_info=torrent_info,
                        context=_context,
                    )
                    if not torrent_mediainfo:
                        continue
                    match_source = _context.match_source
                    if match_source == "title":
                        # 标题兜底使用的是订阅目标 media_info，不能标记为候选自身识别结果。
                        _context.candidate_recognized = False
                        _context.media_info_is_target = True
                        match_label = "标题复核"
                    elif match_source == "unknown":
                        _context.match_source = _get_media_id_match_source(
                            torrent_mediainfo
                        )
                        _context.candidate_recognized = True
                        _context.media_info_is_target = False
                        match_label = "媒体ID"
                    else:
                        _context.candidate_recognized = True
                        _context.media_info_is_target = False
                        match_label = "媒体ID"
                    logger.debug(
                        f"{mediainfo.title_year} 通过{match_label}匹配到可选资源："
                        f"{torrent_info.site_name} - {torrent_info.title}"
                    )
                else:
                    continue

                # 如果是电视剧
                if torrent_mediainfo.type == MediaType.TV:
                    # 有多季的不要
                    if len(torrent_meta.season_list) > 1:
                        logger.debug(f"{torrent_info.title} 有多季，不处理")
                        continue
                    # 比对季
                    if torrent_meta.begin_season is not None:
                        if meta.begin_season != torrent_meta.begin_season:
                            logger.debug(f"{torrent_info.title} 季不匹配")
                            continue
                    elif meta.begin_season != 1:
                        logger.debug(f"{torrent_info.title} 季不匹配")
                        continue
                    # 非洗版
                    if not subscribe.best_version:
                        # 不是缺失的剧集不要
                        if no_exists and no_exists.get(mediakey):
                            # 缺失集
                            no_exists_info = no_exists.get(mediakey).get(subscribe.season)
                            if no_exists_info:
                                # 是否有交集
                                if (
                                    no_exists_info.episodes
                                    and torrent_meta.episode_list
                                    and not set(no_exists_info.episodes).intersection(
                                        set(torrent_meta.episode_list)
                                    )
                                ):
                                    logger.debug(
                                        f"{torrent_info.title} 对应剧集 {torrent_meta.episode_list} 未包含缺失的剧集"
                                    )
                                    continue
                    else:
                        if not self._SubscribeChain__is_full_season_best_version_resource(
                            meta=torrent_meta,
                            subscribe=subscribe,
                        ):
                            logger.debug(
                                f"{subscribe.name} 正在全集洗版，{torrent_info.title} 不是全集资源"
                            )
                            continue
                        # 洗版时，不符合订阅集数的不要
                        if meta.type == MediaType.TV and not self._is_episode_range_covered(
                            meta=torrent_meta,
                            subscribe=subscribe,
                        ):
                            logger.debug(
                                f"{subscribe.name} 正在洗版，{torrent_info.title} 不符合订阅集数范围"
                            )
                            continue

                # 匹配订阅附加参数
                if not torrenthelper.filter_torrent(
                    torrent_info=torrent_info, filter_params=self.get_params(subscribe)
                ):
                    continue

                # 优先级过滤规则
                if subscribe.best_version:
                    rule_groups = subscribe.filter_groups or systemconfig.get(
                        SystemConfigKey.BestVersionFilterRuleGroups
                    )
                else:
                    rule_groups = subscribe.filter_groups or systemconfig.get(
                        SystemConfigKey.SubscribeFilterRuleGroups
                    )
                result: List[TorrentInfo] = self.filter_torrents(
                    rule_groups=rule_groups, torrent_list=[torrent_info], mediainfo=torrent_mediainfo
                )
                if result is not None and not result:
                    # 不符合过滤规则
                    logger.debug(f"{torrent_info.title} 不匹配过滤规则")
                    continue

                # 洗版时，优先级小于已下载优先级的不要
                if subscribe.best_version:
                    if meta.type == MediaType.TV:
                        if not self._SubscribeChain__prepare_best_version_tv_candidate(
                            subscribe=subscribe,
                            context=_context,
                            priority=torrent_info.pri_order,
                        ):
                            logger.debug(
                                f"{subscribe.name} 正在洗版，{torrent_info.title} "
                                f"优先级未达到当前模式的升级条件"
                            )
                            continue
                    if (
                        meta.type != MediaType.TV
                        and subscribe.current_priority
                        and torrent_info.pri_order <= subscribe.current_priority
                    ):
                        logger.debug(
                            f"{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级"
                        )
                        continue

                # 匹配成功
                logger.debug(f"{mediainfo.title_year} 匹配成功：{torrent_info.title}")
                # 自定义属性
                torrent_mediainfo = apply_subscription_classification(
                    torrent_mediainfo,
                    subscribe,
                )
                _context.media_info = torrent_mediainfo
                if subscribe.episode_group:
                    torrent_mediainfo.episode_group = subscribe.episode_group
                _match_context.append(_context)

        if execution_context.should_stop():
            return "skipped"

        if not _match_context:
            # 未匹配到资源
            logger.debug(f"{mediainfo.title_year} 未匹配到符合条件的资源")
            self.finish_subscribe_or_not(
                subscribe=subscribe, meta=meta, mediainfo=mediainfo, lefts=no_exists
            )
            return "completed"

        if execution_context.should_stop():
            return "skipped"

        # 开始批量择优下载
        logger.debug(f"{mediainfo.title_year} 匹配完成，共匹配到{len(_match_context)}个资源")
        downloads, lefts = self._SubscribeChain__download_best_version_with_full_pack_first(
            contexts=_match_context,
            no_exists=no_exists,
            subscribe=subscribe,
            mediakey=mediakey,
            username=subscribe.username,
            save_path=subscribe.save_path,
            downloader=subscribe.downloader,
            source=self.get_subscribe_source_keyword(subscribe),
            execution_context=execution_context,
        )

        # 同步外部修改，更新订阅信息
        updated_subscribe = self.subscription_repository.get(subscribe.id)

        # 判断是否要完成订阅
        if updated_subscribe:
            self.finish_subscribe_or_not(
                subscribe=updated_subscribe,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
                lefts=lefts,
            )
        return "completed"

    @staticmethod
    def _SubscribeChain__reconcile_candidate_media(
        target_mediainfo: MediaInfo,
        candidate_mediainfo: MediaInfo,
        torrent_meta: MetaBase,
        torrent_info: TorrentInfo,
        context: Context,
    ) -> Optional[MediaInfo]:
        """保留组合宿主的稳定私有调用入口。"""
        return _reconcile_candidate_media(
            target_mediainfo=target_mediainfo,
            candidate_mediainfo=candidate_mediainfo,
            torrent_meta=torrent_meta,
            torrent_info=torrent_info,
            context=context,
        )

    @staticmethod
    def _SubscribeChain__get_media_id_match_source(
        mediainfo: Optional[MediaInfo],
    ) -> str:
        """保留组合宿主解析媒体 ID 匹配来源的稳定入口。"""
        return _get_media_id_match_source(mediainfo)
