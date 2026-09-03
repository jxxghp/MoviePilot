from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, NoReturn, Optional, Protocol, Union

from app.application.configuration import TransferRetryConfig, get_transfer_retry_config
from app.application.historymutation import (
    DownloadFileMutationRepository as DownloadFileMutationRepository,
)
from app.application.historymutation import (
    DownloadHistoryMutationCommand as DownloadHistoryMutationCommand,
)
from app.application.historymutation import (
    DownloadHistoryMutationRepository as DownloadHistoryMutationRepository,
)
from app.application.historymutation import (
    HistoryMutationResult as HistoryMutationResult,
)
from app.application.historymutation import (
    HistoryUnitOfWork as HistoryUnitOfWork,
)
from app.application.historymutation import (
    TransferHistoryMutationCommand as TransferHistoryMutationCommand,
)
from app.application.historymutation import (
    TransferHistoryMutationRepository as TransferHistoryMutationRepository,
)
from app.application.transfer import history as history_projection
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation.text import cut as jieba_cut
from app.runtime.cache import TTLCache
from app.runtime.log import logger
from app.schemas.common import JsonData
from app.schemas.file import FileItem
from app.schemas.history import (
    DownloadHistory,
    TransferHistory,
    TransferHistoryPage,
)
from app.schemas.media import resolve_media_identity
from app.schemas.transfer import TransferInfo
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource

# 失败重试次数的合法区间。下界为 1：一次瞬时故障（网络抖动、TMDB 瞬断、移动失败）
# 不该让文件永久漏整理，所以不允许关闭重试；上界为 10：永远识别不出的文件重试再多
# 也不会成功，只会重复推送失败通知，批量导入场景下会刷屏，所以不允许无限重试
MIN_FAILED_RETRIES = 1
MAX_FAILED_RETRIES = 10

# 同一源路径的连续整理失败状态。整理链在写失败历史时累计、整理成功或删除历史时清零，
# 查重闸只读不写，避免监控层与整理链对同一个事件重复计数。缓存值会同时保存文件指纹，
# 因此同一路径的新版本天然获得独立预算；内存缓存会随进程重启清空，Redis 后端则保留到 TTL 到期。
FAILED_RETRY_TTL = 24 * 3600
_failed_retry_counts = TTLCache(region="transfer_failed_retry", maxsize=5000, ttl=FAILED_RETRY_TTL)


class _FrozenJsonDict(dict[str, JsonData]):
    """保留 JSON 字典读取与序列化行为，并拒绝常规原地修改。"""

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> NoReturn:
        """拒绝修改已经进入历史快照的嵌套 JSON。"""
        raise TypeError("历史快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


class _FrozenJsonList(list[JsonData]):
    """保留 JSON 数组读取与序列化行为，并拒绝常规原地修改。"""

    def _reject_mutation(self, *args: Any, **kwargs: Any) -> NoReturn:
        """拒绝修改已经进入历史快照的嵌套 JSON。"""
        raise TypeError("历史快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


def _freeze_json(value: JsonData) -> JsonData:
    """递归复制并冻结 JSON 容器，避免快照内部仍暴露可变引用。"""
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


@dataclass(frozen=True, slots=True)
class TransferHistorySnapshot:
    """脱离数据库会话后供整理、Agent 和历史用例读取的完整历史快照。"""

    id: int
    transfer_task_id: Optional[str] = None
    transfer_settlement_revision: Optional[int] = None
    src: Optional[str] = None
    src_storage: Optional[str] = None
    src_fileitem: Optional[JsonData] = None
    dest: Optional[str] = None
    dest_storage: Optional[str] = None
    dest_fileitem: Optional[JsonData] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    media_category_id: Optional[str] = None
    category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    status: bool = True
    errmsg: Optional[str] = None
    date: Optional[str] = None
    files: Optional[JsonData] = None
    episode_group: Optional[str] = None

    def __post_init__(self) -> None:
        """递归冻结历史 JSON 字段，避免跨层共享可变 ORM 列值。"""
        object.__setattr__(self, "src_fileitem", _freeze_json(self.src_fileitem))
        object.__setattr__(self, "dest_fileitem", _freeze_json(self.dest_fileitem))
        object.__setattr__(self, "files", _freeze_json(self.files))


@dataclass(frozen=True, slots=True)
class TransferHistoryWrite:
    """替换同源整理历史所需的完整稳定写入数据。"""

    src: str
    src_storage: Optional[str] = None
    src_fileitem: Optional[JsonData] = None
    dest: Optional[str] = None
    dest_storage: Optional[str] = None
    dest_fileitem: Optional[JsonData] = None
    mode: Optional[str] = None
    type: Optional[str] = None
    media_category_id: Optional[str] = None
    category: Optional[str] = None
    classification_rule_id: Optional[str] = None
    classification_policy_revision: Optional[int] = None
    classification_source: Optional[str] = None
    title: Optional[str] = None
    year: Optional[str] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    music_type: Optional[str] = None
    total_tracks: Optional[int] = None
    audio_format: Optional[str] = None
    audio_lossless: Optional[bool] = None
    bit_depth: Optional[int] = None
    sample_rate: Optional[int] = None
    bitrate: Optional[int] = None
    seasons: Optional[str] = None
    episodes: Optional[str] = None
    image: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    status: bool = True
    errmsg: Optional[str] = None
    files: Optional[JsonData] = None
    episode_group: Optional[str] = None

    def to_payload(self) -> dict[str, object]:
        """返回仅含持久化字段的独立副本。"""
        payload: dict[str, object] = asdict(self)
        if self.media_source is not None:
            payload["media_source"] = str(self.media_source)
        return payload


@dataclass(frozen=True, slots=True)
class TransferHistoryStatisticSnapshot:
    """单日整理历史数量统计。"""

    date: str
    count: int


@dataclass(frozen=True, slots=True)
class TransferHistoryMonthlyStatistics:
    """本月按媒体类别聚合的整理历史数量。"""

    movies: int
    tv_shows: int
    episodes: int
    music: int


class TransferHistoryQueryPort(Protocol):
    """宿主整理、历史、Agent 和工作流所需的类型化查询端口。"""

    def get(self, history_id: int) -> Optional[TransferHistorySnapshot]:
        """按主键返回整理历史快照。"""
        ...

    def get_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按源路径和可选存储返回最新历史快照。"""
        ...

    def get_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按源路径和可选存储返回最新成功历史快照。"""
        ...

    def get_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按目标路径和可选存储返回最新历史快照。"""
        ...

    def get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """按 durable 整理任务标识返回终态历史快照。"""
        ...

    def get_by_media_identity(
        self,
        media_source: MediaSource,
        media_id: str,
        mtype: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """按规范媒体身份和可选媒体类型返回历史快照。"""
        ...

    def list_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """按源路径返回成功整理历史快照。"""
        ...

    def list_success_move_by_dest(
        self,
        dest: str,
        storage: Optional[str] = None,
        recursive: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """按目标路径返回成功移动历史快照。"""
        ...

    def list_by_hash(self, download_hash: str) -> list[TransferHistorySnapshot]:
        """按下载任务 Hash 返回历史快照。"""
        ...

    async def async_get(
        self,
        history_id: int,
    ) -> Optional[TransferHistorySnapshot]:
        """异步按主键返回整理历史快照。"""
        ...

    async def async_list_by_title(
        self,
        title: str,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> list[TransferHistorySnapshot]:
        """异步按标题或路径分页返回历史快照。"""
        ...

    async def async_list_by_page(
        self,
        page: int = 1,
        count: int = 30,
        status: Optional[bool] = None,
    ) -> list[TransferHistorySnapshot]:
        """异步按时间倒序分页返回历史快照。"""
        ...

    async def async_count(self, status: Optional[bool] = None) -> int:
        """异步统计指定状态的整理历史数量。"""
        ...

    async def async_count_by_title(
        self,
        title: str,
        status: Optional[bool] = None,
        wildcard: bool = False,
    ) -> int:
        """异步统计匹配标题或路径的整理历史数量。"""
        ...

    async def async_statistic(
        self,
        days: int = 7,
    ) -> list[TransferHistoryStatisticSnapshot]:
        """异步返回最近若干天的每日整理数量。"""
        ...

    def monthly_media_statistics(self) -> TransferHistoryMonthlyStatistics:
        """返回本月电影、剧集、单集和音乐整理数量。"""
        ...


class TransferHistoryWritePort(Protocol):
    """整理历史替换、删除与维护所需的类型化事务端口。"""

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """在独立事务中替换同源历史并返回快照。"""
        ...

    def delete(self, history_id: int) -> None:
        """在独立事务中删除一条旧历史。"""
        ...

    async def async_delete(self, history_id: int) -> None:
        """在独立异步事务中删除一条旧历史。"""
        ...

    def truncate(self) -> None:
        """在独立事务中清空没有 durable 任务映射的历史。"""
        ...

    def update_download_hash(self, history_id: int, download_hash: str) -> None:
        """在独立事务中补充整理历史的下载任务 Hash。"""
        ...


class TransferHistoryReplacePort(Protocol):
    """整理历史业务写入规则需要的最小替换端口。"""

    def replace(self, history: TransferHistoryWrite) -> TransferHistorySnapshot:
        """替换同源历史并返回冻结快照。"""
        ...


class TransferHistoryStagingPort(TransferHistoryReplacePort, Protocol):
    """durable 结算事务内查询并替换整理历史的类型化暂存端口。"""

    def get_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """在调用方 Session 内按源路径读取历史快照。"""
        ...

    def get_success_by_src(
        self,
        src: str,
        storage: Optional[str] = None,
    ) -> Optional[TransferHistorySnapshot]:
        """在调用方 Session 内按源路径读取成功历史快照。"""
        ...

class TransferHistoryRepository(
    TransferHistoryQueryPort,
    TransferHistoryWritePort,
    Protocol,
):
    """组合宿主所需全部整理历史查询和变更能力。"""


_configured_transfer_history_repository: (
    Callable[[], TransferHistoryRepository] | None
) = None


def configure_transfer_history_repository(
    provider: Callable[[], TransferHistoryRepository],
) -> None:
    """由启动组合根登记类型化整理历史仓储提供器。"""
    global _configured_transfer_history_repository
    _configured_transfer_history_repository = provider


def reset_transfer_history_repository() -> None:
    """清除当前 lifespan 的整理历史仓储提供器。"""
    global _configured_transfer_history_repository
    _configured_transfer_history_repository = None


def get_transfer_history_repository() -> TransferHistoryRepository:
    """返回启动组合根登记的类型化整理历史仓储。"""
    if _configured_transfer_history_repository is None:
        raise RuntimeError("类型化整理历史仓储尚未配置")
    return _configured_transfer_history_repository()


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
    media_source: Optional[str]
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


class HistoryGateAction:
    """
    整理历史查重闸的判定结果。

    监控分发（app/monitor/dispatcher.py）与整理链计划整理段（app/chain/transfer/plan.py）
    共用本模块，避免两处各写一套去重策略后互相对冲：上游放行的文件被下游按
    「存在记录即拦」全额收回，等于放行逻辑完全失效。
    """
    # 没有整理记录
    PASS_NO_RECORD = "pass_no_record"
    # 上次整理失败且重试次数未用尽，放行重试
    PASS_FAILED = "pass_failed"
    # 上次整理失败但源文件已变为新版本，放行并重置该版本的重试预算
    PASS_FAILED_VERSION_CHANGED = "pass_failed_version_changed"
    # 已整理成功但源文件已变化，放行交由 overwrite_mode 决断
    PASS_SIZE_CHANGED = "pass_size_changed"
    # 上次整理失败且重试次数已用尽，跳过
    SKIP_RETRY_EXHAUSTED = "skip_retry_exhausted"
    # 已整理成功且源文件未变化，跳过
    SKIP = "skip"


def is_skip_action(action: str) -> bool:
    """
    判断查重闸判定是否为跳过整理。
    :param action: HistoryGateAction 之一
    :return: True 表示跳过
    """
    return action in (HistoryGateAction.SKIP, HistoryGateAction.SKIP_RETRY_EXHAUSTED)


def max_failed_retries(config: TransferRetryConfig | None = None) -> int:
    """
    读取失败重试上限并钳制到合法区间。

    配置为负数、0 或超过上界时都会被钳制并记录 warn：关闭重试会让瞬时故障造成
    永久漏件，无限重试会让永久失败的文件反复刷通知，两端都不接受。
    :return: 合法的最大重试次数
    """
    raw = (config or get_transfer_retry_config()).max_failed_retries
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warn(f"TRANSFER_MAX_FAILED_RETRIES 配置非法（{raw!r}），"
                    f"已回退为 {MIN_FAILED_RETRIES}")
        return MIN_FAILED_RETRIES
    if value < MIN_FAILED_RETRIES:
        logger.warn(f"TRANSFER_MAX_FAILED_RETRIES 不能小于 {MIN_FAILED_RETRIES}"
                    f"（当前 {value}），已按 {MIN_FAILED_RETRIES} 处理")
        return MIN_FAILED_RETRIES
    if value > MAX_FAILED_RETRIES:
        logger.warn(f"TRANSFER_MAX_FAILED_RETRIES 不能大于 {MAX_FAILED_RETRIES}"
                    f"（当前 {value}），已按 {MAX_FAILED_RETRIES} 处理")
        return MAX_FAILED_RETRIES
    return value


def failed_retry_key(src_path: Optional[str], storage: Optional[str] = None) -> Optional[str]:
    """
    生成失败重试计数的缓存键。
    :param src_path: 整理记录使用的源路径
    :param storage: 源存储
    :return: 缓存键，源路径为空时返回 None
    """
    if not src_path:
        return None
    return f"{storage or 'local'}:{src_path}"


def coerce_modify_time(modify_time: Any) -> Optional[float]:
    """
    统一转换文件修改时间，无法转换时返回 None。
    :param modify_time: 原始修改时间值
    :return: 文件修改时间
    """
    if modify_time is None:
        return None
    try:
        return float(modify_time)
    except (TypeError, ValueError):
        return None


def coerce_fileid(fileid: Any) -> Optional[str]:
    """
    统一转换文件唯一标识，空值视为不可比对。
    :param fileid: 原始文件唯一标识
    :return: 非空文件唯一标识
    """
    if fileid is None:
        return None
    value = str(fileid).strip()
    return value or None


def file_fingerprint(
        file_size: Any = None,
        file_modify_time: Any = None,
        fileid: Any = None,
) -> Dict[str, Any]:
    """
    生成用于区分同一路径文件版本的稳定指纹。

    大小是所有存储器都尽量提供的最小指纹；两端均有数据时，修改时间和文件 ID 还可
    识别“同大小替换”。只保留可比较字段，避免缺失元数据把同一文件误判成新版本。
    :param file_size: 文件大小
    :param file_modify_time: 文件修改时间
    :param fileid: 存储器文件唯一标识
    :return: 非空且可比较的指纹字段
    """
    fingerprint: Dict[str, Any] = {}
    size = coerce_size(file_size)
    if size is not None:
        fingerprint["size"] = size
    modify_time = coerce_modify_time(file_modify_time)
    if modify_time is not None:
        fingerprint["modify_time"] = modify_time
    normalized_fileid = coerce_fileid(fileid)
    if normalized_fileid is not None:
        fingerprint["fileid"] = normalized_fileid
    return fingerprint


def _retry_state(value: Any) -> tuple[int, Dict[str, Any]]:
    """将新旧缓存值统一转换为失败次数与文件指纹。"""
    if isinstance(value, dict):
        raw_count = value.get("count", 0)
        raw_fingerprint = value.get("fingerprint")
    else:
        # 兼容已写入 Redis 或内存的旧整数计数；下次带指纹写入时会自动升级结构。
        raw_count = value
        raw_fingerprint = None
    try:
        count = max(int(raw_count or 0), 0)
    except (TypeError, ValueError):
        count = 0
    fingerprint = (
        file_fingerprint(
            file_size=raw_fingerprint.get("size"),
            file_modify_time=raw_fingerprint.get("modify_time"),
            fileid=raw_fingerprint.get("fileid"),
        )
        if isinstance(raw_fingerprint, dict)
        else {}
    )
    return count, fingerprint


def _is_file_version_changed(
        recorded_fingerprint: Dict[str, Any],
        current_fingerprint: Dict[str, Any],
) -> bool:
    """判断两个可比文件指纹是否指向不同版本。"""
    for field in ("fileid", "modify_time", "size"):
        recorded_value = recorded_fingerprint.get(field)
        current_value = current_fingerprint.get(field)
        if (
                recorded_value is not None
                and current_value is not None
                and recorded_value != current_value
        ):
            return True
    return False


def failed_retry_count(src_path: Optional[str], storage: Optional[str] = None,
                       file_size: Any = None, file_modify_time: Any = None,
                       fileid: Any = None) -> int:
    """
    读取同一源路径已累计的连续整理失败次数。
    :param src_path: 整理记录使用的源路径
    :param storage: 源存储
    :param file_size: 当前文件大小
    :param file_modify_time: 当前文件修改时间
    :param fileid: 当前文件唯一标识
    :return: 当前文件版本已失败次数，无记录时为 0
    """
    key = failed_retry_key(src_path, storage)
    if not key:
        return 0
    count, recorded_fingerprint = _retry_state(_failed_retry_counts.get(key))
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if (
            recorded_fingerprint
            and current_fingerprint
            and _is_file_version_changed(recorded_fingerprint, current_fingerprint)
    ):
        return 0
    return count


def record_transfer_failure(src_path: Optional[str], storage: Optional[str] = None,
                            file_size: Any = None, file_modify_time: Any = None,
                            fileid: Any = None) -> int:
    """
    累计一次整理失败。
    :param src_path: 整理记录使用的源路径
    :param storage: 源存储
    :param file_size: 当前文件大小
    :param file_modify_time: 当前文件修改时间
    :param fileid: 当前文件唯一标识
    :return: 当前文件版本累计后的失败次数
    """
    key = failed_retry_key(src_path, storage)
    if not key:
        return 0
    count, recorded_fingerprint = _retry_state(_failed_retry_counts.get(key))
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if current_fingerprint and (
            not recorded_fingerprint
            or _is_file_version_changed(recorded_fingerprint, current_fingerprint)
    ):
        count = 0
    count += 1
    if current_fingerprint:
        _failed_retry_counts[key] = {
            "count": count,
            "fingerprint": current_fingerprint,
        }
    elif recorded_fingerprint:
        _failed_retry_counts[key] = {
            "count": count,
            "fingerprint": recorded_fingerprint,
        }
    else:
        _failed_retry_counts[key] = count
    return count


def clear_transfer_failures(src_path: Optional[str], storage: Optional[str] = None) -> None:
    """
    清空同一源路径的失败计数。整理成功、或用户删除整理记录（显式要求重来）时调用。
    :param src_path: 整理记录使用的源路径
    :param storage: 源存储
    """
    key = failed_retry_key(src_path, storage)
    if key:
        # 缺省值必须是 0 而不是 None：CacheBackend.pop 把「default 为 None」当成「未提供
        # default」，键不存在时会抛 KeyError。整理成功路径上绝大多数文件从未失败过，
        # 传 None 会让每一次首次成功整理都炸掉成功回调
        _failed_retry_counts.pop(key, 0)


def coerce_size(size: Any) -> Optional[int]:
    """
    统一转换文件大小，无法转换时返回 None（视为不可比对）。
    :param size: 原始大小值
    :return: 文件大小
    """
    if size is None:
        return None
    try:
        return int(size)
    except (TypeError, ValueError):
        return None


def history_src_size(history: TransferHistorySnapshot) -> Optional[int]:
    """
    读取整理记录中的源文件大小。
    src_fileitem 是 JSON 列，历史数据可能为空、缺 size 键甚至不是字典，
    取不到时统一返回 None 交由调用方保守处理。
    :param history: 整理记录
    :return: 源文件大小，取不到时为 None
    """
    return history_src_fingerprint(history).get("size")


def history_src_fingerprint(history: TransferHistorySnapshot) -> Dict[str, Any]:
    """
    读取整理记录中的源文件版本指纹。
    :param history: 整理记录
    :return: 源文件的可比较指纹字段
    """
    src_fileitem = getattr(history, "src_fileitem", None)
    if not isinstance(src_fileitem, dict):
        return {}
    return file_fingerprint(
        file_size=src_fileitem.get("size"),
        file_modify_time=src_fileitem.get("modify_time"),
        fileid=src_fileitem.get("fileid"),
    )


def resolve_history(
    src_path: str,
    storage: Optional[str] = None,
    transfer_history_oper: Optional[TransferHistoryQueryPort] = None,
) -> Optional[TransferHistorySnapshot]:
    """
    查询源路径对应的整理记录。

    新表通过 (src, src_storage) 唯一索引保证单条记录；仍保留对成功记录的二次确认，
    兼容升级前可能残留的重复数据，避免把已整理成功的文件重复整理。查询异常不在
    此处吞掉，由调用方按各自的重试策略处理。
    :param src_path: 整理记录使用的源路径
    :param storage: 存储
    :param transfer_history_oper: 兼容旧关键字的类型化仓储，未传时使用组合根实现
    :return: 命中的整理记录，未命中时为 None
    """
    repository = transfer_history_oper or get_transfer_history_repository()
    history = repository.get_by_src(src_path, storage=storage)
    if history is not None and not history.status:
        history = repository.get_success_by_src(src_path, storage=storage) or history
    return history


def evaluate_history_gate(history: Optional[TransferHistorySnapshot],
                          file_size: Optional[float] = None,
                          file_modify_time: Optional[float] = None,
                          fileid: Optional[str] = None,
                          retry_count: Optional[int] = None) -> str:
    """
    依据整理历史判断本次是否跳过整理。

    成功记录不能简单地「存在即跳过」：同路径重新上传的新版本会因此没有机会走到
    整理链的 overwrite_mode 判定，升级永远无法入库，故任一可比文件指纹变化时一律放行。
    失败记录按文件版本使用有界重试：新版本先放行并在下一次失败时从 1 重新计数，
    同一版本未达上限时继续重试，让瞬时故障（网络/识别/移动）自愈；达到上限后跳过，
    避免永久失败的文件反复刷失败通知。
    :param history: 整理记录，未命中时为 None
    :param file_size: 当前文件大小，蓝光目录等场景可能为 None
    :param file_modify_time: 当前文件修改时间
    :param fileid: 当前文件唯一标识
    :param retry_count: 已累计的失败次数，None 表示按记录源路径实时查询
    :return: HistoryGateAction 之一
    """
    if history is None:
        return HistoryGateAction.PASS_NO_RECORD
    recorded_fingerprint = history_src_fingerprint(history)
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if not history.status:
        if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
            return HistoryGateAction.PASS_FAILED_VERSION_CHANGED
        if retry_count is None:
            retry_count = failed_retry_count(
                getattr(history, "src", None),
                getattr(history, "src_storage", None),
                file_size=file_size,
                file_modify_time=file_modify_time,
                fileid=fileid,
            )
        if retry_count >= max_failed_retries():
            return HistoryGateAction.SKIP_RETRY_EXHAUSTED
        # 监控事件是稀疏驱动的（落地事件/延迟重扫/补偿扫描），入口还有 TTL 去重兜底，
        # 配合失败次数上限，重试频率与总量都可控
        return HistoryGateAction.PASS_FAILED
    if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
        # 同路径换成了另一个版本（如升级为更高码率），是否覆盖交给整理链的
        # overwrite_mode 决断，查重闸不做替代判断
        return HistoryGateAction.PASS_SIZE_CHANGED
    # 无法比对大小（蓝光目录、历史记录缺 size）时保守跳过，避免重复整理
    return HistoryGateAction.SKIP


def describe_history_gate(history: Optional[TransferHistorySnapshot],
                          file_size: Optional[float] = None,
                          file_modify_time: Optional[float] = None,
                          fileid: Optional[str] = None) -> str:
    """
    生成查重闸判定的可读说明，供日志定位「到底是哪条记录在拦」。
    :param history: 整理记录
    :param file_size: 当前文件大小
    :param file_modify_time: 当前文件修改时间
    :param fileid: 当前文件唯一标识
    :return: 说明文本
    """
    if history is None:
        return "无整理记录"
    recorded_fingerprint = history_src_fingerprint(history)
    current_fingerprint = file_fingerprint(
        file_size=file_size,
        file_modify_time=file_modify_time,
        fileid=fileid,
    )
    if not history.status:
        count = failed_retry_count(
            getattr(history, "src", None),
            getattr(history, "src_storage", None),
            file_size=file_size,
            file_modify_time=file_modify_time,
            fileid=fileid,
        )
        if _is_file_version_changed(recorded_fingerprint, current_fingerprint):
            return f"失败记录 #{history.id}，文件版本已变化，重试预算将重置"
        return f"失败记录 #{history.id}，已重试 {count}/{max_failed_retries()} 次"
    recorded_size = recorded_fingerprint.get("size")
    current_size = current_fingerprint.get("size")
    if recorded_size is None and current_size is None:
        return f"成功记录 #{history.id}，大小不可比对"
    return f"成功记录 #{history.id}，大小 {recorded_size} -> {current_size}"


# --------------------------------------------------------------------------- #
# 整理历史的写入路径
#
# 这两个函数把 FileItem / MetaBase / MediaInfo / TransferInfo 四个领域对象翻译成
# 一行整理历史，是整理历史表的唯一写入口。它们此前由表级适配器承载，但
# 拼标题、拆季集、取海报、判音乐字段都是整理链的业务规则而非数据访问——Oper 只该
# 收敛查询，领域对象不该出现在它的入参里。搬到本模块与查重闸（读侧）作伴：同一张
# 表的读写规则放在一起，字段含义只有一处需要维护。
# --------------------------------------------------------------------------- #

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
    media_source, media_id = resolve_media_identity(media=mediainfo)
    return repository.replace(TransferHistoryWrite(
        src=history_projection.history_source_path(fileitem),
        src_storage=fileitem.storage,
        src_fileitem=fileitem.model_dump(),
        dest=transferinfo.target_item.path if transferinfo.target_item else None,
        dest_storage=transferinfo.target_item.storage if transferinfo.target_item else None,
        dest_fileitem=transferinfo.target_item.model_dump() if transferinfo.target_item else None,
        mode=mode,
        type=mediainfo.type.value,
        **history_projection.classification_fields(mediainfo),
        title=history_projection.history_title(meta, mediainfo),
        year=history_projection.history_year(mediainfo.year),
        media_source=media_source,
        media_id=media_id,
        music_type=getattr(mediainfo, "music_type", None),
        total_tracks=getattr(mediainfo, "total_tracks", None),
        audio_format=getattr(meta, "audio_format", None),
        audio_lossless=getattr(meta, "audio_lossless", None),
        bit_depth=getattr(meta, "bit_depth", None),
        sample_rate=getattr(meta, "sample_rate", None),
        bitrate=getattr(meta, "bitrate", None),
        seasons=meta.season,
        episodes=meta.episode,
        image=mediainfo.get_poster_image(),
        downloader=downloader,
        download_hash=download_hash,
        status=True,
        files=transferinfo.file_list,
    ))


def add_transfer_fail(
    fileitem: FileItem,
    mode: str,
    meta: MetaBase,
    mediainfo: Optional[Union[MediaInfo, MusicInfo]] = None,
    transferinfo: Optional[TransferInfo] = None,
    downloader: Optional[str] = None,
    download_hash: Optional[str] = None,
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
    :param transfer_history_oper: 兼容旧关键字的暂存端口，未传时使用组合根仓储
    :return: 落库后的整理记录
    """
    repository = transfer_history_oper or get_transfer_history_repository()
    if mediainfo and transferinfo:
        media_source, media_id = resolve_media_identity(media=mediainfo)
        history = repository.replace(TransferHistoryWrite(
            src=history_projection.history_source_path(fileitem),
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(),
            dest=transferinfo.target_item.path if transferinfo.target_item else None,
            dest_storage=transferinfo.target_item.storage if transferinfo.target_item else None,
            dest_fileitem=transferinfo.target_item.model_dump() if transferinfo.target_item else None,
            mode=mode,
            type=mediainfo.type.value,
            **history_projection.classification_fields(mediainfo),
            title=history_projection.history_title(meta, mediainfo),
            year=history_projection.history_year(mediainfo.year or meta.year),
            media_source=media_source,
            media_id=media_id,
            music_type=getattr(mediainfo, "music_type", None),
            total_tracks=getattr(mediainfo, "total_tracks", None),
            audio_format=getattr(meta, "audio_format", None),
            audio_lossless=getattr(meta, "audio_lossless", None),
            bit_depth=getattr(meta, "bit_depth", None),
            sample_rate=getattr(meta, "sample_rate", None),
            bitrate=getattr(meta, "bitrate", None),
            seasons=meta.season,
            episodes=meta.episode,
            image=mediainfo.get_poster_image(),
            downloader=downloader,
            download_hash=download_hash,
            episode_group=mediainfo.episode_group,
            status=False,
            errmsg=transferinfo.message or '未知错误',
            files=transferinfo.file_list,
        ))
    else:
        media_source, media_id = resolve_media_identity(media=meta)
        history = repository.replace(TransferHistoryWrite(
            type=meta.type.value if meta.type else None,
            title=history_projection.history_title(meta),
            year=history_projection.history_year(meta.year),
            media_source=media_source,
            media_id=media_id,
            music_type=MUSIC_ENTITY_RECORDING if isinstance(meta, MetaMusic) else None,
            audio_format=getattr(meta, "audio_format", None),
            audio_lossless=getattr(meta, "audio_lossless", None),
            bit_depth=getattr(meta, "bit_depth", None),
            sample_rate=getattr(meta, "sample_rate", None),
            bitrate=getattr(meta, "bitrate", None),
            src=history_projection.history_source_path(fileitem),
            src_storage=fileitem.storage,
            src_fileitem=fileitem.model_dump(),
            mode=mode,
            seasons=meta.season,
            episodes=meta.episode,
            downloader=downloader,
            download_hash=download_hash,
            status=False,
            errmsg="未识别到媒体信息",
        ))
    return history
