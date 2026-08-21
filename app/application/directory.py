import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple

from app.schemas.file import FileURI as _SchemaFileURI
from app.schemas.system import TransferDirectoryConf as _SchemaTransferDirectoryConf
from app.domain.context import MediaInfo
from app.domain.mediapath import resolve_media_root_path
from app.application.configuration import get_configured_system_config
from app.runtime.log import logger
from app.schemas.types import MediaType, StorageSchema, SystemConfigKey
from app.adapters.system.host import SystemUtils

WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")


class DirectoryHelper:
    """
    下载目录/媒体库目录帮助类
    """

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
        return [d for d in self.get_download_dirs() if _SchemaFileURI.is_local(d.storage)]

    def get_download_dir_by_save_path(
            self,
            media: Optional[MediaInfo],
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

        media_type = media.type.value if media else None
        for dir_info in self.get_download_dirs():
            root = _normalize_download_root(dir_info)
            if not root:
                continue
            root_storage, root_style, root_path = root
            if (not _SchemaFileURI.is_same_storage(storage, root_storage)
                    or target_style != root_style or target_path != root_path):
                continue
            if not media_type or not dir_info.media_type:
                return dir_info
            if dir_info.media_type == media_type and not dir_info.media_category:
                return dir_info
            if dir_info.media_type == media_type and dir_info.media_category == media.category:
                return dir_info
        return None

    def get_library_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有媒体库目录
        """
        return sorted([d for d in self.get_dirs() if d.library_path], key=lambda x: x.priority)

    def get_local_library_dirs(self) -> List[_SchemaTransferDirectoryConf]:
        """
        获取所有本地的媒体库目录
        """
        return [d for d in self.get_library_dirs() if _SchemaFileURI.is_local(d.library_storage)]

    def get_dir(self, media: Optional[MediaInfo], include_unsorted: Optional[bool] = False,
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
        # 电影/电视剧
        media_type = media.type.value if media else None
        dirs = self.get_dirs()

        # 如果存在源目录，并源目录为任一下载目录的子目录时，则进行源目录匹配，否则，允许源目录按同盘优先的逻辑匹配
        matching_dirs = [d for d in dirs if src_path.is_relative_to(d.download_path)] if src_path else []
        # 根据是否有匹配的源目录，决定要考虑的目录集合
        dirs_to_consider = matching_dirs if matching_dirs else dirs

        # 已匹配的目录
        matched_dirs: List[_SchemaTransferDirectoryConf] = []
        # 按照配置顺序查找
        for d in dirs_to_consider:
            # 没有启用整理的目录
            if not d.monitor_type and not include_unsorted:
                continue
            # 源存储实例不匹配
            if storage and not _SchemaFileURI.is_same_storage(d.storage, storage):
                continue
            # 目标存储实例不匹配
            if target_storage and not _SchemaFileURI.is_same_storage(d.library_storage, target_storage):
                continue
            # 有目标目录时，目标目录不匹配媒体库目录
            if dest_path and dest_path != Path(d.library_path):
                continue
            # 目录类型为全部的，符合条件
            if not media_type or not d.media_type:
                matched_dirs.append(d)
                continue
            # 目录类型相等，目录类别为全部，符合条件
            if d.media_type == media_type and not d.media_category:
                matched_dirs.append(d)
                continue
            # 目录类型相等，目录类别相等，符合条件
            if d.media_type == media_type and d.media_category == media.category:
                matched_dirs.append(d)
                continue
        if matched_dirs:
            if src_path:
                # 优先源目录同盘
                for matched_dir in matched_dirs:
                    matched_path = Path(matched_dir.download_path)
                    if self._is_same_source((src_path, storage or "local"), (matched_path, matched_dir.library_storage)):
                        return matched_dir
            return matched_dirs[0]
        return None

    @staticmethod
    def _is_same_source(src: Tuple[Path, str],  tar: Tuple[Path, str]) -> bool:
        """
        判断源目录和目标目录是否在同一存储盘

        :param src: 源目录路径和存储令牌
        :param tar: 目标目录路径和存储令牌
        :return: 是否在同一存储盘
        """
        src_path, src_storage = src
        tar_path, tar_storage = tar
        if _SchemaFileURI.is_local(src_storage) and _SchemaFileURI.is_local(tar_storage):
            return SystemUtils.is_same_disk(src_path, tar_path)
        # 网络存储，比较到实例，同类型的不同实例不算同一存储盘
        return _SchemaFileURI.is_same_storage(src_storage, tar_storage)

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
        result = resolve_media_root_path(rename_format, rename_path, media_type)
        if result.warning:
            logger.warn(result.warning)
        if result.error:
            logger.error(result.error)
        return result.path


def _split_file_uri(value: str) -> Tuple[str, str]:
    """
    拆分 FileURI 字符串，保留原始路径用于安全校验。
    """
    storage, raw_path = _SchemaFileURI.split_uri(value)
    return storage or StorageSchema.Local.value, raw_path


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
    if _SchemaFileURI.is_local(storage) and WINDOWS_DRIVE_PREFIX_PATTERN.match(path_value):
        return "windows", _normalize_safe_windows_path(path_value)
    return "posix", _normalize_safe_posix_path(path_value)


def _download_path_uri(storage: str, path: PurePath) -> str:
    """
    生成可传给下载器的 save_path，保持 /download/paths 暴露的本地和远端路径风格。

    只有默认本地实例的裸令牌省略存储前缀，具名本地实例保留前缀，否则回解析时会丢掉实例名。
    """
    return _SchemaFileURI(storage=storage, path=path.as_posix()).uri


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
    storage_prefix, raw_path = _SchemaFileURI.split_uri(value)
    has_storage_prefix = storage_prefix is not None
    storage = storage_prefix or StorageSchema.Local.value
    target_style, target_path = _normalize_download_path(raw_path, storage)

    download_roots = []
    for dir_info in DirectoryHelper().get_download_dirs():
        root = _normalize_download_root(dir_info)
        if root:
            download_roots.append(root)

    for root_storage, root_style, root_path in download_roots:
        if not _SchemaFileURI.is_same_storage(storage, root_storage):
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
            if _SchemaFileURI.is_local(root_storage) or target_style != root_style:
                continue
            if target_path == root_path or target_path.is_relative_to(root_path):
                return _download_path_uri(root_storage, target_path)

    raise ValueError("保存路径不在允许的下载目录范围内")
