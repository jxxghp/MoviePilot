from typing import Optional, Any, Union, Dict

from pydantic import BaseModel, Field


class Site(BaseModel):
    # ID
    id: Optional[int] = None
    # 站点名称
    name: Optional[str] = None
    # 站点主域名Key
    domain: Optional[str] = None
    # 站点地址
    url: Optional[str] = None
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
    note: Optional[Any] = None
    # 超时时间
    timeout: Optional[int] = 15
    # 流控单位周期
    limit_interval: Optional[int] = None
    # 流控次数
    limit_count: Optional[int] = None
    # 流控间隔
    limit_seconds: Optional[int] = None
    # 是否启用
    is_active: Optional[bool] = True
    # 下载器
    downloader: Optional[str] = None

    class Config:
        orm_mode = True


class SiteStatistic(BaseModel):
    # 站点ID
    domain: Optional[str] = None
    # 成功次数
    success: Optional[int] = 0
    # 失败次数
    fail: Optional[int] = 0
    # 平均响应时间
    seconds: Optional[int] = 0
    # 最后状态
    lst_state: Optional[int] = 0
    # 最后修改时间
    lst_mod_date: Optional[str] = None
    # 备注
    note: Optional[Any] = None

    class Config:
        orm_mode = True


class SiteUserData(BaseModel):
    # 站点域名
    domain: Optional[str] = None
    # 用户名
    username: Optional[str] = None
    # 用户ID
    userid: Optional[Union[int, str]] = None
    # 用户等级
    user_level: Optional[str] = None
    # 加入时间
    join_at: Optional[str] = None
    # 积分
    bonus: Optional[float] = 0.0
    # 上传量
    upload: Optional[int] = 0
    # 下载量
    download: Optional[int] = 0
    # 分享率
    ratio: Optional[float] = 0.0
    # 做种数
    seeding: Optional[int] = 0
    # 下载数
    leeching: Optional[int] = 0
    # 做种体积
    seeding_size: Optional[int] = 0
    # 下载体积
    leeching_size: Optional[int] = 0
    # 做种人数, 种子大小
    seeding_info: Optional[list] = Field(default_factory=list)
    # 未读消息
    message_unread: Optional[int] = 0
    # 未读消息内容
    message_unread_contents: Optional[list] = Field(default_factory=list)
    # 错误信息
    err_msg: Optional[str] = None
    # 更新日期
    updated_day: Optional[str] = None
    # 更新时间
    updated_time: Optional[str] = None


class SiteAuth(BaseModel):
    site: Optional[str] = None
    params: Optional[Dict[str, Union[int, str]]] = Field(default_factory=dict)


class SiteCategory(BaseModel):
    id: Optional[int] = None
    cat: Optional[str] = None
    desc: Optional[str] = None
