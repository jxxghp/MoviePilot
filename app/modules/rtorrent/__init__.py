"""
rTorrent 下载模块，负责实例路由、任务投影与下载文件选择。
"""

from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple, Union, cast

from app.domain import torrent as torrent_rules
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation import temporal as time_tools
from app.foundation import text as text_tools
from app.modules._base.downloader import TRANSFERRED_TAG, _DownloaderModuleBase
from app.modules.rtorrent.rtorrent import Rtorrent, TorrentData
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.dashboard import DownloaderInfo as _SchemaDownloaderInfo
from app.schemas.transfer import DownloaderFile, DownloaderTorrent
from app.schemas.types import (
    DownloaderType,
    DownloadTaskState,
    ModuleType,
    TorrentQueryStatus,
    TorrentStatus,
)


class RtorrentModule(_DownloaderModuleBase[Rtorrent]):
    """
    rTorrent 下载器模块，负责任务添加、标签和文件状态转换。
    """

    def init_module(self) -> None:
        """
        根据系统下载器配置创建并注册本类型的客户端实例。
        """
        super().init_service(
            service_name=Rtorrent.__name__.lower(), service_type=Rtorrent
        )

    @staticmethod
    def get_name() -> str:
        """
        返回模块展示名称。
        """
        return "Rtorrent"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Downloader

    @staticmethod
    def get_subtype() -> DownloaderType:
        """
        获取模块子类型。
        """
        return DownloaderType.Rtorrent

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 3

    def stop(self) -> None:
        """
        下载器客户端由服务基类管理，本模块无额外停止动作。
        """
        pass

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        下载器实例由系统配置管理，不声明独立模块开关。
        """
        pass

    def download(
        self,
        content: Union[Path, str, bytes],
        download_dir: Path,
        cookie: str,
        episodes: Optional[Set[int]] = None,
        category: Optional[str] = None,
        label: Optional[str] = None,
        downloader: Optional[str] = None,
        custom_words: Optional[str] = None,
        episode_selection: Optional[torrent_rules.EpisodeSelection] = None,
        confirmed_selection: Optional[torrent_rules.ConfirmedSelection] = None,
        user_specified: bool = False,
    ) -> Optional[Tuple[Optional[str], Optional[str], Optional[str], str]]:
        """
        读取种子内容并添加下载任务，按需选择剧集文件。

        同一 hash 已存在时不新建任务，而是核验已有任务能否受理本次请求并做最小追加，详见
        ``_accept_existing_task``；只有目标确认受理才返回 hash。

        :param content: 本地种子路径、磁力链接或种子文件字节内容；普通 URL 不会由本方法下载
        :param download_dir: MoviePilot 侧的下载目录，会映射为下载器路径
        :param cookie: 传给下载器的站点 Cookie，rTorrent 客户端当前忽略此参数
        :param episodes: 目标集数集合；非空时新任务以暂停状态添加并执行文件选择，已有任务只追加
            这些集的文件；为空时新任务整包下载，已有任务确保全部文件已选中
        :param category: 下载器任务分类，当前下载器不使用
        :param label: 逗号分隔的任务标签；省略时使用系统种子标签
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param custom_words: 来源识别词，按行参与文件名识别，使选集结果与订阅一致
        :param episode_selection: 目标季与已确认的季集对应关系；选集失败清理本次新增任务并保留文件
        :param confirmed_selection: 下载链从下载历史读出的"MP 此前在同一种子上确认选中过的文件"（按下载器分开），
            只在同一 hash 已存在时使用；None 表示 MP 没有该 hash 的下载历史
        :param user_specified: 用户明确指定了这个种子（Web、接口、智能助手、消息交互），只在同一 hash 已存在时
            使用：重新选中之前被取消的目标，任务已暂停且目标未下完时恢复任务
        :return: 下载器名称、任务哈希、内容布局和结果说明；无适用实例时返回 None，
            添加或已有任务核验失败时前三项为空，说明包含失败原因；已有任务的说明为
            ``app.schemas.download`` 中约定的三个常量之一
        """

        if not content:
            return None, None, None, "下载内容为空"

        torrent_from_file, resolved_content = self._get_torrent_info(content)
        is_magnet = (
            isinstance(resolved_content, str)
            and resolved_content.startswith("magnet:")
            or isinstance(resolved_content, bytes)
            and resolved_content.startswith(b"magnet:")
        )
        if not torrent_from_file and not is_magnet:
            return None, None, None, "添加种子任务失败：无法读取种子文件"
        resolved_content = cast(Union[str, bytes], resolved_content)
        expected_hashes = torrent_rules.download_hashes(resolved_content)

        # 失败提示使用种子名称（磁力链接保留原文），不拼接种子文件的二进制内容
        torrent_desc = torrent_from_file.name if torrent_from_file else resolved_content
        torrent_desc_text = (
            torrent_desc.decode(errors="replace")
            if isinstance(torrent_desc, bytes)
            else str(torrent_desc)
        )

        server = self.get_instance(downloader)
        if not server:
            return None

        # rTorrent 的 load.raw/load.start 只把种子放进加载队列就返回成功，之后才在内部判重：同一 hash
        # 已存在时新种子被静默丢弃，RPC 不报错、也不会出现带临时标签的新任务。因此不能只靠"添加失败"
        # 发现已有任务，添加前先按 hash 查一次（也省掉随后最长约 27 秒的临时标签轮询）。代价是每次添加
        # 多一次全量任务列表查询（rTorrent 没有按 hash 过滤的批量接口），任务数上千时约几十到几百毫秒。
        existing, error = self._find_existing_task(server, expected_hashes)
        if error:
            # 预查只是优化：照常添加，添加后的临时标签轮询同样会按 hash 认出已有任务，只是要多等一会儿。
            logger.warning(f"添加前查询 rTorrent 任务列表失败，无法预先判断是否已有同一任务，继续添加：{torrent_desc_text}")
        if existing is not None:
            return self._finish_found_task(
                server, existing, episodes, custom_words, downloader, episode_selection, confirmed_selection,
                user_specified,
            )

        # 生成随机Tag
        tag = text_tools.random_string(10)
        if label:
            tags = label.split(",") + [tag]
        elif get_runtime_setting('TORRENT_TAG'):
            tags = [tag, get_runtime_setting('TORRENT_TAG')]
        else:
            tags = [tag]
        # 如果要选择文件则先暂停
        is_paused = True if episodes else False
        state = server.add_torrent(
            content=resolved_content,
            download_dir=self.normalize_path(download_dir, downloader),
            is_paused=is_paused,
            tags=tags,
            cookie=cookie,
        )

        # rTorrent 始终使用原始种子布局
        torrent_layout = "Original"

        if not state:
            existing, error = self._find_existing_task(server, expected_hashes)
            if error:
                return None, None, None, "无法连接rTorrent下载器"
            if existing is not None:
                return self._finish_found_task(
                    server, existing, episodes, custom_words, downloader, episode_selection, confirmed_selection,
                    user_specified,
                )
            return None, None, None, f"添加种子任务失败：{torrent_desc_text}"
        else:
            added_torrent_hash, existing = self._locate_added_task(server, tag, expected_hashes)
            if existing is not None:
                return self._finish_found_task(
                    server, existing, episodes, custom_words, downloader, episode_selection, confirmed_selection,
                    user_specified,
                )
            if not added_torrent_hash:
                return (
                    None,
                    None,
                    None,
                    f"下载任务添加成功，但获取rTorrent任务信息失败：{torrent_desc_text}",
                )
            else:
                torrent_hash = added_torrent_hash
                if episodes:
                    selected, message = self._select_episode_files(server, torrent_hash, episodes, custom_words, episode_selection)
                    if not selected:
                        return None, None, None, self._discard_failed_selection(server, torrent_hash, message)
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        message,
                    )
                else:
                    return (
                        downloader or self.get_default_config_name(),
                        torrent_hash,
                        torrent_layout,
                        "添加下载成功",
                    )

    @staticmethod
    def _find_existing_task(server: Rtorrent, expected_hashes: frozenset[str]) -> Tuple[Optional[TorrentData], bool]:
        """
        按种子内容计算出的 hash 查找下载器中已有的同一任务。

        :param server: 当前 rTorrent 客户端
        :param expected_hashes: 由种子内容计算出的小写 v1/v2 hash；无法计算时为空，直接视为不存在
        :return: 找到的任务（未找到为 None）与查询是否失败
        """
        if not expected_hashes:
            return None, False
        torrents, error = server.get_torrents()
        if error:
            return None, True
        try:
            return next(
                (torrent for torrent in torrents if str(torrent.get("hash") or "").lower() in expected_hashes), None,
            ), False
        finally:
            torrents.clear()

    def _locate_added_task(
            self, server: Rtorrent, tag: str, expected_hashes: frozenset[str],
    ) -> Tuple[Optional[str], Optional[TorrentData]]:
        """
        添加请求返回成功后定位任务，区分"本次新建"与"同一 hash 的已有任务"。

        先按临时标签轮询本次新建的任务；找不到时再按 hash 查一次：
        - 查到的任务带着本次临时标签：就是本次新建的，只是轮询时没读到（例如查询临时失败），按新建处理
          并移除临时标签；
        - 查到的任务不带本次临时标签：添加前的查询之后同一 hash 的任务刚被别处加入，本次种子已被判重
          丢弃，按已有任务处理；
        - 都没查到：无法确认，交由调用方按失败处理。

        :param server: 当前 rTorrent 客户端
        :param tag: 本次添加写入的随机临时标签
        :param expected_hashes: 由种子内容计算出的小写 v1/v2 hash
        :return: 本次新建任务的 hash 与同一 hash 的已有任务，至多一项非空
        """
        added_torrent_hash = server.get_torrent_id_by_tag(tags=tag)
        if added_torrent_hash:
            return added_torrent_hash, None
        existing, _error = self._find_existing_task(server, expected_hashes)
        if existing is None:
            return None, None
        torrent_hash = cast(str, existing.get("hash"))
        if tag in [str(item).strip() for item in str(existing.get("tags") or "").split(",")]:
            server.remove_torrents_tag(ids=torrent_hash, tag=[tag])
            return torrent_hash, None
        return None, existing

    def _finish_found_task(
            self, server: Rtorrent, torrent: TorrentData, episodes: Optional[Set[int]],
            custom_words: Optional[str], downloader: Optional[str],
            episode_selection: Optional[torrent_rules.EpisodeSelection],
            confirmed_selection: Optional[torrent_rules.ConfirmedSelection],
            user_specified: bool,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        记录日志后按已有任务流程核验找到的同 hash 任务，参数含义同 ``_finish_existing_task``。
        """
        torrent_hash = cast(str, torrent.get("hash"))
        logger.warn(f"下载器中已存在该种子任务：{torrent_hash} - {torrent.get('name')}")
        return self._finish_existing_task(
            server, torrent_hash, episodes, custom_words,
            downloader, episode_selection, confirmed_selection,
            user_specified=user_specified,
        )

    def _finish_existing_task(
            self, server: Rtorrent, torrent_hash: str, episodes: Optional[Set[int]],
            custom_words: Optional[str], downloader: Optional[str],
            episode_selection: Optional[torrent_rules.EpisodeSelection] = None,
            confirmed_selection: Optional[torrent_rules.ConfirmedSelection] = None,
            user_specified: bool = False,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        核验已有任务能否受理本次请求，受理后返回任务信息，规则见 ``_accept_existing_task``。

        拒绝时不改动任务（包括标签），并且不返回 hash，下载链会按失败处理并保留缺集。

        :param server: 当前 rTorrent 客户端
        :param torrent_hash: 已有任务的 hash
        :param episodes: 本次目标集号；为空表示整个种子
        :param custom_words: 来源识别词
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param episode_selection: 目标季与已确认的季集映射
        :param confirmed_selection: MP 此前在同一种子上确认选中过的文件（按下载器分开）；None 表示没有下载历史
        :param user_specified: 用户明确指定了这个种子，见 ``_accept_existing_task``
        :return: 下载器名称、任务哈希、内容布局和约定说明；失败时前三项为空
        """
        accepted, message = self._accept_existing_task(
            server, torrent_hash, episodes, custom_words, episode_selection, confirmed_selection, downloader,
            user_specified=user_specified,
        )
        if not accepted:
            return None, None, None, message
        # rTorrent 始终使用原始种子布局
        return downloader or self.get_default_config_name(), torrent_hash, "Original", message

    def _lookup_pending_task_hash(
            self, server: Rtorrent, expected_hashes: frozenset[str],
    ) -> Tuple[Optional[str], bool]:
        """
        按已知哈希集合严格查询 rTorrent 中是否已出现对应任务。
        """
        existing, error = self._find_existing_task(server, expected_hashes)
        if error:
            return None, True
        matched_hash = existing.get("hash") if existing is not None else None
        return (str(matched_hash) if matched_hash else None), False

    def _finish_pending_whole_download(
            self, server: Rtorrent, torrent_hash: str,
            downloader: Optional[str], state: torrent_rules.ExistingTaskState,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        完成无需选集的待确认 rTorrent 任务收尾。
        """
        return downloader or self.get_default_config_name(), torrent_hash, "Original", "添加下载成功"

    def _finish_pending_selected_download(
            self, server: Rtorrent, torrent_hash: str, episodes: Set[int],
            custom_words: Optional[str], downloader: Optional[str],
            episode_selection: Optional[torrent_rules.EpisodeSelection],
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        为延迟落地的 rTorrent 暂停新任务补做选集并启动。
        """
        selected, message = self._select_episode_files(
            server, torrent_hash, episodes, custom_words, episode_selection,
        )
        if not selected:
            return None, None, None, self._discard_failed_selection(server, torrent_hash, message)
        return downloader or self.get_default_config_name(), torrent_hash, "Original", message

    def _finish_pending_existing_task(
            self, server: Rtorrent, torrent_hash: str, episodes: Optional[Set[int]],
            custom_words: Optional[str], downloader: Optional[str],
            episode_selection: Optional[torrent_rules.EpisodeSelection],
            confirmed_selection: Optional[torrent_rules.ConfirmedSelection],
            user_specified: bool,
    ) -> Tuple[Optional[str], Optional[str], Optional[str], str]:
        """
        将待确认任务转交 rTorrent 已有任务核验流程。
        """
        return self._finish_existing_task(
            server, torrent_hash, episodes, custom_words, downloader,
            episode_selection, confirmed_selection, user_specified=user_specified,
        )

    def _existing_task_files(
            self, server: Rtorrent, torrent_hash: str,
    ) -> Optional[List[torrent_rules.ExistingTaskFile]]:
        """
        读取已有任务的文件选择与完成状态；优先级大于 0 视为已选中，进度达到 100 视为已完成。
        """
        torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return None
        return [
            torrent_rules.ExistingTaskFile(
                file_id=item["id"], name=item["name"],
                wanted=(item.get("priority") or 0) > 0,
                # 空文件的分块数为 0，客户端把进度记为 0；按已完成处理，避免被误判为仍待下载。
                completed=(item.get("progress") or 0) >= 100 or item.get("size") == 0,
            )
            for item in torrent_files
        ]

    def _enable_existing_files(self, server: Rtorrent, torrent_hash: str, file_ids: List[int]) -> bool:
        """
        把指定文件设为普通优先级；客户端写入后会调用 ``d.update_priorities`` 使其生效。
        """
        return server.set_files(torrent_hash=torrent_hash, file_ids=file_ids, priority=1)

    def _refresh_existing_priorities(self, server: Rtorrent, torrent_hash: str) -> bool:
        """
        目标仍有未完成文件时再次调用 ``d.update_priorities``。

        rTorrent 逐个文件写入优先级，最后才刷新；若上一次请求中途失败，重试时读到的优先级已是
        "选中"而不会再写入，只有在这里补一次刷新，已写入的优先级才会真正生效。
        """
        return server.update_priorities(torrent_hash)

    def _resume_existing_task(self, server: Rtorrent, torrent_hash: str) -> bool:
        """
        恢复已暂停的任务并回读确认运行状态。

        ``d.start`` 只把任务放进 started 视图，能启动已停止（state=0）的任务；被 ``d.pause`` 暂停的任务
        （state=1、不活动）本来就在 started 视图里，还需要 ``d.resume``。rTorrent 的命令在回复前已执行，
        回读状态可靠，因此按运行状态（含校验中）确认。
        """
        if not server.start_torrents(torrent_hash) or not server.resume_torrents(torrent_hash):
            return False
        return self._existing_task_state(server, torrent_hash) is torrent_rules.ExistingTaskState.RUNNING

    def _existing_task_state(
            self, server: Rtorrent, torrent_hash: str,
    ) -> torrent_rules.ExistingTaskState:
        """
        按 rTorrent 的 d.state、d.is_active 与 d.is_hash_checking 归一。

        - state=0：已停止（d.stop），暂停；
        - state=1 且 is_active=1：运行；
        - state=1 且 is_active=0：要么正在校验数据（启动后、重启恢复会话或手动重新校验时，校验完成前
          任务不活动，之后会自动开始），按运行处理；要么是被 d.pause 暂停（ruTorrent 的"暂停"），按暂停处理。
          rTorrent 本身没有任务排队；第三方排队脚本若用 d.pause/d.stop 让任务等待，与手动暂停无法区分，
          一律按暂停处理（自动流程不恢复，用户手动指定种子时恢复）；
        - 读不到校验状态时返回未知，不猜。
        rTorrent 没有可靠的任务错误状态，不归为错误。
        """
        torrents, error = server.get_torrents(ids=torrent_hash)
        if error or not torrents:
            return torrent_rules.ExistingTaskState.UNKNOWN
        torrent = torrents[0]
        if str(torrent.get("state")) == "0":
            return torrent_rules.ExistingTaskState.PAUSED
        if str(torrent.get("is_active")) != "0":
            return torrent_rules.ExistingTaskState.RUNNING
        checking = server.is_hash_checking(torrent.get("hash") or torrent_hash)
        if checking is None:
            return torrent_rules.ExistingTaskState.UNKNOWN
        return torrent_rules.ExistingTaskState.RUNNING if checking else torrent_rules.ExistingTaskState.PAUSED

    def _task_tags(self, server: Rtorrent, torrent_hash: str) -> Optional[List[str]]:
        """
        从任务列表读取 d.custom1 中的标签；不用 ``get_torrent_tags``，它在查询失败时返回空列表。
        """
        torrents, error = server.get_torrents(ids=torrent_hash)
        if error or not torrents:
            return None
        return [str(t).strip() for t in str(torrents[0].get("tags") or "").split(",") if str(t).strip()]

    def _add_task_tags(self, server: Rtorrent, torrent_hash: str, tags: List[str], current: List[str]) -> bool:
        """
        客户端在一次请求内读取并合并 d.custom1，不需要使用当前标签。
        """
        return server.set_torrents_tag(ids=torrent_hash, tags=tags)

    def _remove_task_tags(self, server: Rtorrent, torrent_hash: str, tags: List[str], current: List[str]) -> bool:
        """
        客户端在一次请求内读取并改写 d.custom1，不需要使用当前标签。
        """
        return server.remove_torrents_tag(ids=torrent_hash, tag=tags)

    @staticmethod
    def _select_episode_files(
        server: Rtorrent, torrent_hash: str, episodes: Set[int], custom_words: Optional[str] = None,
        episode_selection: Optional[torrent_rules.EpisodeSelection] = None,
    ) -> Tuple[bool, str]:
        """
        对暂停添加的任务完成选集，所有RPC均成功才允许下载链确认任务。

        :param server: 当前下载器客户端
        :param torrent_hash: 已暂停添加的任务Hash
        :param episodes: 本次需要完整覆盖的目标集数，多集文件按交集选择
        :param custom_words: 来源识别词，按行参与物理文件名识别
        :param episode_selection: 目标季与已确认的季集对应关系，跨季冲突不参与唯一视频兜底
        :return: 是否确认选集启动成功及结果说明
        """
        torrent_files = server.get_files(torrent_hash)
        if not torrent_files:
            return False, "获取种子文件失败，下载任务保留暂停状态，请检查下载器"
        media_extensions = {str(extension).lower() for extension in get_runtime_setting("RMT_MEDIAEXT", [])}
        wanted, unwanted, selected_episodes = torrent_rules.select_torrent_files(
            ((item["id"], item["name"]) for item in torrent_files),
            episodes, media_extensions, custom_words, episode_selection,
        )
        if not episodes or not episodes.issubset(selected_episodes):
            return False, "未匹配到目标集数，请检查季集映射，下载任务保留暂停状态"
        if unwanted and not server.set_files(torrent_hash=torrent_hash, file_ids=unwanted, priority=0):
            return False, "设置选集优先级失败，下载任务保留暂停状态"
        if not server.set_files(torrent_hash=torrent_hash, file_ids=wanted, priority=1):
            return False, "设置选集优先级失败，下载任务保留暂停状态"
        if not server.start_torrents(torrent_hash):
            stopped = server.stop_torrents(torrent_hash)
            message = "启动下载失败，已请求暂停，请检查下载器确认状态" if stopped else "启动下载状态未知，请检查下载器"
            return False, message
        return True, f"添加下载成功，已选择集数：{sorted(selected_episodes)}"

    def list_torrents(
        self,
        status: Optional[TorrentStatus] = None,
        hashs: Optional[Union[list[str], str]] = None,
        downloader: Optional[str] = None,
        include_all_tags: bool = False,
    ) -> Optional[List[DownloaderTorrent]]:
        """
        将下载器任务转换为统一列表，并按状态和标签筛选。

        :param status: 任务查询状态；空值或未知值归一为查询全部
        :param hashs: 指定任务哈希或列表；提供时优先于状态筛选
        :param downloader: 下载器配置名称；省略时汇总本类型的所有实例
        :param include_all_tags: 是否包含未设置系统种子标签的任务，默认只查询系统标签范围
        :return: 匹配任务列表；指定实例不可用时返回 None，查询失败的实例不贡献结果
        """
        servers: Dict[Optional[str], Rtorrent]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = {downloader: server}
        else:
            servers = self.get_instances()
        ret_torrents = []
        query_status = self._normalize_query_status(status)
        query_tags = None if include_all_tags else get_runtime_setting('TORRENT_TAG')

        def __get_torrent_path(torrent_data: TorrentData) -> Path:
            """
            获取种子内容路径。
            """
            content_path = torrent_data.get("content_path")
            if content_path:
                return Path(content_path)
            return Path(torrent_data["save_path"]) / torrent_data["name"]

        def __build_torrent(downloader_name: Optional[str], torrent_data: TorrentData) -> DownloaderTorrent:
            """
            构造统一下载器任务对象。
            """
            meta = MetaInfo(torrent_data["name"])
            dlspeed = torrent_data.get("dlspeed") or 0
            upspeed = torrent_data.get("upspeed") or 0
            total_size = torrent_data.get("total_size") or 0
            completed_size = torrent_data.get("completed") or 0
            torrent_path = __get_torrent_path(torrent_data)
            return DownloaderTorrent(
                downloader=downloader_name,
                hash=torrent_data.get("hash"),
                title=torrent_data.get("name"),
                name=meta.name,
                year=str(meta.year) if meta.year is not None else None,
                season_episode=meta.season_episode,
                path=Path(self.normalize_return_path(torrent_path, downloader_name)),
                save_path=self.normalize_return_path(
                    Path(torrent_data.get("save_path") or ""), downloader_name
                ) if torrent_data.get("save_path") else None,
                content_path=self.normalize_return_path(torrent_path, downloader_name),
                progress=torrent_data.get("progress", 0),
                selected_completed=str(torrent_data.get("complete")) == "1",
                size=total_size,
                # complete 表示选中部分已下完（见 Rtorrent.get_torrents），此时已完成字节数近似为选中下载的数据量，
                # 未选中文件里恰好已完成的分块也计入。只用于两处，近似不影响结论：
                # - 整理链判断"整理期间选中范围是否变化"：之后新选中的文件只要还缺分块，下完它就会让该值变大；
                #   数据本来就齐的文件在扫描时已是完整文件，不在未完成列表里，本轮已经整理；
                # - 逐文件准入短路：达到总大小意味着全部分块已完成，所有文件都完整，无需逐文件确认。
                selected_size=completed_size if str(torrent_data.get("complete")) == "1" else None,
                state=self.__normalize_torrent_state(
                    torrent_data.get("state"), torrent_data.get("complete")
                ),
                dlspeed=size_tools.format_compact_size(dlspeed),
                upspeed=size_tools.format_compact_size(upspeed),
                tags=torrent_data.get("tags"),
                left_time=time_tools.format_duration((total_size - completed_size) / dlspeed)
                if dlspeed > 0
                else "",
            )

        if hashs:
            for name, server in servers.items():
                torrents, _ = (
                    server.get_torrents(ids=hashs, tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.TRANSFER:
            # 获取已完成且未整理的
            for name, server in servers.items():
                torrents = (
                    server.get_completed_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        tags = torrent_info.get("tags") or ""
                        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
                        if "已整理" in tag_list:
                            continue
                        ret_torrents.extend(self._admit_transfer_torrent(
                            server, __build_torrent(name, torrent_info),
                            save_path=torrent_info.get("save_path"), content_path=__get_torrent_path(torrent_info),
                        ))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status == TorrentQueryStatus.DOWNLOADING:
            for name, server in servers.items():
                torrents = (
                    server.get_downloading_torrents(tags=query_tags) or []
                )
                try:
                    for torrent_info in torrents:
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        elif query_status in (
                TorrentQueryStatus.ALL,
                TorrentQueryStatus.COMPLETED,
                TorrentQueryStatus.PAUSED,
        ):
            # 获取完整任务列表，由 MoviePilot 统一归一实际下载器状态。
            for name, server in servers.items():
                torrents, _ = server.get_torrents(tags=query_tags) or []
                try:
                    for torrent_info in torrents:
                        torrent_state = self.__normalize_torrent_state(
                            torrent_info.get("state"), torrent_info.get("complete")
                        )
                        if (
                                query_status != TorrentQueryStatus.ALL
                                and torrent_state != query_status.value
                        ):
                            continue
                        ret_torrents.append(__build_torrent(name, torrent_info))
                finally:
                    torrents.clear()
                    del torrents
        else:
            return None
        # 这里返回统一模型列表，保留既有的下载器接口兼容检查豁免。
        return ret_torrents  # noqa

    @staticmethod
    def __normalize_torrent_state(
            state: Optional[Union[int, float, str]],
            complete: Optional[Union[int, float, str]],
    ) -> str:
        """
        归一 rTorrent 原始任务状态。
        """
        if str(state) == "0":
            return DownloadTaskState.PAUSED.value
        if str(complete) == "0":
            return DownloadTaskState.DOWNLOADING.value
        return DownloadTaskState.COMPLETED.value

    def transfer_completed(
        self, hashs: Union[str, list[str]], downloader: Optional[str] = None
    ) -> None:
        """
        为整理完成的种子添加“已整理”标签。

        找不到适用实例时直接返回；标签写入失败不向调用方返回确认。写入后若发现期间有新文件被
        追加（见 ``_has_pending_wanted_files``），撤回标签，让追加的文件下载完成后重新进入整理。

        :param hashs: 已完成整理的种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        for torrent_hash in [hashs] if isinstance(hashs, str) else hashs:
            # 合并模式在同一请求内读取并改写 d.custom1，读取失败时整体放弃写入。旧实现先用
            # get_torrent_tags 读取（失败返回空列表）再覆盖写，会清掉任务的其他标签。
            if not server.set_torrents_tag(ids=torrent_hash, tags=[TRANSFERRED_TAG]):
                continue
            if self._has_pending_wanted_files(server, torrent_hash):
                logger.info(f"种子 {torrent_hash} 在整理期间追加了新文件，撤回已整理标签")
                server.remove_torrents_tag(ids=torrent_hash, tag=[TRANSFERRED_TAG])
        return None

    def remove_torrents(
        self,
        hashs: Union[str, list[str]],
        delete_file: Optional[bool] = True,
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        删除指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param delete_file: 是否同时删除下载文件，默认删除
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.delete_torrents(
            delete_file=delete_file if delete_file is not None else True,
            ids=hashs,
            path_mapper=lambda path: self.normalize_return_path(path, downloader),
        )

    def set_torrents_tag(
        self, hashs: Union[str, list[str]], tags: list[str],
        downloader: Optional[str] = None,
    ) -> Optional[bool]:
        """
        为指定下载任务追加标签。

        :param hashs: 种子哈希或哈希列表
        :param tags: 需要追加的标签列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.set_torrents_tag(ids=hashs, tags=tags)

    def update_torrent(
            self,
            hash_string: str,
            downloader: Optional[str] = None,
            download_limit: Optional[float] = None,
            upload_limit: Optional[float] = None,
            tracker_list: Optional[list[str]] = None,
            save_path: Optional[str] = None,
            category: Optional[str] = None,
            ratio_limit: Optional[float] = None,
            seeding_time_limit: Optional[int] = None,
    ) -> Optional[Dict[str, bool]]:
        """
        修改下载任务属性。

        :param hash_string: 种子Hash
        :param downloader: 下载器配置名称，省略时使用默认实例
        :param download_limit: 下载限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param upload_limit: 上传限速，单位 KB/s；0 表示不限速，None 表示不修改
        :param tracker_list: Tracker URL列表，rTorrent 当前封装不支持
        :param save_path: 保存目录
        :param category: 分类，rTorrent 当前封装不支持
        :param ratio_limit: 分享率限制，rTorrent 当前封装不支持
        :param seeding_time_limit: 做种时间限制，rTorrent 当前封装不支持
        :return: 各项请求的执行结果；未提供的项不写入结果，找不到适用实例时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        results = {}
        if download_limit is not None or upload_limit is not None:
            results["limits"] = server.change_torrent(
                hash_string=hash_string,
                download_limit=download_limit,
                upload_limit=upload_limit,
            )
        if ratio_limit is not None or seeding_time_limit is not None:
            results["seeding_limits"] = False
        if tracker_list is not None:
            results["trackers"] = False
        if save_path is not None:
            results["save_path"] = server.set_torrent_location(
                hash_string=hash_string,
                location=self.normalize_path(Path(save_path), downloader),
            )
        if category is not None:
            results["category"] = False
        return results

    def start_torrents(
        self, hashs: Union[list[str], str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        启动指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.start_torrents(ids=hashs)

    def stop_torrents(
        self, hashs: Union[list[str], str], downloader: Optional[str] = None
    ) -> Optional[bool]:
        """
        暂停指定下载任务。

        :param hashs: 种子哈希或哈希列表
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 客户端报告的执行结果；找不到适用的下载器时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return server.stop_torrents(ids=hashs)

    def torrent_files(
        self, tid: str, downloader: Optional[str] = None
    ) -> Optional[List[DownloaderFile]]:
        """
        将下载器文件列表转换为统一的文件模型。

        :param tid: 种子哈希
        :param downloader: 下载器配置名称，省略时使用默认实例
        :return: 统一文件列表；找不到适用实例或客户端没有返回文件清单时返回 None
        """
        server = self.get_instance(downloader)
        if not server:
            return None
        return self._normalize_torrent_files(
            server.get_files(tid=tid), DownloaderFile.model_validate
        )

    def downloader_info(
        self, downloader: Optional[str] = None
    ) -> Optional[List[_SchemaDownloaderInfo]]:
        """
        汇总下载器的上下行速度和累计传输量。

        :param downloader: 下载器配置名称；省略时查询本类型的所有实例
        :return: 各可用实例的统计列表，速度单位为字节/秒、传输量为字节；
            指定实例不可用时返回 None，统计查询失败的实例被跳过
        """
        servers: list[Rtorrent]
        if downloader:
            server = self.get_instance(downloader)
            if not server:
                return None
            servers = [server]
        else:
            servers = list(self.get_instances().values())
        ret_info = []
        for server in servers:
            info = server.transfer_info()
            if not info:
                continue
            download_speed = info.get("dl_info_speed")
            upload_speed = info.get("up_info_speed")
            download_size = info.get("dl_info_data")
            upload_size = info.get("up_info_data")
            ret_info.append(
                _SchemaDownloaderInfo(
                    download_speed=float(download_speed) if isinstance(download_speed, (int, float)) else None,
                    upload_speed=float(upload_speed) if isinstance(upload_speed, (int, float)) else None,
                    download_size=float(download_size) if isinstance(download_size, (int, float)) else None,
                    upload_size=float(upload_size) if isinstance(upload_size, (int, float)) else None,
                )
            )
        return ret_info
