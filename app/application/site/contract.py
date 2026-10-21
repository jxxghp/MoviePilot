"""
站点配置、用户数据与健康统计的持久化合同。
"""

from __future__ import annotations

import builtins
from dataclasses import dataclass
from typing import Mapping, NoReturn, Optional, Protocol

from app.schemas.common import JsonData

_SITE_MUTATION_FIELDS = frozenset(
    {
        "name",
        "domain",
        "url",
        "pri",
        "rss",
        "cookie",
        "ua",
        "apikey",
        "token",
        "proxy",
        "filter",
        "render",
        "public",
        "note",
        "limit_interval",
        "limit_count",
        "limit_seconds",
        "timeout",
        "is_active",
        "lst_mod_date",
        "downloader",
    }
)

_SITE_USERDATA_MUTATION_FIELDS = frozenset(
    {
        "domain",
        "name",
        "username",
        "userid",
        "user_level",
        "join_at",
        "bonus",
        "upload",
        "download",
        "ratio",
        "seeding",
        "leeching",
        "seeding_size",
        "leeching_size",
        "seeding_info",
        "message_unread",
        "message_unread_contents",
        "err_msg",
        "updated_day",
        "updated_time",
    }
)


class SiteCookieLoginSource(Protocol):
    """
    Cookie 登录消费的只读站点字段，兼容持久化快照和 API 查询 DTO。
    """

    @property
    def id(self) -> Optional[int]:
        """
        返回站点主键，未保存的配置可为空。
        """
        ...

    @property
    def url(self) -> Optional[str]:
        """
        返回站点登录入口。
        """
        ...

    @property
    def proxy(self) -> Optional[int]:
        """
        返回是否使用宿主代理的配置。
        """
        ...

    @property
    def timeout(self) -> Optional[int]:
        """
        返回登录请求的超时秒数。
        """
        ...


class _FrozenJsonDict(dict[str, JsonData]):
    """
    保留 JSON 字典兼容读取，并拒绝原地修改。
    """

    def _reject_mutation(self, *args: object, **kwargs: object) -> NoReturn:
        """
        拒绝修改已经进入站点快照的 JSON。
        """
        raise TypeError("站点快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __ior__ = _reject_mutation
    clear = _reject_mutation
    pop = _reject_mutation
    popitem = _reject_mutation
    setdefault = _reject_mutation
    update = _reject_mutation


class _FrozenJsonList(list[JsonData]):
    """
    保留 JSON 数组兼容读取，并拒绝原地修改。
    """

    def _reject_mutation(self, *args: object, **kwargs: object) -> NoReturn:
        """
        拒绝修改已经进入站点快照的 JSON。
        """
        raise TypeError("站点快照 JSON 不可修改")

    __setitem__ = _reject_mutation
    __delitem__ = _reject_mutation
    __iadd__ = _reject_mutation
    __imul__ = _reject_mutation
    append = _reject_mutation
    clear = _reject_mutation
    extend = _reject_mutation
    insert = _reject_mutation
    pop = _reject_mutation
    remove = _reject_mutation
    reverse = _reject_mutation
    sort = _reject_mutation


def _freeze_json(value: JsonData) -> JsonData:
    """
    递归复制并冻结 JSON 容器。
    """
    if isinstance(value, dict):
        return _FrozenJsonDict({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenJsonList([_freeze_json(item) for item in value])
    return value


def _freeze_mapping(
    values: Mapping[str, JsonData],
    *,
    allowed_fields: frozenset[str],
) -> Mapping[str, JsonData]:
    """
    校验写字段并返回与调用方隔离的冻结映射。
    """
    unknown_fields = sorted(set(values) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"站点写入包含未知字段: {', '.join(unknown_fields)}")
    return _FrozenJsonDict({key: _freeze_json(value) for key, value in values.items()})


@dataclass(frozen=True, slots=True)
class SiteSnapshot:
    """
    脱离数据库 Session 的完整站点配置快照。

    :param id: 站点配置记录的数据库主键
    :param name: 站点显示名称
    :param url: 站点访问入口地址
    :param domain: 用于关联配置、图标和用户数据的规范域名键
    :param pri: 站点优先级，数值越小越靠前
    :param rss: 含认证参数的 RSS 订阅地址
    :param cookie: 站点请求所需的登录 Cookie
    :param ua: 站点请求使用的用户代理字符串
    :param apikey: 站点接口访问密钥
    :param token: 站点接口认证令牌
    :param proxy: 是否启用站点请求代理，1 表示启用，0 表示关闭
    :param filter: 站点专用资源过滤规则
    :param render: 是否使用浏览器渲染，1 表示启用，0 表示关闭
    :param public: 是否为无需私有账户的公开站点，1 表示公开
    :param note: 站点附加配置的冻结 JSON 快照
    :param limit_interval: 请求流控统计窗口长度，单位为秒
    :param limit_count: 统计窗口内允许的请求数量
    :param limit_seconds: 相邻请求之间的最小间隔，单位为秒
    :param timeout: 站点请求超时，单位为秒
    :param is_active: 是否启用该站点参与索引
    :param lst_mod_date: 站点配置最后修改时间文本
    :param downloader: 该站点指定的下载器配置名称
    """

    id: int
    name: str
    url: str
    domain: Optional[str] = None
    pri: Optional[int] = None
    rss: Optional[str] = None
    cookie: Optional[str] = None
    ua: Optional[str] = None
    apikey: Optional[str] = None
    token: Optional[str] = None
    proxy: Optional[int] = None
    filter: Optional[str] = None
    render: Optional[int] = None
    public: Optional[int] = None
    note: Optional[JsonData] = None
    limit_interval: Optional[int] = None
    limit_count: Optional[int] = None
    limit_seconds: Optional[int] = None
    timeout: Optional[int] = None
    is_active: Optional[bool] = None
    lst_mod_date: Optional[str] = None
    downloader: Optional[str] = None

    def __post_init__(self) -> None:
        """
        冻结附加 JSON，避免跨层共享 ORM 可变列。
        """
        object.__setattr__(self, "note", _freeze_json(self.note))


@dataclass(frozen=True, slots=True)
class SiteUserDataSnapshot:
    """
    脱离数据库 Session 的站点用户数据快照。

    :param id: 站点用户数据记录的数据库主键
    :param domain: 用户数据所属站点的规范域名键
    :param name: 采集时保存的站点名称
    :param username: 站点账户用户名
    :param userid: 站点内的账户标识
    :param user_level: 站点用户等级名称
    :param join_at: 站点账户注册时间文本
    :param bonus: 站点账户积分或魔力值
    :param upload: 累计上传量，单位为字节
    :param download: 累计下载量，单位为字节
    :param ratio: 站点提供的上传下载分享率
    :param seeding: 当前做种任务数量
    :param leeching: 当前下载任务数量
    :param seeding_size: 当前做种体积，单位为字节
    :param leeching_size: 当前下载任务体积，单位为字节
    :param seeding_info: 每个做种任务的做种人数和字节大小组成的冻结明细
    :param message_unread: 站内未读消息数量
    :param message_unread_contents: 未读消息标题、日期、内容及可选去重来源的冻结明细
    :param err_msg: 本次采集失败的说明；无错误时为 None
    :param updated_day: 采集所属日期，供每日统计使用
    :param updated_time: 最近一次采集时间文本
    """

    id: int
    domain: Optional[str] = None
    name: Optional[str] = None
    username: Optional[str] = None
    userid: Optional[str] = None
    user_level: Optional[str] = None
    join_at: Optional[str] = None
    bonus: Optional[float] = None
    upload: Optional[float] = None
    download: Optional[float] = None
    ratio: Optional[float] = None
    seeding: Optional[float] = None
    leeching: Optional[float] = None
    seeding_size: Optional[float] = None
    leeching_size: Optional[float] = None
    seeding_info: Optional[JsonData] = None
    message_unread: Optional[int] = None
    message_unread_contents: Optional[JsonData] = None
    err_msg: Optional[str] = None
    updated_day: Optional[str] = None
    updated_time: Optional[str] = None

    def __post_init__(self) -> None:
        """
        冻结做种与未读消息 JSON，避免调用方修改持久化值。
        """
        object.__setattr__(self, "seeding_info", _freeze_json(self.seeding_info))
        object.__setattr__(
            self,
            "message_unread_contents",
            _freeze_json(self.message_unread_contents),
        )


@dataclass(frozen=True, slots=True)
class SiteIconSnapshot:
    """
    脱离数据库 Session 的站点图标快照。

    :param id: 站点图标记录的数据库主键
    :param name: 图标对应的站点名称
    :param url: 图标原始下载地址
    :param domain: 图标对应的规范域名键
    :param base64: 缓存图标的 Base64 内容；未缓存时为 None
    """

    id: int
    name: str
    url: str
    domain: Optional[str] = None
    base64: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SiteStatisticSnapshot:
    """
    脱离数据库 Session 的站点健康统计快照。

    :param id: 站点健康统计记录的数据库主键
    :param domain: 统计对应站点的规范域名键
    :param success: 累计成功访问次数
    :param fail: 累计失败访问次数
    :param seconds: 成功访问的平均耗时，单位为秒
    :param lst_state: 最近一次访问结果，0 表示成功，1 表示失败
    :param lst_mod_date: 最近一次访问时间文本
    :param note: 参与平均耗时计算的冻结历史明细
    """

    id: int
    domain: Optional[str] = None
    success: Optional[int] = None
    fail: Optional[int] = None
    seconds: Optional[int] = None
    lst_state: Optional[int] = None
    lst_mod_date: Optional[str] = None
    note: Optional[JsonData] = None

    def __post_init__(self) -> None:
        """
        冻结耗时明细 JSON。
        """
        object.__setattr__(self, "note", _freeze_json(self.note))


@dataclass(frozen=True, slots=True)
class SiteMutation:
    """
    一次站点新增或更新允许写入的字段集合。

    :param values: 通过站点可写字段白名单校验的变更；构造时复制并递归冻结
    """

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """
        拒绝模型外字段并冻结嵌套 JSON。
        """
        object.__setattr__(
            self,
            "values",
            _freeze_mapping(self.values, allowed_fields=_SITE_MUTATION_FIELDS),
        )

    def to_payload(self) -> dict[str, JsonData]:
        """
        返回交给数据库适配器的独立可变副本。
        """
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SiteUserDataMutation:
    """
    一次站点用户数据更新允许写入的字段集合。

    :param values: 通过用户数据白名单校验的变更；构造时复制并递归冻结
    """

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """
        拒绝模型外字段并冻结嵌套 JSON。
        """
        object.__setattr__(
            self,
            "values",
            _freeze_mapping(
                self.values,
                allowed_fields=_SITE_USERDATA_MUTATION_FIELDS,
            ),
        )

    def to_payload(self) -> dict[str, JsonData]:
        """
        返回交给数据库适配器的独立可变副本。
        """
        return {key: _copy_json(value) for key, value in self.values.items()}


def _copy_json(value: JsonData) -> JsonData:
    """
    把冻结 JSON 递归复制为普通容器。
    """
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SitePriorityMutation:
    """
    一条站点优先级更新。

    :param site_id: 待调整优先级的站点数据库主键
    :param priority: 拟写入的站点优先级，数值越小越靠前
    """

    site_id: int
    priority: int


@dataclass(frozen=True, slots=True)
class SiteWriteResult:
    """
    描述站点写操作是否成功及兼容提示。

    :param success: 站点写操作是否成功
    :param message: 写入结果或失败原因的说明，默认空字符串
    """

    success: bool
    message: str = ""


class SiteQueryPort(Protocol):
    """
    Chain、Agent 与 Application 所需的类型化站点查询端口。
    """

    def get(self, site_id: int) -> Optional[SiteSnapshot]:
        """
        同步按主键读取站点快照。

        :param site_id: 目标站点配置的数据库主键
        :return: 站点配置快照；主键不存在时为 None
        """
        ...

    def get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """
        同步按域名读取站点快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :return: 匹配规范域名的站点快照；不存在时为 None
        """
        ...

    def get_domains_by_ids(
        self,
        ids: builtins.list[int],
    ) -> builtins.list[str]:
        """
        同步读取一组站点主键对应的非空域名。

        :param ids: 需要查询域名的站点主键列表
        :return: 所选站点中非空的域名列表
        """
        ...

    def list(self) -> builtins.list[SiteSnapshot]:
        """
        同步读取全部站点快照。

        :return: 全部站点的独立配置快照
        """
        ...

    def list_order_by_pri(self) -> builtins.list[SiteSnapshot]:
        """
        同步按优先级读取站点快照。

        :return: 按优先级排列的站点快照，数值较小者靠前
        """
        ...

    def list_active(self) -> builtins.list[SiteSnapshot]:
        """
        同步读取已启用站点快照。

        :return: 已启用站点的配置快照列表
        """
        ...

    def get_userdata_latest(self) -> builtins.list[SiteUserDataSnapshot]:
        """
        同步读取各站点最新用户数据快照。

        :return: 各站点最近采集的用户数据快照
        """
        ...

    async def async_get(self, site_id: int) -> Optional[SiteSnapshot]:
        """
        异步按主键读取站点快照。

        :param site_id: 目标站点配置的数据库主键
        :return: 站点配置快照；主键不存在时为 None
        """
        ...

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """
        异步按域名读取站点快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :return: 匹配规范域名的站点快照；不存在时为 None
        """
        ...

    async def async_get_by_name(self, name: str) -> Optional[SiteSnapshot]:
        """
        异步按名称读取站点快照。

        :param name: 站点名称
        :return: 匹配名称的站点快照；不存在时为 None
        """
        ...

    async def async_list(self) -> builtins.list[SiteSnapshot]:
        """
        异步读取全部站点快照。

        :return: 全部站点的独立配置快照
        """
        ...

    async def async_list_order_by_pri(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteSnapshot]:
        """
        按筛选和可选分页窗口异步读取站点快照。

        :param is_active: 启用状态筛选；None 表示不限制
        :param name: 站点名称包含匹配文本；None 表示不限定名称
        :param site_ids: 允许返回的站点主键；None 表示不附加此条件
        :param domains: 允许返回的规范域名键；None 表示不附加此条件
        :param page: 页码，从 1 开始；与 count 同时提供时启用分页
        :param count: 每页最多返回的记录数量；未启用分页时不截断
        :return: 满足筛选和分页条件的站点快照，按优先级从小到大排列
        """
        ...

    async def async_count_sites(
        self,
        *,
        is_active: Optional[bool] = None,
        name: Optional[str] = None,
        site_ids: Optional[builtins.list[int]] = None,
        domains: Optional[builtins.list[str]] = None,
    ) -> int:
        """
        按与站点列表一致的筛选条件统计数量。

        :param is_active: 启用状态筛选；None 表示不限制
        :param name: 站点名称包含匹配文本；None 表示不限定名称
        :param site_ids: 允许返回的站点主键；None 表示不附加此条件
        :param domains: 允许返回的规范域名键；None 表示不附加此条件
        :return: 满足站点筛选条件的记录总数
        """
        ...

    async def async_list_active(self) -> builtins.list[SiteSnapshot]:
        """
        异步读取已启用站点快照。

        :return: 已启用站点的配置快照列表
        """
        ...

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """
        按可选分页窗口异步读取指定域名和日期的用户数据快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param workdate: 用户数据的采集日期；None 表示不限定日期
        :param page: 页码，从 1 开始；与 count 同时提供时启用分页
        :param count: 每页最多返回的记录数量；未启用分页时不截断
        :return: 指定站点与日期范围内的用户数据快照，受可选分页限制
        """
        ...

    async def async_count_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> int:
        """
        统计指定域名和日期的用户数据数量。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param workdate: 用户数据的采集日期；None 表示不限定日期
        :return: 满足站点与日期条件的用户数据记录数
        """
        ...

    async def async_get_userdata_latest(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """
        按可选分页窗口异步读取各站点最新用户数据快照。

        :param page: 页码，从 1 开始；与 count 同时提供时启用分页
        :param count: 每页最多返回的记录数量；未启用分页时不截断
        :return: 各站点最近用户数据组成的列表，受可选分页限制
        """
        ...

    async def async_count_userdata_latest(self) -> int:
        """
        统计各站点最新用户数据查询的结果数量。

        :return: 各站点最近用户数据查询产生的记录数量
        """
        ...

    async def async_get_icon_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteIconSnapshot]:
        """
        异步按域名读取站点图标快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :return: 该站点的图标快照；未缓存时为 None
        """
        ...

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatisticSnapshot]:
        """
        异步按域名读取站点健康统计快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :return: 该站点的健康统计；尚未记录时为 None
        """
        ...

    async def async_list_statistics(
        self,
        *,
        page: Optional[int] = None,
        count: Optional[int] = None,
    ) -> builtins.list[SiteStatisticSnapshot]:
        """
        按可选分页窗口异步读取站点健康统计快照。

        :param page: 页码，从 1 开始；与 count 同时提供时启用分页
        :param count: 每页最多返回的记录数量；未启用分页时不截断
        :return: 站点健康统计列表，受可选分页限制
        """
        ...

    async def async_count_statistics(self) -> int:
        """
        统计站点健康统计记录数量。

        :return: 已有站点健康统计的记录总数
        """
        ...


class SiteWritePort(Protocol):
    """
    站点配置、用户数据和健康统计的类型化写端口。
    """

    def add(self, mutation: SiteMutation) -> SiteWriteResult:
        """
        在独立同步事务中新增站点。

        :param mutation: 经过字段白名单校验的站点变更
        :return: 已提交新增操作的成功标志和结果说明
        """
        ...

    def update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """
        在独立同步事务中更新并返回站点快照。

        :param site_id: 目标站点配置的数据库主键
        :param mutation: 经过字段白名单校验的站点变更
        :return: 提交后的站点快照；站点不存在时为 None
        """
        ...

    async def async_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """
        在独立异步事务中更新并返回站点快照。

        :param site_id: 目标站点配置的数据库主键
        :param mutation: 经过字段白名单校验的站点变更
        :return: 提交后的站点快照；站点不存在时为 None
        """
        ...

    def update_cookie(self, domain: str, cookies: str) -> SiteWriteResult:
        """
        在独立同步事务中更新站点 Cookie。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param cookies: 拟保存的站点登录 Cookie
        :return: Cookie 更新的成功标志和结果说明
        """
        ...

    def update_rss(self, domain: str, rss: str) -> SiteWriteResult:
        """
        在独立同步事务中更新站点 RSS。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param rss: 拟保存的 RSS 订阅地址
        :return: RSS 更新的成功标志和结果说明
        """
        ...

    def update_userdata(
        self,
        domain: str,
        name: str,
        mutation: SiteUserDataMutation,
    ) -> SiteWriteResult:
        """
        在独立同步事务中写入站点用户数据。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param name: 站点名称
        :param mutation: 经过字段白名单校验的站点用户数据变更
        :return: 用户数据写入的成功标志和结果说明
        """
        ...

    def update_icon(
        self,
        name: str,
        domain: str,
        icon_url: str,
        icon_base64: str,
    ) -> bool:
        """
        在独立同步事务中写入站点图标。

        :param name: 站点名称
        :param domain: 用于关联站点配置和数据的规范域名键
        :param icon_url: 站点图标原始下载地址
        :param icon_base64: 缓存图标的 Base64 内容
        :return: 操作正常完成时返回 True；持久化异常向调用方传播
        """
        ...

    def success(self, domain: str, seconds: Optional[int] = None) -> None:
        """
        在独立同步事务中记录站点访问成功。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param seconds: 本次成功请求的耗时，单位为秒；None 表示不补充耗时样本
        """
        ...

    def fail(self, domain: str) -> None:
        """
        在独立同步事务中记录站点访问失败。

        :param domain: 用于关联站点配置和数据的规范域名键
        """
        ...

    async def async_success(
        self,
        domain: str,
        seconds: Optional[int] = None,
    ) -> None:
        """
        在独立异步事务中记录站点访问成功。

        :param domain: 用于关联站点配置和数据的规范域名键
        :param seconds: 本次成功请求的耗时，单位为秒；None 表示不补充耗时样本
        """
        ...

    async def async_fail(self, domain: str) -> None:
        """
        在独立异步事务中记录站点访问失败。

        :param domain: 用于关联站点配置和数据的规范域名键
        """
        ...


class SiteStagingPort(Protocol):
    """
    复用请求 AsyncSession 且不自行提交的站点写端口。
    """

    async def get_by_id(self, site_id: int) -> Optional[SiteSnapshot]:
        """
        在请求会话中按主键读取站点快照。

        :param site_id: 目标站点配置的数据库主键
        :return: 当前事务中的站点快照；主键不存在时为 None
        """
        ...

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """
        在请求会话中按域名读取站点快照。

        :param domain: 用于关联站点配置和数据的规范域名键
        :return: 匹配规范域名的站点快照；不存在时为 None
        """
        ...

    async def stage_create(self, mutation: SiteMutation) -> None:
        """
        在请求事务中暂存新增站点。

        :param mutation: 经过字段白名单校验的站点变更
        """
        ...

    async def stage_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> bool:
        """
        在请求事务中暂存站点更新并返回目标是否存在。

        :param site_id: 目标站点配置的数据库主键
        :param mutation: 经过字段白名单校验的站点变更
        :return: 目标存在且已暂存更新时为 True；目标不存在时为 False
        """
        ...

    async def stage_delete(self, site_id: int) -> None:
        """
        在请求事务中暂存删除站点。

        :param site_id: 目标站点配置的数据库主键
        """
        ...

    async def stage_priorities(
        self,
        priorities: tuple[SitePriorityMutation, ...],
    ) -> None:
        """
        在请求事务中暂存一组站点优先级更新。

        :param priorities: 需要在同一事务内更新的站点及优先级
        """
        ...

    async def stage_reset(self) -> None:
        """
        在请求事务中暂存清空全部站点。
        """
        ...


class SiteRepository(SiteQueryPort, SiteWritePort, Protocol):
    """
    组合宿主所需全部站点查询和写入能力。
    """
