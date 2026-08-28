"""站点配置、用户数据与健康统计的持久化合同。"""

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


class _FrozenJsonDict(dict[str, JsonData]):
    """保留 JSON 字典兼容读取，并拒绝原地修改。"""

    def _reject_mutation(self, *args: object, **kwargs: object) -> NoReturn:
        """拒绝修改已经进入站点快照的 JSON。"""
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
    """保留 JSON 数组兼容读取，并拒绝原地修改。"""

    def _reject_mutation(self, *args: object, **kwargs: object) -> NoReturn:
        """拒绝修改已经进入站点快照的 JSON。"""
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
    """递归复制并冻结 JSON 容器。"""
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
    """校验写字段并返回与调用方隔离的冻结映射。"""
    unknown_fields = sorted(set(values) - allowed_fields)
    if unknown_fields:
        raise ValueError(f"站点写入包含未知字段: {', '.join(unknown_fields)}")
    return _FrozenJsonDict({key: _freeze_json(value) for key, value in values.items()})


@dataclass(frozen=True, slots=True)
class SiteSnapshot:
    """脱离数据库 Session 的完整站点配置快照。"""

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
        """冻结附加 JSON，避免跨层共享 ORM 可变列。"""
        object.__setattr__(self, "note", _freeze_json(self.note))


@dataclass(frozen=True, slots=True)
class SiteUserDataSnapshot:
    """脱离数据库 Session 的站点用户数据快照。"""

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
        """冻结做种与未读消息 JSON，避免调用方修改持久化值。"""
        object.__setattr__(self, "seeding_info", _freeze_json(self.seeding_info))
        object.__setattr__(
            self,
            "message_unread_contents",
            _freeze_json(self.message_unread_contents),
        )


@dataclass(frozen=True, slots=True)
class SiteIconSnapshot:
    """脱离数据库 Session 的站点图标快照。"""

    id: int
    name: str
    url: str
    domain: Optional[str] = None
    base64: Optional[str] = None


@dataclass(frozen=True, slots=True)
class SiteStatisticSnapshot:
    """脱离数据库 Session 的站点健康统计快照。"""

    id: int
    domain: Optional[str] = None
    success: Optional[int] = None
    fail: Optional[int] = None
    seconds: Optional[int] = None
    lst_state: Optional[int] = None
    lst_mod_date: Optional[str] = None
    note: Optional[JsonData] = None

    def __post_init__(self) -> None:
        """冻结耗时明细 JSON。"""
        object.__setattr__(self, "note", _freeze_json(self.note))


@dataclass(frozen=True, slots=True)
class SiteMutation:
    """一次站点新增或更新允许写入的字段集合。"""

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """拒绝模型外字段并冻结嵌套 JSON。"""
        object.__setattr__(
            self,
            "values",
            _freeze_mapping(self.values, allowed_fields=_SITE_MUTATION_FIELDS),
        )

    def to_payload(self) -> dict[str, JsonData]:
        """返回交给 DB adapter 的独立可变副本。"""
        return {key: _copy_json(value) for key, value in self.values.items()}


@dataclass(frozen=True, slots=True)
class SiteUserDataMutation:
    """一次站点用户数据更新允许写入的字段集合。"""

    values: Mapping[str, JsonData]

    def __post_init__(self) -> None:
        """拒绝模型外字段并冻结嵌套 JSON。"""
        object.__setattr__(
            self,
            "values",
            _freeze_mapping(
                self.values,
                allowed_fields=_SITE_USERDATA_MUTATION_FIELDS,
            ),
        )

    def to_payload(self) -> dict[str, JsonData]:
        """返回交给 DB adapter 的独立可变副本。"""
        return {key: _copy_json(value) for key, value in self.values.items()}


def _copy_json(value: JsonData) -> JsonData:
    """把冻结 JSON 递归复制为普通容器。"""
    if isinstance(value, dict):
        return {key: _copy_json(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_copy_json(item) for item in value]
    return value


@dataclass(frozen=True, slots=True)
class SitePriorityMutation:
    """一条站点优先级更新。"""

    site_id: int
    priority: int


@dataclass(frozen=True, slots=True)
class SiteWriteResult:
    """描述站点写操作是否成功及兼容提示。"""

    success: bool
    message: str = ""


class SiteQueryPort(Protocol):
    """Chain、Agent 与 Application 所需的类型化站点查询端口。"""

    def get(self, site_id: int) -> Optional[SiteSnapshot]:
        """同步按主键读取站点快照。"""
        ...

    def get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """同步按域名读取站点快照。"""
        ...

    def get_domains_by_ids(
        self,
        ids: builtins.list[int],
    ) -> builtins.list[str]:
        """同步读取一组站点主键对应的非空域名。"""
        ...

    def list(self) -> builtins.list[SiteSnapshot]:
        """同步读取全部站点快照。"""
        ...

    def list_order_by_pri(self) -> builtins.list[SiteSnapshot]:
        """同步按优先级读取站点快照。"""
        ...

    def list_active(self) -> builtins.list[SiteSnapshot]:
        """同步读取已启用站点快照。"""
        ...

    def get_userdata_latest(self) -> builtins.list[SiteUserDataSnapshot]:
        """同步读取各站点最新用户数据快照。"""
        ...

    async def async_get(self, site_id: int) -> Optional[SiteSnapshot]:
        """异步按主键读取站点快照。"""
        ...

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """异步按域名读取站点快照。"""
        ...

    async def async_get_by_name(self, name: str) -> Optional[SiteSnapshot]:
        """异步按名称读取站点快照。"""
        ...

    async def async_list(self) -> builtins.list[SiteSnapshot]:
        """异步读取全部站点快照。"""
        ...

    async def async_list_order_by_pri(self) -> builtins.list[SiteSnapshot]:
        """异步按优先级读取站点快照。"""
        ...

    async def async_list_active(self) -> builtins.list[SiteSnapshot]:
        """异步读取已启用站点快照。"""
        ...

    async def async_get_userdata_by_domain(
        self,
        domain: str,
        workdate: Optional[str] = None,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """异步读取指定域名和日期的用户数据快照。"""
        ...

    async def async_get_userdata_latest(
        self,
    ) -> builtins.list[SiteUserDataSnapshot]:
        """异步读取各站点最新用户数据快照。"""
        ...

    async def async_get_icon_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteIconSnapshot]:
        """异步按域名读取站点图标快照。"""
        ...

    async def async_get_statistic_by_domain(
        self,
        domain: str,
    ) -> Optional[SiteStatisticSnapshot]:
        """异步按域名读取站点健康统计快照。"""
        ...

    async def async_list_statistics(
        self,
    ) -> builtins.list[SiteStatisticSnapshot]:
        """异步读取全部站点健康统计快照。"""
        ...


class SiteWritePort(Protocol):
    """站点配置、用户数据和健康统计的类型化写端口。"""

    def add(self, mutation: SiteMutation) -> SiteWriteResult:
        """在独立同步事务中新增站点。"""
        ...

    def update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """在独立同步事务中更新并返回站点快照。"""
        ...

    async def async_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> Optional[SiteSnapshot]:
        """在独立异步事务中更新并返回站点快照。"""
        ...

    def update_cookie(self, domain: str, cookies: str) -> SiteWriteResult:
        """在独立同步事务中更新站点 Cookie。"""
        ...

    def update_rss(self, domain: str, rss: str) -> SiteWriteResult:
        """在独立同步事务中更新站点 RSS。"""
        ...

    def update_userdata(
        self,
        domain: str,
        name: str,
        mutation: SiteUserDataMutation,
    ) -> SiteWriteResult:
        """在独立同步事务中写入站点用户数据。"""
        ...

    def update_icon(
        self,
        name: str,
        domain: str,
        icon_url: str,
        icon_base64: str,
    ) -> bool:
        """在独立同步事务中写入站点图标。"""
        ...

    def success(self, domain: str, seconds: Optional[int] = None) -> None:
        """在独立同步事务中记录站点访问成功。"""
        ...

    def fail(self, domain: str) -> None:
        """在独立同步事务中记录站点访问失败。"""
        ...

    async def async_success(
        self,
        domain: str,
        seconds: Optional[int] = None,
    ) -> None:
        """在独立异步事务中记录站点访问成功。"""
        ...

    async def async_fail(self, domain: str) -> None:
        """在独立异步事务中记录站点访问失败。"""
        ...


class SiteStagingPort(Protocol):
    """复用请求 AsyncSession 且不自行提交的站点写端口。"""

    async def get_by_id(self, site_id: int) -> Optional[SiteSnapshot]:
        """在请求会话中按主键读取站点快照。"""
        ...

    async def async_get_by_domain(self, domain: str) -> Optional[SiteSnapshot]:
        """在请求会话中按域名读取站点快照。"""
        ...

    async def stage_create(self, mutation: SiteMutation) -> None:
        """在请求事务中暂存新增站点。"""
        ...

    async def stage_update(
        self,
        site_id: int,
        mutation: SiteMutation,
    ) -> bool:
        """在请求事务中暂存站点更新并返回目标是否存在。"""
        ...

    async def stage_delete(self, site_id: int) -> None:
        """在请求事务中暂存删除站点。"""
        ...

    async def stage_priorities(
        self,
        priorities: tuple[SitePriorityMutation, ...],
    ) -> None:
        """在请求事务中暂存一组站点优先级更新。"""
        ...

    async def stage_reset(self) -> None:
        """在请求事务中暂存清空全部站点。"""
        ...


class SiteRepository(SiteQueryPort, SiteWritePort, Protocol):
    """组合宿主所需全部站点查询和写入能力。"""
