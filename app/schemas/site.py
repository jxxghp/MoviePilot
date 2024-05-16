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
    # ApiKey
    apikey: Optional[str] = None
    # Token
    token: Optional[str] = None
    # 是否使用代理
    proxy: Optional[int] = 0
    # 过滤规则
    filter: Optional[str] = None
    # 是否演染
    render: Optional[int] = 0
    # 是否公开站点
    public: Optional[int] = 0
    # 备注
    note: Optional[str] = None
    # 超时时间
    timeout: Optional[int] = 0
    # 流控单位周期
    limit_interval: Optional[int] = None
    # 流控次数
    limit_count: Optional[int] = None
    # 流控间隔
    limit_seconds: Optional[int] = None
    # 是否启用
    is_active: Optional[bool] = True

    class Config:
        orm_mode = True


class SiteStatistic(BaseModel):
    # 站点ID
    domain: Optional[str]
    # 成功次数
    success: Optional[int] = 0
    # 失败次数
    fail: Optional[int] = 0
    # 平均响应时间
    seconds: Optional[int] = 0
    # 最后状态
    lst_state: Optional[int] = 0
    # 最后修改时间
    lst_mod_date: Optional[str]
    # 备注
    note: Optional[str] = None

    class Config:
        orm_mode = True
