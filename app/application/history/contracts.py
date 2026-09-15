"""整理历史 DTO 与类型化仓储契约。"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, NoReturn, Optional, Protocol

from app.schemas.common import JsonData
from app.schemas.types import MediaSource


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
    failure_stage: Optional[str] = None
    recovery_action: Optional[str] = None
    retry_count: Optional[int] = None
    retry_exhausted: bool = False
    auto_paused: bool = False
    cleanup_status: Optional[str] = None
    cleanup_error: Optional[str] = None
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
    failure_stage: Optional[str] = None
    recovery_action: Optional[str] = None
    # 连续失败次数和自动暂停状态，写入历史后可跨重启恢复查重闸状态。
    retry_count: Optional[int] = None
    auto_paused: bool = False
    cleanup_status: Optional[str] = None
    cleanup_error: Optional[str] = None
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

    async def async_get_by_transfer_task_id(
        self,
        *,
        task_id: str,
    ) -> Optional[TransferHistorySnapshot]:
        """异步按 durable 整理任务标识返回终态历史快照。"""
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

    def update_cleanup_status(
            self,
            history_id: int,
            status: str,
            error: Optional[str] = None,
    ) -> None:
        """在独立事务中记录媒体入库后的下载器清理结果。"""
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
