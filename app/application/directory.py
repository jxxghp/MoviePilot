import re
from dataclasses import dataclass
from functools import partial
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, List, Optional, Protocol, Tuple

from pydantic import ValidationError

from app.application.classification.reference import (
    ClassificationCategoryResolution,
    ClassificationCategoryResolver,
    classification_category_resolver_snapshot,
    classification_media_type,
    configure_classification_category_resolver,
    reset_classification_category_resolver,
)
from app.application.configuration import (
    ConfigurationRepository,
    SystemConfigReader,
    SystemConfigService,
    SystemConfigValueNormalizer,
    SystemConfigWriter,
    SystemConfigWriteResult,
    get_configured_system_config,
)
from app.application.database import AsyncDatabaseExecutor
from app.domain.context import MediaInfo, MusicInfo
from app.runtime.log import logger
from app.schemas.category import ClassificationPolicy
from app.schemas.file import FileURI as _SchemaFileURI
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.schemas.types import MediaType, StorageSchema, SystemConfigKey

JINJA2_VAR_PATTERN = re.compile(r"\{\{.*?}}", re.DOTALL)
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")
DirectoryMedia = MediaInfo | MusicInfo
"""目录选择支持的完整影视或音乐媒体对象。"""


class DiskTopology(Protocol):
    """描述本地路径磁盘拓扑判断能力。"""

    def is_same_disk(self, src: Path, dest: Path) -> bool:
        """返回两个真实本地路径是否位于同一磁盘。"""
        ...


@dataclass(frozen=True, slots=True)
class DirectoryConfigurationWriteResult:
    """目录配置事务提交后的变更状态和规范化快照。"""

    changed: bool | None
    normalized_value: list[dict[str, Any]] | None


class DirectoryConfigurationMutationPort(Protocol):
    """Application 写入目录配置所依赖的原子持久化端口。"""

    def save(self, value: object) -> DirectoryConfigurationWriteResult:
        """基于事务内活动策略规范化并保存目录配置。"""
        ...


class DirectoryConfigurationMutationService:
    """统一执行同步和异步目录配置保存命令。"""

    def __init__(
        self,
        repository: DirectoryConfigurationMutationPort,
        *,
        async_executor: AsyncDatabaseExecutor | None = None,
    ) -> None:
        """注入原子目录仓储和可选异步数据库执行端口。"""
        self._repository = repository
        self._async_executor = async_executor

    def save(self, value: object) -> DirectoryConfigurationWriteResult:
        """同步保存目录配置并返回数据库实际提交的规范化值。"""
        return self._repository.save(value)

    async def async_save(
        self,
        value: object,
    ) -> DirectoryConfigurationWriteResult:
        """在线程私有短事务中异步保存目录配置。"""
        if self._async_executor is None:
            raise RuntimeError("目录配置异步数据库执行端口尚未配置")
        return await self._async_executor.run(partial(self._repository.save, value))


class DirectoryAwareSystemConfigService(SystemConfigService):
    """让所有在线 Directories 写入复用原子目录变更命令。"""

    def __init__(
        self,
        *,
        directory_mutation: DirectoryConfigurationMutationService,
        repository: ConfigurationRepository | None = None,
        reader: SystemConfigReader | None = None,
        writer: SystemConfigWriter | None = None,
        async_executor: AsyncDatabaseExecutor | None = None,
        value_normalizer: SystemConfigValueNormalizer | None = None,
    ) -> None:
        """注入通用配置端口，并为目录键绑定专用原子写入命令。"""
        super().__init__(
            repository=repository,
            reader=reader,
            writer=writer,
            async_executor=async_executor,
            value_normalizer=value_normalizer,
        )
        self._directory_mutation = directory_mutation

    def set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """目录键走原子命令，其他配置保持通用同步写入语义。"""
        if _system_config_key(key) != SystemConfigKey.Directories.value:
            return super().set_with_normalized_value(key, value)
        result = self._directory_mutation.save(value)
        return SystemConfigWriteResult(
            changed=result.changed,
            normalized_value=result.normalized_value,
        )

    async def async_set_with_normalized_value(
        self,
        key: Any,
        value: Any,
    ) -> SystemConfigWriteResult:
        """目录键走原子命令，其他配置保持通用异步写入语义。"""
        if _system_config_key(key) != SystemConfigKey.Directories.value:
            return await super().async_set_with_normalized_value(key, value)
        result = await self._directory_mutation.async_save(value)
        return SystemConfigWriteResult(
            changed=result.changed,
            normalized_value=result.normalized_value,
        )


_disk_topology: Optional[DiskTopology] = None


def configure_disk_topology(topology: Optional[DiskTopology]) -> None:
    """由启动组合根注入或清除本地磁盘拓扑适配器。"""
    global _disk_topology
    _disk_topology = topology


def configure_directory_classification_resolver(
    resolver: Optional[ClassificationCategoryResolver],
) -> Optional[ClassificationCategoryResolver]:
    """装配目录选择使用的分类引用解析器，并返回旧值供测试恢复。"""
    return configure_classification_category_resolver(resolver)


def reset_directory_classification_resolver(
    resolver: Optional[ClassificationCategoryResolver] = None,
) -> None:
    """恢复指定目录分类解析器；省略参数时清除启动组合状态。"""
    reset_classification_category_resolver(resolver)


def _directory_classification_resolver_snapshot() -> ClassificationCategoryResolver:
    """返回当前解析器；未装配时使用只支持兼容快照的空策略解析器。"""
    return classification_category_resolver_snapshot()


def _system_config_key(key: object) -> str:
    """把枚举或字符串配置键统一为持久化字符串。"""
    return key.value if isinstance(key, SystemConfigKey) else str(key)


def normalize_directory_system_config_value(
    key: object,
    value: object,
    *,
    classification_resolver: ClassificationCategoryResolver | None = None,
) -> object:
    """仅在目录配置写入时刷新稳定分类引用和兼容路径快照。"""
    normalized_key = _system_config_key(key)
    if normalized_key != SystemConfigKey.Directories.value:
        return value
    return normalize_directory_configurations(
        value,
        classification_resolver=classification_resolver,
    )


def normalize_directory_configurations(
    value: object,
    *,
    classification_resolver: ClassificationCategoryResolver | None = None,
) -> list[dict[str, Any]] | None:
    """校验目录配置，并把分类类型、稳定 ID 与当前路径快照规范化后返回。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise ValueError("目录配置必须是数组或 null")

    resolver = classification_resolver or _directory_classification_resolver_snapshot()
    normalized_items: list[dict[str, Any]] = []
    for index, item in enumerate(value):
        if item is None:
            continue
        if isinstance(item, _SchemaTransferDirectoryConf):
            normalized = item.model_dump()
        elif isinstance(item, dict):
            normalized = dict(item)
        else:
            raise ValueError(f"第 {index + 1} 项目录配置必须是对象")

        raw_media_type = normalized.get("media_type")
        media_type = classification_media_type(raw_media_type)
        if raw_media_type and media_type is None:
            raise ValueError(
                f"第 {index + 1} 项目录配置的媒体类型 {raw_media_type!r} 不受支持"
            )
        if raw_media_type:
            normalized["media_type"] = media_type

        try:
            directory = _SchemaTransferDirectoryConf.model_validate(normalized)
        except ValidationError as error:
            detail = error.errors(include_url=False)[0].get("msg", str(error))
            raise ValueError(f"第 {index + 1} 项目录配置无效：{detail}") from error

        category_id = str(directory.media_category_id or "").strip() or None
        resolution = resolver.resolve(
            category_id=category_id,
            path_snapshot=directory.media_category,
            media_type=directory.media_type,
        )
        if category_id and not resolution.stable:
            raise ValueError(
                f"第 {index + 1} 项目录配置的分类 ID 无效："
                f"{resolution.message or category_id}"
            )
        if directory.media_category and not resolution.usable:
            raise ValueError(
                f"第 {index + 1} 项目录配置的分类路径无效："
                f"{resolution.message or directory.media_category}"
            )

        normalized["media_category_id"] = resolution.category_id
        normalized["media_category"] = "/".join(resolution.path) or None
        normalized_items.append(normalized)
    return normalized_items or None


def normalize_directory_configurations_for_policy(
    value: object,
    policy: ClassificationPolicy | None,
) -> list[dict[str, Any]] | None:
    """仅使用调用方提供的事务内策略快照规范化目录配置。"""
    return normalize_directory_configurations(
        value,
        classification_resolver=ClassificationCategoryResolver(lambda: policy),
    )


def _is_same_local_disk(src: Path, dest: Path) -> bool:
    """通过已注入端口判断真实本地路径；未装配时稳定拒绝。"""
    if _disk_topology is None:
        raise RuntimeError("本地磁盘拓扑能力尚未由启动组合根配置")
    return _disk_topology.is_same_disk(src, dest)


class DirectoryHelper:
    """
    下载目录/媒体库目录帮助类
    """

    def __init__(
        self,
        classification_resolver: Optional[ClassificationCategoryResolver] = None,
    ) -> None:
        """保存显式解析器，未提供时读取启动组合根装配的当前实例。"""
        self._classification_resolver = (
            classification_resolver or _directory_classification_resolver_snapshot()
        )

    @staticmethod
    def get_dirs() -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有下载目录
        """
        dir_confs: List[dict] = get_configured_system_config().get(SystemConfigKey.Directories)
        if not dir_confs:
            return []
        return [_SchemaTransferDirectoryConf(**d) for d in dir_confs]

    def get_download_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有下载目录
        """
        return sorted([d for d in self.get_dirs() if d.download_path], key=lambda x: x.priority)

    def get_local_download_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有本地的可下载目录
        """
        return [d for d in self.get_download_dirs() if d.storage == "local"]

    def get_download_dir_by_save_path(
            self,
            media: Optional[DirectoryMedia],
            save_path: str,
    ) -> Optional[_SchemaTransferDirectoryConf]:
        """
        按媒体信息和精确保存根路径匹配下载目录配置。

        仅配置根目录本身继承自动分类规则；根目录下的自定义子目录保持调用方指定的完整路径。

        :param media: 媒体信息
        :param save_path: 已选择的下载保存目录，支持本地路径或远端 FileURI
        :return: 匹配的下载目录配置
        """
        value = str(save_path or "").strip()
        try:
            storage, raw_path = _split_file_uri(value)
            target_style, target_path = _normalize_download_path(raw_path, storage)
        except ValueError:
            return None

        candidates: list[tuple[int, _SchemaTransferDirectoryConf]] = []
        for dir_info in self.get_download_dirs():
            root = _normalize_download_root(dir_info)
            if not root:
                continue
            root_storage, root_style, root_path = root
            if storage != root_storage or target_style != root_style or target_path != root_path:
                continue
            rank = self.media_match_rank(
                dir_info,
                media,
                allow_stale_reference=True,
            )
            if rank is not None:
                candidates.append((rank, dir_info))
        if not candidates:
            return None
        return min(candidates, key=lambda item: item[0])[1]

    def get_library_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有媒体库目录
        """
        return sorted([d for d in self.get_dirs() if d.library_path], key=lambda x: x.priority)

    def get_local_library_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有本地的媒体库目录
        """
        return [d for d in self.get_library_dirs() if d.library_storage == "local"]

    def get_dir(self, media: Optional[DirectoryMedia], include_unsorted: Optional[bool] = False,
                storage: Optional[str] = None, src_path: Path = None,
                target_storage: Optional[str] = None, dest_path: Path = None
                ) -> Optional[_SchemaTransferDirectoryConf]:
        """
        根据媒体信息获取下载目录、媒体库目录配置
        :param media: 媒体信息
        :param include_unsorted: 包含不整理目录
        :param storage: 源存储类型
        :param target_storage: 目标存储类型
        :param src_path: 源目录，有值时直接匹配
        :param dest_path: 目标目录，有值时直接匹配
        """
        dirs = self.get_dirs()

        # 如果存在源目录，并源目录为任一下载目录的子目录时，则进行源目录匹配，否则，允许源目录按同盘优先的逻辑匹配
        matching_dirs = [d for d in dirs if src_path.is_relative_to(d.download_path)] if src_path else []
        # 根据是否有匹配的源目录，决定要考虑的目录集合
        dirs_to_consider = matching_dirs if matching_dirs else dirs

        # 已匹配的目录
        matched_dirs: list[tuple[int, int, _SchemaTransferDirectoryConf]] = []
        # 按照配置顺序查找
        for index, d in enumerate(dirs_to_consider):
            # 没有启用整理的目录
            if not d.monitor_type and not include_unsorted:
                continue
            # 源存储类型不匹配
            if storage and d.storage != storage:
                continue
            # 目标存储类型不匹配
            if target_storage and d.library_storage != target_storage:
                continue
            # 有目标目录时，目标目录不匹配媒体库目录
            if dest_path and dest_path != Path(d.library_path):
                continue
            rank = self.media_match_rank(d, media)
            if rank is not None:
                matched_dirs.append((rank, index, d))
                continue
        if matched_dirs:
            best_rank = min(rank for rank, _, _ in matched_dirs)
            best_dirs = [
                (index, directory)
                for rank, index, directory in matched_dirs
                if rank == best_rank
            ]
            best_dirs.sort(
                key=lambda item: (
                    item[1].priority if item[1].priority is not None else 0,
                    item[0],
                )
            )
            if src_path:
                # 同一匹配等级内优先源目录同盘，不能盖过更精确的分类引用。
                for _, matched_dir in best_dirs:
                    matched_path = Path(matched_dir.download_path)
                    if self._is_same_source((src_path, storage or "local"), (matched_path, matched_dir.library_storage)):
                        return matched_dir
            return best_dirs[0][1]
        return None

    def matches_media(
        self,
        directory: _SchemaTransferDirectoryConf,
        media: Optional[DirectoryMedia],
    ) -> bool:
        """按媒体类型和稳定分类引用判断目录是否适用。"""
        return self.media_match_rank(directory, media) is not None

    def media_match_rank(
        self,
        directory: _SchemaTransferDirectoryConf,
        media: Optional[DirectoryMedia],
        *,
        allow_stale_reference: bool = False,
    ) -> Optional[int]:
        """返回目录匹配等级；数值越小越精确，不匹配时返回空值。"""
        if media is None or not getattr(media, "type", None):
            return 3
        media_type = classification_media_type(getattr(media, "type", None))
        directory_type = classification_media_type(directory.media_type)
        if directory.media_type:
            if directory_type and media_type:
                if directory_type != media_type:
                    return None
            elif str(directory.media_type) != str(getattr(media, "type", None)):
                return None
        if not self.has_fixed_category(directory):
            return 2 if directory.media_type else 3

        directory_reference = self.resolve_directory_category(directory, media)
        if (
            getattr(directory, "media_category_id", None)
            and not directory_reference.stable
            and not allow_stale_reference
        ):
            return None
        if not directory_reference.usable:
            return None
        media_reference = self.resolve_media_category(media)
        if not media_reference.usable:
            return None
        if (
            (directory_reference.stable or media_reference.stable)
            and directory_reference.category_id
            and media_reference.category_id
        ):
            if directory_reference.category_id != media_reference.category_id:
                return None
            return 0
        return 1 if directory_reference.path == media_reference.path else None

    def resolve_media_category(
        self,
        media: Optional[DirectoryMedia],
    ) -> ClassificationCategoryResolution:
        """解析媒体当前生效的稳定分类引用和安全路径。"""
        resolution = self._classification_resolver.resolve_media(media)
        self._log_category_downgrade("媒体", resolution)
        return resolution

    def resolve_directory_category(
        self,
        directory: _SchemaTransferDirectoryConf,
        media: Optional[DirectoryMedia] = None,
    ) -> ClassificationCategoryResolution:
        """解析目录固定分类 ID，并在必要时显式降级到路径快照。"""
        resolution = self._classification_resolver.resolve(
            category_id=getattr(directory, "media_category_id", None),
            path_snapshot=getattr(directory, "media_category", None),
            media_type=(
                directory.media_type
                or (getattr(media, "type", None) if media is not None else None)
            ),
        )
        self._log_category_downgrade(
            f"目录 {directory.name or directory.download_path or directory.library_path or ''}".strip(),
            resolution,
        )
        return resolution

    def category_path_for_directory(
        self,
        directory: _SchemaTransferDirectoryConf,
        media: Optional[DirectoryMedia],
    ) -> tuple[str, ...]:
        """返回固定目录引用或媒体自动分类对应的安全相对路径。"""
        if self.has_fixed_category(directory):
            return self.resolve_directory_category(directory, media).path
        return self.resolve_media_category(media).path

    def classification_category_paths(
        self,
        media_type: object = None,
    ) -> tuple[tuple[str, ...], ...]:
        """返回当前活动策略中可用于边界识别的启用分类路径。"""
        return self._classification_resolver.category_paths(media_type)

    @staticmethod
    def has_fixed_category(directory: _SchemaTransferDirectoryConf) -> bool:
        """返回目录是否绑定了稳定 ID 或兼容路径分类。"""
        return bool(
            getattr(directory, "media_category_id", None)
            or getattr(directory, "media_category", None)
        )

    @staticmethod
    def _log_category_downgrade(
        owner: str,
        resolution: ClassificationCategoryResolution,
    ) -> None:
        """把 ID 失效和不安全路径转为明确日志，不静默扩大目录匹配。"""
        if resolution.downgraded and resolution.message:
            suffix = "" if resolution.usable else "；该分类引用不参与目录匹配"
            logger.warning(f"{owner}分类引用降级：{resolution.message}{suffix}")

    @staticmethod
    def _is_same_source(src: Tuple[Path, str],  tar: Tuple[Path, str]) -> bool:
        """
        判断源目录和目标目录是否在同一存储盘

        :param src: 源目录路径和存储类型
        :param tar: 目标目录路径和存储类型
        :return: 是否在同一存储盘
        """
        src_path, src_storage = src
        tar_path, tar_storage = tar
        if "local" == tar_storage == src_storage:
            return _is_same_local_disk(src_path, tar_path)
        # 网络存储，直接比较类型
        return src_storage == tar_storage

    @staticmethod
    def get_media_root_path(
            rename_format: str,
            rename_path: Path,
            media_type: Optional[MediaType] = None,
    ) -> Optional[Path]:
        """
        获取重命名后的媒体文件根路径

        :param rename_format: 重命名格式
        :param rename_path: 重命名后的路径
        :param media_type: 媒体类型；音乐需要避开可选碟片目录并返回专辑目录
        :return: 媒体文件根路径
        """
        if not rename_format:
            logger.error("重命名格式不能为空")
            return None
        if media_type == MediaType.MUSIC:
            # 音乐模板允许按多碟动态增加 Disc 子目录，不能按静态模板层数反推。
            # 文件的直接父目录通常就是专辑目录；命中碟片目录时再上移一级。
            media_root = rename_path.parent
            if re.fullmatch(
                    r"(?:cd|disc|disk)\s*0*\d+",
                    media_root.name,
                    re.IGNORECASE,
            ):
                media_root = media_root.parent
            return media_root
        # 计算重命名中的文件夹层数
        rename_list = rename_format.split("/")
        rename_format_level = len(rename_list) - 1
        # 反向查找标题参数所在层
        for level, name in enumerate(reversed(rename_list)):
            if level == 0:
                # 跳过文件名的标题参数
                continue
            matchs = JINJA2_VAR_PATTERN.findall(name)
            if not matchs:
                continue
            # 处理特例，有的人重命名的第一层是年份、分辨率
            if (any("title" in m for m in matchs)
                and not any("season" in m for m in matchs)):
                # 找出最后一层含有标题且不含季参数的目录作为媒体根目录
                rename_format_level = level
                break
        else:
            # 假定第一层目录是媒体根目录
            logger.warn(f"重命名格式 {rename_format} 缺少标题目录")
        if rename_format_level > len(rename_path.parents):
            # 通常因为路径以/结尾，被Path规范化删除了
            logger.error(f"路径 {rename_path} 不匹配重命名格式 {rename_format}")
            return None
        if rename_format_level <= 0:
            # 所有媒体文件都存在一个目录内的特殊需求
            rename_format_level = 1
        # 媒体根路径
        media_root = rename_path.parents[rename_format_level - 1]
        return media_root


def _split_file_uri(value: str) -> Tuple[str, str]:
    """
    拆分 FileURI 字符串，保留原始路径用于安全校验。
    """
    for storage in StorageSchema:
        protocol = f"{storage.value}:"
        if value.startswith(protocol):
            return storage.value, value[len(protocol):]
    return "local", value


def _normalize_safe_posix_path(raw_path: str) -> PurePosixPath:
    """
    规范化保存目录路径，并拒绝跨目录或跨平台歧义写法。
    """
    if not raw_path:
        raise ValueError("保存路径不能为空")
    if "\\" in raw_path:
        raise ValueError("保存路径不能包含反斜杠")
    if raw_path.startswith("//"):
        raise ValueError("保存路径不能使用 UNC 路径")
    if WINDOWS_DRIVE_PATTERN.match(raw_path):
        raise ValueError("保存路径不能使用 Windows 盘符路径")
    if not raw_path.startswith("/"):
        raise ValueError("保存路径必须是绝对路径")

    path = PurePosixPath(raw_path)
    parts = [part for part in path.parts if part != "/"]
    if ".." in parts:
        raise ValueError("保存路径不能包含上级目录")
    if parts and re.fullmatch(r"[A-Za-z]:", parts[0]):
        raise ValueError("保存路径不能使用 Windows 盘符路径")
    return path


def _normalize_safe_windows_path(raw_path: str) -> PureWindowsPath:
    """
    规范化已配置的 Windows 盘符路径；UNC 与反斜杠写法不参与下载目录 allowlist。
    """
    if not raw_path:
        raise ValueError("保存路径不能为空")
    if "\\" in raw_path:
        raise ValueError("保存路径不能包含反斜杠")
    if raw_path.startswith("//"):
        raise ValueError("保存路径不能使用 UNC 路径")
    if not WINDOWS_DRIVE_PATTERN.match(raw_path):
        raise ValueError("保存路径必须是 Windows 绝对路径")

    path = PureWindowsPath(raw_path)
    if ".." in path.parts:
        raise ValueError("保存路径不能包含上级目录")
    return path


def _normalize_download_path(raw_path: str, storage: str) -> Tuple[str, PurePath]:
    """
    按存储类型解析下载路径，本地允许 POSIX 或已配置的 Windows drive，远端保持 FileURI POSIX 语义。
    """
    path_value = str(raw_path or "").strip()
    if storage == "local" and WINDOWS_DRIVE_PREFIX_PATTERN.match(path_value):
        return "windows", _normalize_safe_windows_path(path_value)
    return "posix", _normalize_safe_posix_path(path_value)


def _download_path_uri(storage: str, path: PurePath) -> str:
    """
    生成可传给下载器的 save_path，保持 /download/paths 暴露的本地和远端路径风格。
    """
    path_value = path.as_posix()
    if storage == "local":
        return path_value
    return _SchemaFileURI(storage=storage, path=path_value).uri


def _normalize_download_root(dir_info: _SchemaTransferDirectoryConf) -> Optional[Tuple[str, str, PurePath]]:
    """
    读取下载目录配置中的根路径；无效配置不参与用户 save_path allowlist。
    """
    if not dir_info.download_path:
        return None
    storage = dir_info.storage or "local"
    try:
        path_style, root_path = _normalize_download_path(dir_info.download_path, storage)
        return storage, path_style, root_path
    except ValueError as err:
        logger.warn(f"跳过无效下载目录配置：{str(err)}")
        return None


def validate_download_save_path(save_path: str) -> str:
    """
    校验用户传入的下载保存目录，/download/paths 暴露的下载目录配置是允许写入的公共合同。

    :param save_path: 下载保存目录，支持本地 /path、远端 <storage>:/path 和旧版订阅中的无前缀远程路径
    :return: 可直接传给下载接口的规范化保存目录
    """
    value = str(save_path or "").strip()
    has_storage_prefix = any(value.startswith(f"{item.value}:") for item in StorageSchema)
    storage, raw_path = _split_file_uri(value)
    target_style, target_path = _normalize_download_path(raw_path, storage)

    download_roots = []
    for dir_info in DirectoryHelper().get_download_dirs():
        root = _normalize_download_root(dir_info)
        if root:
            download_roots.append(root)

    for root_storage, root_style, root_path in download_roots:
        if storage != root_storage:
            continue
        if target_style != root_style:
            continue
        if target_path == root_path or target_path.is_relative_to(root_path):
            return _download_path_uri(storage, target_path)

    # 旧版订阅界面只持久化 download_path，需要从已配置根目录恢复远程存储类型。
    if (not has_storage_prefix
            and storage == StorageSchema.Local.value
            and target_style == "posix"):
        for root_storage, root_style, root_path in download_roots:
            if root_storage == StorageSchema.Local.value or target_style != root_style:
                continue
            if target_path == root_path or target_path.is_relative_to(root_path):
                return _download_path_uri(root_storage, target_path)

    raise ValueError("保存路径不在允许的下载目录范围内")
