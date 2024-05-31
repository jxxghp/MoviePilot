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
