#!/usr/bin/env python3
"""
通过 MoviePilot 本机配置调用媒体服务器自身 API 的受控命令行工具。
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCRIPT_PATH = Path(__file__).resolve()


def _find_project_root() -> Optional[Path]:
    """
    从运行环境或目录标记定位 MoviePilot 程序目录。
    """
    candidates = []
    configured_root = os.environ.get("MOVIEPILOT_ROOT", "").strip()
    if configured_root:
        candidates.append(Path(configured_root).expanduser())
    for base in (Path.cwd().resolve(), SCRIPT_PATH.parent):
        candidates.extend((base, *base.parents))
    for candidate in candidates:
        if (candidate / "app" / "runtime" / "config.py").is_file():
            return candidate
    return None


PROJECT_ROOT = _find_project_root()
DEFAULT_LIMIT = 50
MAX_LIMIT = 200
ALL_PROVIDERS = (
    "emby",
    "jellyfin",
    "plex",
    "zspace",
    "ugreen",
    "trimemedia",
    "navidrome",
    "mediavault",
)
PROVIDER_CLASSES = {
    "emby": "app.modules.emby.emby:Emby",
    "jellyfin": "app.modules.jellyfin.jellyfin:Jellyfin",
    "plex": "app.modules.plex.plex:Plex",
    "zspace": "app.modules.zspace.zspace:ZSpace",
    "ugreen": "app.modules.ugreen.ugreen:Ugreen",
    "trimemedia": "app.modules.trimemedia.trimemedia:TrimeMedia",
    "navidrome": "app.modules.navidrome.navidrome:Navidrome",
    "mediavault": "app.modules.vyo.vyo:Vyo",
}
_UNSET = object()


class OperationError(RuntimeError):
    """
    可安全返回给 Agent 的媒体服务器操作错误。
    """


@dataclass(frozen=True, slots=True)
class ArgumentSpec:
    """
    单个动作参数的公开调用契约。

    :param name: 动作调用中的精确参数名称
    :param type: 公开模式使用的类型表达式，支持联合类型及数组
    :param description: 供调用方理解参数用途、限制和单位的中文说明
    :param required: 该参数是否必填，默认 False
    :param default: 参数默认值；仅在明确声明默认值时向调用方公开
    :param enum: 允许的参数值集合；空元组表示不限制枚举
    """

    name: str
    type: str
    description: str
    required: bool = False
    default: Any = _UNSET
    enum: tuple[Any, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """
        返回可直接交给 Agent 的参数模式。
        """
        result: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
            "required": self.required,
            "description": self.description,
        }
        if self.default is not _UNSET:
            result["default"] = self.default
        if self.enum:
            result["enum"] = list(self.enum)
        return result


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """
    单个允许执行动作的能力、影响范围及参数契约。

    :param description: 公开动作的用途与操作边界说明
    :param effect: 策略分类标识，例如 safe_read、reversible_write 或 destructive_write；保持机器可读原值
    :param providers: 支持该动作的服务标识，默认包括本脚本支持的全部服务
    :param arguments: 按声明顺序排列的参数契约，默认无参数
    :param argument_rules: 参数间的组合约束与安全要求，默认无附加规则
    """

    description: str
    effect: str
    providers: tuple[str, ...] = ALL_PROVIDERS
    arguments: tuple[ArgumentSpec, ...] = ()
    argument_rules: tuple[str, ...] = ()

    @property
    def required(self) -> tuple[str, ...]:
        """
        返回保持旧能力合同兼容的必填参数名。
        """
        return tuple(argument.name for argument in self.arguments if argument.required)

    def to_dict(self, name: str) -> dict[str, Any]:
        """
        返回不包含实现对象的公开能力描述。
        """
        return {
            "action": name,
            "description": self.description,
            "effect": self.effect,
            "providers": list(self.providers),
            "required_arguments": list(self.required),
            "arguments": [argument.to_dict() for argument in self.arguments],
            "argument_rules": list(self.argument_rules),
        }


ITEM_ID = ArgumentSpec("item_id", "string", "所选媒体服务器返回的原生条目标识。")
PARENT = ArgumentSpec("parent", "string|integer", "媒体库或父条目标识；Navidrome 可省略并使用 music。")
OFFSET = ArgumentSpec("offset", "integer", "列表偏移量，从 0 开始。", default=0)
LIMIT = ArgumentSpec("limit", "integer", "返回条目数量，范围为 1 至 200。", default=DEFAULT_LIMIT)
QUERY = ArgumentSpec(
    "query", "object",
    "统一内容投影，如 {\"fields\":[\"identity\",\"path\"]}；省略或 fields=null 保留原内容，fields=[] 仅取基础信息。",
)


ACTIONS: dict[str, ActionSpec] = {
    "instances.list": ActionSpec(
        "列出已配置的媒体服务器实例，不返回连接凭据。",
        "safe_read",
    ),
    "capabilities.list": ActionSpec(
        "列出支持的媒体服务器动作及完整参数契约。",
        "safe_read",
        arguments=(
            ArgumentSpec(
                "action_name",
                "string",
                "可选的精确动作名称，填写时只返回该动作的能力契约。",
            ),
        ),
    ),
    "server.statistics": ActionSpec("读取媒体数量及服务统计。", "safe_read"),
    "server.users.count": ActionSpec(
        "读取服务用户数量。",
        "safe_read",
        ("emby", "jellyfin", "zspace", "ugreen", "trimemedia", "navidrome", "mediavault"),
    ),
    "server.user.library_folders": ActionSpec(
        "读取当前用户可见的媒体库目录。",
        "safe_read",
        ("emby", "jellyfin", "zspace"),
    ),
    "libraries.list": ActionSpec(
        "列出可见媒体库。",
        "safe_read",
        arguments=(
            ArgumentSpec("hidden", "boolean", "只返回已配置为同步的媒体库。", default=False),
            ArgumentSpec("username", "string", "读取该用户名可见的媒体库；Emby、Jellyfin 和 ZSpace 支持。"),
            QUERY,
        ),
    ),
    "items.list": ActionSpec(
        "分页读取指定媒体库或父条目下的内容。",
        "safe_read",
        arguments=(PARENT, OFFSET, LIMIT, QUERY),
        argument_rules=("除 Navidrome 外必须提供 parent；Navidrome 忽略该参数。",),
    ),
    "items.count": ActionSpec(
        "统计指定媒体库或父条目下的内容数量。",
        "safe_read",
        arguments=(PARENT,),
        argument_rules=("除 Navidrome 外必须提供 parent；Navidrome 默认使用 music。",),
    ),
    "items.detail": ActionSpec(
        "按服务原生标识读取单个媒体条目。",
        "safe_read",
        arguments=(ArgumentSpec("item_id", "string", ITEM_ID.description, required=True), QUERY),
    ),
    "items.movies.search": ActionSpec(
        "按标题及可选年份搜索服务中的电影条目。",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "trimemedia", "mediavault"),
        (
            ArgumentSpec("title", "string", "电影标题。", required=True),
            ArgumentSpec("year", "string|integer", "可选的发行年份。"),
            QUERY,
        ),
    ),
    "items.music.search": ActionSpec(
        "按标题、艺术家或专辑搜索服务中的音乐。",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "navidrome"),
        (
            ArgumentSpec("title", "string", "曲目、专辑或音乐条目标题。"),
            ArgumentSpec("artist", "string", "艺术家名称。"),
            ArgumentSpec("album", "string", "专辑名称；须提供 title、artist 或 album。"),
            QUERY,
        ),
        ("Provide at least one of title, artist, and album.",),
    ),
    "items.season_episodes": ActionSpec(
        "读取指定剧集及可选季的现有分集信息。",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "trimemedia", "mediavault"),
        (
            ITEM_ID,
            ArgumentSpec("title", "string", "剧集标题；须提供本字段或 item_id。"),
            ArgumentSpec("year", "string|integer", "可选的首播年份。"),
            ArgumentSpec("season", "integer", "可选的季编号。"),
            QUERY,
        ),
        ("Provide at least one of item_id and title.",),
    ),
    "activity.latest": ActionSpec(
        "读取服务最近添加的媒体条目。",
        "safe_read",
        arguments=(LIMIT, ArgumentSpec("username", "string", "按该用户名读取；Emby、Jellyfin 和 ZSpace 支持。"), QUERY),
    ),
    "activity.resume": ActionSpec(
        "读取服务中正在观看或可继续播放的媒体条目。",
        "safe_read",
        arguments=(LIMIT, ArgumentSpec("username", "string", "按该用户名读取；Emby、Jellyfin 和 ZSpace 支持。"), QUERY),
    ),
    "activity.backdrops": ActionSpec(
        "读取服务近期媒体的背景图。",
        "safe_read",
        ("ugreen", "trimemedia", "mediavault"),
        (LIMIT, ArgumentSpec("remote", "boolean", "是否返回可远程访问的服务 URL。", default=False)),
    ),
    "playback.sessions": ActionSpec("读取正在进行的播放会话。", "safe_read", ("emby", "jellyfin", "plex")),
    "playback.url": ActionSpec(
        "生成单个条目的服务播放链接。",
        "safe_read",
        arguments=(ArgumentSpec("item_id", "string", ITEM_ID.description, required=True),),
    ),
    "library.scan": ActionSpec(
        "触发媒体库扫描。",
        "external_side_effect",
        arguments=(ArgumentSpec("scan_mode", "string|integer", "UGREEN 原生扫描模式；其他服务必须省略。"),),
    ),
    "metadata.refresh": ActionSpec(
        "刷新匹配条目的服务端元数据。",
        "external_side_effect",
        ("emby", "plex", "zspace", "ugreen", "trimemedia"),
        (
            ArgumentSpec(
                "items",
                "object[]",
                "待刷新的条目。每项支持 title 字符串、year 字符串或整数、type 精确 MoviePilot 媒体类型值、category 字符串及 target_path 字符串。",
                required=True,
            ),
        ),
    ),
}


def _ensure_project_import() -> None:
    """
    确保脚本从任意工作目录都可导入 MoviePilot。
    """
    if PROJECT_ROOT is None:
        raise RuntimeError(
            "无法定位 MoviePilot 程序目录；请在程序目录执行，或设置 MOVIEPILOT_ROOT="
            "<程序目录> 后重试。"
        )
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _load_configs() -> list[Any]:
    """
    通过独立短会话读取并校验本机媒体服务器配置。
    """
    _ensure_project_import()
    from app.db.oper.systemconfig import SystemConfigOper
    from app.db.session import SessionFactory
    from app.runtime.extensions.service import ServiceConfigHelper, configure_service_config_reader

    system_config = SystemConfigOper()
    # Skill 在独立 CLI 进程中运行，没有 lifespan 为无会话 Oper 装配事务执行器。
    with SessionFactory() as session:
        system_config.load_snapshot(session)
    configure_service_config_reader(system_config.get)
    return ServiceConfigHelper.get_mediaserver_configs()


def _import_symbol(reference: str) -> type[Any]:
    """
    按审核过的模块引用惰性导入服务客户端。
    """
    module_name, symbol_name = reference.split(":", 1)
    return getattr(importlib.import_module(module_name), symbol_name)


def _select_config(server_name: Optional[str]) -> Any:
    """
    按实例名选择启用配置，省略名称时只接受唯一实例。
    """
    enabled = [config for config in _load_configs() if config.enabled]
    if server_name:
        for config in enabled:
            if config.name == server_name:
                return config
        raise ValueError(f"未找到已启用媒体服务器实例: {server_name}")
    if not enabled:
        raise ValueError("没有已启用的媒体服务器实例")
    if len(enabled) == 1:
        return enabled[0]
    names = "、".join(str(config.name) for config in enabled)
    raise ValueError(f"存在多个媒体服务器实例，请用 --server 指定以下之一：{names}")


def _build_client(config: Any) -> Any:
    """
    使用本机私有配置构造具体媒体服务器 API client。
    """
    provider = str(config.type or "").strip().lower()
    reference = PROVIDER_CLASSES.get(provider)
    if not reference:
        raise ValueError(f"暂不支持媒体服务器类型: {provider or 'unknown'}")
    client = _import_symbol(reference)(
        **dict(config.config or {}),
        sync_libraries=list(config.sync_libraries or []),
    )
    if client.is_inactive():
        client.reconnect()
    if client.is_inactive():
        raise OperationError("媒体服务器连接不可用")
    return client


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """
    将服务返回的数据对象转成有界、无私有字段的 JSON 值。
    """
    if depth > 6:
        return str(value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return value.as_posix()
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return _jsonable(model_dump(mode="json"), depth=depth + 1)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item, depth=depth + 1)
            for key, item in value.items()
            if not str(key).startswith("_") and not _is_sensitive_key(str(key))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_jsonable(item, depth=depth + 1) for item in value]
    data = getattr(value, "__dict__", None)
    if isinstance(data, dict):
        return _jsonable(data, depth=depth + 1)
    try:
        return _jsonable(list(value), depth=depth + 1)
    except TypeError:
        return str(value)


def _is_sensitive_key(key: str) -> bool:
    """
    识别服务实现结果中的凭据、会话和认证字段。
    """
    normalized = "".join(character for character in key.lower() if character.isalnum())
    return any(
        marker in normalized
        for marker in (
            "password",
            "passwd",
            "secret",
            "token",
            "apikey",
            "cookie",
            "authorization",
            "sessionid",
        )
    )


def _require(arguments: Mapping[str, Any], name: str) -> Any:
    """
    读取必填动作参数。
    """
    value = arguments.get(name)
    if value is None or value == "" or value == []:
        raise ValueError(f"缺少必填参数: {name}")
    return value


def _matches_argument_type(value: Any, declared_type: str) -> bool:
    """
    判断 JSON 值是否符合公开参数合同中的紧凑类型表达式。
    """
    for candidate in declared_type.split("|"):
        if candidate.endswith("[]"):
            if isinstance(value, list) and all(
                _matches_argument_type(item, candidate[:-2]) for item in value
            ):
                return True
            continue
        if candidate == "string" and isinstance(value, str):
            return True
        if candidate == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if candidate == "boolean" and isinstance(value, bool):
            return True
        if candidate == "object" and isinstance(value, Mapping):
            return True
    return False


def _validate_refresh_items(items: Any) -> list[str]:
    """
    校验 metadata.refresh 的嵌套条目并返回全部错误。
    """
    if not isinstance(items, list):
        return []
    errors: list[str] = []
    allowed_fields = {"title", "year", "type", "category", "target_path"}
    allowed_types = {"电影", "电视剧", "音乐"}
    for index, item in enumerate(items):
        if not isinstance(item, Mapping):
            continue
        unknown = sorted(set(item) - allowed_fields)
        if unknown:
            errors.append(f"items[{index}] 未知字段: {', '.join(unknown)}")
        if item.get("type") is not None and item.get("type") not in allowed_types:
            errors.append(f"items[{index}].type 仅支持: 电影、电视剧、音乐")
        for name in ("title", "category", "target_path"):
            if item.get(name) is not None and not isinstance(item.get(name), str):
                errors.append(f"items[{index}].{name} 必须是 string")
        year = item.get("year")
        if year is not None and not (
            isinstance(year, str) or (isinstance(year, int) and not isinstance(year, bool))
        ):
            errors.append(f"items[{index}].year 必须是 string|integer")
    return errors


def _validate_action_arguments(action: str, spec: ActionSpec, arguments: Mapping[str, Any]) -> None:
    """
    一次性校验动作的全部参数，避免 Agent 按单个错误反复试调用。
    """
    errors: list[str] = []
    argument_specs = {argument.name: argument for argument in spec.arguments}
    unknown = sorted(set(arguments) - set(argument_specs))
    if unknown:
        errors.append(f"未知参数: {', '.join(unknown)}")
    for name, argument in argument_specs.items():
        value = arguments.get(name)
        if argument.required and (value is None or value == "" or value == []):
            errors.append(f"缺少必填参数: {name}")
            continue
        if value is not None and not _matches_argument_type(value, argument.type):
            errors.append(f"参数 {name} 必须是 {argument.type}")
        if value is not None and argument.enum and value not in argument.enum:
            errors.append(f"参数 {name} 仅支持: {', '.join(map(str, argument.enum))}")

    if action == "items.music.search" and not any(arguments.get(name) for name in ("title", "artist", "album")):
        errors.append("title、artist、album 至少提供一项")
    if action == "items.season_episodes" and not any(arguments.get(name) for name in ("item_id", "title")):
        errors.append("item_id 与 title 至少提供一项")
    if action == "metadata.refresh":
        errors.extend(_validate_refresh_items(arguments.get("items")))
    if errors:
        raise ValueError("参数校验失败：" + "；".join(errors))
    if "query" in argument_specs:
        _query_kwargs(arguments)


def _validate_provider_arguments(action: str, provider: str, arguments: Mapping[str, Any]) -> None:
    """
    校验需要知道服务实现后才能确定的条件参数。
    """
    errors: list[str] = []
    if arguments.get("query") is not None and provider not in {"emby", "jellyfin", "plex", "navidrome"}:
        errors.append(f"{provider} 暂不支持参数: query")
    if action in {"items.list", "items.count"} and provider != "navidrome" and not arguments.get("parent"):
        errors.append(f"{provider} 的 {action} 必须提供 parent")
    username_providers = {"emby", "jellyfin", "zspace"}
    if (
        action in {"libraries.list", "activity.latest", "activity.resume"}
        and arguments.get("username") is not None
        and provider not in username_providers
    ):
        errors.append(f"{provider} 的 {action} 不支持参数: username")
    if action == "library.scan" and arguments.get("scan_mode") is not None and provider != "ugreen":
        errors.append(f"{provider} 的 library.scan 不支持参数: scan_mode")
    if errors:
        raise ValueError("参数校验失败：" + "；".join(errors))


def _limit(arguments: Mapping[str, Any]) -> int:
    """
    读取安全的分页条数。
    """
    return min(MAX_LIMIT, max(1, int(arguments.get("limit", DEFAULT_LIMIT))))


def _query_kwargs(arguments: Mapping[str, Any]) -> dict[str, Any]:
    """
    按需加载统一投影模型，默认调用不增加参数或加载宿主配置。

    :param arguments: 所选动作的参数
    :return: 已校验的 query 关键字；未指定时为空字典
    :raises ValueError: 投影含未知内容组或非法字段
    """
    if arguments.get("query") is None:
        return {}
    _ensure_project_import()
    from app.schemas.mediaserver import MediaServerQueryOptions

    return {"query": MediaServerQueryOptions.model_validate(arguments["query"])}


def _call_supported(method: Any, **kwargs: Any) -> Any:
    """
    仅向服务实现公共方法传递其显式支持的关键字参数。
    """
    parameters = inspect.signature(method).parameters
    supported = {key: value for key, value in kwargs.items() if key in parameters}
    return method(**supported)


def _items_list(client: Any, provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """
    按服务实现签名列出媒体条目。
    """
    start = max(0, int(arguments.get("offset", 0)))
    limit = _limit(arguments)
    if provider == "navidrome":
        items = client.get_items(start_index=start, limit=limit, **_query_kwargs(arguments))
    else:
        parent = _require(arguments, "parent")
        items = client.get_items(parent=parent, start_index=start, limit=limit, **_query_kwargs(arguments))
    materialized = list(items or [])
    return {"offset": start, "limit": limit, "items": _jsonable(materialized)}


def _items_count(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """
    按服务实现签名统计媒体条目。
    """
    if provider == "navidrome":
        return client.get_items_count(str(arguments.get("parent") or "music"))
    return client.get_items_count(_require(arguments, "parent"))


def _activity(client: Any, action: str, arguments: Mapping[str, Any]) -> Any:
    """
    读取最近入库或继续播放条目。
    """
    method = client.get_latest if action == "activity.latest" else client.get_resume
    limit = _limit(arguments)
    return _call_supported(
        method, num=limit, count=limit, username=arguments.get("username"), **_query_kwargs(arguments)
    )


def _search_music(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """
    按服务实现公共接口查询原生音乐条目。
    """
    title = arguments.get("title")
    artist = arguments.get("artist")
    album = arguments.get("album")
    if not any((title, artist, album)):
        raise ValueError("title、artist、album 至少提供一项")
    method = client.search_music if provider == "navidrome" else client.get_music
    return method(title=title, artist=artist, album=album, **_query_kwargs(arguments))


def _season_episodes(client: Any, arguments: Mapping[str, Any]) -> Any:
    """
    查询一个服务实现原生剧集的已入库集数。
    """
    if not arguments.get("item_id") and not arguments.get("title"):
        raise ValueError("item_id 与 title 至少提供一项")
    return _call_supported(
        client.get_tv_episodes,
        item_id=arguments.get("item_id"),
        title=arguments.get("title"),
        year=arguments.get("year"),
        season=arguments.get("season"),
        **_query_kwargs(arguments),
    )


def _playback_sessions(client: Any, provider: str) -> Any:
    """
    通过固定服务实现端点读取活动播放会话。
    """
    if provider == "plex":
        plex = client.get_plex()
        return plex.sessions() if plex else []
    if provider == "emby":
        from app.adapters.network.http import RequestUtils

        response = RequestUtils().get_res(
            f"{client._host}emby/Sessions",
            params={"api_key": client._apikey},
        )
    else:
        response = client._request().get_res(f"{client._host}Sessions")
    return response.json() if response else []


def _refresh_metadata(client: Any, arguments: Mapping[str, Any]) -> Any:
    """
    把结构化刷新条目转换为宿主 DTO 后调用服务实现。
    """
    _ensure_project_import()
    from app.schemas.mediaserver import RefreshMediaItem

    items = [RefreshMediaItem.model_validate(item) for item in _require(arguments, "items")]
    return client.refresh_library_by_items(items)


def _dispatch(client: Any, provider: str, action: str, arguments: Mapping[str, Any]) -> Any:
    """
    执行动作注册表中允许的媒体服务器调用。
    """
    if action == "server.statistics":
        return client.get_medias_count()
    if action == "server.users.count":
        return client.get_user_count()
    if action == "server.user.library_folders":
        return client.get_user_library_folders()
    if action == "libraries.list":
        return _call_supported(
            client.get_librarys,
            hidden=bool(arguments.get("hidden", False)),
            username=arguments.get("username"),
            **_query_kwargs(arguments),
        )
    if action == "items.list":
        return _items_list(client, provider, arguments)
    if action == "items.count":
        return _items_count(client, provider, arguments)
    if action == "items.detail":
        return client.get_iteminfo(str(_require(arguments, "item_id")), **_query_kwargs(arguments))
    if action == "items.movies.search":
        return client.get_movies(
            title=str(_require(arguments, "title")), year=arguments.get("year"), **_query_kwargs(arguments)
        )
    if action == "items.music.search":
        return _search_music(client, provider, arguments)
    if action == "items.season_episodes":
        return _season_episodes(client, arguments)
    if action in {"activity.latest", "activity.resume"}:
        return _activity(client, action, arguments)
    if action == "activity.backdrops":
        return client.get_latest_backdrops(num=_limit(arguments), remote=bool(arguments.get("remote", False)))
    if action == "playback.sessions":
        return _playback_sessions(client, provider)
    if action == "playback.url":
        return client.get_play_url(str(_require(arguments, "item_id")))
    if action == "library.scan":
        return _call_supported(
            client.refresh_root_library,
            scan_mode=arguments.get("scan_mode"),
        )
    if action == "metadata.refresh":
        return _refresh_metadata(client, arguments)
    raise ValueError(f"未知 media server action: {action}")


def list_instances() -> dict[str, Any]:
    """
    列出媒体服务器实例的非敏感投影。
    """
    instances = [
        {
            "name": config.name,
            "provider": config.type,
            "enabled": bool(config.enabled),
            "sync_library_count": len(config.sync_libraries or []),
        }
        for config in _load_configs()
    ]
    return {"success": True, "instances": instances}


def list_capabilities(server_name: Optional[str], action: Optional[str] = None) -> dict[str, Any]:
    """
    返回全部或指定实例支持的动作及完整参数合同。
    """
    provider = None
    if server_name:
        provider = str(_select_config(server_name).type or "").lower()
    if action and action not in ACTIONS:
        raise ValueError(f"未知 media server action: {action}")
    actions = [
        spec.to_dict(name)
        for name, spec in ACTIONS.items()
        if (not action or name == action) and (provider is None or provider in spec.providers)
    ]
    if action and not actions:
        raise ValueError(f"{provider} 不支持 action: {action}")
    return {
        "success": True,
        "server": server_name,
        "provider": provider,
        "actions": actions,
    }


def call_action(server_name: Optional[str], action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """
    校验动作与服务实现合同并执行一次受控调用。
    """
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"未知 media server action: {action}")
    _validate_action_arguments(action, spec, arguments)
    if action == "instances.list":
        return {
            "success": True,
            "server": None,
            "provider": None,
            "action": action,
            "effect": spec.effect,
            "data": list_instances()["instances"],
        }
    if action == "capabilities.list":
        capabilities = list_capabilities(server_name, arguments.get("action_name"))
        return {
            "success": True,
            "server": capabilities["server"],
            "provider": capabilities["provider"],
            "action": action,
            "effect": spec.effect,
            "data": capabilities["actions"],
        }
    config = _select_config(server_name)
    provider = str(config.type or "").lower()
    if provider not in spec.providers:
        raise ValueError(f"{provider} 不支持 action: {action}")
    _validate_provider_arguments(action, provider, arguments)
    result = _dispatch(_build_client(config), provider, action, arguments)
    if spec.effect != "safe_read" and result is False:
        raise OperationError("媒体服务器 action 返回失败")
    return {
        "success": True,
        "server": config.name,
        "provider": provider,
        "action": action,
        "effect": spec.effect,
        "data": _jsonable(result),
    }


def _parse_arguments(raw: str) -> dict[str, Any]:
    """
    解析并限制调用参数为单个 JSON 对象。
    """
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("--arguments 必须是 JSON 对象")
    return value


def _build_parser() -> argparse.ArgumentParser:
    """
    构建固定命令和参数解析器。
    """
    parser = argparse.ArgumentParser(description="MoviePilot 媒体服务器 API 操作工具")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("instances", help="列出已配置实例")
    capabilities = subparsers.add_parser("capabilities", help="列出允许的动作")
    capabilities.add_argument("--server")
    capabilities.add_argument("--action")
    call = subparsers.add_parser("call", help="调用一个允许的动作")
    call.add_argument("--server")
    call.add_argument("--action", required=True)
    call.add_argument("--arguments", default="{}")
    return parser


def main() -> int:
    """
    执行 CLI 并以稳定 JSON 封装返回结果。
    """
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "instances":
            payload = list_instances()
        elif args.command == "capabilities":
            payload = list_capabilities(args.server, args.action)
        elif args.command == "call":
            payload = call_action(args.server, args.action, _parse_arguments(args.arguments))
        else:
            parser.print_help()
            return 0
    # 命令行边界统一转换底层异常，维持调用方可解析的 JSON 错误格式。
    except Exception as error:  # noqa: BLE001
        payload = {
            "success": False,
            "error_type": type(error).__name__,
            "message": (
                str(error)
                if isinstance(error, (ValueError, OperationError, ImportError, SyntaxError, RuntimeError))
                else "媒体服务器调用失败"
            ),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
