import re
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import List, Optional, Tuple

from app import schemas
from app.core.context import MediaInfo
from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import StorageSchema, SystemConfigKey
from app.utils.system import SystemUtils

JINJA2_VAR_PATTERN = re.compile(r"\{\{.*?}}", re.DOTALL)
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")
WINDOWS_DRIVE_PREFIX_PATTERN = re.compile(r"^[A-Za-z]:")


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
                target_storage: Optional[str] = None, dest_path: Path = None
                ) -> Optional[schemas.TransferDirectoryConf]:
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
        matched_dirs: List[schemas.TransferDirectoryConf] = []
        # 按照配置顺序查找
        for d in dirs_to_consider:
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
    def get_media_root_path(rename_format: str, rename_path: Path) -> Optional[Path]:
        """
        获取重命名后的媒体文件根路径

        :param rename_format: 重命名格式
        :param rename_path: 重命名后的路径
        :return: 媒体文件根路径
        """
        if not rename_format:
            logger.error("重命名格式不能为空")
            return None
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

    :param save_path: 下载保存目录，支持本地 /path 或远端 <storage>:/path
    :return: 可直接传给下载接口的规范化保存目录
    """
    value = str(save_path or "").strip()
    storage, raw_path = _split_file_uri(value)
    target_style, target_path = _normalize_download_path(raw_path, storage)

    for dir_info in DirectoryHelper().get_download_dirs():
        root = _normalize_download_root(dir_info)
        if not root:
            continue
        root_storage, root_style, root_path = root
        if storage != root_storage:
            continue
        if target_style != root_style:
            continue
        if target_path == root_path or target_path.is_relative_to(root_path):
            return _download_path_uri(storage, target_path)

    raise ValueError("保存路径不在允许的下载目录范围内")
