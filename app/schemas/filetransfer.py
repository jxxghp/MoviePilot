
from pydantic import BaseModel


class MediaDirectory(BaseModel):
    """
    下载目录/媒体库目录
    """
    # 类型 download/library
    type: str | None = None
    # 别名
    name: str | None = None
    # 路径
    path: str | None = None
    # 媒体类型 电影/电视剧
    media_type: str | None = None
    # 媒体类别 动画电影/国产剧
    category: str | None = None
    # 刮削媒体信息
    scrape: bool | None = False
    # 自动二级分类，未指定类别时自动分类
    auto_category: bool | None = False
    # 优先级
    priority: int | None = 0
