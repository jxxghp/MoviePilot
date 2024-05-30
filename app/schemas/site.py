
from pydantic import BaseModel


class Site(BaseModel):
    # ID
    id: int | None
    # 站点名称
    name: str | None
    # 站点主域名Key
    domain: str | None
    # 站点地址
    url: str | None
    # 站点优先级
    pri: int | None = 0
    # RSS地址
    rss: str | None = None
    # Cookie
    cookie: str | None = None
    # User-Agent
    ua: str | None = None
    # ApiKey
    apikey: str | None = None
    # Token
    token: str | None = None
    # 是否使用代理
    proxy: int | None = 0
    # 过滤规则
    filter: str | None = None
    # 是否演染
    render: int | None = 0
    # 是否公开站点
    public: int | None = 0
    # 备注
    note: str | None = None
    # 超时时间
    timeout: int | None = 0
    # 流控单位周期
    limit_interval: int | None = None
    # 流控次数
    limit_count: int | None = None
    # 流控间隔
    limit_seconds: int | None = None
    # 是否启用
    is_active: bool | None = True

    class Config:
        orm_mode = True


class SiteStatistic(BaseModel):
    # 站点ID
    domain: str | None
    # 成功次数
    success: int | None = 0
    # 失败次数
    fail: int | None = 0
    # 平均响应时间
    seconds: int | None = 0
    # 最后状态
    lst_state: int | None = 0
    # 最后修改时间
    lst_mod_date: str | None
    # 备注
    note: str | None = None

    class Config:
        orm_mode = True
