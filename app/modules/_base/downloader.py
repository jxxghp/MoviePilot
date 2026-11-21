"""
下载器模块业务样板基类。

沉淀三个内置下载器模块（qbittorrent/transmission/rtorrent）逐字复制的样板：
连接测试、定时重连、种子信息读取与查询状态归一，以及同一 hash 已存在时的统一核验流程
（``_accept_existing_task``）。差异化逻辑（任务添加、原始状态映射、任务列表构建，以及核验流程
依赖的文件、状态、标签读写接口）仍留在各模块。
"""
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any, List, Optional, Protocol, Set, Tuple, TypeVar, Union, cast

from torrentool.torrent import Torrent

from app.domain import torrent as torrent_rules
from app.modules import _DownloaderBase, _ModuleBase
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.schemas.download import (
    DOWNLOAD_TASK_ALREADY_EXISTS,
    DOWNLOAD_TASK_FILES_APPENDED,
    DOWNLOAD_TASK_RESUMED,
    DownloadTaskAction,
    ExistingTaskRejection,
)
from app.schemas.transfer import DownloaderTorrent
from app.schemas.types import TorrentQueryStatus, TorrentStatus

# 整理完成后写入下载器的标签；带此标签的任务不再进入下载器整理轮询。
TRANSFERRED_TAG = "已整理"
# 受理已有任务时，下载器动作到 download() 约定说明的映射；新建任务不经过这里。
_EXISTING_TASK_MESSAGES = {
    DownloadTaskAction.APPENDED: DOWNLOAD_TASK_FILES_APPENDED,
    DownloadTaskAction.RESUMED: DOWNLOAD_TASK_RESUMED,
    DownloadTaskAction.REUSED: DOWNLOAD_TASK_ALREADY_EXISTS,
}


class _DownloaderClient(Protocol):
    """
    下载器连接巡检依赖的最小同步客户端接口。
    """

    def is_inactive(self) -> bool:
        """
        判断已配置的客户端是否需要重新建立连接。

        :return: 连接未就绪时返回 True，否则返回 False
        """
        ...

    def reconnect(self) -> None:
        """
        按下载器的登录策略刷新连接。

        实现方负责替换连接状态；调用结束不代表连接一定成功，需继续检查服务状态。
        """
        ...

    def transfer_info(self) -> object:
        """
        读取下载器的原始传输状态。

        :return: 下载器状态对象；不可用时返回空值，巡检调用方仅检查其真假值
        """
        ...

    def delete_torrents(self, delete_file: bool, ids: str) -> bool:
        """
        请求移除指定任务，选集失败清理必须保留已存在的文件。

        :param delete_file: 是否删除文件；选集清理始终传 False
        :param ids: 本次新增任务的哈希，不能传入已有任务
        :return: 下载器是否接受移除请求；False 时由上层通知人工核查
        """
        ...


DownloaderServiceT = TypeVar("DownloaderServiceT", bound=_DownloaderClient)
TFile = TypeVar("TFile")


class _DownloaderModuleBase(_ModuleBase, _DownloaderBase[DownloaderServiceT]):
    """
    下载器模块业务样板基类。
    """

    @staticmethod
    def _discard_failed_selection(server: _DownloaderClient, torrent_hash: str, message: str) -> str:
        """
        清理本次新增但选集失败的任务，保留文件并交回下载链通知、退避和换源。

        只由新增任务分支调用；已有任务不暂停、不重选也不删除。
        """
        if server.delete_torrents(delete_file=False, ids=torrent_hash):
            return f"{message}；已请求移除本次新增任务（保留文件），等待后续重试或换源"
        return f"{message}；移除本次新增任务失败，请在下载器核实（保留文件）"

    def _accept_existing_task(
            self, server: DownloaderServiceT, torrent_hash: str,
            episodes: Optional[Set[int]], custom_words: Optional[str],
            episode_selection: Optional[torrent_rules.EpisodeSelection],
            confirmed_selection: Optional[torrent_rules.ConfirmedSelection],
            downloader: Optional[str],
            user_specified: bool = False,
    ) -> Tuple[bool, str]:
        """
        同一 hash 已在下载器中时，核验已有任务能否受理本次请求，能受理就做最小改动，只有确认受理才算成功。

        历史背景（修改前务必理解）：
        - 4955b7b9e 曾对已有任务"暂停 + 重新选集"，会把正在下载或做种的整包改成只下几集（#7362）；
        - #7405 改为已有任务直接返回"任务已存在"，但下载链仍按成功结算，导致同一季包先下 E01、
          再要 E02 时 E02 根本没被选中，订阅却记成已下载。

        现在的原则是"MoviePilot 不推翻用户、插件或外部对已有任务的决定"，具体规则见
        ``app.domain.torrent.decide_existing_task``；这里只负责读取实时状态、执行决策并回读确认：
        - 绝不暂停任务、绝不把任何文件改为不下载、绝不删除已有任务，也不修改保存目录；
        - 只把目标中尚未选中、也不是被别人取消的文件改为下载；暂停的任务目标没下完就拒绝，只有用户手动
          指定了这个种子时才追加并恢复任务（用户这次的明确要求比之前的暂停更新）；
        - 受理前重新读取文件列表，确认本次需要的全部目标文件都处于选中状态：追加时不只核对新追加的
          那几个，原样复用时也要核对（读取快照之后，别人可能在下载器里取消了其中之前已选中的文件）。
          任何一步无法确认都返回失败，不能让下载链把未受理的集记成已下载；
        - 受理后才移除"已整理"标签、补写系统种子标签：先移除的话，旧整理回调可能赶在追加生效前按"已完成"
          重新打回标签，追加的文件下载完成后就再也不会进入下载器整理。两者都回读确认，见
          ``_refresh_accepted_task_tags``；
        - 调用方（下载链）在整个 download() 期间持有 ``app.runtime.locks.downloader_task_locks``，
          整理链"判断完成 + 写已整理标签"与移动模式"检查 + 删除任务"、``ChainBase`` 的暂停/开始与改标签
          持有同一把锁，按 hash 互斥。

        :param server: 当前下载器客户端
        :param torrent_hash: 下载器中已有任务的 hash
        :param episodes: 本次目标集号；None 或空集合表示请求下载整个种子
        :param custom_words: 来源识别词，与新任务选集使用同一份
        :param episode_selection: 目标季与已确认的季集映射，与新任务选集使用同一份
        :param confirmed_selection: 下载链从下载历史读出的"MP 此前在同一种子上确认选中过的文件"（按下载器分开）；
            None 表示 MP 没有该 hash 的下载历史（外部添加的任务）
        :param downloader: 下载器配置名称，省略时为默认实例；用于取出这台下载器上的确认记录
        :param user_specified: 用户明确指定了这个种子：重新选中被取消的目标，暂停且目标未下完时恢复任务
        :return: 是否确认受理及说明；受理时说明为 ``app.schemas.download`` 中约定的三个常量之一，
            拒绝时以 ``ExistingTaskRejection`` 的某个原因开头
        """
        snapshot = torrent_rules.ExistingTaskSnapshot(
            files=tuple(self._existing_task_files(server, torrent_hash) or ()),
            state=self._existing_task_state(server, torrent_hash),
        )
        media_extensions = [str(extension).lower() for extension in get_runtime_setting("RMT_MEDIAEXT", [])]
        confirmed = None if confirmed_selection is None else confirmed_selection.files_for(
            downloader or self.get_default_config_name() or "",
        )
        decision = torrent_rules.decide_existing_task(
            snapshot, episodes=episodes, confirmed=confirmed, media_extensions=media_extensions,
            custom_words=custom_words, episode_selection=episode_selection, user_specified=user_specified,
        )
        if decision.rejection is not None:
            return False, decision.rejection.describe(torrent_hash, decision.detail)
        failure = self._apply_existing_task_decision(server, torrent_hash, decision)
        if failure is None:
            failure = self._refresh_accepted_task_tags(server, torrent_hash)
        if failure is not None:
            return False, ExistingTaskRejection.NOT_CONFIRMED.describe(torrent_hash, failure)
        return True, _EXISTING_TASK_MESSAGES[cast(DownloadTaskAction, decision.action)]

    def _apply_existing_task_decision(
            self, server: DownloaderServiceT, torrent_hash: str, decision: torrent_rules.ExistingTaskDecision,
    ) -> Optional[str]:
        """
        按受理决策追加选择文件、刷新优先级，需要时恢复任务，每一步都确认。

        先追加再恢复：恢复后才追加的话，任务会先按旧的选择跑一会儿，追加失败时还要再决定是否暂停回去。
        失败时不回滚已写入的文件，也不把已恢复的任务暂停回去：下一次请求会重新读取实时状态，只补做仍未完成的部分。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :param decision: 受理决策
        :return: 全部确认时返回 None，否则返回失败细节
        """
        if not self._confirm_required_files_selected(server, torrent_hash, decision.enable, decision.required):
            if decision.enable:
                return "追加选择目标文件未确认，请在下载器核实，未记录成功"
            return "目标文件未确认处于选中状态（可能刚在下载器中被取消选择），未记录成功"
        if decision.pending and not self._refresh_existing_priorities(server, torrent_hash):
            return "文件优先级刷新未确认，请在下载器核实，未记录成功"
        if decision.resume and not self._resume_existing_task(server, torrent_hash):
            return "按手动指定恢复已暂停的任务未确认，请在下载器核实，未记录成功"
        return None

    def _confirm_required_files_selected(
            self, server: DownloaderServiceT, torrent_hash: str, file_ids: Tuple[int, ...],
            required: Tuple[int, ...],
    ) -> bool:
        """
        按需追加选择文件，然后重新读取文件列表，确认本次需要的全部目标文件都处于选中状态。

        下载器接口返回成功只代表请求已发出（rTorrent 还会逐个文件写入），必须回读实际状态。回读时核对的是
        全部目标文件，而不只是本次追加的；没有追加（原样复用）时同样回读一次：决策读取快照之后，别人可能在
        下载器里取消了其中之前已选中的文件，不核对就会把那一集记成已下载。代价是每次受理多一次文件列表查询。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :param file_ids: 需要追加选择的文件索引；为空表示不需要写入
        :param required: 受理后必须处于选中状态的全部目标文件索引，包含 ``file_ids``
        :return: 写入请求（如有）完成且回读确认全部目标文件已选中时返回 True
        """
        if file_ids and not self._enable_existing_files(server, torrent_hash, list(file_ids)):
            return False
        selected = {item.file_id for item in self._existing_task_files(server, torrent_hash) or [] if item.wanted}
        return set(file_ids).union(required).issubset(selected)

    def _refresh_accepted_task_tags(self, server: DownloaderServiceT, torrent_hash: str) -> Optional[str]:
        """
        受理已有任务后刷新标签：移除"已整理"，并补上系统种子标签，两者都回读确认。

        两个标签都决定追加的文件下载完成后能否进入下载器整理，因此都是受理的一部分，任一未确认本次受理就失败
        （已追加的文件不回滚，下次请求会重新读取标签再试）：
        - "已整理"：整理轮询跳过带这个标签的任务；
        - 系统种子标签（``TORRENT_TAG``，可以是逗号分隔的多个标签）：整理轮询默认只扫描全部带上这些标签的任务，
          外部添加的任务没有它们，补写失败的话，追加的集下载完成后不会被自动整理，订阅却已经记成下载中；只补缺少的，
          回读确认全部都在；
        - 改动前重新读取标签，不用受理前读到的：期间整理回调可能已经写入了"已整理"；
        - 改动后再读一次确认：下载器接口返回成功只代表请求已发出，不代表标签真的改好了；
        - 任一次读取失败都按未确认处理，也不在读不到时写标签（Transmission 只能整体覆盖标签，
          把读取失败当成"没有标签"写回会清掉 PT 站等其他标签）。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :return: 全部确认时返回 None，否则返回失败细节
        """
        current = self._task_tags(server, torrent_hash)
        if current is None:
            return '已受理本次目标，但读取任务标签失败，无法确认"已整理"与系统种子标签，追加的文件下载完成后可能不会自动整理，请在下载器核实'
        if TRANSFERRED_TAG in current:
            if not self._remove_task_tags(server, torrent_hash, [TRANSFERRED_TAG], current):
                return '已受理本次目标，但移除"已整理"标签失败，追加的文件下载完成后可能不会自动整理，请在下载器核实'
            current = self._task_tags(server, torrent_hash)
            if current is None or TRANSFERRED_TAG in current:
                return '已受理本次目标，但移除"已整理"标签未确认，追加的文件下载完成后可能不会自动整理，请在下载器核实'
        # 与整理轮询同一口径：TORRENT_TAG 可配置多个逗号分隔的标签，任务必须全部带上才会被扫描。
        required = [tag.strip() for tag in str(get_runtime_setting('TORRENT_TAG') or "").split(",") if tag.strip()]
        missing = [tag for tag in required if tag not in current]
        if not missing:
            return None
        logger.info(f"给种子 {torrent_hash} 打上标签：{'、'.join(missing)}")
        if self._add_task_tags(server, torrent_hash, missing, current):
            refreshed = self._task_tags(server, torrent_hash)
            if refreshed is not None and set(required).issubset(refreshed):
                return None
        return (
            f"已受理本次目标，但补写系统种子标签 {'、'.join(missing)} 未确认，下载器整理只扫描带全部系统种子标签的任务，"
            "追加的文件下载完成后可能不会自动整理，请在下载器核实"
        )

    def _has_pending_wanted_files(self, server: DownloaderServiceT, torrent_hash: str) -> bool:
        """
        打上"已整理"标签后复核任务：仍有已选中但未下载完成的文件，说明期间被追加了新文件。

        整理回调先确认种子已下载完成、再写入标签，这两步之间若有新的下载请求在同一任务上追加了
        文件（同一季包分多次下载不同集），标签会把追加的文件挡在下载器整理之外。MP 进程内的追加与
        整理回调由 ``app.runtime.locks.downloader_task_locks`` 串行化；这里的复核是兜底，覆盖锁管不到的
        情况（例如用户在下载器里手动勾选了文件）。追加后又在整理期间就下载完成的情况，由整理链在
        打标签前比对选中大小处理（见整理链 ``_TransferChain__selection_changed_since_scan``）。
        读取失败时无法确认，按"有待下载"处理撤回标签：撤回只会让下一轮整理重新检查该任务，
        已整理过的文件会被跳过，代价远小于把新文件永久挡在整理之外。

        :param server: 当前下载器客户端
        :param torrent_hash: 刚写入"已整理"标签的任务 hash
        :return: 存在已选中但未完成的文件，或无法读取文件状态时返回 True，调用方应撤回标签
        """
        files = self._existing_task_files(server, torrent_hash)
        if not files:
            return True
        return any(item.wanted and not item.completed for item in files)

    def _admit_transfer_torrent(
            self,
            server: DownloaderServiceT,
            torrent: DownloaderTorrent,
            *,
            save_path: Optional[Union[str, Path]],
            content_path: Optional[Union[str, Path]],
    ) -> List[DownloaderTorrent]:
        """
        下载器整理轮询的逐文件准入：只选中了部分文件的已完成任务，附上下载器报告尚未下载完成的文件。

        "已完成"指选中的文件已下完，但整理链会递归扫描整个任务目录：取消选择前已下了一部分的文件，以及与选中文件
        共用分块、被 rTorrent / Transmission 写入了部分数据的相邻文件，都留在磁盘上，不过滤就会被当成正常剧集整理入库。
        选中大小小于总大小才需要逐文件确认（多一次文件列表查询）。rTorrent 的选中大小是已完成字节数的近似值（含未选中
        文件里已完成的分块），达到总大小意味着全部分块都已完成，所有文件都是完整的，同样无需确认。
        读不到文件状态、或文件路径对应不到任务内容路径时本轮不交给整理，下一轮再试，宁可晚整理，也不整理残缺文件。

        :param server: 当前下载器客户端
        :param torrent: 整理轮询查到的已完成任务
        :param save_path: 下载器返回的原始保存目录（未经路径映射）
        :param content_path: 下载器返回的原始任务内容路径（未经路径映射），即映射后作为整理根目录的那个路径
        :return: 需要交给整理的任务（至多一个）；读不到文件状态时为空列表
        """
        selected, total = torrent.selected_size, torrent.size
        if selected is None or not total or selected >= total or not torrent.hash:
            return [torrent]
        files = self._existing_task_files(server, torrent.hash)
        if not files:
            logger.warning(f"读取种子 {torrent.hash} 的文件状态失败，无法排除未下载完成的文件，本轮暂不整理")
            return []
        names = [item.name for item in files if not item.completed]
        incomplete = self._content_relative_paths(names, save_path, content_path)
        if incomplete is None:
            logger.warning(
                f"种子 {torrent.hash} 的文件路径对应不到任务内容路径（下载器保存目录 {save_path}，内容路径 "
                f"{content_path}），无法排除未下载完成的文件，本轮暂不整理"
            )
            return []
        torrent.incomplete_files = incomplete
        return [torrent]

    @staticmethod
    def _content_relative_paths(
            names: List[str], save_path: Optional[Union[str, Path]], content_path: Optional[Union[str, Path]],
    ) -> Optional[List[str]]:
        """
        把下载器文件列表中的文件名换算为相对任务内容路径（映射后即整理根目录）的路径。

        三个下载器的文件名都相对保存目录：qBittorrent、Transmission 的多文件任务带顶层目录名；rTorrent 多文件任务的
        保存目录（``d.directory``）本身就是任务目录，文件名不带顶层目录。按"保存目录 + 文件名"拼出完整路径再求相对
        内容路径，各自的命名差异都能对上，整理链据此逐级精确比较，不靠文件名后缀猜测。
        必须用下载器返回的原始路径计算：路径映射可以分别作用于保存目录和内容路径（例如只把季包目录映射到远程存储），
        映射后的两者不一定还有共同前缀；相对路径与映射无关，整理链把它接到映射后的整理根目录上即可。

        :param names: 下载器报告的文件名
        :param save_path: 下载器返回的原始保存目录
        :param content_path: 下载器返回的原始任务内容路径
        :return: 相对内容路径的路径（内容路径本身记为 "."）；缺少路径或文件不在内容路径之下时返回 None
        """
        if not names:
            return []
        if not save_path or not content_path:
            return None
        root = PurePosixPath(str(content_path).replace("\\", "/"))
        base = PurePosixPath(str(save_path).replace("\\", "/"))
        relative: List[str] = []
        for name in names:
            try:
                relative.append((base / str(name).replace("\\", "/").lstrip("/")).relative_to(root).as_posix())
            except ValueError:
                return None
        return relative

    def _existing_task_files(
            self, server: DownloaderServiceT, torrent_hash: str,
    ) -> Optional[List[torrent_rules.ExistingTaskFile]]:
        """
        读取已有任务的实时文件状态，由各下载器模块实现。

        只读一次，不为等待元数据而启动或暂停任务（#7348 的取元数据流程只用于新任务）。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :return: 文件状态列表；读取失败或尚无元数据时返回 None 或空列表
        """
        raise NotImplementedError

    def _enable_existing_files(self, server: DownloaderServiceT, torrent_hash: str, file_ids: List[int]) -> bool:
        """
        把指定文件设为需要下载，由各下载器模块实现；不得改动其他文件。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :param file_ids: 需要追加选择的非空文件索引
        :return: 请求完成时返回 True，不代表状态已生效，调用方会回读确认
        """
        raise NotImplementedError

    def _refresh_existing_priorities(self, server: DownloaderServiceT, torrent_hash: str) -> bool:
        """
        让已写入的文件优先级生效；默认无需额外动作。

        rTorrent 写入文件优先级后必须调用 ``d.update_priorities`` 才会生效，且中途失败的写入
        在重试时读回来已是"选中"，因此只要目标仍有未完成文件就需要再刷新一次，由其模块覆盖。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :return: 刷新请求完成或无需刷新时返回 True
        """
        return True

    def _resume_existing_task(self, server: DownloaderServiceT, torrent_hash: str) -> bool:
        """
        恢复已暂停的已有任务，由各下载器模块实现；只在用户手动指定种子时调用。

        确认口径与各模块新建任务选集后启动任务的方式一致。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :return: 恢复已确认（或请求已完成，视下载器能力）时返回 True
        """
        raise NotImplementedError

    def _existing_task_state(
            self, server: DownloaderServiceT, torrent_hash: str,
    ) -> torrent_rules.ExistingTaskState:
        """
        读取已有任务的运行状态并归一，由各下载器模块实现。

        :param server: 当前下载器客户端
        :param torrent_hash: 已有任务的 hash
        :return: 归一后的状态；读取失败或无法识别时返回 UNKNOWN
        """
        raise NotImplementedError

    def _task_tags(self, server: DownloaderServiceT, torrent_hash: str) -> Optional[List[str]]:
        """
        读取任务当前的全部标签，由各下载器模块实现。

        :param server: 当前下载器客户端
        :param torrent_hash: 任务 hash
        :return: 标签列表；读取失败时返回 None，不能用空列表代替（否则会被当成"没有标签"写回）
        """
        raise NotImplementedError

    def _add_task_tags(
            self, server: DownloaderServiceT, torrent_hash: str, tags: List[str], current: List[str],
    ) -> bool:
        """
        给任务追加标签并保留其他标签，由各下载器模块实现。

        :param server: 当前下载器客户端
        :param torrent_hash: 任务 hash
        :param tags: 需要追加的标签
        :param current: 调用方刚读取到的全部标签；只能整体覆盖标签的下载器（Transmission）据此计算新值
        :return: 请求完成时返回 True
        """
        raise NotImplementedError

    def _remove_task_tags(
            self, server: DownloaderServiceT, torrent_hash: str, tags: List[str], current: List[str],
    ) -> bool:
        """
        从任务上移除指定标签并保留其他标签，由各下载器模块实现。

        :param server: 当前下载器客户端
        :param torrent_hash: 任务 hash
        :param tags: 需要移除的标签
        :param current: 调用方刚读取到的全部标签；只能整体覆盖标签的下载器（Transmission）据此计算新值
        :return: 请求完成时返回 True
        """
        raise NotImplementedError

    def test(self) -> Optional[Tuple[bool, str]]:
        """
        检查全部已启用下载器的连接状态，必要时先尝试重连。

        :return: 首个失败的说明或整体成功结果；没有实例时返回 None
        """
        if not self.get_instances():
            return None
        for name, server in self.get_instances().items():
            if server.is_inactive():
                server.reconnect()
            if not server.transfer_info():
                return False, f"无法连接{self.get_name()}下载器：{name}"
        return True, ""

    def scheduler_job(self) -> None:
        """
        巡检已配置下载器，对失活连接发起重连。

        由模块调度器每十分钟调用一次。
        """
        for name, server in self.get_instances().items():
            if server.is_inactive():
                logger.info(f"{self.get_name()}下载器 {name} 连接断开，尝试重连 ...")
                server.reconnect()

    @staticmethod
    def _normalize_torrent_files(
        files: Any, item_factory: Callable[[Any], TFile]
    ) -> Optional[List[TFile]]:
        """
        将下载器文件集合投影为宿主数据对象。

        :param files: 下载器返回的集合；带 data 属性时读取该属性
        :param item_factory: 把单个下载器文件转换为宿主文件对象的函数
        :return: 转换后的文件列表；下载器返回 None 时仍返回 None
        """
        if files is None:
            return None
        source = getattr(files, "data", files)
        return [item_factory(item) for item in source]

    def _get_torrent_info(self, content: Union[Path, str, bytes]) \
            -> Tuple[Optional[Torrent], Optional[Union[str, bytes]]]:
        """
        读取种子内容并解析元信息，磁力链接保持原样。

        路径不存在时查询本地种子缓存，不向外部站点重新下载。

        :param content: 本地路径、种子内容或磁力链接
        :return: 种子对象与原始内容；磁力链接的对象为空，读取或解析异常时两项均为空
        """
        torrent_content: Optional[Union[str, bytes]]
        torrent_info, torrent_content = None, None
        try:
            if isinstance(content, Path):
                if content.exists():
                    torrent_content = content.read_bytes()
                else:
                    # 读取缓存的种子文件；与 TorrentHelper 写入路由一致，只在本地文件系统
                    torrent_content = FileCache(local_only=True).get(
                        content.as_posix(), region="torrents"
                    )
            else:
                torrent_content = content

            if torrent_content:
                # 检查是否为磁力链接
                if torrent_rules.is_magnet_link(torrent_content):
                    return None, torrent_content
                else:
                    torrent_info = Torrent.from_string(torrent_content)

            return torrent_info, torrent_content
        except Exception as e:
            logger.error(f"获取种子名称失败：{e}")
            return None, None

    @staticmethod
    def _normalize_query_status(
            status: Optional[Union[TorrentStatus, TorrentQueryStatus, str]]
    ) -> TorrentQueryStatus:
        """
        把旧状态枚举、文本别名和新查询状态归一。

        :param status: 下载状态枚举或字符串，兼容中文与旧名称
        :return: 规范查询状态；空值或未知值回退为查询全部
        """
        status_value = getattr(status, "value", status)
        status_text = str(status_value or "").strip().lower()
        if not status_text or status_text in {"all", "全部"}:
            return TorrentQueryStatus.ALL
        if status_text in {
            TorrentStatus.TRANSFER.value,
            TorrentQueryStatus.TRANSFER.value,
            "transfer",
        }:
            return TorrentQueryStatus.TRANSFER
        if status_text in {
            TorrentStatus.DOWNLOADING.value,
            TorrentQueryStatus.DOWNLOADING.value,
            "downloading",
        }:
            return TorrentQueryStatus.DOWNLOADING
        if status_text in {
            TorrentQueryStatus.COMPLETED.value,
            "complete",
            "seeding",
            "完成",
            "已完成",
        }:
            return TorrentQueryStatus.COMPLETED
        if status_text in {TorrentQueryStatus.PAUSED.value, "pause", "暂停", "已暂停"}:
            return TorrentQueryStatus.PAUSED
        return TorrentQueryStatus.ALL
