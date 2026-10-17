"""
整理规划、任务对象与持久准入契约。
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, List, Optional, Protocol, TypeAlias, Union

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from app.application.history import DownloadHistorySnapshot
from app.application.transfer import checkpoint as checkpoint_codec
from app.application.transfer.execution import TransferExecutionCheckpoint
from app.application.transfer.projection import (
    domain_to_dict as _domain_to_dict,
)
from app.domain.context import MediaInfo, MusicInfo
from app.domain.meta.metabase import MetaBase
from app.schemas.file import FileItem
from app.schemas.media import OptionalMediaIdentityMixin
from app.schemas.system import TransferDirectoryConf
from app.schemas.tmdb import TmdbEpisode
from app.schemas.transfer import TransferInfo
from app.schemas.types import MediaSource, MediaType

if TYPE_CHECKING:
    class _ApplicationModel:
        """
        描述当前模块依赖的最小 Pydantic 模型类型形状。
        """

        def __init__(self, **data: Any) -> None:
            """
            接受模型字段关键字参数。
            """
            raise NotImplementedError

        def model_dump(self, **kwargs: Any) -> dict[str, Any]:
            """
            返回模型字段字典。
            """
            raise NotImplementedError
else:
    _ApplicationModel = BaseModel

JSONValue: TypeAlias = Union[None, bool, int, float, str, list["JSONValue"], dict[str, "JSONValue"]]

# 任务已持久接纳，尚未生成可执行计划
TRANSFER_ADMISSION_ACCEPTED = "accepted"
# 已冻结旧插件提供方和调用输入，等待提供方完成规划或执行
TRANSFER_ADMISSION_PROVIDER_PENDING = "provider_pending"
# 完整计划已持久化，恢复时直接使用检查点
TRANSFER_ADMISSION_PLANNED = "planned"
# 整理规划输入的当前持久格式版本
TRANSFER_PLANNING_INPUT_VERSION = 1
# 包含最终分类快照的当前整理计划格式版本
TRANSFER_PLAN_CHECKPOINT_VERSION = 2
# 仍可读取的历史整理计划格式版本，不包含最终分类快照
TRANSFER_PLAN_CHECKPOINT_LEGACY_VERSION = 1
# 旧插件调用参数快照的当前格式版本
TRANSFER_PROVIDER_INVOCATION_VERSION = 1


@dataclass(frozen=True, slots=True)
class TransferPlanningInput:
    """
    保存可跨重启重放的版本化整理规划输入。

    :param source_fileitem: 源文件项的可序列化快照，必须包含存储标识和路径
    :param meta: 已解析发布名元数据快照；None 表示尚未解析
    :param mediainfo: 已识别媒体详情快照；None 表示尚未识别
    :param target_directory: 目标资源目录配置快照；None 表示尚未选择
    :param target_storage: 目标存储配置标识
    :param target_path: 目标存储内的文件或目录路径
    :param requested_transfer_type: 调用方要求的整理方式；None 由目录策略决定
    :param media_source: 媒体身份来源，与 media_id 配对使用
    :param media_id: 该来源中的原生媒体标识
    :param media_type: 电影、电视剧或音乐等目标媒体类型
    :param need_scrape: 是否需要生成媒体刮削资料
    :param need_rename: 是否按照命名规则重命名文件
    :param need_notify: 是否发送整理结果通知
    :param overwrite_mode: 目标已存在时的覆盖策略；None 使用默认策略
    :param episodes_info: 计划使用的剧集详情快照，空元组表示未提供
    :param preview: 是否仅生成预览而不执行真实整理
    :param options: 影响规划且可持久化的附加选项
    :param schema_version: 持久快照结构版本；只接受当前支持版本
    """

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
        """
        拒绝不可恢复的输入版本或缺少源文件身份的快照。
        """
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
        """
        从受版本约束的 JSON 对象恢复规划输入。
        """
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
        """
        生成仅含 JSON 值且字段稳定的规划输入投影。
        """
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
        """
        返回规范 JSON 的稳定 SHA-256 指纹。
        """
        return hashlib.sha256(checkpoint_codec.canonical_json(self.to_payload()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class TransferPlanItem:
    """
    描述规划后按序执行的一条叶子文件操作。

    :param sequence: 文件在整理计划中的连续序号，从 0 开始
    :param source_fileitem: 源文件项的可序列化快照，必须包含存储标识和路径
    :param target_storage: 目标存储配置标识
    :param target_path: 目标存储内的文件或目录路径
    :param action: 计划动作，默认 transfer 表示转移文件
    """

    sequence: int
    source_fileitem: dict[str, JSONValue]
    target_storage: str
    target_path: str
    action: str = "transfer"

    def __post_init__(self) -> None:
        """
        拒绝无法定位源文件或目标文件的计划项。
        """
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
        """
        从 JSON 对象恢复单条叶子文件操作。
        """
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
        """
        生成单条叶子文件操作的 JSON 投影。
        """
        return {
            "sequence": self.sequence,
            "source_fileitem": deepcopy(self.source_fileitem),
            "target_storage": self.target_storage,
            "target_path": self.target_path,
            "action": self.action,
        }


@dataclass(frozen=True, slots=True)
class TransferProviderReference:
    """
    保存无需依赖运行时 dispatcher 即可持久化的旧 transfer 提供方引用。

    :param plugin_id: 提供旧整理能力的插件标识
    :param plugin_name: 提供方插件的展示名称
    :param method: 调用插件的方法名，默认 transfer
    """

    plugin_id: str
    plugin_name: str
    method: str = "transfer"

    def __post_init__(self) -> None:
        """
        拒绝无法稳定定位插件或指向非 transfer 方法的引用。
        """
        if not isinstance(self.plugin_id, str) or not self.plugin_id.strip():
            raise ValueError("旧 transfer provider 的 plugin_id 必须是非空字符串")
        if not isinstance(self.plugin_name, str) or not self.plugin_name.strip():
            raise ValueError("旧 transfer provider 的 plugin_name 必须是非空字符串")
        if self.method != "transfer":
            raise ValueError("旧 transfer provider 的 method 必须是 transfer")
        checkpoint_codec.canonical_json(self.to_payload())

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferProviderReference":
        """
        从 JSON 对象恢复旧 transfer 提供方引用。
        """
        if not isinstance(payload, dict):
            raise ValueError("旧 transfer provider 引用必须是 JSON 对象")
        return cls(
            plugin_id=payload.get("plugin_id", ""),
            plugin_name=payload.get("plugin_name", ""),
            method=payload.get("method", "transfer"),
        )

    def to_payload(self) -> dict[str, JSONValue]:
        """
        生成仅包含稳定插件身份与方法名的 JSON 投影。
        """
        return {
            "plugin_id": self.plugin_id,
            "plugin_name": self.plugin_name,
            "method": self.method,
        }


@dataclass(frozen=True, slots=True)
class TransferProviderInvocationSnapshot:
    """
    冻结旧 transfer ABI 中可跨重启恢复的精确参数值。

    :param fileitem: 传给提供方的源文件项快照
    :param meta: 已解析发布名元数据快照；None 表示尚未解析
    :param meta_kind: 发布名元数据的对象种类，用于正确恢复影视或音乐对象
    :param mediainfo: 已识别媒体详情快照；None 表示尚未识别
    :param mediainfo_kind: 媒体详情的对象种类，用于恢复对应领域类型
    :param target_directory: 目标资源目录配置快照；None 表示尚未选择
    :param target_storage: 目标存储配置标识
    :param target_path: 目标存储内的文件或目录路径
    :param transfer_type: 调用提供方时指定的整理方式；None 表示未显式指定
    :param scrape: 调用提供方时是否要求刮削；None 保留未指定语义
    :param library_type_folder: 是否在媒体库按媒体类型建立目录；None 表示未指定
    :param library_category_folder: 是否在媒体库按分类建立目录；None 表示未指定
    :param episodes_info: 计划使用的剧集详情快照，空元组表示未提供
    :param preview: 是否仅生成预览而不执行真实整理
    :param schema_version: 持久快照结构版本；只接受当前支持版本
    """

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
        """
        拒绝缺少源身份、类型不稳定或版本未知的提供方快照。
        """
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
        """
        从版本化 JSON 对象严格恢复旧 transfer ABI 调用快照。
        """
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
        """
        生成仅含 JSON 值且保留 None 与 False 差异的调用投影。
        """
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
    """
    保存无需重触发识别或重命名即可执行的完整有序计划。

    :param planning_input: 生成该计划的稳定输入快照
    :param target_storage: 目标存储配置标识
    :param root_target_path: 所选资源目录对应的目标根路径
    :param final_target_path: 规则与分类解析后的最终整理路径
    :param resolved_transfer_type: 规划后已确定的整理方式
    :param items: 按执行顺序保存的文件计划项
    :param classification_snapshot: 规划时已确定的最终分类，不在恢复执行时重新匹配策略
    :param resolved_meta: 规划后解析出的发布名元数据快照
    :param resolved_meta_kind: 用于恢复已解析元数据的对象种类
    :param resolved_mediainfo: 规划后识别出的完整媒体详情快照
    :param resolved_mediainfo_kind: 用于恢复媒体详情的对象种类
    :param resolved_episodes_info: 规划时已获取的剧集详情快照
    :param legacy_transfer_providers: 本轮规划选定的旧插件提供方，恢复时保持该顺序
    :param provider_invocation: 调用旧提供方所需的完整输入；无提供方任务时为 None
    :param pre_execution_cleanup_completed: 执行前清理步骤是否已经完成，避免恢复时重复处理
    :param need_scrape: 是否需要生成媒体刮削资料
    :param need_rename: 是否按照命名规则重命名文件
    :param need_notify: 是否发送整理结果通知
    :param overwrite_mode: 目标已存在时的覆盖策略；None 使用默认策略
    :param preview: 是否仅生成预览而不执行真实整理
    :param skip_reason: 未产生外部操作的明确原因；有步骤时可为 None
    :param rejection_error: 计划被业务规则拒绝的原因；未拒绝时为 None
    :param schema_version: 持久快照结构版本；只接受当前支持版本
    """

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
        """
        验证版本、目标身份和计划项顺序组成完整检查点。
        """
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
        """
        返回该检查点是否只冻结提供方调用、尚未生成宿主计划。
        """
        return self.provider_invocation is not None

    @property
    def fingerprint(self) -> str:
        """
        返回完整冻结计划规范 JSON 的稳定 SHA-256 指纹。
        """
        return hashlib.sha256(
            checkpoint_codec.canonical_json(self.to_payload()).encode("utf-8")
        ).hexdigest()

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "TransferPlanCheckpoint":
        """
        从受版本约束的 JSON 对象恢复完整执行检查点。
        """
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
        """
        生成可原子落库的完整版本化 JSON 检查点。
        """
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
    """
    同一源文件以不同规划输入重复准入时抛出的冲突错误。
    """


class TransferPlanningStateError(RuntimeError):
    """
    计划检查点无法从当前持久状态推进时抛出的状态错误。
    """


class TransferAdmissionProjectionError(RuntimeError):
    """
    持久登记无法安全恢复为应用层整理准入投影时抛出的错误。
    """


class TransferLeaseLostError(TransferPlanningStateError):
    """
    整理执行者已失去持久租约、不得继续推进任务时抛出的错误。
    """


class TransferTask(OptionalMediaIdentityMixin, _ApplicationModel):
    """
    文件整理任务与宿主恢复状态。

    公开字段供业务编排和插件读取；租约、检查点及临时预览证据保存在私有属性。

    :ivar _admission_task_id: 宿主持久准入标识，不进入插件可见序列化字段
    :ivar _planning_input: 恢复规划用的持久输入；尚未绑定时为 None
    :ivar _plan_checkpoint: 恢复计划执行用的检查点；尚未生成时为 None
    :ivar _execution_checkpoint: 恢复终态结算用的执行结果检查点；尚未生成时为 None
    :ivar _terminal_settled: 历史、事件和待处理记录是否已共同提交，初始为 False
    :ivar _planning_context_restored: 当前领域上下文是否已从持久快照离线恢复
    :ivar _music_preview_context: 只供本次音乐预览展示的临时证据，不参与持久规划
    :ivar _lease_owner: 任务当前绑定的执行者标识；未认领时为 None
    :ivar _lease_token: 任务当前绑定的持久租约令牌；未认领时为 None
    """

    # MetaBase 与 MediaInfo / MusicInfo 都是普通类而非 BaseModel，pydantic 需要显式放行
    model_config = ConfigDict(arbitrary_types_allowed=True)

    fileitem: FileItem = Field(description='本次整理的源文件项，必须包含有效存储与路径')
    meta: Optional[MetaBase] = Field(default=None, description='已解析的发布名元数据；None 表示尚未解析')
    mediainfo: Optional[Union[MusicInfo, MediaInfo]] = Field(default=None, description='已识别的影视或音乐详情；None 表示尚未识别')
    media_source: Optional[MediaSource] = Field(default=None, description='显式指定的媒体来源；None 表示自动选择')
    media_id: Optional[str] = Field(default=None, description='指定来源中的原生媒体标识；None 表示自动识别')
    mtype: Optional[MediaType] = Field(default=None, description='限定的媒体类型；None 表示未限定')
    target_directory: Optional[TransferDirectoryConf] = Field(default=None, description='指定的目标资源目录配置；None 使用目录匹配规则')
    target_storage: Optional[str] = Field(default=None, description='指定的目标存储标识；None 使用选定目录的存储')
    target_path: Optional[Path] = Field(default=None, description='指定的目标路径；None 按规划规则生成')
    transfer_type: Optional[str] = Field(default=None, description='指定的整理方式；None 使用目录默认方式')
    scrape: Optional[bool] = Field(default=False, description='是否生成刮削资料，默认 False')
    library_type_folder: Optional[bool] = Field(default=False, description='是否按媒体类型生成库目录，默认 False')
    library_category_folder: Optional[bool] = Field(default=False, description='是否按媒体分类生成库目录，默认 False')
    episodes_info: Optional[List[TmdbEpisode]] = Field(default=None, description='已取得的剧集详情；None 表示尚未取得')
    username: Optional[str] = Field(default=None, description='触发整理的本地用户名；未绑定时为 None')
    downloader: Optional[str] = Field(default=None, description='关联下载器的配置名称；无下载器任务时为 None')
    download_hash: Optional[str] = Field(default=None, description='关联下载器任务的哈希；无下载任务时为 None')
    download_history: Optional[DownloadHistorySnapshot] = Field(default=None, description='关联的下载历史快照；未匹配时为 None')
    transfer_batch_id: Optional[str] = Field(default=None, description='关联的整理批次标识；未分配批次时为 None')
    manual: Optional[bool] = Field(default=False, description='是否为用户手动发起的整理，默认 False')
    background: Optional[bool] = Field(default=True, description='是否作为后台任务执行，默认 True')
    preview: Optional[bool] = Field(default=False, description='是否只返回预览而不执行真实整理，默认 False')
    _admission_task_id: Optional[str] = PrivateAttr(default=None)
    _planning_input: Optional[TransferPlanningInput] = PrivateAttr(default=None)
    _plan_checkpoint: Optional[TransferPlanCheckpoint] = PrivateAttr(default=None)
    _execution_checkpoint: Optional[TransferExecutionCheckpoint] = PrivateAttr(default=None)
    _terminal_settled: bool = PrivateAttr(default=False)
    _planning_context_restored: bool = PrivateAttr(default=False)
    _music_preview_context: dict[str, Any] = PrivateAttr(default_factory=dict)
    _lease_owner: Optional[str] = PrivateAttr(default=None)
    _lease_token: Optional[str] = PrivateAttr(default=None)

    @property
    def music_preview_context(self) -> dict[str, Any]:
        """
        返回本次扫描的展示证据副本，不作为持久重试范围或文件访问凭证。
        """
        return dict(getattr(self, "_music_preview_context", {}))

    def bind_music_preview_context(self, context: dict[str, Any]) -> None:
        """
        绑定临时音乐分组与可读性，保持旧插件序列化和持久规划契约。
        """
        self._music_preview_context = dict(context)

    @property
    def admission_task_id(self) -> Optional[str]:
        """
        返回仅供宿主持久准入和终态结算使用的内部任务标识。
        """
        return self._admission_task_id

    def bind_admission_task_id(self, task_id: str) -> None:
        """
        绑定持久准入生成的稳定身份，不改变插件可见序列化字段。
        """
        self._admission_task_id = task_id

    @property
    def planning_input(self) -> Optional[TransferPlanningInput]:
        """
        返回宿主恢复规划使用的内部输入快照。
        """
        return self._planning_input

    @property
    def plan_checkpoint(self) -> Optional[TransferPlanCheckpoint]:
        """
        返回宿主直接执行已规划任务使用的内部检查点。
        """
        return self._plan_checkpoint

    def bind_planning_input(self, planning_input: TransferPlanningInput) -> None:
        """
        绑定持久规划输入，不改变插件可见序列化字段。
        """
        self._planning_input = planning_input

    def bind_plan_checkpoint(self, checkpoint: TransferPlanCheckpoint) -> None:
        """
        绑定持久执行检查点，不改变插件可见序列化字段。
        """
        self._plan_checkpoint = checkpoint

    @property
    def execution_checkpoint(self) -> Optional[TransferExecutionCheckpoint]:
        """
        返回仅供宿主终态结算使用的内部执行检查点。
        """
        return self._execution_checkpoint

    def bind_execution_checkpoint(
            self, checkpoint: TransferExecutionCheckpoint
    ) -> None:
        """
        绑定执行结果检查点，不改变插件可见的旧任务序列化字段。
        """
        self._execution_checkpoint = checkpoint

    @property
    def terminal_settled(self) -> bool:
        """
        返回历史、事件与待处理记录是否已由同一 UoW 提交。
        """
        return self._terminal_settled

    def mark_terminal_settled(self) -> None:
        """
        仅在支持任务结算的写入端口成功返回后标记终态已经提交。
        """
        self._terminal_settled = True

    @property
    def planning_context_restored(self) -> bool:
        """
        返回当前领域上下文是否来自持久快照。
        """
        return self._planning_context_restored

    def mark_planning_context_restored(self) -> None:
        """
        标记领域上下文已离线恢复，禁止旧流程再次在线补充。
        """
        self._planning_context_restored = True

    @property
    def lease_owner(self) -> Optional[str]:
        """
        返回当前任务绑定的进程级执行者标识。
        """
        return self._lease_owner

    @property
    def lease_token(self) -> Optional[str]:
        """
        返回当前任务绑定的持久租约令牌。
        """
        return self._lease_token

    def bind_execution_lease(self, *, owner_id: str, lease_token: str) -> None:
        """
        绑定持久认领结果，且不改变插件可见的旧任务序列化字段。
        """
        if not owner_id or not lease_token:
            raise ValueError("整理执行租约缺少 owner 或 token")
        self._lease_owner = owner_id
        self._lease_token = lease_token

    def to_dict(self) -> dict[str, Any]:
        """
        返回用于任务展示和兼容调用的独立字段字典。

        meta 与 mediainfo 是领域对象，使用各自的 to_dict()；文件项与目录配置使用
        Pydantic 的 model_dump()，避免把两类对象当作同一种序列化接口。
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
    进程内整理队列项，保存任务、完成回调和执行结果。

    回调属于运行期对象，本类不承担跨进程持久化格式。
    """
    task: Optional[TransferTask] = Field(default=None, description='队列中待处理的整理任务；None 表示尚未绑定')
    callback: Optional[TransferCallback] = Field(default=None, description='整理完成后的业务回调；None 表示无附加回调')
    result: Optional[TransferInfo] = Field(default=None, description='已经产生的整理结果；尚未执行时为 None')


@dataclass(frozen=True, slots=True)
class TransferAdmission:
    """
    描述已经持久化、可在进程退出后恢复的整理任务准入事实。

    :param task_id: 持久整理任务的稳定标识
    :param storage: 源文件所在的存储配置标识
    :param src_path: 任务源文件在存储中的路径
    :param state: 规划准入状态，区分 accepted、provider_pending 和 planned
    :param created_at: 任务首次持久登记时间文本
    :param updated_at: 最近一次持久状态更新时间文本
    :param planning_input: 跨重启恢复规划所需的版本化输入
    :param last_error: 最近失败的可读原因；未失败时为 None
    :param input_fingerprint: 规划输入的稳定内容指纹；旧数据可能为 None
    :param checkpoint: 已完成的整理计划；尚未规划时为 None
    :param lease_owner: 当前租约所属执行者标识；未认领时为 None
    :param lease_token: 当前执行租约令牌；未认领时为 None
    :param lease_expires_at: 租约到期时间文本；无租约时为 None
    :param heartbeat_at: 最近一次成功续租时间；未续租时为 None
    :param attempt_count: 任务已被认领的累计次数
    """

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
    """
    整理任务持久准入所需的类型化持久化端口。

    实现方拥有短事务及提交、回滚；文件操作和媒体识别由调用方在事务外执行。
    认领和状态推进必须核对当前租约，不能让过期执行者覆盖新执行者的结果。
    """

    def defer_planning(self, *, task_id: str, lease_token: str, error: str,
                       retry_after: int, max_retries: int) -> bool:
        """
        仅将无计划、无步骤的接纳任务退避；预算用尽返回 False，租约或状态冲突抛错。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param error: 需要持久记录的可读失败原因
        :param retry_after: 规划重试前的等待时长，单位为秒
        :param max_retries: 本轮规划允许的最大重试次数
        :return: 成功安排退避时为 True；重试预算用尽时为 False
        """
        ...

    def admit(
            self,
            *,
            storage: str,
            src_path: str,
            planning_input: TransferPlanningInput,
            replace_inactive: bool = False,
    ) -> TransferAdmission:
        """
        幂等登记源文件；显式重做可原子替换无有效租约且无历史绑定的旧任务。

        :param storage: 源文件所在的存储配置标识
        :param src_path: 源文件在存储中的规范路径
        :param planning_input: 跨重启恢复规划所需的版本化输入快照
        :param replace_inactive: 是否替换无有效租约且没有历史绑定的旧任务，默认 False
        :return: 新建或幂等复用的持久准入事实
        """
        ...

    def record_enqueue_failure(self, *, task_id: str, error: str) -> None:
        """
        记录内存队列接收失败，保留任务供后续恢复。

        :param task_id: 目标持久整理任务的稳定标识
        :param error: 需要持久记录的可读失败原因
        """
        ...

    def checkpoint_plan(
            self,
            *,
            task_id: str,
            lease_token: str,
            input_fingerprint: str,
            checkpoint: TransferPlanCheckpoint,
    ) -> TransferAdmission:
        """
        按有效租约原子保存完整计划并推进匹配输入的任务。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param input_fingerprint: 预期规划输入的内容指纹，防止保存到已变更的任务
        :param checkpoint: 拟持久化的完整规划检查点
        :return: 已绑定计划检查点的准入事实
        """
        ...

    def record_planning_failure(
            self, *, task_id: str, lease_token: str, error: str
    ) -> None:
        """
        按有效租约记录规划失败，保留业务状态供恢复重试。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param error: 需要持久记录的可读失败原因
        """
        ...

    def claim_task(
            self,
            *,
            task_id: str,
            owner_id: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """
        为指定任务取得唯一执行租约；已有有效租约时返回 None。

        :param task_id: 目标持久整理任务的稳定标识
        :param owner_id: 本次认领任务的执行者标识
        :param lease_seconds: 认领或续租后有效的租约时长，单位为秒
        :return: 带当前租约的任务；不存在或暂时不可认领时为 None
        """
        ...

    def claim_recoverable(
            self,
            *,
            owner_id: str,
            limit: int,
            lease_seconds: int,
    ) -> list[TransferAdmission]:
        """
        按登记顺序原子取得可恢复任务的唯一执行租约。

        :param owner_id: 本次认领任务的执行者标识
        :param limit: 本次最多认领的可恢复任务数量
        :param lease_seconds: 认领或续租后有效的租约时长，单位为秒
        :return: 本次成功认领的任务列表，可能少于 limit
        """
        ...

    def heartbeat(
            self,
            *,
            task_id: str,
            lease_token: str,
            lease_seconds: int,
    ) -> Optional[TransferAdmission]:
        """
        仅为当前且未过期的令牌延长租约；过期令牌不得复活。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param lease_seconds: 认领或续租后有效的租约时长，单位为秒
        :return: 延长租约后的任务；令牌失效或任务不存在时为 None
        """
        ...

    def release_claim(
            self,
            *,
            task_id: str,
            lease_token: str,
            error: Optional[str] = None,
    ) -> bool:
        """
        按令牌释放当前认领，并可记录可恢复错误。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :param error: 释放租约时附带的可恢复错误；None 表示无附加错误
        :return: 成功释放当前有效租约时为 True；租约不匹配时为 False
        """
        ...

    def abandon_unstarted(self, *, task_id: str, lease_token: str) -> int:
        """
        仅允许当前租约持有者删除确认未开始且源已消失的登记。

        :param task_id: 目标持久整理任务的稳定标识
        :param lease_token: 当前认领获得的有效租约令牌，用于拒绝过期执行者
        :return: 实际删除的任务数量，未满足安全删除条件时为 0
        """
        ...
