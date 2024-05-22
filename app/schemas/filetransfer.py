from typing import Optional

from pydantic import BaseModel


class MediaDirectory(BaseModel):
    """
    下载目录/媒体库目录
    """
    # 类型 download/library
    type: Optional[str] = None
    # 别名
    name: Optional[str] = None
    # 路径
    path: Optional[str] = None
    # 媒体类型 电影/电视剧
    media_type: Optional[str] = None
    # 媒体类别 动画电影/国产剧
    category: Optional[str] = None
    # 刮削媒体信息
    scrape: Optional[bool] = False
    # 自动二级分类，未指定类别时自动分类
    auto_category: Optional[bool] = False
    # 优先级
    priority: Optional[int] = 0
