import re
from typing import Optional

from pathlib import Path
from pydantic import BaseModel, Field
from app.schemas.types import StorageSchema

# Windows 盘符绝对路径，如 Z:/Downloads 或 Z:\Downloads
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")

# 存储标识：字母开头且长度不小于 2，与单字母的 Windows 盘符区分开
_STORAGE_SCHEME_EXPR = r"[A-Za-z][A-Za-z0-9_.+-]+"
_STORAGE_SCHEME_PATTERN = re.compile(rf"^{_STORAGE_SCHEME_EXPR}$")
_STORAGE_URI_PATTERN = re.compile(rf"^({_STORAGE_SCHEME_EXPR}):(.*)$", re.DOTALL)


class FileURI(BaseModel):
    """带存储类型的文件 URI。"""

    # 文件路径
    path: Optional[str] = "/"
    # 存储类型
    storage: Optional[str] = Field(default=StorageSchema.Local.value)

    @property
    def uri(self) -> str:
        """
        文件 URI，本地存储直接返回路径，其他存储带上存储前缀
        """
        return self.path if self.storage == StorageSchema.Local.value else f"{self.storage}:{self.path}"

    @classmethod
    def is_storage_scheme(cls, value: str) -> bool:
        """
        判断字符串能否作为文件 URI 的存储前缀

        :param value: 待判断的存储标识
        :return: 可作为存储前缀时为 True
        """
        return bool(value) and bool(_STORAGE_SCHEME_PATTERN.match(value))

    @classmethod
    def split_uri(cls, uri: str) -> tuple[Optional[str], str]:
        """
        拆分文件 URI 的存储前缀与路径，路径原样保留

        :param uri: 文件 URI，如 /media/movie、u115:/media/movie 或 Windows 盘符路径 Z:/media
        :return: 存储标识与去掉前缀的路径；无存储前缀时存储标识为 None
        """
        matched = _STORAGE_URI_PATTERN.match(uri or "")
        if not matched:
            return None, uri
        return matched.group(1), matched.group(2)

    @classmethod
    def from_uri(cls, uri: str) -> "FileURI":
        """
        解析文件 URI 为存储类型和路径

        :param uri: 文件 URI，如 /media/movie、u115:/media/movie 或 Windows 盘符路径 Z:/media
        :return: FileURI 对象
        """
        storage, path = cls.split_uri(uri)
        storage = storage or StorageSchema.Local.value
        # Windows 盘符路径本身就是绝对路径，补上根斜杠会得到 /Z:/xxx 这样的非法路径
        if not path.startswith("/") and not WINDOWS_DRIVE_PATTERN.match(path):
            path = "/" + path
        path = Path(path).as_posix()
        return cls(storage=storage, path=path)


class FileItem(FileURI):
    """文件或目录条目，目录可递归包含子条目。"""

    # 类型 dir/file
    type: Optional[str] = None
    # 文件名
    name: Optional[str] = None
    # 文件名
    basename: Optional[str] = None
    # 文件后缀
    extension: Optional[str] = None
    # 文件大小
    size: Optional[int] = None
    # 修改时间
    modify_time: Optional[float] = None
    # 子节点
    children: Optional[list["FileItem"]] = Field(default_factory=list)
    # ID
    fileid: Optional[str] = None
    # 父ID
    parent_fileid: Optional[str] = None
    # 缩略图
    thumbnail: Optional[str] = None
    # 115 pickcode
    pickcode: Optional[str] = None
    # drive_id
    drive_id: Optional[str] = None
    # url
    url: Optional[str] = None


class StorageUsage(BaseModel):
    """存储空间使用情况。"""

    # 总空间
    total: float = 0.0
    # 剩余空间
    available: float = 0.0


class StorageTransType(BaseModel):
    """存储支持的传输类型及其显示名称。"""

    # 传输类型
    transtype: Optional[dict[str, str]] = Field(default_factory=dict)
