"""
站点配置、分类、登录凭据和认证结果的数据结构。
"""

from typing import Dict, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel

from app.schemas.common import JsonData

SiteUnreadMessage = Union[
    tuple[Optional[str], Optional[str], Optional[str]],
    tuple[Optional[str], Optional[str], Optional[str], Optional[str]],
]
"""
站点未读消息，第四项为部分站点提供的持久化去重来源。
"""
class Site(BaseModel):
    """
    站点配置及运行状态。
    """
    id: Optional[int] = Field(default=None, description="记录标识")
    name: Optional[str] = Field(default=None, description="站点名称")
    domain: Optional[str] = Field(default=None, description="用于匹配站点的主域名键")
    url: Optional[str] = Field(default=None, description="站点地址")
    pri: Optional[int] = Field(default=0, description="站点搜索优先级，数值越小越靠前，默认 0")
    rss: Optional[str] = Field(default=None, description="RSS地址")
    cookie: Optional[str] = Field(default=None, description="站点登录凭据")
    ua: Optional[str] = Field(default=None, description="站点请求使用的浏览器标识")
    apikey: Optional[str] = Field(default=None, description="站点接口访问密钥")
    token: Optional[str] = Field(default=None, description="认证令牌")
    proxy: Optional[int] = Field(default=0, description="是否使用代理")
    filter: Optional[str] = Field(default=None, description="过滤规则")
    render: Optional[int] = Field(default=0, description="是否使用浏览器渲染站点页面，0 表示关闭")
    public: Optional[int] = Field(default=0, description="是否公开站点")
    note: Optional[JsonData] = Field(default=None, description="备注")
    timeout: Optional[int] = Field(default=15, description="单次站点请求的超时秒数，默认 15 秒")
    limit_interval: Optional[int] = Field(default=None, description="请求流控统计窗口长度，单位为秒；None 表示未配置")
    limit_count: Optional[int] = Field(default=None, description="流控统计窗口内允许的最大请求数；None 表示未配置")
    limit_seconds: Optional[int] = Field(default=None, description="相邻请求之间的最小间隔，单位为秒；None 表示未配置")
    is_active: Optional[bool] = Field(default=True, description="是否启用")
    downloader: Optional[str] = Field(default=None, description="下载器")

    model_config = ConfigDict(from_attributes=True)


class SitePriorityUpdate(BaseModel):
    """
    站点批量优先级更新项。
    """

    id: int = Field(..., description="记录标识")
    pri: int = Field(..., description="站点搜索优先级")


class SiteStatistic(BaseModel):
    """
    单个站点的访问成功率与耗时统计。
    """

    domain: Optional[str] = Field(default=None, description="站点ID")
    success: Optional[int] = Field(default=0, description="成功次数")
    fail: Optional[int] = Field(default=0, description="失败次数")
    seconds: Optional[int] = Field(default=0, description="平均响应时间")
    lst_state: Optional[int] = Field(default=0, description="最后状态")
    lst_mod_date: Optional[str] = Field(default=None, description="最后修改时间")
    note: Optional[Dict[str, int]] = Field(default=None, description="备注")

    model_config = ConfigDict(from_attributes=True)


class SiteUserData(BaseModel):
    """
    站点用户账户、流量、做种和未读消息数据。
    """

    domain: Optional[str] = Field(default=None, description="站点域名")
    name: Optional[str] = Field(default=None, description="站点名称")
    username: Optional[str] = Field(default=None, description="用户名")
    userid: Optional[Union[str, int]] = Field(default=None, description="用户ID")
    user_level: Optional[str] = Field(default=None, description="用户等级")
    join_at: Optional[str] = Field(default=None, description="加入时间")
    bonus: Optional[float] = Field(default=0.0, description="积分")
    upload: Optional[int] = Field(default=0, description="站点累计上传流量，单位为字节")
    download: Optional[int] = Field(default=0, description="站点累计下载流量，单位为字节")
    ratio: Optional[float] = Field(default=0.0, description="分享率")
    seeding: Optional[int] = Field(default=0, description="做种数")
    leeching: Optional[int] = Field(default=0, description="下载数")
    seeding_size: Optional[int] = Field(default=0, description="正在做种内容的总体积，单位为字节")
    leeching_size: Optional[int] = Field(default=0, description="下载体积")
    seeding_info: Optional[list[tuple[int, int]]] = Field(default_factory=list, description="做种人数, 种子大小")
    message_unread: Optional[int] = Field(default=0, description="未读消息")
    message_unread_contents: Optional[list[SiteUnreadMessage]] = Field(default_factory=list, description="未读消息内容")
    err_msg: Optional[str] = Field(default=None, description="错误信息")
    updated_day: Optional[str] = Field(default=None, description="更新日期")
    updated_time: Optional[str] = Field(default=None, description="更新时间")

    # 查询仓储返回 SQLAlchemy ORM 对象时，从对象属性读取字段。
    model_config = ConfigDict(from_attributes=True)


class SiteAuth(BaseModel):
    """
    站点认证模块及其参数。
    """

    site: Optional[str] = Field(default=None, description="站点ID")
    params: Optional[Dict[str, Union[int, str]]] = Field(default_factory=dict, description="请求参数")


class SiteCookieUpdate(BaseModel):
    """
    站点 Cookie 与 UA 更新请求。
    """
    username: str = Field(..., description="站点登录用户名")
    password: str = Field(..., description="站点登录密码")
    code: Optional[str] = Field(None, description="二步验证码或密钥")


class SiteCookieSet(BaseModel):
    """
    直接保存浏览器登录后取得的站点 Cookie 与 User-Agent。
    """

    cookie: str = Field(..., description="可信浏览器登录会话提供的站点 Cookie 请求头")
    ua: Optional[str] = Field(None, description="与已认证浏览器会话对应的浏览器标识")


class SiteCategory(BaseModel):
    """
    站点资源分类。
    """

    id: Optional[int] = Field(default=None, description="记录标识")
    cat: Optional[str] = Field(default=None, description="站点分类标识")
    desc: Optional[str] = Field(default=None, description="分类说明")


class SiteIconData(BaseModel):
    """
    站点图标地址或 Base64 内容。
    """

    icon: str = Field(description="Vuetify 存储图标")


class SiteMappingData(RootModel[dict[str, str]]):
    """
    站点域名到显示名称的映射。
    """
