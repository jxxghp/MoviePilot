"""批量下载编排 owner。"""

import copy
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Set, Tuple, TypedDict, cast

from app.application.download import selection as _selection
from app.application.download.admission import SubscriptionDownloadGovernance
from app.application.torrent.download import TorrentHelper
from app.chain.download.contract import _DownloadOwnerBase
from app.domain import episode as episode_rules
from app.domain.context import (
    Context,
)
from app.runtime.log import logger
from app.runtime.stop import runtime_stop_state
from app.schemas.mediaserver import NotExistMediaInfo
from app.schemas.types import (
    MediaType,
    NotificationChannel,
)

if TYPE_CHECKING:
    from app.application.download.failures import DownloadFailureSnapshot


def _new_torrent_helper() -> TorrentHelper:
    """构造保留动态初始化行为但具有静态返回类型的种子助手。"""
    factory = cast(Callable[[], TorrentHelper], TorrentHelper)
    return factory()


class DownloadBatchOwner(_DownloadOwnerBase):
    """批量下载编排 owner。"""

    def batch_download(
            self,
            contexts: List[Context],
            no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
            save_path: Optional[str] = None,
            channel: Optional[NotificationChannel] = None,
            source: Optional[str] = None,
            userid: Optional[str] = None,
            username: Optional[str] = None,
            downloader: Optional[str] = None,
            custom_words: Optional[str] = None, governance: Optional[SubscriptionDownloadGovernance] = None,
            allowed_episodes: Optional[Set[int]] = None,
    ) -> Tuple[
        List[Context],
        Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
    ]:
        """
        兼容批量下载公开入口，委托给内部候选匹配阶段。

        该签名被订阅链、消息入口和插件调用；内部策略拆分不改变候选排序、失败冷却、
        完整覆盖判断或剩余缺集的返回结构。
        allowed_episodes 限制本次调用的电视剧集数，在插件替换候选后仍生效；None 沿用
        既有行为，空集合拒绝所有剧集，候选自身的限制只会进一步收窄范围。
        """
        return self._execute_batch_download(
            contexts=contexts,
            no_exists=no_exists,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            downloader=downloader,
            custom_words=custom_words, governance=governance, allowed_episodes=allowed_episodes,
        )

    def _execute_batch_download(self,
                                contexts: List[Context],
                                no_exists: Optional[Dict[str, Dict[int, NotExistMediaInfo]]] = None,
                                save_path: Optional[str] = None,
                                channel: Optional[NotificationChannel] = None,
                                source: Optional[str] = None,
                                userid: Optional[str] = None,
                                username: Optional[str] = None,
                                downloader: Optional[str] = None,
                                custom_words: Optional[str] = None,
                                governance: Optional[SubscriptionDownloadGovernance] = None,
                                allowed_episodes: Optional[Set[int]] = None,
                                ) -> Tuple[
        List[Context],
        Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
    ]:
        """
        根据缺失数据，自动种子列表中组合择优下载
        :param contexts:  资源上下文列表
        :param no_exists:  缺失的剧集信息
        :param save_path:  保存路径, 支持<storage>:<path>, 如rclone:/MP, smb:/server/share/Movies等
        :param channel:  通知渠道
        :param source:  来源（消息通知、订阅、手工下载等）
        :param userid:  用户ID
        :param username: 调用下载的用户名/插件名
        :param downloader: 下载器
        :param custom_words: 下载来源自定义词
        :param governance: 订阅取消与下载器副作用边界
        :param allowed_episodes: 本批电视剧允许集数，在资源选择事件之后与候选限制取交集
        :return: 已下载资源列表及剩余缺集，键格式为 no_exists[source:id]
        """
        missing = no_exists if no_exists is not None else {}
        options = _BatchDownloadOptions(
            save_path=save_path, channel=channel, source=source, userid=userid,
            username=username, downloader=downloader, custom_words=custom_words,
            governance=governance,
        )
        batch = _BatchDownloadRun(self, contexts, missing, options, allowed_episodes)
        batch.run()
        return batch.downloaded, None if no_exists is None else missing


class _BatchDownloadOptions(TypedDict):
    """各阶段原样传递给单资源下载的调用参数，包括订阅副作用治理边界。"""

    save_path: Optional[str]
    channel: Optional[NotificationChannel]
    source: Optional[str]
    userid: Optional[str]
    username: Optional[str]
    downloader: Optional[str]
    custom_words: Optional[str]
    governance: Optional[SubscriptionDownloadGovernance]


class _BatchDownloadRun:
    """单次批量调用的状态；候选、失败冷却与缺集记账不保存在共享 Chain 上。"""

    def __init__(
            self, owner: _DownloadOwnerBase, contexts: List[Context],
            no_exists: Dict[str, Dict[int, NotExistMediaInfo]], options: _BatchDownloadOptions,
            allowed_episodes: Optional[Set[int]],
    ) -> None:
        self.owner = owner
        self.contexts = contexts
        self.no_exists = no_exists
        self.options = options
        self.allowed_episodes = set(allowed_episodes) if allowed_episodes is not None else None
        self.downloaded: List[Context] = []
        self.failures: Dict[str, Optional[DownloadFailureSnapshot]] = {}
        words = options["custom_words"]
        self.custom_words = words.splitlines() if words else None

    def run(self) -> None:
        """只准备一次候选，依次匹配电影/音乐、整季、完整集和拆包补集。"""
        self.contexts, self.failures = self.owner._prepare_batch_download_contexts(
            contexts=self.contexts,
            downloader=self.options["downloader"], source=self.options["source"],
        )
        # 插件可替换整个候选列表，批次范围必须独立保留并作用于最终候选。
        if self.allowed_episodes is not None:
            for context in self.contexts:
                if context.media_info and context.media_info.type == MediaType.TV:
                    context.allowed_episodes = _selection.apply_allowed_episodes(self.allowed_episodes, context)
        self.owner._download_movie_music_candidates(
            contexts=self.contexts, downloaded_list=self.downloaded,
            active_failure_records=self.failures, **self.options,
        )
        if self.no_exists:
            self._download_whole_seasons()
        if self.no_exists:
            self._download_episode_packs()
        if self.no_exists:
            self._download_partial_packs()
        logger.info(f"成功下载种子数：{len(self.downloaded)}，剩余未下载的剧集：{self.no_exists}")

    def _in_failure_cooldown(self, context: Context) -> bool:
        fingerprint = self.owner._build_download_failure_fingerprint(context)
        if fingerprint and fingerprint in self.failures:
            self.owner._log_download_failure_cooldown(context, self.failures[fingerprint])
            return True
        return False

    def _remember_failure(self, context: Context) -> None:
        """按当前识别集数记录失败，后续阶段不得重复提交本轮失败候选。"""
        fingerprint = self.owner._build_download_failure_fingerprint(context)
        if fingerprint:
            self.failures[fingerprint] = None

    def _read_torrent(
            self, context: Context, *, partial: bool = False,
    ) -> Optional[Tuple[bytes, List[int]]]:
        """读取真实文件集数；空内容进入冷却，无法检查的磁力链只跳过当前阶段。"""
        torrent = context.torrent_info
        meta = context.meta_info
        assert meta is not None
        logger.info(f"开始下载种子 {torrent.title} ...")
        content, _, files = self.owner.download_torrent(torrent)
        if not content:
            log_failure = logger.info if partial else logger.warn
            log_failure(f"{torrent.title} 种子下载失败！")
            self.owner._record_download_failure(
                context=context, error_msg="下载种子内容为空",
                downloader=self.options["downloader"], source=self.options["source"],
            )
            self._remember_failure(context)
            return None
        if isinstance(content, str):
            reason = "无法解析" if partial else "无法确定"
            logger.warn(f"{meta.org_string} 下载地址是磁力链，{reason}种子文件集数")
            return None
        episodes = cast(List[int], _new_torrent_helper().get_torrent_episodes(
            files, custom_words=self.custom_words,
        ))
        if partial:
            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{episodes}")
        else:
            logger.info(f"{meta.org_string} 解析种子文件集数为 {episodes}")
        return content, episodes

    def _download_whole_seasons(self) -> None:
        logger.info(f"开始匹配电视剧整季：{self.no_exists}")
        needed: Dict[str, List[int]] = {}
        for media_key, missing_seasons in self.no_exists.items():
            for info in missing_seasons.values():
                if info and not info.episodes:
                    needed.setdefault(media_key, []).append(info.season if info.season is not None else 1)
        logger.info(f"缺失整季：{needed}")
        for media_key, need_seasons in needed.items():
            for context in self.contexts:
                if runtime_stop_state.is_system_stopped:
                    break
                seasons = self._whole_season_candidate(context, media_key, need_seasons)
                if not seasons or not self._submit_whole_season(context, media_key, seasons):
                    continue
                need_seasons = _selection.update_no_exists_seasons(
                    self.no_exists, media_key, need_seasons, seasons,
                )
                logger.info(f"{media_key} 剩余需要季：{need_seasons}")
                if not need_seasons:
                    break

    def _whole_season_candidate(
            self, context: Context, media_key: str, needed: List[int],
    ) -> List[int]:
        media, meta = context.media_info, context.meta_info
        if media is None or meta is None or media.type != MediaType.TV:
            return []
        seasons = meta.season_list or [1]
        if meta.episode_list or not self.owner._matches_media_identity(media, media_key):
            return []
        if context in self.downloaded or self._in_failure_cooldown(context):
            return []
        if not set(seasons).issubset(set(needed)):
            return []
        required = _selection.get_required_episodes(self.no_exists, media_key, seasons[0])
        if not _selection.allows_full_season(seasons, required, context):
            return []
        return seasons

    def _inspect_whole_season(
            self, context: Context, media_key: str, season: int,
    ) -> Optional[Tuple[bytes, bool]]:
        """整季不合格也保留识别集数，供后续完整集阶段继续判断该候选。"""
        parsed = self._read_torrent(context)
        if parsed is None:
            return None
        content, episodes = parsed
        if not episodes or not _selection.allows_full_pack(set(episodes), context):
            return None
        meta = context.meta_info
        assert meta is not None
        meta.set_episodes(begin=min(episodes), end=max(episodes))
        info = _selection.get_no_exist_media(self.no_exists, media_key, season)
        required = _selection.get_required_episodes(self.no_exists, media_key, season) \
            if _selection.requires_complete_coverage(info) else set()
        total = _selection.get_season_episodes(self.no_exists, media_key, season)
        complete = bool(required) and required.issubset(set(episodes))
        if complete:
            logger.info(
                f"{meta.org_string} 解析文件集数已完整覆盖目标范围："
                f"{episode_rules.format_ranges(sorted(required))}")
        if required and not complete:
            missing = sorted(required.difference(episodes))
            logger.info(
                f"{meta.org_string} 解析文件集数未覆盖目标范围，"
                f"缺少 {episode_rules.format_ranges(missing)}，先放弃这个种子")
            return None
        if not required and total and len(episodes) < total:
            logger.info(f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
            return None
        return content, complete

    def _submit_whole_season(
            self, context: Context, media_key: str, seasons: List[int],
    ) -> bool:
        complete = False
        if len(seasons) == 1:
            inspected = self._inspect_whole_season(context, media_key, seasons[0])
            if inspected is None:
                return False
            content, complete = inspected
            logger.info(f"开始下载 {context.torrent_info.title} ...")
            download_id = self.owner.download_single(
                context=context, torrent_content=content, **self.options,
            )
        else:
            logger.info(f"开始下载 {context.torrent_info.title} ...")
            download_id = self.owner.download_single(context, **self.options)
        if not download_id:
            self._remember_failure(context)
            return False
        if complete:
            context.confirmed_full_coverage = True
        logger.info(f"{context.torrent_info.title} 添加下载成功")
        self.downloaded.append(context)
        return True

    def _download_episode_packs(self) -> None:
        logger.info(f"开始电视剧完整集匹配：{self.no_exists}")
        for media_key in list(self.no_exists):
            seasons = self.no_exists.get(media_key)
            if not seasons:
                continue
            # 单季成功会重写缺集字典；每个媒体仍按阶段开始时的季快照遍历。
            for season, info in copy.deepcopy(seasons).items():
                self._download_season_episode_packs(media_key, season, info)

    def _download_season_episode_packs(
            self, media_key: str, season: int, info: NotExistMediaInfo,
    ) -> None:
        needed = info.episodes or list(range(info.start_episode or 1, (info.total_episode or 0) + 1))
        for context in self.contexts:
            if runtime_stop_state.is_system_stopped:
                break
            episodes = self._episode_pack_candidate(context, media_key, season, needed, info)
            if not episodes:
                continue
            meta = context.meta_info
            assert meta is not None
            logger.info(f"开始下载 {meta.title} ...")
            if not self.owner.download_single(context, **self.options):
                self._remember_failure(context)
                continue
            if _selection.requires_complete_coverage(info):
                context.confirmed_full_coverage = True
            logger.info(f"{meta.title} 添加下载成功")
            self.downloaded.append(context)
            needed = _selection.update_no_exists_episodes(
                self.no_exists, media_key, season, needed, episodes,
            )
            logger.info(f"季 {season} 剩余需要集：{needed}")

    def _episode_pack_candidate(
            self, context: Context, media_key: str, season: int,
            needed: List[int], info: NotExistMediaInfo,
    ) -> Set[int]:
        media, meta = context.media_info, context.meta_info
        if media is None or meta is None or media.type != MediaType.TV:
            return set()
        if not self.owner._matches_media_identity(media, media_key):
            return set()
        if context in self.downloaded or self._in_failure_cooldown(context):
            return set()
        if len(meta.season_list) != 1 or meta.season_list[0] != season:
            return set()
        episodes = set(meta.episode_list)
        if not episodes:
            return set()
        effective = _selection.apply_allowed_episodes(set(needed), context)
        if not effective:
            return set()
        if _selection.requires_complete_coverage(info):
            required = _selection.get_required_episodes(self.no_exists, media_key, season)
            matches = bool(required) and required.issubset(episodes) \
                and _selection.allows_full_pack(episodes, context)
        else:
            matches = episodes.issubset(effective)
        return episodes if matches else set()

    def _download_partial_packs(self) -> None:
        logger.info(f"开始电视剧多集拆包匹配：{self.no_exists}")
        for media_key in list(self.no_exists):
            seasons = self.no_exists.get(media_key)
            if not seasons:
                continue
            for season in list(seasons):
                info = seasons.get(season)
                if info is None or _selection.requires_complete_coverage(info) or not info.episodes:
                    continue
                self._download_season_partial_packs(media_key, season, info.episodes)

    def _download_season_partial_packs(
            self, media_key: str, season: int, needed: List[int],
    ) -> None:
        for context in self.contexts:
            if runtime_stop_state.is_system_stopped:
                break
            media, meta = context.media_info, context.meta_info
            if media is None or meta is None or media.type != MediaType.TV:
                continue
            # 拆包阶段沿用先检查重复/冷却、再检查缺集与媒体身份的顺序。
            if context in self.downloaded or self._in_failure_cooldown(context):
                continue
            if not needed:
                break
            effective = _selection.apply_allowed_episodes(set(needed), context)
            if not effective or not self._matches_partial_pack(context, media_key, season, effective):
                continue
            selected = self._submit_partial_pack(context, effective)
            if selected:
                needed = _selection.update_no_exists_episodes(
                    self.no_exists, media_key, season, needed, selected,
                )
                logger.info(f"季 {season} 剩余需要集：{needed}")

    def _matches_partial_pack(
            self, context: Context, media_key: str, season: int, effective: Set[int],
    ) -> bool:
        meta = context.meta_info
        assert meta is not None
        return self.owner._matches_media_identity(context.media_info, media_key) \
            and (not meta.episode_list or bool(set(meta.episode_list).intersection(effective))) \
            and len(meta.season_list) == 1 and meta.season_list[0] == season

    def _submit_partial_pack(self, context: Context, effective: Set[int]) -> Set[int]:
        """只在成功提交选中文件后更新识别集数，失败时保留原候选供换源记账。"""
        parsed = self._read_torrent(context, partial=True)
        if parsed is None:
            return set()
        content, episodes = parsed
        torrent = context.torrent_info
        selected = set(episodes).intersection(effective)
        if not selected:
            logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
            return set()
        logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected}")
        logger.info(f"开始下载 {torrent.title} ...")
        if not self.owner.download_single(
                context=context, torrent_content=content, episodes=selected, **self.options,
        ):
            self._remember_failure(context)
            return set()
        logger.info(f"{torrent.title} 添加下载成功")
        self.downloaded.append(context)
        meta = context.meta_info
        assert meta is not None
        meta.set_episodes(begin=min(episodes), end=max(episodes))
        return selected
