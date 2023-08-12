from typing import Optional

from pydantic import BaseModel


class Rss(BaseModel):
    id: Optional[int]
    # 名称
    name: Optional[str]
    # RSS地址
    url: Optional[str]
    # 类型
    type: Optional[str]
    # 标题
    title: Optional[str]
    # 年份
    year: Optional[str]
    # TMDBID
    tmdbid: Optional[int]
    # 季号
    season: Optional[int]
    # 海报
    poster: Optional[str]
    # 背景图
    backdrop: Optional[str]
    # 评分
    vote: Optional[float]
    # 简介
    description: Optional[str]
    # 总集数
    total_episode: Optional[int]
    # 包含
    include: Optional[str]
    # 排除
    exclude: Optional[str]
    # 洗版
    best_version: Optional[int]
    # 是否使用代理服务器
    proxy: Optional[int]
    # 是否使用过滤规则
    filter: Optional[int]
    # 保存路径
    save_path: Optional[str]
    # 附加信息
    note: Optional[str]
    # 已处理数量
    processed: Optional[int]
    # 最后更新时间
    last_update: Optional[str]
    # 状态 0-停用，1-启用
    state: Optional[int]

    class Config:
        orm_mode = True
