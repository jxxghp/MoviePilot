from typing import Dict, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.schemas.common import JsonData

SiteUnreadMessage = Union[
    tuple[Optional[str], Optional[str], Optional[str]],
    tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
]
"""站点未读消息，第四项为部分站点提供的持久化去重来源。"""


class Site(BaseModel):
    """站点配置及运行状态。"""

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
    note: Optional[JsonData] = None
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

    model_config = ConfigDict(from_attributes=True)


class SitePriorityUpdate(BaseModel):
    """站点批量优先级更新项。"""

    id: int = Field(..., description="Persistent site ID returned by site.list.")
    pri: int = Field(..., description="Replacement site search priority value.")


class SiteStatistic(BaseModel):
    """单个站点的访问成功率与耗时统计。"""

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
    note: Optional[Dict[str, int]] = None

    model_config = ConfigDict(from_attributes=True)


class SiteUserData(BaseModel):
    """站点用户账户、流量、做种和未读消息数据。"""

    # 站点域名
    domain: Optional[str] = None
    # 站点名称
    name: Optional[str] = None
    # 用户名
    username: Optional[str] = None
    # 用户ID
    userid: Optional[Union[str, int]] = None
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
    seeding_info: Optional[list[tuple[int, int]]] = Field(default_factory=list)
    # 未读消息
    message_unread: Optional[int] = 0
    # 未读消息内容
    message_unread_contents: Optional[list[SiteUnreadMessage]] = Field(default_factory=list)
    # 错误信息
    err_msg: Optional[str] = None
    # 更新日期
    updated_day: Optional[str] = None
    # 更新时间
    updated_time: Optional[str] = None

    # 查询仓储返回 SQLAlchemy ORM 对象时，从对象属性读取字段。
    model_config = ConfigDict(from_attributes=True)


class SiteAuth(BaseModel):
    """站点认证模块及其参数。"""

    site: Optional[str] = None
    params: Optional[Dict[str, Union[int, str]]] = Field(default_factory=dict)


class SiteCookieUpdate(BaseModel):
    """
    站点 Cookie 与 UA 更新请求。
    """
    username: str = Field(..., description="站点登录用户名")
    password: str = Field(..., description="站点登录密码")
    code: Optional[str] = Field(None, description="二步验证码或密钥")


class SiteCategory(BaseModel):
    """站点资源分类。"""

    id: Optional[int] = None
    cat: Optional[str] = None
    desc: Optional[str] = None


class SiteIconData(BaseModel):
    """站点图标地址或 Base64 内容。"""

    icon: str


class SiteMappingData(RootModel[dict[str, str]]):
    """站点域名到显示名称的映射。"""
