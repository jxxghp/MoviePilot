"""
整理任务：整理链的进程内工作项。

TransferTask 此前住在 app/schemas/transfer.py，但它不是出网的 DTO——meta 装的是领域侧
的 MetaBase 子类，mediainfo 装的是领域侧的 MediaInfo / MusicInfo，都带行为而非纯数据。
放在 app.schemas 的代价是它没法命名自己真正装的类型：app.schemas 一旦 import 领域类型，
app.schemas -> app.schemas.transfer -> app.domain.* -> app.schemas.types -> app.schemas
就闭环，仓库自己的 test_migrated_modules_are_not_in_import_cycles 会红（已实测）。于是
两个字段只能标成 Optional[Any]，把「这里到底能放什么」这件事整个交给了口头约定。

搬到应用层就没有这个约束：app.application 允许依赖 app.domain 与 app.schemas，两个
字段因此能标出真实类型。它面向前端的投影仍是 app/schemas/transfer.py 里的
TransferJob / TransferJobTask，那两个用 app.schemas 的同名 DTO——一个是工作项，一个是
视图，分开表达之后两边都不必再迁就对方。
"""
import asyncio
import hashlib
import threading
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    List,
    Optional,
    Protocol,
    Tuple,
    TypeAlias,
    Union,
    cast,
)

from pydantic import BaseModel, ConfigDict, PrivateAttr

from app.application.history import DownloadHistorySnapshot
from app.application.transfer import checkpoint as checkpoint_codec
from app.application.transfer.execution import TransferExecutionCheckpoint
from app.domain.context import MediaInfo, MusicInfo
from app.domain.media import normalize_music_type
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.foundation import text as text_tools
from app.runtime.log import logger
from app.schemas.context import MediaInfo as _SchemaMediaInfo
from app.schemas.context import MetaInfo as _SchemaMetaInfo
from app.schemas.file import FileItem
from app.schemas.media import OptionalMediaIdentityMixin, resolve_media_identity
from app.schemas.music import MusicInfo as _SchemaMusicInfo
from app.schemas.music import MusicMeta as _SchemaMusicMeta
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo, TransferJob, TransferJobTask
from app.schemas.types import (
    MUSIC_ENTITY_ALBUM,
    MUSIC_ENTITY_RECORDING,
    MediaSource,
    MediaType,
)

if TYPE_CHECKING:
    class _ApplicationModel:
        """描述当前模块依赖的最小 Pydantic 模型类型形状。"""

        def __init__(self, **data: Any) -> None:
            """接受模型字段关键字参数。"""
            raise NotImplementedError

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            """返回模型字段字典。"""
            raise NotImplementedError
else:
    _ApplicationModel = BaseModel

JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]
JobId: TypeAlias = tuple[object, ...]
FileKey: TypeAlias = tuple[str, str]


class _DictionarySerializable(Protocol):
    """描述领域对象沿用的字典投影能力。"""

    def to_dict(self) -> dict[str, Any]:
        """返回领域对象的字典投影。"""


class DirectorySize(Protocol):
    """描述本地文件或目录大小读取能力。"""

    def get_directory_size(self, path: Path) -> int:
        """返回真实本地路径占用的字节数。"""
        ...


_directory_size: Optional[DirectorySize] = None


def configure_directory_size(reader: Optional[DirectorySize]) -> None:
    """由启动组合根注入或清除本地目录大小适配器。"""
    global _directory_size
    _directory_size = reader


def _domain_to_dict(value: object) -> dict[str, Any]:
    """按领域对象既有 ``to_dict`` 合同生成字典投影。"""
    return cast(_DictionarySerializable, value).to_dict()


def _job_tasks(job: TransferJob) -> list[TransferJobTask]:
    """声明进程内作业始终使用已初始化的任务列表。"""
    return cast(list[TransferJobTask], job.tasks)


def _job_task_fileitem(task: TransferJobTask) -> FileItem:
    """声明进程内作业任务始终绑定源文件。"""
    return cast(FileItem, task.fileitem)


def _job_task_size(task: TransferJobTask) -> int:
    """按既有本地目录回退规则返回已完成任务的文件大小。"""
    fileitem = _job_task_fileitem(task)
    if fileitem.size is not None:
        return fileitem.size
    if fileitem.storage == "local":
        if _directory_size is None:
            raise RuntimeError("本地目录大小能力尚未由启动组合根配置")
        return _directory_size.get_directory_size(Path(cast(str, fileitem.path)))
    return 0


def _transfer_task_meta(task: "TransferTask") -> MetaBase:
    """声明进入作业管理器的整理任务已经完成元数据解析。"""
    return cast(MetaBase, task.meta)

TRANSFER_ADMISSION_ACCEPTED = "accepted"
TRANSFER_ADMISSION_PROVIDER_PENDING = "provider_pending"
TRANSFER_ADMISSION_PLANNED = "planned"
TRANSFER_PLANNING_INPUT_VERSION = 1
TRANSFER_PLAN_CHECKPOINT_VERSION = 2
TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION = 1
TRANSFER_PROVIDER_INVOCATION_VERSION = 1


@dataclass(frozen=True, slots=True)
class TransferPlanningInput:
    """保存可跨重启重放的版本化整理规划输入。"""

    source_fileitem: dict[str, JSONValue]
    meta: Optional[dict[str, JSONValue]] = None
    mediainfo: Optional[dict[str, JSONValue]] = None
    target_directory: Optional[dict[str, JSONValue]] = None
    target_storage: Optional[str] = None
    target_path: Optional[str] = None
    requested_transfer_type: Optional[str] = None
    media_source: Optional[str] = None
    media_id: Optional[str] = None
    media_type: Optional[str] = None
    need_scrape: bool = False
    need_rename: bool = True
    need_notify: bool = True
    overwrite_mode: Optional[str] = None
    episodes_info: tuple[dict[str, JSONValue], ...] = ()
    preview: bool = False
    options: dict[str, JSONValue] = field(default_factory=dict)
    schema_version: int = TRANSFER_PLANNING_INPUT_VERSION

    def __post_init__(self) -> None:
        """拒绝不可恢复的输入版本或缺少源文件身份的快照。"""
        if self.schema_version != TRANSFER_PLANNING_INPUT_VERSION:
            raise ValueError(f"不支持的整理规划输入版本: {self.schema_version}")
        object.__setattr__(self, "source_fileitem", deepcopy(self.source_fileitem))
        object.__setattr__(self, "meta", checkpoint_codec.copy_json_mapping(self.meta))
        object.__setattr__(self, "mediainfo", checkpoint_codec.copy_json_mapping(self.mediainfo))
        object.__setattr__(
            self,
            "target_directory",
            checkpoint_codec.copy_json_mapping(self.target_directory),
        )
        object.__setattr__(
            self,
            "episodes_info",
            tuple(deepcopy(item) for item in self.episodes_info),
        )
        object.__setattr__(self, "options", deepcopy(self.options))
        storage = self.source_fileitem.get("storage")
        path = self.source_fileitem.get("path")
        if not isinstance(storage, str) or not storage or not isinstance(path, str) or not path:
            raise ValueError("整理规划输入缺少源文件存储或路径")
        checkpoint_codec.canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferPlanningInput":
        """从受版本约束的 JSON 对象恢复规划输入。"""
        if not isinstance(payload, dict):
            raise ValueError("整理规划输入必须是 JSON 对象")
        source_fileitem = checkpoint_codec.read_json_mapping(payload, "source_fileitem")
        if source_fileitem is None:
            raise ValueError("整理规划输入缺少 source_fileitem")
        return cls(
            source_fileitem=source_fileitem,
            meta=checkpoint_codec.read_json_mapping(payload, "meta"),
            mediainfo=checkpoint_codec.read_json_mapping(payload, "mediainfo"),
            target_directory=checkpoint_codec.read_json_mapping(payload, "target_directory"),
            target_storage=payload.get("target_storage"),
            target_path=payload.get("target_path"),
            requested_transfer_type=payload.get("requested_transfer_type"),
            media_source=payload.get("media_source"),
            media_id=payload.get("media_id"),
            media_type=payload.get("media_type"),
            need_scrape=payload.get("need_scrape", False),
            need_rename=payload.get("need_rename", True),
            need_notify=payload.get("need_notify", True),
            overwrite_mode=payload.get("overwrite_mode"),
            episodes_info=checkpoint_codec.read_json_tuple(payload, "episodes_info"),
            preview=payload.get("preview", False),
            options=checkpoint_codec.read_json_mapping(payload, "options") or {},
            schema_version=payload.get("schema_version", 0),
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """生成仅含 JSON 值且字段稳定的规划输入投影。"""
        return {
            "schema_version": self.schema_version,
            "source_fileitem": deepcopy(self.source_fileitem),
            "meta": checkpoint_codec.copy_json_mapping(self.meta),
            "mediainfo": checkpoint_codec.copy_json_mapping(self.mediainfo),
            "target_directory": checkpoint_codec.copy_json_mapping(self.target_directory),
            "target_storage": self.target_storage,
            "target_path": self.target_path,
            "requested_transfer_type": self.requested_transfer_type,
            "media_source": self.media_source,
            "media_id": self.media_id,
            "media_type": self.media_type,
            "need_scrape": self.need_scrape,
            "need_rename": self.need_rename,
            "need_notify": self.need_notify,
            "overwrite_mode": self.overwrite_mode,
            "episodes_info": [deepcopy(item) for item in self.episodes_info],
            "preview": self.preview,
            "options": deepcopy(self.options),
        }

    @property
    def fingerprint(self) -> str:
        """返回规范 JSON 的稳定 SHA-256 指纹。"""
        return hashlib.sha256(checkpoint_codec.canonical_json(self.to_payload()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TransferPlanItem:
    """描述规划后按序执行的一条叶子文件操作。"""

    sequence: int
    source_fileitem: dict[str, JSONValue]
    target_storage: str
    target_path: str
    action: str = "transfer"

    def __post_init__(self) -> None:
        """拒绝无法定位源文件或目标文件的计划项。"""
        object.__setattr__(self, "source_fileitem", deepcopy(self.source_fileitem))
        if self.sequence < 0:
            raise ValueError("整理计划项序号不能小于零")
        if not self.target_storage or not self.target_path or not self.action:
            raise ValueError("整理计划项缺少目标身份或动作")
        if not self.source_fileitem.get("storage") or not self.source_fileitem.get("path"):
            raise ValueError("整理计划项缺少源文件身份")
        checkpoint_codec.canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferPlanItem":
        """从 JSON 对象恢复单条叶子文件操作。"""
        source_fileitem = checkpoint_codec.read_json_mapping(payload, "source_fileitem")
        if source_fileitem is None:
            raise ValueError("整理计划项缺少 source_fileitem")
        return cls(
            sequence=payload.get("sequence", -1),
            source_fileitem=source_fileitem,
            target_storage=payload.get("target_storage", ""),
            target_path=payload.get("target_path", ""),
            action=payload.get("action", "transfer"),
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """生成单条叶子文件操作的 JSON 投影。"""
        return {
            "sequence": self.sequence,
            "source_fileitem": deepcopy(self.source_fileitem),
            "target_storage": self.target_storage,
            "target_path": self.target_path,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class TransferProviderReference:
    """保存无需依赖运行时 dispatcher 即可持久化的旧 transfer provider 引用。"""

    plugin_id: str
    plugin_name: str
    method: str = "transfer"

    def __post_init__(self) -> None:
        """拒绝无法稳定定位插件或指向非 transfer 方法的引用。"""
        if not isinstance(self.plugin_id, str) or not self.plugin_id.strip():
            raise ValueError("旧 transfer provider 的 plugin_id 必须是非空字符串")
        if not isinstance(self.plugin_name, str) or not self.plugin_name.strip():
            raise ValueError("旧 transfer provider 的 plugin_name 必须是非空字符串")
        if self.method != "transfer":
            raise ValueError("旧 transfer provider 的 method 必须是 transfer")
        checkpoint_codec.canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferProviderReference":
        """从 JSON 对象恢复旧 transfer provider 引用。"""
        if not isinstance(payload, dict):
            raise ValueError("旧 transfer provider 引用必须是 JSON 对象")
        return cls(
            plugin_id=payload.get("plugin_id", ""),
            plugin_name=payload.get("plugin_name", ""),
            method=payload.get("method", "transfer"),
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """生成仅包含稳定插件身份与方法名的 JSON 投影。"""
        return {
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class TransferProviderInvocationSnapshot:
    """冻结旧 transfer ABI 中可跨重启恢复的精确参数值。"""

    fileitem: dict[str, JSONValue]
    meta: Optional[dict[str, JSONValue]]
    meta_kind: Optional[str]
    mediainfo: Optional[dict[str, JSONValue]]
    mediainfo_kind: Optional[str]
    target_directory: Optional[dict[str, JSONValue]] = None
    target_storage: Optional[str] = None
    target_path: Optional[str] = None
    transfer_type: Optional[str] = None
    scrape: Optional[bool] = None
    library_type_folder: Optional[bool] = None
    library_category_folder: Optional[bool] = None
    episodes_info: tuple[dict[str, JSONValue], ...] = ()
    preview: bool = False
    schema_version: int = TRANSFER_PROVIDER_INVOCATION_VERSION

    def __post_init__(self) -> None:
        """拒绝缺少源身份、类型不稳定或版本未知的 provider 快照。"""
        if self.schema_version != TRANSFER_PROVIDER_INVOCATION_VERSION:
            raise ValueError(
                f"不支持的旧 transfer provider 调用快照版本: {self.schema_version}"
            )
        object.__setattr__(self, "fileitem", deepcopy(self.fileitem))
        object.__setattr__(self, "meta", checkpoint_codec.copy_json_mapping(self.meta))
        object.__setattr__(self, "mediainfo", checkpoint_codec.copy_json_mapping(self.mediainfo))
        object.__setattr__(
            self,
            "target_directory",
            checkpoint_codec.copy_json_mapping(self.target_directory),
        )
        object.__setattr__(
            self,
            "episodes_info",
            tuple(deepcopy(item) for item in self.episodes_info),
        )
        if not self.fileitem.get("storage") or not self.fileitem.get("path"):
            raise ValueError("旧 transfer provider 调用快照缺少源文件身份")
        for kind in (self.meta_kind, self.mediainfo_kind):
            if kind is not None and (not isinstance(kind, str) or not kind):
                raise ValueError("旧 transfer provider 调用快照类型必须是非空字符串")
        for value in (
                self.target_storage,
                self.target_path,
                self.transfer_type,
        ):
            if value is not None and not isinstance(value, str):
                raise ValueError("旧 transfer provider 可选文本参数必须是字符串或 None")
        for bool_value in (
                self.scrape,
                self.library_type_folder,
                self.library_category_folder,
        ):
            if bool_value is not None and not isinstance(bool_value, bool):
                raise ValueError("旧 transfer provider 可选布尔参数必须是 bool 或 None")
        if not isinstance(self.preview, bool):
            raise ValueError("旧 transfer provider preview 参数必须是 bool")
        checkpoint_codec.canonical_json(self.to_payload())

    @classmethod
    def from_payload(
            cls,
            payload: dict[str, Any],
    ) -> "TransferProviderInvocationSnapshot":
        """从版本化 JSON 对象严格恢复旧 transfer ABI 调用快照。"""
        if not isinstance(payload, dict):
            raise ValueError("旧 transfer provider 调用快照必须是 JSON 对象")
        fileitem = checkpoint_codec.read_json_mapping(payload, "fileitem")
        if fileitem is None:
            raise ValueError("旧 transfer provider 调用快照缺少 fileitem")
        return cls(
            fileitem=fileitem,
            meta=checkpoint_codec.read_json_mapping(payload, "meta"),
            meta_kind=payload.get("meta_kind"),
            mediainfo=checkpoint_codec.read_json_mapping(payload, "mediainfo"),
            mediainfo_kind=payload.get("mediainfo_kind"),
            target_directory=checkpoint_codec.read_json_mapping(payload, "target_directory"),
            target_storage=payload.get("target_storage"),
            target_path=payload.get("target_path"),
            transfer_type=payload.get("transfer_type"),
            scrape=payload.get("scrape"),
            library_type_folder=payload.get("library_type_folder"),
            library_category_folder=payload.get("library_category_folder"),
            episodes_info=checkpoint_codec.read_json_tuple(payload, "episodes_info"),
            preview=payload.get("preview", False),
            schema_version=payload.get("schema_version", 0),
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """生成仅含 JSON 值且保留 None 与 False 差异的调用投影。"""
        return {
            "schema_version": self.schema_version,
            "fileitem": deepcopy(self.fileitem),
            "meta": checkpoint_codec.copy_json_mapping(self.meta),
            "meta_kind": self.meta_kind,
            "mediainfo": checkpoint_codec.copy_json_mapping(self.mediainfo),
            "mediainfo_kind": self.mediainfo_kind,
            "target_directory": checkpoint_codec.copy_json_mapping(self.target_directory),
            "target_storage": self.target_storage,
            "target_path": self.target_path,
            "transfer_type": self.transfer_type,
            "scrape": self.scrape,
            "library_type_folder": self.library_type_folder,
            "library_category_folder": self.library_category_folder,
            "episodes_info": [deepcopy(item) for item in self.episodes_info],
            "preview": self.preview,
        }


@dataclass(frozen=True, slots=True)
class TransferPlanCheckpoint:
    """保存无需重触发识别或重命名即可执行的完整有序计划。"""

    planning_input: TransferPlanningInput
    target_storage: str
    root_target_path: str
    final_target_path: str
    resolved_transfer_type: str
    items: tuple[TransferPlanItem, ...]
    classification_snapshot: checkpoint_codec.EffectiveClassificationSnapshot = field(
        default_factory=checkpoint_codec.EffectiveClassificationSnapshot
    )
    resolved_meta: Optional[dict[str, JSONValue]] = None
    resolved_meta_kind: Optional[str] = None
    resolved_mediainfo: Optional[dict[str, JSONValue]] = None
    resolved_mediainfo_kind: Optional[str] = None
    resolved_episodes_info: tuple[dict[str, JSONValue], ...] = ()
    legacy_transfer_providers: tuple[TransferProviderReference, ...] = ()
    provider_invocation: Optional[TransferProviderInvocationSnapshot] = None
    pre_execution_cleanup_completed: bool = False
    need_scrape: bool = False
    need_rename: bool = False
    need_notify: bool = True
    overwrite_mode: Optional[str] = None
    preview: bool = False
    skip_reason: Optional[str] = None
    rejection_error: Optional[str] = None
    schema_version: int = TRANSFER_PLAN_CHECKPOINT_VERSION

    def __post_init__(self) -> None:
        """验证版本、目标身份和计划项顺序组成完整检查点。"""
        if self.schema_version not in {
            TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION,
            TRANSFER_PLAN_CHECKPOINT_VERSION,
        }:
            raise ValueError(f"不支持的整理计划检查点版本: {self.schema_version}")
        if not isinstance(
            self.classification_snapshot,
            checkpoint_codec.EffectiveClassificationSnapshot,
        ):
            raise ValueError("整理计划分类快照必须使用类型化对象")
        if not isinstance(self.pre_execution_cleanup_completed, bool):
            raise ValueError("整理计划预执行 cleanup 完成标记必须是 bool")
        object.__setattr__(self, "resolved_meta", checkpoint_codec.copy_json_mapping(self.resolved_meta))
        object.__setattr__(
            self,
            "resolved_mediainfo",
            checkpoint_codec.copy_json_mapping(self.resolved_mediainfo),
        )
        object.__setattr__(
            self,
            "resolved_episodes_info",
            tuple(deepcopy(item) for item in self.resolved_episodes_info),
        )
        if not isinstance(self.legacy_transfer_providers, tuple) or any(
                not isinstance(provider, TransferProviderReference)
                for provider in self.legacy_transfer_providers
        ):
            raise ValueError("整理计划的旧 transfer provider 引用必须是类型化元组")
        provider_ids = tuple(
            provider.plugin_id for provider in self.legacy_transfer_providers
        )
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("整理计划的旧 transfer provider plugin_id 不能重复")
        for resolved_kind in (
                self.resolved_meta_kind,
                self.resolved_mediainfo_kind,
        ):
            if resolved_kind is not None and (
                    not isinstance(resolved_kind, str) or not resolved_kind
            ):
                raise ValueError("整理计划的已解析上下文类型必须是非空字符串")
        if self.provider_invocation is not None:
            if not self.legacy_transfer_providers:
                raise ValueError("provider_pending 检查点缺少冻结 provider")
            if self.pre_execution_cleanup_completed:
                raise ValueError("provider_pending 提交时 cleanup 尚未执行")
            if (
                    not self.provider_invocation.meta
                    or not self.provider_invocation.meta_kind
                    or not self.provider_invocation.mediainfo
                    or not self.provider_invocation.mediainfo_kind
            ):
                raise ValueError("provider_pending 检查点缺少可重放的媒体上下文")
            if (
                    self.target_storage
                    or self.root_target_path
                    or self.final_target_path
                    or self.resolved_transfer_type
                    or self.items
                    or self.skip_reason
                    or self.rejection_error
            ):
                raise ValueError("provider_pending 检查点不得包含宿主执行计划")
        else:
            if not self.target_storage or not self.root_target_path or not self.final_target_path:
                raise ValueError("整理计划检查点缺少目标身份")
            if not self.resolved_transfer_type:
                raise ValueError("整理计划检查点缺少已解析的整理方式")
        if self.rejection_error is not None and (
                not isinstance(self.rejection_error, str)
                or not self.rejection_error.strip()
                or self.items
        ):
            raise ValueError("整理拒绝检查点必须包含非空错误且不得包含文件步骤")
        if tuple(item.sequence for item in self.items) != tuple(range(len(self.items))):
            raise ValueError("整理计划项必须按从零开始的连续序号保存")
        if (
                self.provider_invocation is None
                and not self.items
                and not self.preview
                and not self.skip_reason
                and not self.rejection_error
        ):
            raise ValueError("非预览空计划必须记录合法跳过原因")
        checkpoint_codec.canonical_json(self.to_payload())

    @property
    def is_provider_pending(self) -> bool:
        """返回该检查点是否只冻结 provider 调用、尚未生成宿主计划。"""
        return self.provider_invocation is not None

    @property
    def fingerprint(self) -> str:
        """返回完整冻结计划规范 JSON 的稳定 SHA-256 指纹。"""
        return hashlib.sha256(
            checkpoint_codec.canonical_json(self.to_payload()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferPlanCheckpoint":
        """从受版本约束的 JSON 对象恢复完整执行检查点。"""
        if not isinstance(payload, dict):
            raise ValueError("整理计划检查点必须是 JSON 对象")
        planning_input_payload = checkpoint_codec.read_json_mapping(payload, "planning_input")
        if planning_input_payload is None:
            raise ValueError("整理计划检查点缺少 planning_input")
        item_payloads = payload.get("items", [])
        if not isinstance(item_payloads, list) or not all(
                isinstance(item, dict) for item in item_payloads
        ):
            raise ValueError("整理计划检查点 items 必须是 JSON 对象数组")
        legacy_provider_payloads = payload.get("legacy_transfer_providers", [])
        if not isinstance(legacy_provider_payloads, list):
            raise ValueError(
                "整理计划检查点 legacy_transfer_providers 必须是 JSON 对象数组"
            )
        provider_invocation_payload = payload.get("provider_invocation")
        if provider_invocation_payload is not None and not isinstance(
                provider_invocation_payload,
                dict,
        ):
            raise ValueError("整理计划检查点 provider_invocation 必须是 JSON 对象")
        schema_version = payload.get("schema_version", 0)
        if schema_version == TRANSFER_PLAN_CHECKPOINT_VERSION:
            classification_payload = payload.get("classification_snapshot")
            classification_snapshot = checkpoint_codec.read_classification_snapshot(
                classification_payload,
                require_all_fields=True,
            )
        elif schema_version == TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION:
            classification_snapshot = checkpoint_codec.legacy_classification_snapshot(payload)
        else:
            raise ValueError(f"不支持的整理计划检查点版本: {schema_version}")
        return cls(
            planning_input=TransferPlanningInput.from_payload(planning_input_payload),
            target_storage=payload.get("target_storage", ""),
            root_target_path=payload.get("root_target_path", ""),
            final_target_path=payload.get("final_target_path", ""),
            resolved_transfer_type=payload.get("resolved_transfer_type", ""),
            items=tuple(TransferPlanItem.from_payload(item) for item in item_payloads),
            classification_snapshot=classification_snapshot,
            resolved_meta=checkpoint_codec.read_json_mapping(payload, "resolved_meta"),
            resolved_meta_kind=payload.get("resolved_meta_kind"),
            resolved_mediainfo=checkpoint_codec.read_json_mapping(
                payload,
                "resolved_mediainfo",
            ),
            resolved_mediainfo_kind=payload.get("resolved_mediainfo_kind"),
            resolved_episodes_info=checkpoint_codec.read_json_tuple(
                payload,
                "resolved_episodes_info",
            ),
            legacy_transfer_providers=tuple(
                TransferProviderReference.from_payload(provider)
                for provider in legacy_provider_payloads
            ),
            provider_invocation=(
                TransferProviderInvocationSnapshot.from_payload(
                    provider_invocation_payload
                )
                if provider_invocation_payload is not None
                else None
            ),
            pre_execution_cleanup_completed=payload.get(
                "pre_execution_cleanup_completed",
                False,
            ),
            need_scrape=payload.get("need_scrape", False),
            need_rename=payload.get("need_rename", False),
            need_notify=payload.get("need_notify", True),
            overwrite_mode=payload.get("overwrite_mode"),
            preview=payload.get("preview", False),
            skip_reason=payload.get("skip_reason"),
            rejection_error=payload.get("rejection_error"),
            schema_version=schema_version,
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """生成可原子落库的完整版本化 JSON 检查点。"""
        payload: dict[str, JSONValue] = {
            "schema_version": self.schema_version,
            "planning_input": self.planning_input.to_payload(),
            "target_storage": self.target_storage,
            "root_target_path": self.root_target_path,
            "final_target_path": self.final_target_path,
            "resolved_transfer_type": self.resolved_transfer_type,
            "items": [item.to_payload() for item in self.items],
            "resolved_meta": checkpoint_codec.copy_json_mapping(self.resolved_meta),
            "resolved_meta_kind": self.resolved_meta_kind,
            "resolved_mediainfo": checkpoint_codec.copy_json_mapping(
                self.resolved_mediainfo
            ),
            "resolved_mediainfo_kind": self.resolved_mediainfo_kind,
            "resolved_episodes_info": [
                deepcopy(item) for item in self.resolved_episodes_info
            ],
            "legacy_transfer_providers": [
                provider.to_payload()
                for provider in self.legacy_transfer_providers
            ],
            "provider_invocation": (
                self.provider_invocation.to_payload()
                if self.provider_invocation
                else None
            ),
            "pre_execution_cleanup_completed": (
                self.pre_execution_cleanup_completed
            ),
            "need_scrape": self.need_scrape,
            "need_rename": self.need_rename,
            "need_notify": self.need_notify,
            "overwrite_mode": self.overwrite_mode,
            "preview": self.preview,
            "skip_reason": self.skip_reason,
            "rejection_error": self.rejection_error,
        }
        if self.schema_version == TRANSFER_PLAN_CHECKPOINT_VERSION:
            payload["classification_snapshot"] = checkpoint_codec.classification_snapshot_payload(
                self.classification_snapshot
            )
        return payload


class TransferAdmissionConflictError(ValueError):
    """同一源文件以不同规划输入重复准入时抛出的冲突错误。"""


class TransferPlanningStateError(RuntimeError):
    """计划检查点无法从当前持久状态推进时抛出的状态错误。"""


class TransferAdmissionProjectionError(RuntimeError):
    """持久登记无法安全恢复为应用层整理准入投影时抛出的错误。"""


class TransferLeaseLostError(TransferPlanningStateError):
    """整理 worker 已失去持久租约、不得继续推进任务时抛出的错误。"""


class TransferTask(OptionalMediaIdentityMixin, _ApplicationModel):
    """
    文件整理任务。
    """

    # MetaBase 与 MediaInfo / MusicInfo 都是普通类而非 BaseModel，pydantic 需要显式放行
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fileitem: FileItem
    meta: Optional[MetaBase] = None
    mediainfo: Optional[Union[MusicInfo, MediaInfo]] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    mtype: Optional[MediaType] = None
    target_directory: Optional[TransferDirectoryConf] = None
    target_storage: Optional[str] = None
    target_path: Optional[Path] = None
    transfer_type: Optional[str] = None
    scrape: Optional[bool] = False
    library_type_folder: Optional[bool] = False
    library_category_folder: Optional[bool] = False
    episodes_info: Optional[List[TmdbEpisode]] = None
    username: Optional[str] = None
    downloader: Optional[str] = None
    download_hash: Optional[str] = None
    download_history: Optional[DownloadHistorySnapshot] = None
    transfer_batch_id: Optional[str] = None
    manual: Optional[bool] = False
    background: Optional[bool] = True
    preview: Optional[bool] = False
    _admission_task_id: Optional[str] = PrivateAttr(default=None)
    _planning_input: Optional[TransferPlanningInput] = PrivateAttr(default=None)
    _plan_checkpoint: Optional[TransferPlanCheckpoint] = PrivateAttr(default=None)
    _execution_checkpoint: Optional[TransferExecutionCheckpoint] = PrivateAttr(default=None)
    _terminal_settled: bool = PrivateAttr(default=False)
    _planning_context_restored: bool = PrivateAttr(default=False)
    _lease_owner: Optional[str] = PrivateAttr(default=None)
    _lease_token: Optional[str] = PrivateAttr(default=None)

    @property
    def admission_task_id(self) -> Optional[str]:
        """返回仅供宿主持久准入和终态结算使用的内部任务标识。"""
        return self._admission_task_id

    def bind_admission_task_id(self, task_id: str) -> None:
        """绑定持久准入生成的稳定身份，不改变插件可见序列化字段。"""
        self._admission_task_id = task_id

    @property
    def planning_input(self) -> Optional[TransferPlanningInput]:
        """返回宿主恢复规划使用的内部输入快照。"""
        return self._planning_input

    @property
    def plan_checkpoint(self) -> Optional[TransferPlanCheckpoint]:
        """返回宿主直接执行已规划任务使用的内部检查点。"""
        return self._plan_checkpoint

    def bind_planning_input(self, planning_input: TransferPlanningInput) -> None:
        """绑定持久规划输入，不改变插件可见序列化字段。"""
        self._planning_input = planning_input

    def bind_plan_checkpoint(self, checkpoint: TransferPlanCheckpoint) -> None:
        """绑定持久执行检查点，不改变插件可见序列化字段。"""
        self._plan_checkpoint = checkpoint

    @property
    def execution_checkpoint(self) -> Optional[TransferExecutionCheckpoint]:
        """返回仅供宿主终态结算使用的内部执行检查点。"""
        return self._execution_checkpoint

    def bind_execution_checkpoint(
            self, checkpoint: TransferExecutionCheckpoint
    ) -> None:
        """绑定执行结果检查点，不改变插件可见的旧任务序列化字段。"""
        self._execution_checkpoint = checkpoint

    @property
    def terminal_settled(self) -> bool:
        """返回历史、事件与 pending 是否已由同一 UoW 提交。"""
        return self._terminal_settled

    def mark_terminal_settled(self) -> None:
        """仅在 task-aware writer 成功返回后标记终态已经提交。"""
        self._terminal_settled = True

    @property
    def planning_context_restored(self) -> bool:
        """返回当前领域上下文是否来自持久快照。"""
        return self._planning_context_restored

    def mark_planning_context_restored(self) -> None:
        """标记领域上下文已离线恢复，禁止旧流程再次在线补充。"""
        self._planning_context_restored = True

    @property
    def lease_owner(self) -> Optional[str]:
        """返回当前任务绑定的进程级 worker owner。"""
        return self._lease_owner

    @property
    def lease_token(self) -> Optional[str]:
        """返回当前任务绑定的持久租约令牌。"""
        return self._lease_token

    def bind_execution_lease(self, *, owner_id: str, lease_token: str) -> None:
        """绑定持久 claim 结果，且不改变插件可见的旧任务序列化字段。"""
        if not owner_id or not lease_token:
            raise ValueError("整理执行租约缺少 owner 或 token")
        self._lease_owner = owner_id
        self._lease_token = lease_token

    def to_dict(self) -> dict[str, Any]:
        """
        返回字典。

        meta 与 mediainfo 用 to_dict() 而非 model_dump()：它们是领域对象，没有
        model_dump。此前这里写的是 model_dump()，仓内无人调用才一直没炸——字段类型
        标成 Any 时，这种错配静态检查也看不出来。
        """
        dicts = vars(self).copy()
        dicts["fileitem"] = self.fileitem.model_dump() if self.fileitem else None
        dicts["meta"] = _domain_to_dict(self.meta) if self.meta else None
        dicts["mediainfo"] = _domain_to_dict(self.mediainfo) if self.mediainfo else None
        dicts["target_directory"] = self.target_directory.model_dump() if self.target_directory else None
        return dicts


TransferCallback: TypeAlias = Callable[[TransferTask, TransferInfo], tuple[bool, str]]


class TransferQueue(_ApplicationModel):
    """
    异步整理队列信息。

    和 TransferTask 一起从 app/schemas 搬来：它装着一个 TransferTask 和一个回调函数，
    回调根本不可序列化，因此从来就不是 DTO，只是恰好和视图模型住在同一个文件里。
    """
    # 任务信息
    task: Optional[TransferTask] = None
    # 回调函数
    callback: Optional[TransferCallback] = None
    # 整理结果
    result: Optional[TransferInfo] = None


@dataclass(frozen=True, slots=True)
class TransferAdmission:
    """描述已经持久化、可在进程退出后恢复的整理任务准入事实。"""

    task_id: str
    storage: str
    src_path: str
    state: str
    created_at: str
    updated_at: str
    planning_input: TransferPlanningInput
    last_error: Optional[str] = None
    input_fingerprint: Optional[str] = None
    checkpoint: Optional[TransferPlanCheckpoint] = None
    lease_owner: Optional[str] = None
    lease_token: Optional[str] = None
    lease_expires_at: Optional[str] = None
    heartbeat_at: Optional[str] = None
    attempt_count: int = 0


class TransferAdmissionRepository(Protocol):
    """整理任务 durable admission 所需的类型化持久化端口。"""

    def admit(
            self,
            *,
            storage: str,
            src_path: str,
            planning_input: TransferPlanningInput,
    ) -> TransferAdmission:
        """按规划输入幂等登记源文件并返回稳定任务身份。"""
        ...

    def record_enqueue_failure(self, *, task_id: str, error: str) -> None:
        """记录内存队列接收失败，保留任务供后续恢复。"""
        ...

    def checkpoint_plan(
            self,
            *,
            task_id: str,
            lease_token: str,
            input_fingerprint: str,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferAdmission:
        """按有效租约原子保存完整计划并推进匹配输入的任务。"""
        ...

    def record_planning_failure(
            self, *, task_id: str, lease_token: str, error: str
    ) -> None:
        """按有效租约记录规划失败，保留业务状态供恢复重试。"""
        ...

    def claim_task(
            self,
            *,
            task_id: str,
            owner_id: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """为指定任务取得唯一执行租约；已有有效租约时返回 None。"""
        ...

    def claim_recoverable(
            self,
            *,
            owner_id: str,
            limit: int,
            lease_seconds: int,
    ) -> list[TransferAdmission]:
        """按登记顺序原子取得可恢复任务的唯一执行租约。"""
        ...

    def heartbeat(
            self,
            *,
            task_id: str,
            lease_token: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """仅为当前且未过期的 token 延长租约；过期 token 不得复活。"""
        ...

    def release_claim(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: Optional[str] = None,
    ) -> bool:
        """按 token 释放当前 claim，并可记录可恢复错误。"""
        ...

    def abandon_unstarted(self, *, task_id: str, lease_token: str) -> int:
        """仅允许当前 lease owner 删除确认未开始且源已消失的登记。"""
        ...


class TransferQueueService:
    """协调整理任务登记、入队、移除和队列视图查询。"""

    def __init__(
            self,
            *,
            register_task: Callable[[TransferTask], bool],
            admit_task: Callable[[TransferTask], TransferAdmission],
            enqueue: Callable[[TransferQueue], None],
            before_enqueue: Callable[[TransferTask], None],
            enqueue_failed: Callable[[TransferTask, Exception], None],
            remove_task: Callable[[FileItem], None],
            list_tasks: Callable[[], List[TransferJob]],
            expire_tasks: Callable[[], None],
    ) -> None:
        """保存队列用例依赖，避免 Application 服务绑定具体线程队列实现。"""
        self._register_task = register_task
        self._admit_task = admit_task
        self._enqueue = enqueue
        self._before_enqueue = before_enqueue
        self._enqueue_failed = enqueue_failed
        self._remove_task = remove_task
        self._list_tasks = list_tasks
        self._expire_tasks = expire_tasks

    def put(self, task: TransferTask, callback: TransferCallback) -> bool:
        """先持久化准入事实再入队；任何前置失败都撤销内存作业视图。"""
        if not task or not self._register_task(task):
            return False
        try:
            admission = self._admit_task(task)
            task.bind_admission_task_id(admission.task_id)
        except Exception:
            self._remove_task(task.fileitem)
            raise
        try:
            self._before_enqueue(task)
            self._enqueue(TransferQueue(task=task, callback=callback))
        except Exception as err:
            try:
                self._enqueue_failed(task, err)
            finally:
                self._remove_task(task.fileitem)
            raise
        return True

    def remove(self, fileitem: FileItem) -> None:
        """从整理任务视图移除指定文件。"""
        if fileitem:
            self._remove_task(fileitem)

    def list(self) -> List[TransferJob]:
        """先处理失活任务，再返回当前整理作业视图。"""
        self._expire_tasks()
        return self._list_tasks()


@dataclass(frozen=True, slots=True)
class TransferFailureNotification:
    """整理失败聚合器保存的单条通知快照。"""

    media_title: str
    season_episode: str
    reason: str
    history_id: Optional[int]
    image: Optional[str]
    username: Optional[str]
    manual_identity: bool = False


def build_transfer_failure_group_key(task: TransferTask) -> str:
    """构造主程序和第三方整理路径可共同使用的失败通知分组键。"""
    media_source, media_id = resolve_media_identity(media=task.mediainfo)
    if not media_source or not media_id:
        media_source, media_id = resolve_media_identity(media=task)
    season = getattr(task.meta, "begin_season", None) if task.meta else None
    username = task.username or ""
    if media_source and media_id:
        return f"media:{media_source}:{media_id}:season:{season}:user:{username}"
    if task.download_hash:
        return f"download:{task.download_hash}:user:{username}"
    source_path = str(task.fileitem.path) if task.fileitem else ""
    parent_path = str(Path(source_path).parent) if source_path else ""
    return f"path:{parent_path or source_path}:user:{username}"


class TransferFailureNotificationAggregator:
    """在短暂静默窗口内按媒体合并整理失败通知。"""

    NOTIFICATION_DEBOUNCE_SECONDS = 30

    def __init__(self) -> None:
        """初始化分组缓冲、回调、定时器与关闭状态。"""
        self._buffers: dict[str, list[TransferFailureNotification]] = {}
        self._callbacks: dict[
            str,
            Callable[[list[TransferFailureNotification]], None],
        ] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._generations: dict[str, int] = {}
        self._lock = threading.Lock()
        self._closed = False

    def schedule(
            self,
            *,
            group_key: str,
            notification: TransferFailureNotification,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """从整理线程安全地把失败快照加入事件循环中的聚合缓冲。"""
        # 先在调用线程登记快照，关闭流程才能覆盖已接收但尚未进入事件循环的通知。
        with self._lock:
            if self._closed:
                raise RuntimeError("整理失败通知聚合器正在关闭，不能再接收通知")
            self._buffers.setdefault(group_key, []).append(notification)
            self._callbacks[group_key] = callback
            generation = self._generations.get(group_key, 0) + 1
            self._generations[group_key] = generation
        try:
            loop.call_soon_threadsafe(
                self._schedule_on_loop,
                group_key,
                generation,
                callback,
                loop,
            )
        except Exception as err:
            logger.error(
                f"创建整理失败通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {err}"
            )
            self.flush(group_key, generation, callback)

    def _schedule_on_loop(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[TransferFailureNotification]], None],
            loop: asyncio.AbstractEventLoop,
    ) -> None:
        """在所属事件循环中为已登记缓冲重置静默窗口。"""
        schedule_error: Optional[Exception] = None
        with self._lock:
            if (
                    self._closed
                    or group_key not in self._buffers
                    or self._generations.get(group_key) != generation
            ):
                return
            timer = self._timers.pop(group_key, None)
            if timer:
                timer.cancel()
            try:
                self._timers[group_key] = loop.call_later(
                    self.NOTIFICATION_DEBOUNCE_SECONDS,
                    self.flush,
                    group_key,
                    generation,
                    callback,
                )
            except Exception as err:
                schedule_error = err
        if schedule_error is not None:
            logger.error(
                f"创建整理失败通知聚合定时器失败，将立即发送 "
                f"(group={group_key}): {schedule_error}"
            )
            self.flush(group_key, generation, callback)

    def flush(
            self,
            group_key: str,
            generation: int,
            callback: Callable[[list[TransferFailureNotification]], None],
    ) -> None:
        """发送一个分组内的聚合结果并释放缓冲。"""
        with self._lock:
            # 新通知已登记但 timer 重置回调尚未执行时，旧代不得提前发送新批次。
            if self._generations.get(group_key) != generation:
                return
            notifications = self._buffers.pop(group_key, [])
            self._callbacks.pop(group_key, None)
            timer = self._timers.pop(group_key, None)
            self._generations.pop(group_key, None)
        if timer:
            timer.cancel()
        if not notifications:
            return
        self._deliver(group_key, notifications, callback)

    @staticmethod
    def _deliver(
            group_key: str,
            notifications: list[TransferFailureNotification],
            callback: Callable[[list[TransferFailureNotification]], None],
    ) -> None:
        """调用聚合通知回调，并统一观察发送异常。"""
        try:
            callback(notifications)
        except Exception as err:
            logger.error(f"发送整理失败聚合通知失败 (group={group_key}): {err}")

    def close(self) -> None:
        """停止接收新通知，取消定时器并同步发送全部已缓冲通知。"""
        with self._lock:
            if self._closed and not self._buffers and not self._timers:
                return
            self._closed = True
            timers = list(self._timers.values())
            pending = []
            orphaned = []
            for group_key, notifications in self._buffers.items():
                callback = self._callbacks.get(group_key)
                if callback is None:
                    orphaned.append((group_key, len(notifications)))
                    continue
                pending.append((group_key, notifications, callback))
            self._timers.clear()
            self._generations.clear()
            self._buffers.clear()
            self._callbacks.clear()

        for timer in timers:
            timer.cancel()
        for group_key, notification_count in orphaned:
            logger.error(
                f"整理失败通知聚合缓冲缺少发送回调，无法刷新 "
                f"(group={group_key}, count={notification_count})"
            )
        for group_key, notifications, callback in pending:
            self._deliver(group_key, notifications, callback)


# 作业锁：JobManager 与 TransferChain 共享，保护整理作业视图。
job_lock = threading.Lock()

class JobManager:
    """
    作业管理器
    task任务负责一个文件的整理，job作业负责一个媒体的整理
    """

    # 整理中的作业
    _job_view: Dict[JobId, TransferJob] = {}
    # 汇总季集清单
    _season_episodes: Dict[JobId, List[int]] = {}
    # 记录从 meta 作业迁移到 media 作业的关系，用于清理提前失败后残留的 media 作业
    _meta_to_media_ids: Dict[JobId, set[JobId]] = {}
    # 记录任务最近一次状态心跳，供外部异步接管任务的失活检测使用
    _task_state_changed_at: Dict[FileKey, float] = {}
    # 记录仍由主程序整理线程直接执行的任务，避免把阻塞中的本地任务误判为失活
    _active_executions: set[FileKey] = set()

    def __init__(self) -> None:
        """初始化当前进程内的整理作业状态。"""
        self._job_view = {}
        self._season_episodes = {}
        self._meta_to_media_ids = {}
        self._task_state_changed_at = {}
        self._active_executions = set()

    @staticmethod
    def __get_meta_id(
            meta: Optional[MetaBase] = None,
            season: Optional[int] = None,
    ) -> JobId:
        """
        获取元数据ID
        """
        return cast(MetaBase, meta).name, season

    @staticmethod
    def __get_media_id(media: Optional[Union[MediaInfo, MusicInfo]] = None,
                       season: Optional[int] = None) -> JobId:
        """
        获取媒体ID；音乐额外区分实体类型，并为无远端ID的曲目构造稳定身份。
        """
        if not media:
            return None, season
        source, media_id = resolve_media_identity(media=media)
        if getattr(media, "type", None) == MediaType.MUSIC:
            music_type = normalize_music_type(
                getattr(media, "music_type", None),
            ) or MUSIC_ENTITY_RECORDING
            if source and media_id:
                return "music", source, media_id, music_type

            artists = tuple(
                text_tools.normalize_upper(artist)
                for artist in (getattr(media, "artists", None) or [])
                if text_tools.normalize_upper(artist)
            )
            if music_type == MUSIC_ENTITY_ALBUM:
                album_artist = text_tools.normalize_upper(
                    getattr(media, "album_artist", None)
                    or (artists[0] if artists else "")
                )
                album = text_tools.normalize_upper(
                    getattr(media, "album", None) or getattr(media, "title", None) or ""
                )
                return "music", "local", music_type, album_artist, album, getattr(media, "year", None)

            return (
                "music",
                "local",
                music_type,
                artists,
                text_tools.normalize_upper(getattr(media, "title", None) or ""),
                text_tools.normalize_upper(getattr(media, "album", None) or ""),
                getattr(media, "disc_number", None),
                getattr(media, "track_number", None),
            )
        return (source, media_id), season

    @staticmethod
    def __get_file_key(fileitem: FileItem) -> Optional[Tuple[str, str]]:
        """
        获取源文件唯一键，用于跨媒体作业识别同一个整理任务。
        """
        if not fileitem or not fileitem.path:
            return None
        normalized_path = (
            Path(str(fileitem.path).replace("\\", "/")).as_posix().rstrip("/") or "/"
        )
        return fileitem.storage or "local", normalized_path

    def __get_id(self, task: Optional[TransferTask] = None) -> JobId:
        """
        获取作业ID
        """
        resolved_task = cast(TransferTask, task)
        meta = _transfer_task_meta(resolved_task)
        if resolved_task.mediainfo:
            return self.__get_media_id(
                media=resolved_task.mediainfo, season=meta.begin_season
            )
        return self.__get_meta_id(meta=meta, season=meta.begin_season)

    def get_job_id(self, task: TransferTask) -> JobId:
        """返回任务当前所属的稳定作业身份，供作业级附加状态隔离使用。"""
        return self.__get_id(task)

    @staticmethod
    def __get_media(task: TransferTask) -> Union[_SchemaMediaInfo, _SchemaMusicInfo]:
        """
        获取媒体信息
        """
        if task.mediainfo:
            # 有媒体信息
            mediainfo = deepcopy(task.mediainfo)
            mediainfo.clear()
            if isinstance(mediainfo, MusicInfo):
                return _SchemaMusicInfo(**_domain_to_dict(mediainfo))
            return _SchemaMediaInfo(**_domain_to_dict(mediainfo))
        else:
            # 没有媒体信息
            meta = _transfer_task_meta(task)
            if isinstance(meta, MetaMusic):
                # 未识别的音乐按已解析元数据兜底展示；音乐年份为 int，
                # 不能复用 MediaInfo（year 为 str），否则触发 pydantic 校验异常
                return _SchemaMusicInfo(
                    title=meta.name,
                    artists=list(meta.artists or []),
                    artist=meta.artist,
                    album=meta.album,
                    album_artist=meta.album_artist,
                    year=meta.year,
                    title_year=f"{meta.name} ({meta.year})" if meta.year else meta.name,
                    media_source=meta.media_source,
                    media_id=meta.media_id,
                )
            return _SchemaMediaInfo(
                title=meta.name,
                year=meta.year,
                title_year=f"{meta.name} ({meta.year})",
                type=meta.type.value if meta.type else None,
            )

    @staticmethod
    def __get_meta(task: TransferTask) -> _SchemaMetaInfo:
        """
        获取元数据
        """
        if isinstance(task.meta, MetaMusic):
            return _SchemaMusicMeta(**task.meta.to_dict())
        return _SchemaMetaInfo(**_domain_to_dict(_transfer_task_meta(task)))

    def add_task(self, task: TransferTask, state: Optional[str] = "waiting") -> bool:
        """
        添加整理任务，自动分组到对应的作业中
        :return: True表示任务已添加，False表示任务无效或已存在（重复）
        """
        if not all([task, task.meta, task.fileitem]):
            return False
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return False
        with job_lock:
            __mediaid__ = self.__get_id(task)
            # 同一个源文件可能在识别前后落入不同作业，必须跨作业去重。
            if any(
                    self.__get_file_key(_job_task_fileitem(t)) == file_key
                    for job in self._job_view.values()
                    for t in _job_tasks(job)
            ):
                logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                return False
            if __mediaid__ not in self._job_view:
                self._job_view[__mediaid__] = TransferJob(
                    media=self.__get_media(task),
                    season=_transfer_task_meta(task).begin_season,
                    tasks=[
                        TransferJobTask(
                            fileitem=task.fileitem,
                            meta=self.__get_meta(task),
                            downloader=task.downloader,
                            download_hash=task.download_hash,
                            state=state,
                        )
                    ],
                )
            else:
                # 不重复添加任务
                if any(
                        [
                            self.__get_file_key(_job_task_fileitem(t)) == file_key
                            for t in _job_tasks(self._job_view[__mediaid__])
                        ]
                ):
                    logger.debug(f"任务 {task.fileitem.name} 已存在，跳过重复添加")
                    return False
                _job_tasks(self._job_view[__mediaid__]).append(
                    TransferJobTask(
                        fileitem=task.fileitem,
                        meta=self.__get_meta(task),
                        downloader=task.downloader,
                        download_hash=task.download_hash,
                        state=state,
                    )
                )
            self._task_state_changed_at[file_key] = monotonic()
            # 添加季集信息
            if self._season_episodes.get(__mediaid__):
                self._season_episodes[__mediaid__].extend(
                    _transfer_task_meta(task).episode_list
                )
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__])
                )
            else:
                self._season_episodes[__mediaid__] = _transfer_task_meta(task).episode_list
            return True

    def migrate_task(self, task: TransferTask) -> bool:
        """
        将任务从 meta 作业迁移到 media 作业
        """
        curr_task, source_job_id = self.__remove_task_with_job_id(
            task.fileitem, preserve_execution=True
        )
        if not self.add_task(task, state=curr_task.state if curr_task else "waiting"):
            return False
        if curr_task and task.mediainfo:
            meta = _transfer_task_meta(task)
            metaid = self.__get_meta_id(meta=meta, season=meta.begin_season)
            mediaid = self.__get_id(task)
            if source_job_id == metaid and mediaid != metaid:
                with job_lock:
                    self._meta_to_media_ids.setdefault(metaid, set()).add(mediaid)
        return True

    def __is_job_done(self, job_id: JobId) -> bool:
        """
        检查指定作业是否已完成
        """
        if job_id not in self._job_view:
            return True
        return all(
            task.state in ["completed", "failed"]
            for task in _job_tasks(self._job_view[job_id])
        )

    def __pop_job(self, job_id: JobId) -> None:
        """
        移除指定作业和对应季集缓存
        """
        job = self._job_view.pop(job_id, None)
        self._season_episodes.pop(job_id, None)
        if not job:
            return
        for task in _job_tasks(job):
            file_key = self.__get_file_key(_job_task_fileitem(task))
            if file_key:
                self._task_state_changed_at.pop(file_key, None)
                self._active_executions.discard(file_key)

    def __remove_done_job_groups(self, job_ids: set[JobId]) -> None:
        """
        清理已进入终态的独立作业或关联作业组。
        """
        candidates = set(job_ids)
        for metaid, mediaids in list(self._meta_to_media_ids.items()):
            related_ids = {metaid, *mediaids}
            if not related_ids.intersection(candidates):
                continue
            if all(self.__is_job_done(job_id) for job_id in related_ids):
                for job_id in related_ids:
                    self.__pop_job(job_id)
                self._meta_to_media_ids.pop(metaid, None)
                candidates.difference_update(related_ids)

        referenced_ids = {
            job_id
            for metaid, mediaids in self._meta_to_media_ids.items()
            for job_id in {metaid, *mediaids}
        }
        for job_id in candidates - referenced_ids:
            if self.__is_job_done(job_id):
                self.__pop_job(job_id)

    def start_execution(self, task: TransferTask) -> None:
        """
        标记任务仍由主程序整理线程直接执行。

        :param task: 整理任务
        """
        if not task or not task.fileitem:
            return
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            self._active_executions.add(file_key)

    def finish_execution(self, task: TransferTask) -> None:
        """
        结束主程序整理线程对任务的直接执行标记。

        :param task: 整理任务
        """
        if not task or not task.fileitem:
            return
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            self._active_executions.discard(file_key)

    def expire_stale_running_tasks(
            self, timeout_seconds: int
    ) -> List[tuple[FileItem, int]]:
        """
        将外部接管后长期无心跳的运行中任务标记失败并清理作业视图。

        主程序整理线程仍在直接执行的任务不会被清理，以免把阻塞中的真实任务
        误报为已终止。外部接管方可重复调用 ``running_task`` 刷新状态心跳。

        :param timeout_seconds: 失活超时秒数，小于等于 0 时禁用
        :return: 已失活任务及其无心跳秒数
        """
        if timeout_seconds <= 0:
            return []

        current_time = monotonic()
        expired: List[tuple[FileItem, int]] = []
        affected_job_ids: set[JobId] = set()
        with job_lock:
            for mediaid, job in self._job_view.items():
                for task in _job_tasks(job):
                    fileitem = _job_task_fileitem(task)
                    file_key = self.__get_file_key(fileitem)
                    if (
                            not file_key
                            or task.state != "running"
                            or file_key in self._active_executions
                    ):
                        continue
                    updated_at = self._task_state_changed_at.get(file_key, current_time)
                    inactive_seconds = current_time - updated_at
                    if inactive_seconds < timeout_seconds:
                        continue
                    task.state = "failed"
                    self._task_state_changed_at[file_key] = current_time
                    episodes = getattr(task.meta, "episode_list", None) or []
                    if mediaid in self._season_episodes:
                        self._season_episodes[mediaid] = list(
                            set(self._season_episodes[mediaid]) - set(episodes)
                        )
                    expired.append((fileitem, int(inactive_seconds)))
                    affected_job_ids.add(mediaid)

            self.__remove_done_job_groups(affected_job_ids)
        return expired

    def running_task(self, task: TransferTask) -> None:
        """
        设置任务为运行中，并刷新外部异步任务的状态心跳。
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "running"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break

    def finish_task(self, task: TransferTask) -> None:
        """
        设置任务为完成/成功
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "completed"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break

    def fail_task(self, task: TransferTask) -> None:
        """
        设置任务为失败
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ not in self._job_view:
                return
            # 更新状态
            for t in _job_tasks(self._job_view[__mediaid__]):
                if t.fileitem == task.fileitem:
                    t.state = "failed"
                    file_key = self.__get_file_key(_job_task_fileitem(t))
                    if file_key:
                        self._task_state_changed_at[file_key] = monotonic()
                    break
            # 移除剧集信息
            if __mediaid__ in self._season_episodes:
                self._season_episodes[__mediaid__] = list(
                    set(self._season_episodes[__mediaid__])
                    - set(_transfer_task_meta(task).episode_list)
                )

    def fail_unfinished_task(self, task: TransferTask) -> None:
        """
        将指定任务视图中的非终态任务标记为失败
        """
        if not task or not task.fileitem:
            return
        file_key = self.__get_file_key(task.fileitem)
        if not file_key:
            return
        with job_lock:
            for mediaid, job in self._job_view.items():
                for job_task in _job_tasks(job):
                    if self.__get_file_key(_job_task_fileitem(job_task)) != file_key:
                        continue
                    if job_task.state not in ["completed", "failed"]:
                        job_task.state = "failed"
                        self._task_state_changed_at[file_key] = monotonic()
                        if mediaid in self._season_episodes:
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid])
                                - set(_transfer_task_meta(task).episode_list)
                            )
                    return

    def remove_task(self, fileitem: FileItem) -> Optional[TransferJobTask]:
        """
        根据文件项移除任务
        """
        task, _ = self.__remove_task_with_job_id(fileitem)
        return task

    def __remove_task_with_job_id(
            self,
            fileitem: FileItem,
            preserve_execution: bool = False,
    ) -> tuple[Optional[TransferJobTask], Optional[JobId]]:
        """
        根据文件项移除任务，并返回任务所在的作业ID
        """
        file_key = self.__get_file_key(fileitem)
        if not file_key:
            return None, None
        with job_lock:
            for mediaid in list(self._job_view):
                job = self._job_view[mediaid]
                for task in _job_tasks(job):
                    if self.__get_file_key(_job_task_fileitem(task)) == file_key:
                        _job_tasks(job).remove(task)
                        self._task_state_changed_at.pop(file_key, None)
                        if not preserve_execution:
                            self._active_executions.discard(file_key)
                        # 如果没有作业了，则移除作业
                        if not _job_tasks(job):
                            self._job_view.pop(mediaid)
                        # 移除季集信息
                        if mediaid in self._season_episodes:
                            episodes = getattr(task.meta, "episode_list", None) or []
                            self._season_episodes[mediaid] = list(
                                set(self._season_episodes[mediaid])
                                - set(episodes)
                            )
                        return task, mediaid
            return None, None

    def remove_job(self, task: TransferTask) -> Optional[TransferJob]:
        """
        移除任务对应的作业（强制，线程不安全）
        """
        with job_lock:
            __mediaid__ = self.__get_id(task)
            if __mediaid__ in self._job_view:
                job = self._job_view[__mediaid__]
                self.__pop_job(__mediaid__)
                return job
            return None

    def try_remove_job(self, task: TransferTask) -> None:
        """
        尝试移除任务对应的作业（严格检查未完成作业，线程安全）
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )

            related_media_ids = set(self._meta_to_media_ids.get(__metaid__, set()))
            if task.mediainfo:
                related_media_ids.add(__mediaid__)

            meta_done = self.__is_job_done(__metaid__)
            media_done = all(
                self.__is_job_done(mediaid) for mediaid in related_media_ids
            )

            if meta_done and media_done:
                remove_ids = {__metaid__, self.__get_id(task), *related_media_ids}
                for job_id in remove_ids:
                    self.__pop_job(job_id)
                self._meta_to_media_ids.pop(__metaid__, None)

    def is_done(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否整理完成（不管成功还是失败）
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_done = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_done = True
            if __mediaid__ in self._job_view:
                media_done = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__mediaid__])
                )
            else:
                media_done = True
            return meta_done and media_done

    def is_finished(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否已完成且有成功的记录
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_finished = all(
                    task.state in ["completed", "failed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_finished = True
            if __mediaid__ in self._job_view:
                tasks = _job_tasks(self._job_view[__mediaid__])
                media_finished = all(
                    task.state in ["completed", "failed"] for task in tasks
                ) and any(task.state == "completed" for task in tasks)
            else:
                media_finished = True
            return meta_finished and media_finished

    def is_success(self, task: TransferTask) -> bool:
        """
        检查任务对应的作业是否全部成功
        """
        with job_lock:
            meta = _transfer_task_meta(task)
            __metaid__ = self.__get_meta_id(meta=meta, season=meta.begin_season)
            __mediaid__ = self.__get_media_id(
                media=task.mediainfo, season=meta.begin_season
            )
            if __metaid__ in self._job_view:
                meta_success = all(
                    task.state in ["completed"]
                    for task in _job_tasks(self._job_view[__metaid__])
                )
            else:
                meta_success = True
            if __mediaid__ in self._job_view:
                media_success = all(
                    task.state in ["completed"]
                    for task in _job_tasks(self._job_view[__mediaid__])
                )
            else:
                media_success = True
            return meta_success and media_success

    def get_all_torrent_hashes(self) -> set[str]:
        """
        获取所有种子的哈希值集合
        """
        with job_lock:
            return {
                cast(str, task.download_hash)
                for job in self._job_view.values()
                for task in _job_tasks(job)
            }

    def is_torrent_done(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已完成
        """
        with job_lock:
            if any(
                    task.state not in {"completed", "failed"}
                    for job in self._job_view.values()
                    for task in _job_tasks(job)
                    if task.download_hash == download_hash
            ):
                return False
            return True

    def is_torrent_success(self, download_hash: str) -> bool:
        """
        检查指定种子的所有任务是否都已成功
        """
        with job_lock:
            if any(
                    task.state != "completed"
                    for job in self._job_view.values()
                    for task in _job_tasks(job)
                    if task.download_hash == download_hash
            ):
                return False
            return True

    def has_tasks(
            self,
            meta: MetaBase,
            mediainfo: Optional[MediaInfo] = None,
            season: Optional[int] = None,
    ) -> bool:
        """
        判断作业是否还有任务正在处理
        """
        with job_lock:
            if mediainfo:
                __mediaid__ = self.__get_media_id(media=mediainfo, season=season)
                if __mediaid__ in self._job_view:
                    return True

            __metaid__ = self.__get_meta_id(meta=meta, season=season)
            return (
                    __metaid__ in self._job_view
                    and len(_job_tasks(self._job_view[__metaid__])) > 0
            )

    def success_tasks(
            self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None
    ) -> List[TransferJobTask]:
        """
        获取作业中所有成功的任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return [
                task
                for task in _job_tasks(self._job_view[__mediaid__])
                if task.state == "completed"
            ]

    def all_tasks(
            self, media: MediaInfo, season: Optional[int] = None
    ) -> List[TransferJobTask]:
        """
        获取作业中全部任务
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return []
            return _job_tasks(self._job_view[__mediaid__])

    def count(self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None) -> int:
        """
        获取作业中成功总数
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return len(
                [
                    task
                    for task in _job_tasks(self._job_view[__mediaid__])
                    if task.state == "completed"
                ]
            )

    def size(self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None) -> int:
        """
        获取作业中所有成功文件总大小
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            if __mediaid__ not in self._job_view:
                return 0
            return sum(
                [
                    _job_task_size(task)
                    for task in _job_tasks(self._job_view[__mediaid__])
                    if task.state == "completed"
                ]
            )

    def total(self) -> int:
        """
        获取所有任务总数
        """
        with job_lock:
            return sum([len(_job_tasks(job)) for job in self._job_view.values()])

    def pending_total(self) -> int:
        """
        获取未到终态的任务总数。

        作业要等关联任务全部终态才整体移除,追更/分批场景下已完成任务会
        跨批次残留在视图中;批次统计若用全量 total() 会把历史任务计入
        「当前共 N 个文件」并压低进度百分比,因此只数未终态任务。
        """
        with job_lock:
            return sum(
                1
                for job in self._job_view.values()
                for task in _job_tasks(job)
                if task.state not in ("completed", "failed")
            )

    def list_jobs(self) -> List[TransferJob]:
        """
        获取所有作业的任务列表
        """
        with job_lock:
            return list(self._job_view.values())

    def season_episodes(
            self, media: Union[MediaInfo, MusicInfo], season: Optional[int] = None
    ) -> List[int]:
        """
        获取作业的季集清单
        """
        with job_lock:
            __mediaid__ = self.__get_media_id(media=media, season=season)
            return self._season_episodes.get(__mediaid__) or []
