"""批量下载编排 owner。"""

import copy
from typing import Callable, Dict, List, Optional, Set, Tuple, cast

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
                       ) -> Tuple[
                           List[Context],
                           Optional[Dict[str, Dict[int, NotExistMediaInfo]]],
                       ]:
        """
        兼容批量下载公开入口，委托给内部候选匹配阶段。

        该签名被订阅链、消息入口和插件调用；内部策略拆分不改变候选排序、失败冷却、
        完整覆盖判断或剩余缺集的返回结构。
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
            custom_words=custom_words, governance=governance,
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
                       custom_words: Optional[str] = None, governance: Optional[SubscriptionDownloadGovernance] = None,
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
        :return: 已下载资源列表及剩余缺集，键格式为 no_exists[source:id]
        """
        no_exists_was_none = no_exists is None
        if no_exists is None:
            no_exists = {}
        # 已下载的项目
        downloaded_list: List[Context] = []
        custom_word_list = custom_words.splitlines() if custom_words else None

        # 缺集记账与覆盖判定规则已下沉至 app/application/download/selection.py；
        # 此处仅保留闭包委托，让编排循环保持原调用形态。
        def __update_seasons(
            _mid: str,
            _need: List[int],
            _current: List[int],
        ) -> List[int]:
            """更新need_tvs季数，返回剩余季数。"""
            return _selection.update_no_exists_seasons(no_exists, _mid, _need, _current)

        def __update_episodes(
            _mid: str,
            _sea: int,
            _need: List[int],
            _current: Set[int],
        ) -> List[int]:
            """更新need_tvs集数，返回剩余集数。"""
            return _selection.update_no_exists_episodes(no_exists, _mid, _sea, _need, _current)

        def __get_season_episodes(_mid: str, season: int) -> int:
            """获取需要的季的集数。"""
            return _selection.get_season_episodes(no_exists, _mid, season)

        def __get_no_exist_media(_mid: str, season: int) -> Optional[NotExistMediaInfo]:
            """获取指定媒体和季的缺失信息。"""
            return _selection.get_no_exist_media(no_exists, _mid, season)

        def __get_required_episodes(_mid: str, season: int) -> Set[int]:
            """获取整季候选必须覆盖的目标集范围。"""
            return _selection.get_required_episodes(no_exists, _mid, season)

        def __requires_complete_coverage(_tv: Optional[NotExistMediaInfo]) -> bool:
            """判断当前缺失范围是否要求候选资源完整覆盖目标范围。"""
            return _selection.requires_complete_coverage(_tv)

        def __apply_allowed_episodes(
            _need_episodes: Set[int] | List[int],
            _context: Context,
        ) -> Set[int]:
            """根据候选允许集裁剪 need_episodes，返回真正可下载的剧集集合。"""
            return _selection.apply_allowed_episodes(set(_need_episodes), _context)

        def __get_movie_download_key(_context: Context) -> str:
            """获取电影下载去重键。"""
            return _selection.get_movie_download_key(_context)

        def __get_music_download_key(_context: Context) -> str:
            """获取音乐下载去重键。"""
            return _selection.get_music_download_key(_context)

        # 仅排序，不提前按媒体控重；下载失败时需要继续尝试同组后续候选。
        contexts, active_failure_records = self._prepare_batch_download_contexts(
            contexts=contexts,
            downloader=downloader,
            source=source,
        )

        def __is_context_in_failure_cooldown(_context: Context) -> bool:
            """
            判断候选资源是否仍处于失败冷却期。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint and fingerprint in active_failure_records:
                self._log_download_failure_cooldown(
                    _context,
                    active_failure_records[fingerprint],
                )
                return True
            return False

        def __remember_context_failure(_context: Context) -> None:
            """
            将本轮失败候选加入内存冷却集合，避免同一批次重复尝试。
            """
            fingerprint = self._build_download_failure_fingerprint(_context)
            if fingerprint:
                active_failure_records[fingerprint] = None

        self._download_movie_music_candidates(
            contexts=contexts,
            downloaded_list=downloaded_list,
            active_failure_records=active_failure_records,
            save_path=save_path,
            channel=channel,
            source=source,
            userid=userid,
            username=username,
            downloader=downloader,
            custom_words=custom_words, governance=governance,
        )

        # 电视剧整季匹配
        if no_exists:
            logger.info(f"开始匹配电视剧整季：{no_exists}")
            # 先把整季缺失的拿出来，看是否刚好有所有季都满足的种子 {source:id: [seasons]}
            need_seasons: Dict[str, List[int]] = {}
            for need_mid, missing_seasons in no_exists.items():
                for tv in missing_seasons.values():
                    if not tv:
                        continue
                    # 季列表为空的，代表全季缺失
                    if not tv.episodes:
                        if not need_seasons.get(need_mid):
                            need_seasons[need_mid] = []
                        need_seasons[need_mid].append(tv.season if tv.season is not None else 1)
            logger.info(f"缺失整季：{need_seasons}")
            # 查找整季包含的种子，只处理整季没集的种子或者是集数超过季的种子
            for need_mid, need_season in need_seasons.items():
                # 循环种子
                for context in contexts:
                    if runtime_stop_state.is_system_stopped:
                        break
                    # 媒体信息
                    media = context.media_info
                    # 识别元数据
                    meta = context.meta_info
                    # 种子信息
                    torrent = context.torrent_info
                    if media is None or meta is None:
                        continue
                    # 排除电视剧
                    if media.type != MediaType.TV:
                        continue
                    # 种子的季清单
                    torrent_season = meta.season_list
                    # 没有季的默认为第1季
                    if not torrent_season:
                        torrent_season = [1]
                    # 种子有集的不要
                    if meta.episode_list:
                        continue
                    # 匹配TMDBID
                    if self._matches_media_identity(media, need_mid):
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 种子季是需要季或者子集
                        if set(torrent_season).issubset(set(need_season)):
                            complete_coverage_matched = False
                            if len(torrent_season) == 1:
                                # 只有一季的可能是命名错误，需要打开种子鉴别，只有实际集数大于等于总集数才下载
                                logger.info(f"开始下载种子 {torrent.title} ...")
                                content, _, torrent_files = self.download_torrent(torrent)
                                if not content:
                                    logger.warn(f"{torrent.title} 种子下载失败！")
                                    self._record_download_failure(
                                        context=context,
                                        error_msg="下载种子内容为空",
                                        downloader=downloader,
                                        source=source,
                                    )
                                    __remember_context_failure(context)
                                    continue
                                if isinstance(content, str):
                                    logger.warn(f"{meta.org_string} 下载地址是磁力链，无法确定种子文件集数")
                                    continue
                                torrent_episodes = cast(
                                    List[int],
                                    _new_torrent_helper().get_torrent_episodes(
                                        torrent_files,
                                        custom_words=custom_word_list,
                                    ),
                                )
                                logger.info(f"{meta.org_string} 解析种子文件集数为 {torrent_episodes}")
                                if not torrent_episodes:
                                    continue
                                torrent_episodes_set = set(torrent_episodes)
                                # 更新集数范围
                                begin_ep = min(torrent_episodes)
                                end_ep = max(torrent_episodes)
                                meta.set_episodes(begin=begin_ep, end=end_ep)
                                # 需要目标集范围；完整覆盖场景必须覆盖范围内每一集，不能只按数量判断。
                                need_tv_info = __get_no_exist_media(need_mid, torrent_season[0])
                                required_episodes = __get_required_episodes(need_mid, torrent_season[0]) \
                                    if __requires_complete_coverage(need_tv_info) else set()
                                need_total = __get_season_episodes(need_mid, torrent_season[0])
                                complete_coverage_matched = bool(required_episodes) \
                                    and required_episodes.issubset(torrent_episodes_set)
                                if complete_coverage_matched:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数已完整覆盖目标范围："
                                        f"{episode_rules.format_ranges(sorted(required_episodes))}")
                                if required_episodes and not complete_coverage_matched:
                                    missing_episodes = sorted(required_episodes.difference(torrent_episodes_set))
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数未覆盖目标范围，"
                                        f"缺少 {episode_rules.format_ranges(missing_episodes)}，先放弃这个种子")
                                    continue
                                if not required_episodes and need_total and len(torrent_episodes) < need_total:
                                    logger.info(
                                        f"{meta.org_string} 解析文件集数发现不是完整合集，先放弃这个种子")
                                    continue
                                else:
                                    # 下载
                                    logger.info(f"开始下载 {torrent.title} ...")
                                    download_id = self.download_single(
                                        context=context,
                                        torrent_content=content,
                                        save_path=save_path,
                                        channel=channel,
                                        source=source,
                                        userid=userid,
                                        username=username,
                                        downloader=downloader,
                                        custom_words=custom_words, governance=governance,
                                    )
                            else:
                                # 下载
                                logger.info(f"开始下载 {torrent.title} ...")
                                download_id = self.download_single(context, save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words, governance=governance)

                            if download_id:
                                # 下载成功
                                if complete_coverage_matched:
                                    context.confirmed_full_coverage = True
                                logger.info(f"{torrent.title} 添加下载成功")
                                downloaded_list.append(context)
                                # 更新仍需季集
                                need_season = __update_seasons(_mid=need_mid,
                                                               _need=need_season,
                                                               _current=torrent_season)
                                logger.info(f"{need_mid} 剩余需要季：{need_season}")
                                if not need_season:
                                    # 全部下载完成
                                    break
                            else:
                                __remember_context_failure(context)
        # 电视剧季内的集匹配
        if no_exists:
            logger.info(f"开始电视剧完整集匹配：{no_exists}")
            # TMDBID列表
            media_keys = list(no_exists)
            for need_mid in media_keys:
                # dict[season, [NotExistMediaInfo]]
                season_map = no_exists.get(need_mid)
                if not season_map:
                    continue
                season_map_copy = copy.deepcopy(season_map)
                # 循环每一季
                for sea, missing_info in season_map_copy.items():
                    # 当前需要季
                    season_number = sea
                    # 当前需要集
                    need_episodes = missing_info.episodes
                    # TMDB总集数
                    total_episode = missing_info.total_episode or 0
                    # 需要开始集
                    start_episode = missing_info.start_episode or 1
                    # 缺失整季的转化为缺失集进行比较
                    if not need_episodes:
                        need_episodes = list(range(start_episode, total_episode + 1))
                    # 循环种子
                    for context in contexts:
                        if runtime_stop_state.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 非剧集不处理
                        if media is None or meta is None or media.type != MediaType.TV:
                            continue
                        # 匹配TMDB
                        if self._matches_media_identity(media, need_mid):
                            # 不重复添加
                            if context in downloaded_list:
                                continue
                            if __is_context_in_failure_cooldown(context):
                                continue
                            # 种子季
                            torrent_season = meta.season_list
                            # 只处理单季含集的种子
                            if len(torrent_season) != 1 or torrent_season[0] != season_number:
                                continue
                            # 种子集列表
                            candidate_episodes: Set[int] = set(meta.episode_list)
                            # 整季的不处理
                            if not candidate_episodes:
                                continue
                            # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                            effective_need = __apply_allowed_episodes(need_episodes, context)
                            if not effective_need:
                                continue
                            if __requires_complete_coverage(missing_info):
                                # 完整覆盖任务要求候选集数覆盖目标范围，允许资源包含范围外的额外集。
                                required_episodes = __get_required_episodes(need_mid, season_number)
                                match_episodes = required_episodes.issubset(candidate_episodes) \
                                    if required_episodes else False
                            else:
                                # 普通缺集下载保持原语义：候选自身必须是所需集的子集。
                                match_episodes = candidate_episodes.issubset(effective_need)
                            if match_episodes:
                                # 下载
                                logger.info(f"开始下载 {meta.title} ...")
                                download_id = self.download_single(context, save_path=save_path,
                                                                   channel=channel, source=source,
                                                                   userid=userid, username=username,
                                                                   downloader=downloader,
                                                                   custom_words=custom_words, governance=governance)
                                if download_id:
                                    # 下载成功
                                    if __requires_complete_coverage(missing_info):
                                        context.confirmed_full_coverage = True
                                    logger.info(f"{meta.title} 添加下载成功")
                                    downloaded_list.append(context)
                                    # 更新仍需集数
                                    need_episodes = __update_episodes(_mid=need_mid,
                                                                      _need=need_episodes,
                                                                      _sea=season_number,
                                                                      _current=candidate_episodes)
                                    logger.info(f"季 {season_number} 剩余需要集：{need_episodes}")
                                else:
                                    __remember_context_failure(context)

        # 仍然缺失的剧集，从整季中选择需要的集数文件下载，仅支持QB和TR
        if no_exists:
            logger.info(f"开始电视剧多集拆包匹配：{no_exists}")
            # TMDBID列表
            no_exists_list = list(no_exists)
            for need_mid in no_exists_list:
                # dict[season, [NotExistMediaInfo]]
                remaining_seasons = no_exists.get(need_mid)
                if not remaining_seasons:
                    continue
                # 需要季列表
                season_numbers = list(remaining_seasons)
                # 循环需要季
                for sea in season_numbers:
                    # NotExistMediaInfo
                    remaining_info = remaining_seasons.get(sea)
                    if remaining_info is None:
                        continue
                    # 当前需要季
                    season_number = sea
                    # 当前需要集
                    need_episodes = remaining_info.episodes
                    if __requires_complete_coverage(remaining_info):
                        continue
                    # 没有集的不处理
                    if not need_episodes:
                        continue
                    # 循环种子
                    for context in contexts:
                        if runtime_stop_state.is_system_stopped:
                            break
                        # 媒体信息
                        media = context.media_info
                        # 识别元数据
                        meta = context.meta_info
                        # 种子信息
                        torrent = context.torrent_info
                        # 非剧集不处理
                        if media is None or meta is None or media.type != MediaType.TV:
                            continue
                        # 不重复添加
                        if context in downloaded_list:
                            continue
                        if __is_context_in_failure_cooldown(context):
                            continue
                        # 没有需要集后退出
                        if not need_episodes:
                            break
                        # 上游对本候选施加的允许集（如洗版按集允许列表）裁剪本季缺集，得到真正可下载范围。
                        effective_need = __apply_allowed_episodes(need_episodes, context)
                        if not effective_need:
                            continue
                        # 选中一个单季整季的或单季包括需要的所有集的
                        if self._matches_media_identity(media, need_mid) \
                                and (not meta.episode_list
                                     or set(meta.episode_list).intersection(effective_need)) \
                                and len(meta.season_list) == 1 \
                                and meta.season_list[0] == season_number:
                            # 检查种子看是否有需要的集
                            logger.info(f"开始下载种子 {torrent.title} ...")
                            content, _, torrent_files = self.download_torrent(torrent)
                            if not content:
                                logger.info(f"{torrent.title} 种子下载失败！")
                                self._record_download_failure(
                                    context=context,
                                    error_msg="下载种子内容为空",
                                    downloader=downloader,
                                    source=source,
                                )
                                __remember_context_failure(context)
                                continue
                            if isinstance(content, str):
                                logger.warn(f"{meta.org_string} 下载地址是磁力链，无法解析种子文件集数")
                                continue
                            # 种子全部集
                            torrent_episodes = cast(
                                List[int],
                                _new_torrent_helper().get_torrent_episodes(
                                    torrent_files,
                                    custom_words=custom_word_list,
                                ),
                            )
                            logger.info(f"{torrent.site_name} - {meta.org_string} 解析种子文件集数：{torrent_episodes}")
                            # 选中的集
                            selected_episodes = set(torrent_episodes).intersection(effective_need)
                            if not selected_episodes:
                                logger.info(f"{torrent.site_name} - {torrent.title} 没有需要的集，跳过...")
                                continue
                            logger.info(f"{torrent.site_name} - {torrent.title} 选中集数：{selected_episodes}")
                            # 添加下载
                            logger.info(f"开始下载 {torrent.title} ...")
                            download_id = self.download_single(
                                context=context,
                                torrent_content=content,
                                episodes=selected_episodes,
                                save_path=save_path,
                                channel=channel,
                                source=source,
                                userid=userid,
                                username=username,
                                downloader=downloader,
                                custom_words=custom_words, governance=governance,
                            )
                            if not download_id:
                                __remember_context_failure(context)
                                continue
                            # 下载成功
                            logger.info(f"{torrent.title} 添加下载成功")
                            downloaded_list.append(context)
                            # 更新种子集数范围
                            begin_ep = min(torrent_episodes)
                            end_ep = max(torrent_episodes)
                            meta.set_episodes(begin=begin_ep, end=end_ep)
                            # 更新仍需集数
                            need_episodes = __update_episodes(_mid=need_mid,
                                                              _need=need_episodes,
                                                              _sea=season_number,
                                                              _current=selected_episodes)
                            logger.info(f"季 {season_number} 剩余需要集：{need_episodes}")

        # 返回下载的资源，剩下没下完的
        logger.info(f"成功下载种子数：{len(downloaded_list)}，剩余未下载的剧集：{no_exists}")
        return downloaded_list, None if no_exists_was_none else no_exists
