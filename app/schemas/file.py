"""
存储文件地址、文件条目和存储空间信息的数据结构。
"""

import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from pydantic import field_serializer as _field_serializer

from app.schemas.types import StorageSchema

# Windows 盘符绝对路径，如 Z:/Downloads 或 Z:\Downloads
WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:[\\/]")


class FileURI(BaseModel):
    """
    带存储类型的文件 URI。
    """

    path: Optional[str] = Field(default="/", description="文件路径")
    storage: Optional[str] = Field(default="local", description="存储类型，默认 local 表示本地存储")

    @property
    def uri(self) -> str:
        """
        文件 URI，本地存储直接返回路径，其他存储带上存储前缀。
        """
        return (self.path or "/") if self.storage == "local" else f"{self.storage}:{self.path}"

    @classmethod
    def from_uri(cls, uri: str) -> "FileURI":
        """
        解析文件 URI 为存储类型和路径。

        :param uri: 文件 URI，如 /media/movie、u115:/media/movie 或 Windows 盘符路径 Z:/media
        :return: FileURI 对象
        """
        storage, path = "local", uri
        for s in StorageSchema:
            protocol = f"{s.value}:"
            if uri.startswith(protocol):
                path = uri[len(protocol):]
                storage = s.value
                break
        # Windows 盘符路径本身就是绝对路径，补上根斜杠会得到 /Z:/xxx 这样的非法路径
        if not path.startswith("/") and not WINDOWS_DRIVE_PATTERN.match(path):
            path = "/" + path
        path = Path(path).as_posix()
        return cls(storage=storage, path=path)


class FileItem(FileURI):
    """
    文件或目录条目，目录可递归包含子条目。
    """

    type: Optional[str] = Field(default=None, description="条目类型，dir 表示目录，file 表示文件")
    name: Optional[str] = Field(default=None, description="文件名")
    basename: Optional[str] = Field(default=None, description="不含扩展名的文件基本名称")
    extension: Optional[str] = Field(default=None, description="文件后缀")
    size: Optional[int] = Field(default=None, description="文件大小，单位为字节；目录或来源未提供时可为 None")
    modify_time: Optional[float] = Field(default=None, description="文件修改时间的 Unix 时间戳，单位为秒，未提供时为 None")
    modify_time_ns: Optional[int] = Field(default=None, description="本地修改时间的 Unix 纳秒值，JSON 使用十进制字符串避免精度丢失；旧快照或远端未提供时为 None")
    change_time_ns: Optional[int] = Field(default=None, description="本地状态变化时间的 Unix 纳秒值，JSON 使用十进制字符串；用于同路径清理核验，未提供时为 None")
    children: Optional[list["FileItem"]] = Field(default_factory=list, description="子节点")
    fileid: Optional[str] = Field(default=None, description="存储服务分配的文件标识")
    parent_fileid: Optional[str] = Field(default=None, description="父ID")
    thumbnail: Optional[str] = Field(default=None, description="缩略图")
    pickcode: Optional[str] = Field(default=None, description="115 文件下载提取码")
    drive_id: Optional[str] = Field(default=None, description="云盘驱动器标识")
    url: Optional[str] = Field(default=None, description="访问地址")

    @_field_serializer("modify_time_ns", "change_time_ns", when_used="json")
    def serialize_local_timestamp(self, value: Optional[int]) -> Optional[str]:
        """
        使用十进制字符串传输纳秒时间，避免浏览器数字精度改变恢复凭据。

        :param value: Python 中的纳秒整数；未提供时为 None
        :return: 可无损回传的十进制字符串，未提供时保持 None
        """
        return str(value) if value is not None else None


class StorageUsage(BaseModel):
    """
    存储空间使用情况。
    """

    total: float = Field(default=0.0, description="存储总容量，单位为字节")
    available: float = Field(default=0.0, description="存储剩余容量，单位为字节")


class StorageTransType(BaseModel):
    """
    存储支持的传输类型及其显示名称。
    """

    transtype: Optional[dict[str, str]] = Field(default_factory=dict, description="传输类型")
