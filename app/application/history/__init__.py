from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Optional, Protocol, Union

from app.application.history.contracts import (
    TransferHistoryMonthlyStatistics,
    TransferHistoryQueryPort,
    TransferHistoryReplacePort,
    TransferHistoryRepository,
    TransferHistorySnapshot,
    TransferHistoryStagingPort,
    TransferHistoryStatisticSnapshot,
    TransferHistoryWrite,
    TransferHistoryWritePort,
    _freeze_json,
    configure_transfer_history_repository,
    get_transfer_history_repository,
    reset_transfer_history_repository,
)
from app.application.history.mutation import (
    DownloadFileMutationRepository as DownloadFileMutationRepository,
)
from app.application.history.mutation import (
    DownloadHistoryMutationCommand as DownloadHistoryMutationCommand,
)
from app.application.history.mutation import (
    DownloadHistoryMutationRepository as DownloadHistoryMutationRepository,
)
from app.application.history.mutation import (
    HistoryMutationResult as HistoryMutationResult,
)
from app.application.history.mutation import (
    HistoryUnitOfWork as HistoryUnitOfWork,
)
from app.application.history.mutation import (
    TransferHistoryMutationCommand as TransferHistoryMutationCommand,
)
from app.application.history.mutation import (
    TransferHistoryMutationRepository as TransferHistoryMutationRepository,
)
from app.application.transfer import history as history_projection
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.foundation.text import cut as jieba_cut
from app.schemas.common import JsonData
from app.schemas.file import FileItem
from app.schemas.history import (
    DownloadHistory,
    TransferHistory,
    TransferHistoryPage,
)
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource

__all__ = [
    "DownloadFileMutationRepository",
    "DownloadFileSnapshot",
    "DownloadFileWrite",
    "DownloadHistory",
    "DownloadHistoryMutationCommand",
    "DownloadHistoryMutationRepository",
    "DownloadHistoryQueryPort",
    "DownloadHistoryRepository",
    "DownloadHistorySnapshot",
    "DownloadHistoryWrite",
    "DownloadHistoryWritePort",
    "HistoryMutationResult",
    "HistoryQueryService",
    "HistoryUnitOfWork",
    "ManualTransferHistory",
    "TransferHistory",
    "TransferHistoryLookupRepository",
    "TransferHistoryLookupService",
    "TransferHistoryMonthlyStatistics",
    "TransferHistoryMutationCommand",
    "TransferHistoryPage",
    "TransferHistoryQueryPort",
    "TransferHistoryReplacePort",
    "TransferHistoryRepository",
    "TransferHistorySnapshot",
    "TransferHistoryStagingPort",
    "TransferHistoryStatisticSnapshot",
    "TransferHistoryWrite",
    "TransferHistoryWritePort",
    "add_transfer_fail",
    "add_transfer_success",
    "configure_transfer_history_repository",
    "get_transfer_history_repository",
    "reset_transfer_history_repository",
]


@dataclass(frozen=True, slots=True)
class DownloadHistorySnapshot:
    """脱离数据库会话后供宿主下载、订阅和整理用例读取的历史快照。"""

    id: int
    path: str
    type: str
    title: str
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    poster: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_description: Optional[str] = None
    torrent_site: Optional[str] = None
    userid: Optional[str] = None
    username: Optional[str] = None
    channel: Optional[str] = None
    date: Optional[str] = None
    note: Optional[JsonData] = None
    media_category_id: Optional[str] = None
    media_category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    episode_group: Optional[str] = None
    custom_words: Optional[str] = None

    def __post_init__(self) -> None:
        """递归冻结可变 JSON 字段，使 DTO 在所有层级都不可修改。"""
        object.__setattr__(self, "note", _freeze_json(self.note))


@dataclass(frozen=True, slots=True)
class DownloadFileSnapshot:
    """脱离数据库会话的下载文件关联快照。"""

    id: int
    downloader: Optional[str]
    download_hash: Optional[str]
    fullpath: Optional[str]
    savepath: Optional[str]
    filepath: Optional[str]
    torrentname: Optional[str]
    state: int


@dataclass(frozen=True, slots=True)
class DownloadHistoryWrite:
    """一次下载成功后写入历史所需的完整稳定数据。"""

    path: str
    type: str
    title: str
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    poster: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    torrent_name: Optional[str] = None
    torrent_description: Optional[str] = None
    torrent_site: Optional[str] = None
    userid: Optional[Union[str, int]] = None
    username: Optional[str] = None
    channel: Optional[str] = None
    date: Optional[str] = None
    note: Optional[JsonData] = None
    media_category_id: Optional[str] = None
    media_category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    episode_group: Optional[str] = None
    custom_words: Optional[str] = None

    def to_payload(self) -> dict[str, Any]:
        """返回可交给持久化适配器的独立字段副本。"""
        payload = asdict(self)
        if self.media_source is not None:
            payload["media_source"] = str(self.media_source)
        if self.userid is not None:
            payload["userid"] = str(self.userid)
        return payload


@dataclass(frozen=True, slots=True)
class DownloadFileWrite:
    """下载任务关联文件的一次稳定写入。"""

    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    fullpath: Optional[str] = None
    savepath: Optional[str] = None
    filepath: Optional[str] = None
    torrentname: Optional[str] = None
    state: int = 1

    def to_payload(self) -> dict[str, Any]:
        """返回可交给持久化适配器的独立字段副本。"""
        return asdict(self)


class DownloadHistoryQueryPort(Protocol):
    """宿主下载、订阅、Agent 和整理用例所需的类型化查询端口。"""

    def get_by_hash(
        self,
        download_hash: str,
    ) -> Optional[DownloadHistorySnapshot]:
        """按下载任务 Hash 返回最新历史快照。"""
        ...

    def get_by_hashes(
        self,
        download_hashes: list[str],
    ) -> dict[str, DownloadHistorySnapshot]:
        """批量返回以下载任务 Hash 为键的最新历史快照。"""
        ...

    def get_by_path(self, path: str) -> Optional[DownloadHistorySnapshot]:
        """按下载保存路径返回历史快照。"""
        ...

    def get_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        music_type: Optional[str] = None,
    ) -> list[DownloadHistorySnapshot]:
        """按规范媒体身份返回历史快照。"""
        ...

    def get_file_by_fullpath(
        self,
        fullpath: str,
    ) -> Optional[DownloadFileSnapshot]:
        """按完整路径返回一条有效下载文件快照。"""
        ...

    def get_files_by_hash(
        self,
        download_hash: str,
        state: Optional[int] = None,
    ) -> list[DownloadFileSnapshot]:
        """按下载任务 Hash 返回文件快照。"""
        ...

    def get_files_by_savepath(self, savepath: str) -> list[DownloadFileSnapshot]:
        """按保存目录返回下载文件快照。"""
        ...

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
    ) -> list[DownloadHistorySnapshot]:
        """异步按下载时间倒序分页返回历史快照。"""
        ...


class DownloadHistoryWritePort(Protocol):
    """下载历史新增和删除所需的类型化事务端口。"""

    def add(
        self,
        history: DownloadHistoryWrite,
        files: tuple[DownloadFileWrite, ...] = (),
    ) -> int:
        """在单一事务中新增历史与关联文件并返回历史 ID。"""
        ...

    async def async_delete(self, history_id: int) -> None:
        """在独立异步事务中删除指定历史。"""
        ...


class DownloadHistoryRepository(DownloadHistoryQueryPort, DownloadHistoryWritePort, Protocol):
    """组合宿主所需全部下载历史查询和变更能力。"""


class AsyncDownloadHistoryQueryRepository(Protocol):
    """下载历史只读用例需要的最小异步持久化端口。"""

    async def async_list_by_page(self, page: int = 1, count: int = 30) -> list[DownloadHistorySnapshot]:
        """按下载时间倒序分页读取历史记录。"""
        ...

    async def async_count(self) -> int:
        """返回下载历史记录总数。"""
        ...


@dataclass(frozen=True, slots=True)
class ManualTransferHistory:
    """手动整理准备阶段需要的稳定历史投影。"""

    id: int
    status: bool
    mode: Optional[str]
    src_fileitem: Optional[dict[str, JsonData]]
    dest_fileitem: Optional[dict[str, JsonData]]
    downloader: Optional[str]
    download_hash: Optional[str]
    type: Optional[str]
    media_source: Optional[MediaSource]
    media_id: Optional[str]
    music_type: Optional[str]
    seasons: Optional[str]
    episodes: Optional[str]
    episode_group: Optional[str]


class TransferHistoryLookupRepository(Protocol):
    """手动整理历史投影所需的同步查询端口。"""

    def get(self, history_id: int) -> Optional[TransferHistorySnapshot]:
        """按主键读取整理历史。"""
        ...


class TransferHistoryLookupService:
    """向同步整理用例提供脱离 ORM 会话的历史投影。"""

    def __init__(self, repository: TransferHistoryLookupRepository) -> None:
        """保存整理历史只读端口。"""
        self._repository = repository

    def get(self, history_id: int) -> Optional[ManualTransferHistory]:
        """按主键读取手动整理所需字段。"""
        record = self._repository.get(history_id)
        if record is None:
            return None
        src_fileitem = (
            record.src_fileitem if isinstance(record.src_fileitem, dict) else None
        )
        dest_fileitem = (
            record.dest_fileitem if isinstance(record.dest_fileitem, dict) else None
        )
        return ManualTransferHistory(
            id=record.id,
            status=bool(record.status),
            mode=record.mode,
            src_fileitem=src_fileitem,
            dest_fileitem=dest_fileitem,
            downloader=record.downloader,
            download_hash=record.download_hash,
            type=record.type,
            media_source=record.media_source,
            media_id=record.media_id,
            music_type=getattr(record, "music_type", None),
            seasons=record.seasons,
            episodes=record.episodes,
            episode_group=record.episode_group,
        )


class HistoryQueryService:
    """提供历史列表和详情 DTO，隔离 API 与数据库模型。"""

    def __init__(
        self,
        *,
        download_repository: AsyncDownloadHistoryQueryRepository,
        transfer_repository: TransferHistoryQueryPort,
    ) -> None:
        """保存下载历史和整理历史的只读端口。"""
        self._download_repository = download_repository
        self._transfer_repository = transfer_repository

    async def list_download(
        self,
        *,
        page: int = 1,
        count: int = 30,
    ) -> list[DownloadHistory]:
        """分页读取下载历史并转换为稳定的接口 DTO。"""
        records = await self._download_repository.async_list_by_page(page, count)
        return [DownloadHistory.model_validate(record) for record in records]

    async def count_download(self) -> int:
        """返回下载历史精确总数，供分页 API 通过附加元数据报告。"""
        return await self._download_repository.async_count()

    async def list_transfer(
        self,
        *,
        title: Optional[str] = None,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
    ) -> TransferHistoryPage:
        """应用历史筛选规则并返回整理历史分页 DTO。"""
        if title:
            wildcard = "*" in title or "?" in title
            if wildcard:
                pattern = self._glob_to_like(title)
            else:
                pattern = "%".join(jieba_cut(title, HMM=False))
            total = await self._transfer_repository.async_count_by_title(
                pattern,
                status=status,
                wildcard=wildcard,
            )
            records = await self._transfer_repository.async_list_by_title(
                pattern,
                page=page,
                count=count,
                status=status,
                wildcard=wildcard,
            )
        else:
            records = await self._transfer_repository.async_list_by_page(
                page=page,
                count=count,
                status=status,
            )
            total = await self._transfer_repository.async_count(status=status)

        return TransferHistoryPage(
            list=[TransferHistory.model_validate(record) for record in records],
            total=int(total or 0),
        )

    async def get_transfer(self, history_id: int) -> Optional[TransferHistory]:
        """读取单条整理历史 DTO，不向调用方泄漏 ORM 实例。"""
        record = await self._transfer_repository.async_get(history_id)
        if record is None:
            return None
        return TransferHistory.model_validate(record)

    async def get_transfers(
        self,
        history_ids: list[int],
    ) -> tuple[list[TransferHistory], list[int]]:
        """按输入顺序读取多条整理历史，并同时返回缺失 ID。"""
        records: list[TransferHistory] = []
        missing_ids: list[int] = []
        for history_id in history_ids:
            record = await self.get_transfer(history_id)
            if record is None:
                missing_ids.append(history_id)
            else:
                records.append(record)
        return records, missing_ids

    @staticmethod
    def _glob_to_like(pattern: str) -> str:
        """将 glob 通配符转换为使用反斜杠转义的 SQL LIKE 模式。"""
        result = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        return result.replace("*", "%").replace("?", "_")


# 整理历史写入口保留仓储事务契约；领域对象的字段映射由 transfer.history 统一维护。

def add_transfer_success(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Union[MediaInfo, MusicInfo],
    transferinfo: TransferInfo,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
    transfer_history_oper: Optional[TransferHistoryReplacePort] = None,
) -> TransferHistorySnapshot:
    """
    新增转移成功历史记录。
    :param fileitem: 源文件项
    :param mode: 整理方式
    :param meta: 文件名识别结果
    :param mediainfo: 媒体识别结果
    :param transferinfo: 整理结果
    :param downloader: 下载器
    :param download_hash: 种子 hash
    :param transfer_history_oper: 兼容旧关键字的暂存端口，未传时使用组合根仓储
    :return: 落库后的整理记录
    """
    repository = transfer_history_oper or get_transfer_history_repository()
    fields = history_projection.success_fields(
        fileitem=fileitem,
        mode=mode,
        meta=meta,
        mediainfo=mediainfo,
        transferinfo=transferinfo,
        downloader=downloader,
        download_hash=download_hash,
    )
    return repository.replace(TransferHistoryWrite(**fields))


def add_transfer_fail(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
    transferinfo: Optional[TransferInfo] = None,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
    retry_count: Optional[int] = None,
    auto_paused: bool = False,
    transfer_history_oper: Optional[TransferHistoryReplacePort] = None,
) -> TransferHistorySnapshot:
    """
    新增转移失败历史记录。

    识别结果与整理结果齐备时按完整字段落库；缺任一项则走「未识别到媒体信息」分支，
    此时只有文件名解析出的元数据可用，不写目标路径。
    :param fileitem: 源文件项
    :param mode: 整理方式
    :param meta: 文件名识别结果
    :param mediainfo: 媒体识别结果，未识别时为 None
    :param transferinfo: 整理结果，未进入整理时为 None
    :param downloader: 下载器
    :param download_hash: 种子 hash
    :param retry_count: 当前文件版本累计失败次数
    :param auto_paused: 是否已达到自动整理暂停阈值
    :param transfer_history_oper: 兼容旧关键字的暂存端口，未传时使用组合根仓储
    :return: 落库后的整理记录
    """
    repository = transfer_history_oper or get_transfer_history_repository()
    fields = history_projection.failure_fields(
        fileitem=fileitem,
        mode=mode,
        meta=meta,
        mediainfo=mediainfo,
        transferinfo=transferinfo,
        downloader=downloader,
        download_hash=download_hash,
        retry_count=retry_count,
        auto_paused=auto_paused,
    )
    return repository.replace(TransferHistoryWrite(**fields))
