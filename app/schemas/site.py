from typing import Optional

from pydantic import BaseModel


class Site(BaseModel):
    # ID
    id: Optional[int]
    # 站点名称
    name: Optional[str]
    # 站点主域名Key
    domain: Optional[str]
    # 站点地址
    url: Optional[str]
    # 站点优先级
    pri: Optional[int] = 0
    # RSS地址
    rss: Optional[str] = None
    # Cookie
    cookie: Optional[str] = None
    # User-Agent
    ua: Optional[str] = None
    # 是否使用代理
    proxy: Optional[int] = 0
    # 过滤规则
    filter: Optional[str] = None
    # 是否演染
    render: Optional[int] = 0
    # 备注
    note: Optional[str] = None
    # 流控单位周期
    limit_interval: Optional[int] = 0
    # 流控次数
    limit_count: Optional[int] = 0
    # 流控间隔
    limit_seconds: Optional[int] = 0
    # 是否启用
    is_active: Optional[bool] = True

    class Config:
        orm_mode = True
