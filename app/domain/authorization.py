"""
通知渠道授权与网页用户权限的领域规则与能力映射。

旧投递接收人只在升级时用于保留已有管理员身份，运行时不再据此授权；
网页用户实时权限由超级管理员标识与权限分类/功能开关共同决定。
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

# 新成员默认拥有的业务能力；定时任务和管理员能力必须单独授予。
DEFAULT_MEMBER_SCOPES = ("search", "subscribe", "download")
# 授权系统接受的完整能力集合，管理员拥有全部能力。
AUTHORIZATION_SCOPES = (*DEFAULT_MEMBER_SCOPES, "schedule")

@dataclass(frozen=True, slots=True)
class ChannelActor:
    """
    平台已经解析的主体事实，不包含协议前缀或授权结论。

    :param id: 规范主体标识，作用域由调用方连接身份空间限定
    :param kind: 个人、群组或无法确认的主体类型
    :param aliases: 平台能够证明属于同一主体的确定性历史标识
    """

    id: str
    kind: str = "person"
    aliases: tuple[str, ...] = ()


def channel_actor_ids(actor: ChannelActor) -> tuple[str, ...]:
    """
    仅接受规范个人主体，并返回同一主体的去重查询标识。

    :param actor: 由平台产生、尚未授予任何业务权限的身份事实
    :return: 规范主体 ID 及已证明的别名
    :raises ValueError: 主体不是个人，或者标识为空
    """
    if actor.kind != "person":
        raise ValueError("群地址或未知主体不能作为个人渠道授权身份，请核对实际发送者")
    values = tuple(dict.fromkeys(value.strip() for value in (actor.id, *actor.aliases)))
    if not values or any(not value for value in values):
        raise ValueError("缺少渠道发送者 ID")
    return values


# 普通渠道成员仅开放明确审计过的数据归属操作，其余能力由管理员执行。
CHANNEL_OPERATION_SCOPES = {
    "media.recognize": "search",
    "search.torrents": "search",
    "download.add": "download",
    "download.artist_collection": "download",
    "download.history.list": "download",
    "download.tasks.active": "download",
    "download.clients": "download",
    "download.paths": "download",
    "subscription.add": "subscribe",
    "subscription.update": "subscribe",
    "subscription.search": "subscribe",
    "subscription.list": "subscribe",
    "subscription.get": "subscribe",
    "subscription.find": "subscribe",
    "subscription.history": "subscribe",
    "subscription.delete": "subscribe",
    "subscription.delete_by_media": "subscribe",
    "subscription.status.update": "subscribe",
    "subscription.reset": "subscribe",
    "subscription.files": "subscribe",
    "subscription.execution.list": "subscribe",
    "subscription.execution.get": "subscribe",
    "subscription.execution.cancel": "subscribe",
}

# 已审计的公共资料查询，已授权成员也可以在固定自身身份下读取。
CHANNEL_PUBLIC_OPERATIONS = frozenset(
    {
        "media.search",
        "media.person.search",
        "media.person.credits",
        "media.episode_schedule",
        "media.detail",
        "media.episode_groups",
        "media.episode_group.seasons",
        "media.seasons",
        "music.recognize",
        "music.explore",
        "music.album.get",
        "music.album.related",
        "music.artist.get",
        "music.artist.albums",
        "music.artist.related",
        "recommendation.list",
        "subscription.shares",
        "subscription.popular",
        "subscription.share.statistics",
        "plugin.statistics",
    }
)

# 网页普通用户支持的顶层权限分类；admin 标记不能授予超级管理员身份。
WEB_PERMISSION_CATEGORIES = ("discovery", "search", "subscribe", "manage")

# 网页权限分类的中文名称，用于向模型说明当前用户具备的真实边界。
WEB_PERMISSION_CATEGORY_LABELS = {
    "discovery": "发现（推荐/探索）",
    "search": "资源搜索",
    "subscribe": "订阅管理",
    "manage": "系统管理（站点/下载/历史/文件/工作流等受托管理项）",
}

# 网页功能级权限键的中文名称，与前端权限配置保持一致。
WEB_PERMISSION_FEATURE_LABELS = {
    "discovery.recommend": "推荐",
    "discovery.explore": "探索",
    "search.resource": "资源搜索",
    "subscribe.movie": "电影订阅",
    "subscribe.tv": "电视剧订阅",
    "subscribe.music": "音乐订阅",
    "subscribe.calendar": "订阅日历",
    "subscribe.share": "订阅分享",
    "manage.workflow": "工作流",
    "manage.downloading": "正在下载",
    "manage.history": "历史记录",
    "manage.filemanager": "文件管理",
    "manage.site": "站点管理",
}

# 订阅媒体类型与功能级权限键的映射。
_SUBSCRIBE_MEDIA_TYPE_FEATURES = {
    "movie": "subscribe.movie",
    "电影": "subscribe.movie",
    "tv": "subscribe.tv",
    "电视剧": "subscribe.tv",
    "music": "subscribe.music",
    "音乐": "subscribe.music",
}

# 网页登录用户调用白名单业务 API 的最小权限要求。
# 映射值为满足任一 (category, feature) 条件即放行的元组；空元组表示任意激活登录用户可调用；
# 未列入此表的操作一律仅允许超级管理员执行。
WebPermissionRequirement = tuple[str, str | None]
WEB_OPERATION_REQUIREMENTS: dict[str, tuple[WebPermissionRequirement, ...]] = {
    # 基础登录配置
    "config.user.get": (),
    "config.public.get": (),
    # 发现与探索
    "recommendation.list": (("discovery", "discovery.recommend"),),
    "music.explore": (("discovery", "discovery.explore"),),
    "library.latest": (("discovery", None),),
    # 媒体资料查询（发现、搜索、订阅流程均依赖基础识别与详情）
    "media.search": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.person.search": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.person.credits": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.recognize": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.recognize_file": (
        ("discovery", None),
        ("search", "search.resource"),
        ("subscribe", None),
        ("manage", "manage.filemanager"),
    ),
    "media.episode_schedule": (
        ("discovery", None),
        ("subscribe", "subscribe.calendar"),
        ("subscribe", None),
    ),
    "media.detail": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.sources": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.episode_groups": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "media.episode_group.seasons": (
        ("discovery", None),
        ("search", "search.resource"),
        ("subscribe", None),
    ),
    "media.seasons": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "library.exists": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.recognize": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.album.get": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.album.related": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.artist.get": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.artist.albums": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    "music.artist.related": (("discovery", None), ("search", "search.resource"), ("subscribe", None)),
    # 搜索、下载与规则读取
    "search.torrents": (("search", "search.resource"),),
    "search.results": (("search", "search.resource"),),
    "search.title": (("search", "search.resource"),),
    "search.recommend": (("discovery", "discovery.recommend"), ("search", "search.resource")),
    "subtitle.search.title": (("search", "search.resource"),),
    "subtitle.search.media": (("search", "search.resource"),),
    "download.add": (("search", "search.resource"), ("subscribe", None)),
    "download.artist_collection": (("search", "search.resource"), ("subscribe", "subscribe.music")),
    "download.clients": (
        ("search", "search.resource"),
        ("subscribe", None),
        ("manage", "manage.downloading"),
    ),
    "download.paths": (
        ("search", "search.resource"),
        ("subscribe", None),
        ("manage", "manage.downloading"),
    ),
    "download.tasks.active": (
        ("search", "search.resource"),
        ("subscribe", None),
        ("manage", "manage.downloading"),
    ),
    "download.history.list": (
        ("search", "search.resource"),
        ("subscribe", None),
        ("manage", "manage.downloading"),
    ),
    "filter.builtin": (("search", "search.resource"), ("subscribe", None)),
    "filter.custom": (("search", "search.resource"), ("subscribe", None)),
    "filter.groups": (("search", "search.resource"), ("subscribe", None)),
    "site.list": (("search", "search.resource"), ("subscribe", None), ("manage", "manage.site")),
    # 订阅
    "subscription.add": (("subscribe", None),),
    "subscription.update": (("subscribe", None),),
    "subscription.search": (("subscribe", None),),
    "subscription.search_all": (("subscribe", None),),
    "subscription.list": (("subscribe", None),),
    "subscription.get": (("subscribe", None),),
    "subscription.find": (("subscribe", None),),
    "subscription.user.list": (("subscribe", None),),
    "subscription.history": (("subscribe", None),),
    "subscription.history.delete": (("subscribe", None),),
    "subscription.delete": (("subscribe", None),),
    "subscription.delete_by_media": (("subscribe", None),),
    "subscription.status.update": (("subscribe", None),),
    "subscription.reset": (("subscribe", None),),
    "subscription.files": (("subscribe", None),),
    "subscription.execution.list": (("subscribe", None),),
    "subscription.execution.get": (("subscribe", None),),
    "subscription.execution.cancel": (("subscribe", None),),
    "subscription.shares": (("subscribe", "subscribe.share"),),
    "subscription.popular": (("subscribe", "subscribe.share"),),
    "subscription.share.statistics": (("subscribe", "subscribe.share"),),
    "subscription.share": (("subscribe", "subscribe.share"),),
    "subscription.fork": (("subscribe", "subscribe.share"),),
    "subscription.share.delete": (("manage", None),),
    "subscription.follow.list": (("manage", None),),
    "subscription.follow.add": (("manage", None),),
    "subscription.follow.delete": (("manage", None),),
    # 受托管理：站点
    "site.add": (("manage", "manage.site"),),
    "site.update": (("manage", "manage.site"),),
    "site.delete": (("manage", "manage.site"),),
    "site.userdata": (("manage", "manage.site"),),
    "site.userdata.refresh": (("manage", "manage.site"),),
    "site.userdata.latest": (("manage", "manage.site"),),
    "site.cookie.update": (("manage", "manage.site"),),
    "site.cookie.set": (("manage", "manage.site"),),
    "site.priorities.update": (("manage", "manage.site"),),
    "site.category": (("manage", "manage.site"),),
    "site.resource": (("manage", "manage.site"),),
    "site.searchable": (("manage", "manage.site"),),
    "site.rss": (("manage", "manage.site"),),
    "site.test": (("manage", "manage.site"),),
    "site.statistics": (("manage", "manage.site"),),
    "site.statistic": (("manage", "manage.site"),),
    # 受托管理：下载与整理历史
    "download.history.delete": (("manage", "manage.downloading"),),
    "transfer.history": (("manage", "manage.history"),),
    "transfer.history.delete": (("manage", "manage.history"),),
    "transfer.history.redo": (("manage", "manage.history"),),
    "transfer.history.redo_batch": (("manage", "manage.history"),),
    "transfer.history.replan": (("manage", "manage.history"),),
    "transfer.queue": (("manage", "manage.history"),),
    "transfer.queue.delete": (("manage", "manage.history"),),
    "transfer.name": (("manage", "manage.history"),),
    "transfer.target_path": (("manage", "manage.history"),),
    "transfer.manual_history": (("manage", "manage.history"),),
    "transfer.episode_format.recommend": (("manage", "manage.history"),),
    "transfer.manual_reviews": (("manage", "manage.history"),),
    "transfer.manual_review": (("manage", "manage.history"),),
    "transfer.manual_review.resolve": (("manage", "manage.history"),),
    # 受托管理：文件管理与刮削
    "storage.list": (("manage", "manage.filemanager"),),
    "storage.settings": (("manage", "manage.filemanager"),),
    "storage.mkdir": (("manage", "manage.filemanager"),),
    "storage.rename": (("manage", "manage.filemanager"),),
    "storage.delete": (("manage", "manage.filemanager"),),
    "transfer.file": (("manage", "manage.filemanager"),),
    "media.scrape": (("manage", "manage.filemanager"),),
    # 受托管理：工作流
    "workflow.list": (("manage", "manage.workflow"),),
    "workflow.run": (("manage", "manage.workflow"),),
    "workflow.create": (("manage", "manage.workflow"),),
    "workflow.get": (("manage", "manage.workflow"),),
    "workflow.update": (("manage", "manage.workflow"),),
    "workflow.delete": (("manage", "manage.workflow"),),
    "workflow.actions": (("manage", "manage.workflow"),),
    "workflow.event_types": (("manage", "manage.workflow"),),
    "workflow.plugin.actions": (("manage", "manage.workflow"),),
    "workflow.start": (("manage", "manage.workflow"),),
    "workflow.pause": (("manage", "manage.workflow"),),
    "workflow.reset": (("manage", "manage.workflow"),),
    "workflow.shares": (("manage", "manage.workflow"),),
    "workflow.share": (("manage", "manage.workflow"),),
    "workflow.share.delete": (("manage", "manage.workflow"),),
    "workflow.fork": (("manage", "manage.workflow"),),
}


def normalize_web_permissions(permissions: Mapping[str, Any] | None) -> dict[str, Any]:
    """
    归一化网页用户权限字典，保持缺失分类按拒绝处理且忽略非布尔功能开关。

    :param permissions: 原始用户权限映射
    :return: 仅包含规范分类布尔值与 features 映射的权限字典
    """
    source = permissions if isinstance(permissions, Mapping) else {}
    raw_features = source.get("features")
    features: dict[str, bool] = {}
    if isinstance(raw_features, Mapping):
        for key, value in raw_features.items():
            if isinstance(key, str) and isinstance(value, bool):
                features[key] = value
    normalized: dict[str, Any] = {
        category: source.get(category) is True
        for category in WEB_PERMISSION_CATEGORIES
    }
    normalized["features"] = features
    return normalized


def web_permissions_signature(
    is_superuser: bool,
    permissions: Mapping[str, Any] | None,
    *,
    is_active: bool = True,
) -> tuple[Any, ...]:
    """
    构造用于 Agent 缓存键的确定性网页用户权限签名。

    :param is_superuser: 是否为超级管理员
    :param permissions: 网页用户权限字典
    :param is_active: 账号是否处于激活状态
    :return: 可哈希的权限状态元组
    """
    normalized = normalize_web_permissions(permissions)
    features = tuple(sorted(normalized["features"].items()))
    return (
        bool(is_active),
        bool(is_superuser),
        *(normalized[category] for category in WEB_PERMISSION_CATEGORIES),
        features,
    )


def has_web_permission(
    is_superuser: bool,
    permissions: Mapping[str, Any] | None,
    category: str,
    feature: str | None = None,
) -> bool:
    """
    判断网页用户是否拥有指定分类权限及可选功能级权限。

    超级管理员始终放行；普通用户必须具备对应分类权限，且指定功能键不能为 False
    （未显式配置的功能键默认继承其所属分类权限，与前端和数据库约定一致）。

    :param is_superuser: 是否为超级管理员
    :param permissions: 网页用户权限字典
    :param category: 顶层权限分类名
    :param feature: 可选功能级权限键
    :return: 是否允许访问
    """
    if is_superuser:
        return True
    if category not in WEB_PERMISSION_CATEGORIES:
        return False
    normalized = normalize_web_permissions(permissions)
    if normalized.get(category) is not True:
        return False
    if feature and normalized["features"].get(feature) is False:
        return False
    return True


def _resolve_subscribe_media_feature(media_type: str | None) -> str | None:
    """
    将订阅请求中的媒体类型映射到对应的功能级权限键。

    :param media_type: 请求中的媒体类型值
    :return: 对应的订阅功能键；无法识别时返回 None
    """
    if not isinstance(media_type, str):
        return None
    normalized = media_type.strip().lower()
    if not normalized:
        return None
    return _SUBSCRIBE_MEDIA_TYPE_FEATURES.get(normalized)


def authorize_web_operation(
    is_superuser: bool,
    permissions: Mapping[str, Any] | None,
    operation_id: str,
    *,
    media_type: str | None = None,
) -> tuple[bool, str]:
    """
    校验网页登录用户是否允许执行指定的白名单业务 API 操作。

    :param is_superuser: 是否为超级管理员
    :param permissions: 网页用户权限字典
    :param operation_id: 白名单业务 API 操作 ID
    :param media_type: 可选媒体类型，用于细分订阅功能权限
    :return: ``(是否允许, 拒绝原因)`` 二元组
    """
    if is_superuser:
        return True, ""

    requirements = WEB_OPERATION_REQUIREMENTS.get(operation_id)
    if requirements is None:
        return False, f"权限不足：操作 {operation_id} 仅允许系统管理员执行"

    if not requirements:
        return True, ""

    if operation_id in {"subscription.add", "subscription.update"}:
        media_feature = _resolve_subscribe_media_feature(media_type)
        if media_feature is not None:
            if has_web_permission(False, permissions, "subscribe", media_feature):
                return True, ""
            label = WEB_PERMISSION_FEATURE_LABELS.get(media_feature, media_feature)
            return False, f"权限不足：当前账号未开通「{label}」权限，无法执行操作 {operation_id}"

    for category, feature in requirements:
        if has_web_permission(False, permissions, category, feature):
            return True, ""

    labels: list[str] = []
    for category, feature in requirements:
        label = (
            WEB_PERMISSION_FEATURE_LABELS.get(feature, feature)
            if feature
            else WEB_PERMISSION_CATEGORY_LABELS.get(category, category)
        )
        if label not in labels:
            labels.append(label)
    needed = "或".join(f"「{label}」" for label in labels)
    return False, f"权限不足：当前账号缺少{needed}权限，无法执行操作 {operation_id}"


def describe_web_permissions(
    is_superuser: bool,
    permissions: Mapping[str, Any] | None,
) -> str:
    """
    生成用于注入系统提示词的当前用户权限摘要，防止模型假定自己拥有管理员能力。

    :param is_superuser: 是否为超级管理员
    :param permissions: 网页用户权限字典
    :return: 面向模型的中文权限边界描述；管理员返回空字符串
    """
    if is_superuser:
        return ""

    normalized = normalize_web_permissions(permissions)
    features = normalized["features"]
    granted_categories = [
        WEB_PERMISSION_CATEGORY_LABELS[category]
        for category in WEB_PERMISSION_CATEGORIES
        if normalized[category] is True
    ]
    denied_features = [
        WEB_PERMISSION_FEATURE_LABELS.get(key, key)
        for key, value in sorted(features.items())
        if value is False
    ]
    granted_text = "、".join(granted_categories) if granted_categories else "无业务功能权限（仅基础对话与基础配置读取）"
    lines = [
        "- 当前会话身份：普通网页登录用户（非系统管理员）。",
        f"- 当前已开通权限：{granted_text}。",
    ]
    if denied_features:
        lines.append(f"- 当前已关闭的子功能：{'、'.join(denied_features)}。")
    lines.extend(
        [
            "- 严禁越权：无权执行系统配置修改、插件安装/配置/调用、定时任务调度、宿主机命令/代码执行、后台任务或查看他人下载与管理敏感信息。",
            "- 若用户请求超出上述权限范围，请直接说明权限不足，并建议联系系统管理员调整账号权限。",
        ]
    )
    return "\n".join(lines)
