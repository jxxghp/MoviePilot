
from pydantic import BaseModel


class FileItem(BaseModel):
    # 类型 dir/file
    type: str | None = None
    # 文件路径
    path: str | None = None
    # 文件名
    name: str | None = None
    # 文件名
    basename: str | None = None
    # 文件后缀
    extension: str | None = None
    # 文件大小
    size: int | None = None
    # 修改时间
    modify_time: float | None = None
