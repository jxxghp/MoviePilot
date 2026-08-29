"""订阅主动搜索编排"""

import random
import time
from datetime import datetime
from typing import Any, Callable, Optional, cast

from app.application.configuration import get_configured_system_config
from app.application.subscription.contract import (
    SubscriptionRepository,
    SubscriptionSnapshot,
    build_subscribe_meta,
    subscribe_media_key,
)
from app.application.subscription.query import SubscriptionQueryService
from app.chain.media import MediaChain
from app.chain.search.facade import SearchChain
from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.chain.subscribe.identity import subscribe_recognize_kwargs
from app.domain.context import (
    MediaInfo,
)
from app.domain.meta.metabase import MetaBase
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.types import (
    MediaType,
    SystemConfigKey,
)


class SubscribeSearchOwner(_SubscribeOwnerBase):
    """订阅主动搜索编排，作为 SubscribeChain 的单一职责实现 owner。"""

    def _subscription_query(self) -> SubscriptionQueryService:
        """构造绑定订阅 Oper 的查询应用服务。"""
        return SubscriptionQueryService(self.subscription_repository)

    @classmethod
    def exists(
        cls,
        mediainfo: MediaInfo,
        meta: MetaBase = None,
        repository: Optional[SubscriptionRepository] = None,
    ) -> bool:
        """
        判断订阅是否已存在
        """
        chain = cls()
        if repository is not None:
            chain.subscription_repository = repository
        result: bool = chain._subscription_query().exists(mediainfo, meta)
        return result

    def _acquire_run_lock(
        self,
        operation: str,
        progress_callback: Optional[Callable[..., None]],
    ) -> bool:
        """获取订阅任务锁，超时时统一记录并结束本轮进度。"""
        if self._rlock.acquire(blocking=True, timeout=self._LOCK_TIMOUT):
            logger.debug(f"{operation} lock acquired at {datetime.now()}")
            return True
        operation_label = {"search": "搜索", "match": "匹配"}[operation]
        progress_text = {
            "search": "订阅搜索锁等待超时，已跳过本轮",
            "match": "订阅匹配锁等待超时，已跳过本轮",
        }[operation]
        logger.error(f"订阅{operation_label}锁等待超时，已中止本轮执行")
        if progress_callback:
            progress_callback(
                value=100,
                text=progress_text,
            )
        return False

    def search(
        self,
        sid: Optional[int] = None,
        state: Optional[str] = "N",
        manual: Optional[bool] = False,
        progress_callback: Optional[Callable[..., None]] = None,
        sids: Optional[tuple[int, ...]] = None,
    ) -> None:
        """
        执行订阅搜索。

        保持定时任务、API 和插件使用的公开签名，搜索实现委托给内部执行阶段。
        """
        return self._execute_search(
            sid=sid,
            sids=sids,
            state=state,
            manual=manual,
            progress_callback=progress_callback,
        )

    def _execute_search(
        self,
        sid: Optional[int] = None,
        state: Optional[str] = "N",
        manual: Optional[bool] = False,
        progress_callback: Optional[Callable[..., None]] = None,
        sids: Optional[tuple[int, ...]] = None,
    ) -> None:
        """
        订阅搜索
        :param sid: 订阅ID，有值时只处理该订阅
        :param state: 订阅状态 N:新建, R:订阅中, P:待定, S:暂停
        :param manual: 是否手动搜索
        :param progress_callback: 定时服务进度更新回调
        :param sids: 订阅ID集合，有值时按给定顺序处理
        :return: 更新订阅状态为R或删除订阅
        """
        lock_acquired = self._acquire_run_lock("search", progress_callback)
        if not lock_acquired:
            return
        subscribes = []
        processed = []
        try:
            subscribes = self._load_search_subscriptions(sid=sid, sids=sids, state=state)
            total = len(subscribes)
            if progress_callback:
                progress_callback(
                    value=0,
                    text=f"开始订阅搜索，共 {total} 个订阅 ...",
                    data={"total": total, "finished": 0},
                )
            searchchain = SearchChain()
            for index, subscribe in enumerate(subscribes, start=1):
                if runtime_stop_state.is_system_stopped:
                    break
                processed.append(subscribe)
                self._report_search_progress(progress_callback, subscribe, index, total)
                if self._defer_recent_subscription(subscribe):
                    continue
                self._wait_before_scheduled_search(sid, sids, state, progress_callback)
                current = subscribe
                try:
                    current = self._process_search_subscription(subscribe, searchchain)
                finally:
                    if current and current.state == "N":
                        self._SubscribeChain__apply_subscribe_update(
                            current,
                            {"state": "R"},
                            scene="search_reset",
                        )
                    self._report_search_progress(progress_callback, subscribe, index, total, finished=True)
            self._notify_manual_search(manual, sid, sids, subscribes, processed)
            if progress_callback:
                progress_callback(value=100, text="订阅搜索完成")
        finally:
            subscribes.clear()
            self._rlock.release()
            logger.debug(f"search Lock released at {datetime.now()}")

    def _load_search_subscriptions(
        self,
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
    ) -> list[SubscriptionSnapshot]:
        """按单条、指定批次或状态读取本轮搜索订阅。"""
        repository = self.subscription_repository
        if sid:
            subscribe = repository.get(sid)
            return [subscribe] if subscribe else []
        if sids is not None:
            return [item for current_id in sids if (item := repository.get(current_id)) is not None]
        return cast(list[SubscriptionSnapshot], repository.list(self.get_states_for_search(state or "N")))

    @staticmethod
    def _defer_recent_subscription(subscribe: SubscriptionSnapshot) -> bool:
        """新增一分钟内保留 N 状态，为用户编辑筛选条件留出窗口。"""
        if not subscribe.date:
            return False
        subscribe_time = datetime.strptime(subscribe.date, "%Y-%m-%d %H:%M:%S")
        if (datetime.now() - subscribe_time).total_seconds() >= 60:
            return False
        logger.debug(f"订阅标题：{subscribe.name} 新增小于1分钟，暂不搜索...")
        return True

    @staticmethod
    def _wait_before_scheduled_search(
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        state: Optional[str],
        progress_callback: Optional[Callable[..., None]],
    ) -> None:
        """为自动搜索增加随机间隔，手动和定向批次不等待。"""
        if sid or sids is not None or state not in ["R", "P"]:
            return
        sleep_time = random.randint(60, 300)
        logger.info(f"订阅搜索随机休眠 {sleep_time} 秒 ...")
        if progress_callback:
            progress_callback(text=f"订阅搜索随机休眠 {sleep_time} 秒后继续 ...")
        time.sleep(sleep_time)

    def _process_search_subscription(
        self,
        subscribe: SubscriptionSnapshot,
        searchchain: SearchChain,
    ) -> Optional[SubscriptionSnapshot]:
        """处理单个订阅，并返回下载后重新读取的状态快照。"""
        logger.info(f"开始搜索订阅，标题：{subscribe.name} ...")
        if subscribe.type == MediaType.MUSIC.value:
            self._search_music_subscribe(subscribe)
            return subscribe
        try:
            meta = build_subscribe_meta(subscribe)
        except ValueError:
            logger.error(f"订阅 {subscribe.name} 类型错误：{subscribe.type}")
            return subscribe
        mediainfo: MediaInfo = MediaChain().recognize_media(
            meta=meta,
            mtype=meta.type,
            **subscribe_recognize_kwargs(subscribe),
            episode_group=subscribe.episode_group,
            cache=False,
        )
        if not mediainfo:
            logger.warning(
                f"未识别到媒体信息，标题：{subscribe.name}，"
                f"媒体来源：{subscribe.media_source}，媒体 ID：{subscribe.media_id}"
            )
            return subscribe
        mediakey = subscribe_media_key(subscribe)
        exists, no_exists = self.check_and_handle_existing_media(
            subscribe=subscribe,
            meta=meta,
            mediainfo=mediainfo,
            mediakey=mediakey,
        )
        if exists:
            return subscribe
        rule_key = (
            SystemConfigKey.BestVersionFilterRuleGroups
            if subscribe.best_version
            else SystemConfigKey.SubscribeFilterRuleGroups
        )
        contexts = searchchain.process(
            mediainfo=mediainfo,
            keyword=subscribe.keyword,
            no_exists=no_exists,
            sites=self.get_sub_sites(subscribe),
            rule_groups=subscribe.filter_groups or get_configured_system_config().get(rule_key) or [],
            area="imdbid" if subscribe.search_imdbid else "title",
            custom_words=subscribe.custom_words.split("\n") if subscribe.custom_words else None,
            filter_params=self.get_params(subscribe),
        )
        if not contexts:
            logger.warning(f"订阅 {subscribe.keyword or subscribe.name} 未搜索到资源")
            self.finish_subscribe_or_not(
                subscribe=subscribe,
                meta=meta,
                mediainfo=mediainfo,
                lefts=no_exists,
            )
            return subscribe
        matched = self._filter_search_contexts(subscribe, contexts)
        if not matched:
            logger.warning(f"订阅 {subscribe.name} 没有符合过滤条件的资源")
            self.finish_subscribe_or_not(subscribe=subscribe, meta=meta, mediainfo=mediainfo, lefts=no_exists)
            return subscribe
        downloads, lefts = self._SubscribeChain__download_best_version_with_full_pack_first(
            contexts=matched,
            no_exists=no_exists,
            subscribe=subscribe,
            mediakey=mediakey,
            username=subscribe.username,
            save_path=subscribe.save_path,
            downloader=subscribe.downloader,
            source=self.get_subscribe_source_keyword(subscribe),
        )
        current = self.subscription_repository.get(subscribe.id)
        if current:
            self.finish_subscribe_or_not(
                subscribe=current,
                meta=meta,
                mediainfo=mediainfo,
                downloads=downloads,
                lefts=lefts,
            )
        return cast(Optional[SubscriptionSnapshot], current)

    def _filter_search_contexts(
        self,
        subscribe: SubscriptionSnapshot,
        contexts: list[Any],
    ) -> list[Any]:
        """过滤不满足洗版范围或优先级的搜索候选，并释放原结果列表。"""
        matched = []
        try:
            for context in contexts:
                if runtime_stop_state.is_system_stopped:
                    break
                torrent_meta = context.meta_info
                torrent_info = context.torrent_info
                media = context.media_info
                if subscribe.best_version and media.type == MediaType.TV:
                    if not self._SubscribeChain__is_full_season_best_version_resource(torrent_meta, subscribe):
                        logger.info(f"{subscribe.name} 正在全集洗版，{torrent_info.title} 不是全集资源")
                        continue
                    if not self._is_episode_range_covered(torrent_meta, subscribe):
                        logger.info(f"{subscribe.name} 正在洗版，{torrent_info.title} 不符合订阅集数范围")
                        continue
                    if not self._SubscribeChain__prepare_best_version_tv_candidate(
                        subscribe,
                        context,
                        torrent_info.pri_order,
                    ):
                        logger.info(f"{subscribe.name} 正在洗版，{torrent_info.title} 优先级未达到当前模式的升级条件")
                        continue
                if (
                    subscribe.best_version
                    and media.type != MediaType.TV
                    and subscribe.current_priority
                    and torrent_info.pri_order <= subscribe.current_priority
                ):
                    logger.info(f"{subscribe.name} 正在洗版，{torrent_info.title} 优先级低于或等于已下载优先级")
                    continue
                if subscribe.media_category:
                    media.category = subscribe.media_category
                if subscribe.episode_group:
                    media.episode_group = subscribe.episode_group
                matched.append(context)
        finally:
            contexts.clear()
        return matched

    @staticmethod
    def _report_search_progress(
        progress_callback: Optional[Callable[..., None]],
        subscribe: SubscriptionSnapshot,
        index: int,
        total: int,
        finished: bool = False,
    ) -> None:
        """发布单订阅搜索开始或完成进度。"""
        if not progress_callback:
            return
        done = index if finished else index - 1
        data = {"total": total, "finished": done}
        if not finished:
            data["current"] = subscribe.id
        progress_callback(
            value=done / total * 100 if total else 100,
            text=(
                f"订阅搜索（{index}/{total}）处理完成"
                if finished
                else f"正在搜索订阅（{index}/{total}）{subscribe.name} ..."
            ),
            data=data,
        )

    def _notify_manual_search(
        self,
        manual: Optional[bool],
        sid: Optional[int],
        sids: Optional[tuple[int, ...]],
        subscribes: list[SubscriptionSnapshot],
        processed: list[SubscriptionSnapshot],
    ) -> None:
        """为手动触发的搜索发布保持旧文案的系统消息。"""
        if not manual:
            return
        if not subscribes:
            self.messagehelper.put("没有找到订阅！", title="订阅搜索", role="system")
        elif sid:
            self.messagehelper.put(f"{subscribes[0].name} 搜索完成！", title="订阅搜索", role="system")
        elif sids is not None:
            for subscribe in processed:
                self.messagehelper.put(f"{subscribe.name} 搜索完成！", title="订阅搜索", role="system")
        else:
            self.messagehelper.put("所有订阅搜索完成！", title="订阅搜索", role="system")
