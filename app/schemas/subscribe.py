from typing import Optional

from pydantic import BaseModel


class Subscribe(BaseModel):
    id: Optional[int]
    # 订阅名称
    name: Optional[str]
    # 订阅年份
    year: Optional[str]
    # 订阅类型 电影/电视剧
    type: Optional[str]
    # 搜索关键字
    keyword: Optional[str]
    tmdbid: Optional[int]
    doubanid: Optional[str]
    # 季号
    season: Optional[int]
    # 海报
    poster: Optional[str]
    # 背景图
    backdrop: Optional[str]
    # 评分
    vote: Optional[int]
    # 描述
    description: Optional[str]
    # 过滤规则
    filter: Optional[str]
    # 包含
    include: Optional[str]
    # 排除
    exclude: Optional[str]
    # 总集数
    total_episode: Optional[int]
    # 开始集数
    start_episode: Optional[int]
    # 缺失集数
    lack_episode: Optional[int]
    # 附加信息
    note: Optional[str]
    # 状态：N-新建， R-订阅中
    state: Optional[str]

    class Config:
        orm_mode = True
