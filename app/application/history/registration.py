"""
外部已完成整理的只读事实核验和不可覆盖登记用例。
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from math import isfinite
from pathlib import Path
from typing import Literal, Protocol
from unicodedata import normalize

from app.application.history.contracts import TransferHistorySnapshot
from app.application.history.mutation import HistoryUnitOfWork
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem
from app.schemas.history import TransferHistoryRegisterRequest, TransferRegistrationAudit
from app.schemas.types import MediaSource


class RegistrationValidationError(ValueError):
    """
    存储未提供足够一致的文件事实，不能登记成功历史。
    """


class RegistrationConflictError(ValueError):
    """
    相同源地址已经关联不同或非外部登记的历史，禁止覆盖。
    """


@dataclass(frozen=True, slots=True)
class RegistrationActor:
    """
    认证依赖提供的管理员身份。

    :param id: 已认证用户主键
    :param name: 已认证用户名
    """

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class LocalRegistrationEntry:
    """
    不跟随最终符号链接的本地文件和父目录事实。

    :param dev: 文件设备号
    :param ino: 文件节点号
    :param size: 文件字节数
    :param mtime_ns: 纳秒修改时间
    :param ctime_ns: 纳秒状态变化时间
    :param is_file: 是否为普通文件
    :param is_symlink: 最终目录项是否为符号链接
    :param parent_dev: 父目录设备号
    :param parent_ino: 父目录节点号
    :param canonical_parent: 仅解析父目录后的绝对地址
    :param name: 原请求的最终目录项名称
    """

    dev: int
    ino: int
    size: int
    mtime_ns: int
    ctime_ns: int
    is_file: bool
    is_symlink: bool
    parent_dev: int
    parent_ino: int
    canonical_parent: str
    name: str


@dataclass(frozen=True, slots=True)
class RegistrationFileSnapshot:
    """
    核验后允许持久化的文件最小快照，不携带访问地址和 provider 私有字段。

    :param path: 原声明的规范地址
    :param storage: 存储标识
    :param type: 确认存在时为 file，已移动的源保持 None
    :param size: 确认存在时的字节数
    :param fileid: 可选的有界存储文件标识
    :param modify_time: 可选的有限修改时间
    """

    path: str
    storage: str
    type: Literal["file"] | None = None
    size: int | None = None
    fileid: str | None = None
    modify_time: float | None = None

    def to_dict(self) -> dict[str, str | int | float]:
        """
        返回独立的有界 JSON 数据，源不存在时只包含地址。
        """
        return {key: value for key, value in asdict(self).items() if value is not None}


@dataclass(frozen=True, slots=True)
class TransferRegistrationDeclaration:
    """
    在任何 I/O 之前冻结管理员的完整声明，避免持久化阶段依赖可变请求模型。

    :param src: 规范源地址
    :param src_storage: 源存储
    :param dest: 规范目标地址
    :param dest_storage: 目标存储
    :param mode: 已完成的整理模式
    :param type: 电影或电视剧
    :param title: 管理员确认的媒体标题
    :param year: 可选发行年份
    :param media_source: 规范来源
    :param media_id: 来源原生标识
    :param seasons: 可选季号
    :param episodes: 可选集号
    :param download_hash: 可选下载任务标识
    """

    src: str
    src_storage: str
    dest: str
    dest_storage: str
    mode: Literal["copy", "link", "move"]
    type: Literal["电影", "电视剧"]
    title: str
    year: str | None
    media_source: MediaSource
    media_id: str
    seasons: str | None
    episodes: str | None
    download_hash: str | None

    @classmethod
    def from_request(cls, request: TransferHistoryRegisterRequest) -> "TransferRegistrationDeclaration":
        """
        显式复制已校验请求的标量，不保留可变请求引用。
        """
        return cls(
            src=request.src, src_storage=request.src_storage,
            dest=request.dest, dest_storage=request.dest_storage,
            mode=request.mode, type=request.type, title=request.title, year=request.year,
            media_source=request.media_source, media_id=request.media_id,
            seasons=request.seasons, episodes=request.episodes, download_hash=request.download_hash,
        )


@dataclass(frozen=True, slots=True)
class TransferRegistrationWrite:
    """
    通过文件事实核验后提交给原子持久端口的数据。

    :param request: 不可变的管理员媒体与地址声明
    :param source: 源文件已核验快照
    :param destination: 目标文件已核验快照
    :param audit: 服务端生成的不可变审计
    :param date: 兼容历史页面的本地时间文本
    """

    request: TransferRegistrationDeclaration
    source: RegistrationFileSnapshot
    destination: RegistrationFileSnapshot
    audit: TransferRegistrationAudit
    date: str


@dataclass(frozen=True, slots=True)
class TransferRegistrationResult:
    """
    已提交的外部登记回执。

    :param history: 脱离 Session 的不可变历史快照
    :param created: 是否由此次请求新增记录
    """

    history: TransferHistorySnapshot
    created: bool


class TransferRegistrationRepository(Protocol):
    """
    绑定短 Session 的外部登记原子读写端口。
    """

    def get_by_source(self, src: str, storage: str) -> TransferHistorySnapshot | None:
        """
        读取相同源地址的历史，供幂等和不可覆盖判断。

        :param src: 已规范化的绝对源地址
        :param storage: 对应存储配置标识
        :return: 脱离 Session 的冻结快照；没有记录时返回 None
        """
        ...

    def insert_if_absent(self, write: TransferRegistrationWrite) -> TransferRegistrationResult:
        """
        原子插入或返回唯一约束已保留的原历史，不提交事务。

        :param write: 应用用例已核验的声明、文件事实和服务端审计
        :return: 当前事务内的冻结记录及是否成功插入，由用例提交后才作为成功回执
        """
        ...


@dataclass(frozen=True, slots=True)
class TransferRegistrationScope:
    """
    单次短事务需要的仓储与工作单元。

    :param repository: 当前 Session 的登记仓储
    :param unit_of_work: 由用例决定提交或回滚的工作单元
    """

    repository: TransferRegistrationRepository
    unit_of_work: HistoryUnitOfWork


RegistrationScopeFactory = Callable[[], AbstractContextManager[TransferRegistrationScope]]
LocalEntryProbe = Callable[[str], LocalRegistrationEntry | None]
StrictFileQuery = Callable[[str, Path], FileItem | None]


def _actor_display_name(actor: RegistrationActor) -> str:
    """
    生成有界管理员名称显示快照，认证身份仍以完整 actor.id 为准。

    :param actor: 认证依赖提供的管理员，不改变既有账号名称规则
    :return: 普通名称原样保留；超长名称为前 127 字符加省略号，空名称为 #用户ID
    """
    if not actor.name:
        return f"#{actor.id}"
    if len(actor.name) > 128:
        return f"{actor.name[:127]}…"
    return actor.name


def _matching_receipt(
    history: TransferHistorySnapshot, request: TransferRegistrationDeclaration,
) -> TransferRegistrationResult:
    """
    只允许完整声明相同的既有外部成功记录作为重复回执。
    """
    if history.origin != "external" or not history.status or history.transfer_task_id:
        raise RegistrationConflictError("该源地址已有内部整理或失败历史，不能覆盖")
    if history.transfer_settlement_revision is not None or history.registration is None:
        raise RegistrationConflictError("该源地址的历史不属于可重复的外部登记")
    for key, value in asdict(request).items():
        if getattr(history, key) != value:
            raise RegistrationConflictError("该源地址已有不同目标或媒体声明的历史，不能覆盖")
    return TransferRegistrationResult(history, False)


def _snapshot(item: FileItem, storage: str, path: str) -> RegistrationFileSnapshot:
    """
    从严格 provider 回执投影已确认的正规文件，拒绝不完整或不一致的事实。
    """
    if item.storage != storage or item.path != path or item.type != "file":
        raise RegistrationValidationError("存储返回的文件地址或类型与登记声明不一致")
    if item.size is None or item.size < 0:
        raise RegistrationValidationError("存储未返回有效文件大小")
    if item.fileid is not None and len(item.fileid) > 512:
        raise RegistrationValidationError("存储返回的文件标识过长")
    if item.modify_time is not None and not isfinite(item.modify_time):
        raise RegistrationValidationError("存储返回的文件时间无效")
    return RegistrationFileSnapshot(
        path=path, storage=storage, type="file", size=item.size,
        fileid=item.fileid, modify_time=item.modify_time,
    )


def _check_local_item(entry: LocalRegistrationEntry | None, item: FileItem | None) -> None:
    """
    对照不跟随链接的事实和 provider 回执，拒绝链接或观察期间已变化的文件。
    """
    if entry is None:
        if item is not None:
            raise RegistrationValidationError("文件状态在核验期间发生变化，请重试")
        return
    if entry.is_symlink or not entry.is_file:
        raise RegistrationValidationError("外部登记只接受普通文件，不接受符号链接或目录")
    if item is None or item.size != entry.size:
        raise RegistrationValidationError("文件状态在核验期间发生变化，请重试")


def _distinct_entries(source: LocalRegistrationEntry, dest: LocalRegistrationEntry) -> None:
    """
    拒绝同一父目录的同名、大小写及 Unicode 规范等价别名，不把拼写差异当成硬链接。
    """
    same_parent = (source.parent_dev, source.parent_ino) == (dest.parent_dev, dest.parent_ino)
    source_name = normalize("NFC", normalize("NFC", source.name).casefold())
    dest_name = normalize("NFC", normalize("NFC", dest.name).casefold())
    if same_parent and source_name == dest_name:
        raise RegistrationValidationError("源与目标指向同一或大小写含糊的目录项")


class TransferRegistrationCommand:
    """
    核验外部整理事实并登记历史，完全不拥有文件变更或下载器副作用能力。
    """

    def __init__(
        self, *, scope_factory: RegistrationScopeFactory, query_file: StrictFileQuery,
        local_entry: LocalEntryProbe,
    ) -> None:
        """
        保存显式注入的短事务、严格存储查询和本地目录项只读能力。
        """
        self._scope_factory = scope_factory
        self._query_file = query_file
        self._local_entry = local_entry

    def _probe(self, storage: str, path: str) -> tuple[FileItem | None, LocalRegistrationEntry | None]:
        """
        用本地非跟随证明包围严格查询，只把已确认不存在投影为 None。
        """
        try:
            before = self._local_entry(path) if storage == "local" else None
            item = self._query_file(storage, Path(path))
            if storage == "local":
                after = self._local_entry(path)
                if before != after:
                    raise RegistrationValidationError("文件状态在核验期间发生变化，请重试")
                _check_local_item(after, item)
            return item, before
        except (OSError, StorageQueryError) as error:
            raise RegistrationValidationError("无法确认存储文件状态，请检查存储后重试") from error

    def _verify_files(
        self, request: TransferRegistrationDeclaration,
    ) -> tuple[RegistrationFileSnapshot, RegistrationFileSnapshot, Literal["matching_sizes", "native_hardlink", "destination_only"]]:
        """
        按复制、硬链接或移动分别验证事实；不推断内容校验和。
        """
        source, source_entry = self._probe(request.src_storage, request.src)
        dest, dest_entry = self._probe(request.dest_storage, request.dest)
        if dest is None:
            raise RegistrationValidationError("目标文件不存在，不能登记成功历史")
        destination = _snapshot(dest, request.dest_storage, request.dest)
        if request.mode == "move":
            if source is not None:
                raise RegistrationValidationError("移动登记要求源文件已经不存在")
            return RegistrationFileSnapshot(request.src, request.src_storage), destination, "destination_only"
        if source is None:
            raise RegistrationValidationError("源文件不存在，不能核验复制或硬链接")
        original = _snapshot(source, request.src_storage, request.src)
        if original.size != destination.size:
            raise RegistrationValidationError("源文件与目标文件大小不一致")
        if source_entry is not None and dest_entry is not None:
            _distinct_entries(source_entry, dest_entry)
        if request.mode == "link":
            self._verify_hardlink(source_entry, dest_entry)
            return original, destination, "native_hardlink"
        return original, destination, "matching_sizes"

    @staticmethod
    def _verify_hardlink(
        source: LocalRegistrationEntry | None, dest: LocalRegistrationEntry | None,
    ) -> None:
        """
        只有本地相同设备和节点的不同目录项才能证明 native 硬链接。
        """
        if source is None or dest is None:
            raise RegistrationValidationError("当前仅支持核验本地存储的原生硬链接")
        if (source.dev, source.ino) != (dest.dev, dest.ino):
            raise RegistrationValidationError("源文件与目标文件不是同一原生硬链接")

    def execute(
        self, request: TransferHistoryRegisterRequest, actor: RegistrationActor,
    ) -> TransferRegistrationResult:
        """
        先短读精确回执，再核验文件，最后用独立短写事务原子登记。

        :param request: 已通过入口校验的管理员声明，用例在 I/O 前冻结副本
        :param actor: 认证依赖提供的管理员身份
        :return: 事务提交后的新建或既有回执
        """
        declaration = TransferRegistrationDeclaration.from_request(request)
        with self._scope_factory() as scope:
            previous = scope.repository.get_by_source(declaration.src, declaration.src_storage)
            if previous is not None:
                return _matching_receipt(previous, declaration)
        source, destination, verification = self._verify_files(declaration)
        audit = TransferRegistrationAudit(
            actor_id=actor.id, actor_name=_actor_display_name(actor),
            registered_at=datetime.now(timezone.utc).isoformat(),
            verification=verification, source_state="absent" if declaration.mode == "move" else "present",
        )
        write = TransferRegistrationWrite(
            declaration, source, destination, audit, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        )
        with self._scope_factory() as scope:
            try:
                result = scope.repository.insert_if_absent(write)
                if not result.created:
                    result = _matching_receipt(result.history, declaration)
                scope.unit_of_work.commit()
                return result
            except Exception:
                scope.unit_of_work.rollback()
                raise
