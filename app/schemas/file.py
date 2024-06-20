from typing import Optional

from pydantic import BaseModel


class FileItem(BaseModel):
    # 类型 dir/file
    type: Optional[str] = None
    # 文件路径
    path: Optional[str] = None
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
    children: Optional[list] = []
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
