from typing import Optional

from pydantic import BaseModel, Field


class FileItem(BaseModel):
    # 存储类型
    storage: Optional[str] = "local"
    # 类型 dir/file
    type: Optional[str] = None
    # 文件路径
    path: Optional[str] = "/"
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
    children: Optional[list] = Field(default_factory=list)
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
    # 总空间
    total: float = 0.0
    # 剩余空间
    available: float = 0.0


class StorageTransType(BaseModel):
    # 传输类型
    transtype: Optional[dict] = Field(default_factory=dict)
