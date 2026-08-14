import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple

from app import schemas
from app.domain.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.platform.log import logger
from app.schemas.types import (
    DirectoryMatchMode,
    MediaType,
    StorageSchema,
    SystemConfigKey,
)
from app.infrastructure.system import SystemUtils

JINJA2_VAR_PATTERN = re.compile(r"\{\{.*?}}", re.DOTALL)
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")
DIRECTORY_MATCH_LEVEL_SCORES = {"wildcard": 0, "media_type": 1, "category": 2}


class DirectoryHelper:
    """
    下载目录/媒体库目录帮助类
    """

    @staticmethod
    def get_dirs() -> List[schemas.TransferDirectoryConf]:
        """
        获取所有下载目录
        """
        dir_confs: List[dict] = SystemConfigOper().get(SystemConfigKey.Directories)
        if not dir_confs:
            return []
        return [schemas.TransferDirectoryConf(**d) for d in dir_confs]

    def get_download_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有下载目录
        """
        return sorted([d for d in self.get_dirs() if d.download_path], key=lambda x: x.priority)

    def get_local_download_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有本地的可下载目录
        """
        return [d for d in self.get_download_dirs() if d.storage == "local"]

    def get_download_dir_by_save_path(
            self,
            media: Optional[MediaInfo],
            save_path: str,
            match_mode: Optional[DirectoryMatchMode] = None,
    ) -> Optional[schemas.TransferDirectoryConf]:
        """
        按媒体信息和精确保存根路径匹配下载目录配置。

        仅配置根目录本身继承自动分类规则；根目录下的自定义子目录保持调用方指定的完整路径。

        :param media: 媒体信息
        :param save_path: 已选择的下载保存目录，支持本地路径或远端 FileURI
        :param match_mode: 目录候选选择模式，未传入时读取系统配置
        :return: 匹配的下载目录配置
        """
        value = str(save_path or "").strip()
        try:
            storage, raw_path = _split_file_uri(value)
            target_style, target_path = _normalize_download_path(raw_path, storage)
        except ValueError:
            return None

        matched_dirs = []
        for dir_info in self.get_download_dirs():
            root = _normalize_download_root(dir_info)
            if not root:
                continue
            root_storage, root_style, root_path = root
            if storage != root_storage or target_style != root_style or target_path != root_path:
                continue
            matched_dirs.append(dir_info)
        return self.evaluate_route(
            media=media,
            directories=matched_dirs,
            include_unsorted=True,
            match_mode=match_mode,
        ).selected_directory

    def get_library_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有媒体库目录
        """
        return sorted([d for d in self.get_dirs() if d.library_path], key=lambda x: x.priority)

    def get_local_library_dirs(self) -> List[schemas.TransferDirectoryConf]:
        """
        获取所有本地的媒体库目录
        """
        return [d for d in self.get_library_dirs() if d.library_storage == "local"]

    def get_dir(self, media: Optional[MediaInfo], include_unsorted: Optional[bool] = False,
                storage: Optional[str] = None, src_path: Path = None,
                target_storage: Optional[str] = None, dest_path: Path = None,
                match_mode: Optional[DirectoryMatchMode] = None,
                ) -> Optional[schemas.TransferDirectoryConf]:
        """
        根据媒体信息获取下载目录、媒体库目录配置
        :param media: 媒体信息
        :param include_unsorted: 包含不整理目录
        :param storage: 源存储类型
        :param target_storage: 目标存储类型
        :param src_path: 源目录，有值时直接匹配
        :param dest_path: 目标目录，有值时直接匹配
        :param match_mode: 目录候选选择模式，未传入时读取系统配置
        """
        return self.evaluate_route(
            media=media,
            include_unsorted=include_unsorted,
            storage=storage,
            src_path=src_path,
            target_storage=target_storage,
            dest_path=dest_path,
            match_mode=match_mode,
        ).selected_directory

    @staticmethod
    def get_match_mode(
            match_mode: Optional[DirectoryMatchMode] = None,
    ) -> DirectoryMatchMode:
        """
        获取有效目录匹配模式，缺失或非法配置回退为顺序模式。

        :param match_mode: 调用方显式指定的匹配模式
        :return: 有效匹配模式
        """
        value = match_mode
        if value is None:
            value = SystemConfigOper().get(SystemConfigKey.DirectoryMatchMode)
        try:
            return DirectoryMatchMode(value or DirectoryMatchMode.SEQUENTIAL)
        except (TypeError, ValueError):
            return DirectoryMatchMode.SEQUENTIAL

    def evaluate_route(
            self,
            media: Optional[MediaInfo],
            directories: Optional[List[schemas.TransferDirectoryConf]] = None,
            include_unsorted: Optional[bool] = False,
            storage: Optional[str] = None,
            src_path: Path = None,
            target_storage: Optional[str] = None,
            dest_path: Path = None,
            match_mode: Optional[DirectoryMatchMode] = None,
            valid_categories: Optional[List[str]] = None,
    ) -> schemas.DirectoryRouteDecision:
        """
        求值目录候选并按指定模式返回完整路由决策。

        :param media: 媒体信息
        :param directories: 可选目录配置草稿，未传入时读取当前配置
        :param include_unsorted: 包含未启用监控整理的目录
        :param storage: 源存储类型
        :param src_path: 源路径
        :param target_storage: 目标存储类型
        :param dest_path: 显式目标路径
        :param match_mode: 目录候选选择模式
        :param valid_categories: 当前媒体类型可用的分类名称
        :return: 目录候选、排除原因、警告和最终选择
        """
        mode = self.get_match_mode(match_mode)
        dirs = self.get_dirs() if directories is None else list(directories)
        media_type = self._media_type_value(media)

        source_match_indices = []
        if src_path:
            source_match_indices = [
                index
                for index, directory in enumerate(dirs)
                if directory.download_path
                and src_path.is_relative_to(Path(directory.download_path))
            ]
        source_match_set = set(source_match_indices)

        candidates = []
        for index, directory in enumerate(dirs):
            reasons = []
            if source_match_set and index not in source_match_set:
                reasons.append(schemas.RouteDiagnosticReason(
                    code="source_path_scope_mismatch",
                    message="源路径已命中其它下载目录",
                ))
            if not directory.monitor_type and not include_unsorted:
                reasons.append(schemas.RouteDiagnosticReason(
                    code="monitor_disabled",
                    message="目录未启用整理监控",
                ))
            if storage and directory.storage != storage:
                reasons.append(schemas.RouteDiagnosticReason(
                    code="source_storage_mismatch",
                    message="源存储类型不匹配",
                ))
            if target_storage and directory.library_storage != target_storage:
                reasons.append(schemas.RouteDiagnosticReason(
                    code="target_storage_mismatch",
                    message="目标存储类型不匹配",
                ))
            if dest_path and (
                    not directory.library_path
                    or dest_path != Path(directory.library_path)
            ):
                reasons.append(schemas.RouteDiagnosticReason(
                    code="destination_path_mismatch",
                    message="显式目标路径不匹配",
                ))

            match_level = self._media_match_level(directory, media)
            if not reasons and match_level == "none":
                reasons.append(schemas.RouteDiagnosticReason(
                    code="media_rule_mismatch",
                    message="媒体类型或类别不匹配",
                ))
            eligible = not reasons
            same_source = None
            if eligible and src_path:
                same_source = bool(
                    directory.download_path
                    and self._is_same_source(
                        (src_path, storage or "local"),
                        (Path(directory.download_path), directory.library_storage),
                    )
                )
            candidates.append(schemas.DirectoryRouteCandidate(
                index=index,
                directory=directory,
                eligible=eligible,
                match_level=match_level,
                same_source=same_source,
                reasons=reasons,
            ))

        eligible_candidates = [candidate for candidate in candidates if candidate.eligible]
        same_source_candidates = [
            candidate for candidate in eligible_candidates if candidate.same_source
        ]
        selection_pool = same_source_candidates or eligible_candidates
        selected = self._select_candidate(selection_pool, mode, bool(media_type))
        if selected:
            selected.selected = True

        warnings = self._directory_warnings(
            dirs=dirs,
            selection_pool=selection_pool,
            selected=selected,
            mode=mode,
            valid_categories=valid_categories,
            media_type=media_type,
        )
        return schemas.DirectoryRouteDecision(
            mode=mode,
            selected_index=selected.index if selected else None,
            selected_directory=selected.directory if selected else None,
            candidates=candidates,
            warnings=warnings,
        )

    @staticmethod
    def _media_type_value(media: Optional[MediaInfo]) -> Optional[str]:
        """获取兼容枚举和字符串的媒体类型值。"""
        if not media or not media.type:
            return None
        return media.type.value if isinstance(media.type, MediaType) else str(media.type)

    @classmethod
    def _media_match_level(
            cls,
            directory: schemas.TransferDirectoryConf,
            media: Optional[MediaInfo],
    ) -> str:
        """返回目录规则对媒体的匹配精确度。"""
        media_type = cls._media_type_value(media)
        if not media_type or not directory.media_type:
            return "wildcard"
        if directory.media_type != media_type:
            return "none"
        if not directory.media_category:
            return "media_type"
        if directory.media_category == media.category:
            return "category"
        return "none"

    @staticmethod
    def _select_candidate(
            candidates: List[schemas.DirectoryRouteCandidate],
            mode: DirectoryMatchMode,
            has_media_type: bool,
    ) -> Optional[schemas.DirectoryRouteCandidate]:
        """在硬约束候选池中选择最终目录。"""
        if not candidates:
            return None
        if mode == DirectoryMatchMode.SEQUENTIAL or not has_media_type:
            return candidates[0]
        highest_score = max(
            DIRECTORY_MATCH_LEVEL_SCORES[candidate.match_level]
            for candidate in candidates
        )
        return next(
            candidate
            for candidate in candidates
            if DIRECTORY_MATCH_LEVEL_SCORES[candidate.match_level] == highest_score
        )

    @staticmethod
    def _directory_warnings(
            dirs: List[schemas.TransferDirectoryConf],
            selection_pool: List[schemas.DirectoryRouteCandidate],
            selected: Optional[schemas.DirectoryRouteCandidate],
            mode: DirectoryMatchMode,
            valid_categories: Optional[List[str]],
            media_type: Optional[str],
    ) -> List[schemas.RouteDiagnosticWarning]:
        """生成目录配置与当前选择的非阻断警告。"""
        warnings = []
        if not selected:
            warnings.append(schemas.RouteDiagnosticWarning(
                code="no_matching_directory",
                message="没有目录满足当前硬约束和媒体规则",
            ))
        elif mode == DirectoryMatchMode.SEQUENTIAL and any(
                DIRECTORY_MATCH_LEVEL_SCORES[candidate.match_level]
                > DIRECTORY_MATCH_LEVEL_SCORES[selected.match_level]
                for candidate in selection_pool
        ):
            warnings.append(schemas.RouteDiagnosticWarning(
                code="generic_before_specific",
                message="顺序模式下通用目录先于更精确的类别目录命中",
                related_indices=[candidate.index for candidate in selection_pool],
            ))

        if valid_categories is not None:
            invalid_indices = [
                index
                for index, directory in enumerate(dirs)
                if directory.media_category
                and (
                    not directory.media_type
                    or directory.media_type == media_type
                )
                and directory.media_category not in valid_categories
            ]
            if invalid_indices:
                warnings.append(schemas.RouteDiagnosticWarning(
                    code="unknown_media_category",
                    message="目录引用了当前分类配置中不存在的二级分类",
                    related_indices=invalid_indices,
                ))

        duplicate_indices = []
        conditions = {}
        for index, directory in enumerate(dirs):
            condition = (
                directory.storage,
                directory.download_path,
                directory.media_type,
                directory.media_category,
            )
            target = (directory.library_storage, directory.library_path)
            previous = conditions.get(condition)
            if previous and previous[1] != target:
                duplicate_indices.extend([previous[0], index])
            else:
                conditions[condition] = (index, target)
        if duplicate_indices:
            warnings.append(schemas.RouteDiagnosticWarning(
                code="duplicate_directory_conditions",
                message="多个相同目录条件指向不同目标路径",
                related_indices=sorted(set(duplicate_indices)),
            ))
        return warnings

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
            return SystemUtils.is_same_disk(src_path, tar_path)
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
    return schemas.FileURI(storage=storage, path=path_value).uri


def _normalize_download_root(dir_info: schemas.TransferDirectoryConf) -> Optional[Tuple[str, str, PurePath]]:
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
