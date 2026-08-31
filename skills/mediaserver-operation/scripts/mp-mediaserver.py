#!/usr/bin/env python3
"""通过 MoviePilot 本机配置调用媒体服务器自身 API 的受控命令行工具。"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

SCRIPT_PATH = Path(__file__).resolve()
PROJECT_ROOT = SCRIPT_PATH.parents[3]
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
)
PROVIDER_CLASSES = {
    "emby": "app.modules.emby.emby:Emby",
    "jellyfin": "app.modules.jellyfin.jellyfin:Jellyfin",
    "plex": "app.modules.plex.plex:Plex",
    "zspace": "app.modules.zspace.zspace:ZSpace",
    "ugreen": "app.modules.ugreen.ugreen:Ugreen",
    "trimemedia": "app.modules.trimemedia.trimemedia:TrimeMedia",
    "navidrome": "app.modules.navidrome.navidrome:Navidrome",
}


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """描述一个允许调用的媒体服务器 action。"""

    description: str
    effect: str
    providers: tuple[str, ...] = ALL_PROVIDERS
    required: tuple[str, ...] = ()

    def to_dict(self, name: str) -> dict[str, Any]:
        """返回不包含实现对象的公开能力描述。"""
        return {
            "action": name,
            "description": self.description,
            "effect": self.effect,
            "providers": list(self.providers),
            "required_arguments": list(self.required),
        }


ACTIONS: dict[str, ActionSpec] = {
    "server.statistics": ActionSpec("Read media counts and provider statistics.", "safe_read"),
    "server.users.count": ActionSpec(
        "Read provider user count.",
        "safe_read",
        ("emby", "jellyfin", "zspace", "ugreen", "trimemedia", "navidrome"),
    ),
    "server.user.library_folders": ActionSpec(
        "Read the current user's visible library folders.",
        "safe_read",
        ("emby", "jellyfin", "zspace"),
    ),
    "libraries.list": ActionSpec("List visible provider libraries.", "safe_read"),
    "items.list": ActionSpec("Page items below one library or parent.", "safe_read"),
    "items.count": ActionSpec("Count items below one library or parent.", "safe_read"),
    "items.detail": ActionSpec("Read one provider item by native ID.", "safe_read", required=("item_id",)),
    "items.movies.search": ActionSpec(
        "Search provider-native movie items by title and optional year.",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "trimemedia"),
        ("title",),
    ),
    "items.music.search": ActionSpec(
        "Search provider-native music by title, artist, or album.",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "navidrome"),
    ),
    "items.season_episodes": ActionSpec(
        "Read native episode coverage for one series and optional season.",
        "safe_read",
        ("emby", "jellyfin", "plex", "zspace", "ugreen", "trimemedia"),
    ),
    "activity.latest": ActionSpec("Read recently added provider items.", "safe_read"),
    "activity.resume": ActionSpec("Read in-progress/resumable provider items.", "safe_read"),
    "activity.backdrops": ActionSpec(
        "Read recent provider backdrop images.",
        "safe_read",
        ("ugreen", "trimemedia"),
    ),
    "playback.sessions": ActionSpec("Read active playback sessions.", "safe_read", ("emby", "jellyfin", "plex")),
    "playback.url": ActionSpec("Build the provider play URL for one item.", "safe_read", required=("item_id",)),
    "library.scan": ActionSpec("Trigger a provider library scan.", "external_side_effect"),
    "metadata.refresh": ActionSpec(
        "Refresh provider metadata for mapped items.",
        "external_side_effect",
        ("emby", "plex", "zspace", "ugreen", "trimemedia"),
        ("items",),
    ),
}


def _ensure_project_import() -> None:
    """确保脚本从任意工作目录都可导入 MoviePilot。"""
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _load_configs() -> list[Any]:
    """读取并校验本机媒体服务器配置。"""
    _ensure_project_import()
    from app.runtime.extensions.service import ServiceConfigHelper

    return ServiceConfigHelper.get_mediaserver_configs()


def _import_symbol(reference: str) -> type[Any]:
    """按审核过的模块引用惰性导入 provider client。"""
    module_name, symbol_name = reference.split(":", 1)
    return getattr(importlib.import_module(module_name), symbol_name)


def _select_config(server_name: Optional[str]) -> Any:
    """按实例名选择启用配置，省略名称时只接受唯一实例。"""
    enabled = [config for config in _load_configs() if config.enabled]
    if server_name:
        for config in enabled:
            if config.name == server_name:
                return config
        raise ValueError(f"未找到已启用媒体服务器实例: {server_name}")
    if len(enabled) == 1:
        return enabled[0]
    raise ValueError("存在多个媒体服务器实例，请显式提供 --server")


def _build_client(config: Any) -> Any:
    """使用本机私有配置构造具体媒体服务器 API client。"""
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
        raise RuntimeError("媒体服务器连接不可用")
    return client


def _jsonable(value: Any, *, depth: int = 0) -> Any:
    """将 provider DTO 转成有界、无私有字段的 JSON 值。"""
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
    """识别 provider 结果中的凭据、会话和认证字段。"""
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
    """读取必填 action 参数。"""
    value = arguments.get(name)
    if value is None or value == "" or value == []:
        raise ValueError(f"缺少必填参数: {name}")
    return value


def _limit(arguments: Mapping[str, Any]) -> int:
    """读取安全的分页条数。"""
    return min(MAX_LIMIT, max(1, int(arguments.get("limit", DEFAULT_LIMIT))))


def _call_supported(method: Any, **kwargs: Any) -> Any:
    """仅向 provider 公共方法传递其显式支持的关键字参数。"""
    parameters = inspect.signature(method).parameters
    supported = {key: value for key, value in kwargs.items() if key in parameters}
    return method(**supported)


def _items_list(client: Any, provider: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """按 provider 签名列出媒体条目。"""
    start = max(0, int(arguments.get("offset", 0)))
    limit = _limit(arguments)
    if provider == "navidrome":
        items = client.get_items(start_index=start, limit=limit)
    else:
        parent = _require(arguments, "parent")
        items = client.get_items(parent=parent, start_index=start, limit=limit)
    materialized = list(items or [])
    return {"offset": start, "limit": limit, "items": _jsonable(materialized)}


def _items_count(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """按 provider 签名统计媒体条目。"""
    if provider == "navidrome":
        return client.get_items_count(str(arguments.get("parent") or "music"))
    return client.get_items_count(_require(arguments, "parent"))


def _activity(client: Any, action: str, arguments: Mapping[str, Any]) -> Any:
    """读取最近入库或继续播放条目。"""
    method = client.get_latest if action == "activity.latest" else client.get_resume
    limit = _limit(arguments)
    return _call_supported(
        method,
        num=limit,
        count=limit,
        username=arguments.get("username"),
    )


def _search_music(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """按 provider 公共接口查询原生音乐条目。"""
    title = arguments.get("title")
    artist = arguments.get("artist")
    album = arguments.get("album")
    if not any((title, artist, album)):
        raise ValueError("title、artist、album 至少提供一项")
    method = client.search_music if provider == "navidrome" else client.get_music
    return method(title=title, artist=artist, album=album)


def _season_episodes(client: Any, arguments: Mapping[str, Any]) -> Any:
    """查询一个 provider 原生剧集的已入库集数。"""
    if not arguments.get("item_id") and not arguments.get("title"):
        raise ValueError("item_id 与 title 至少提供一项")
    return _call_supported(
        client.get_tv_episodes,
        item_id=arguments.get("item_id"),
        title=arguments.get("title"),
        year=arguments.get("year"),
        season=arguments.get("season"),
    )


def _playback_sessions(client: Any, provider: str) -> Any:
    """通过固定 provider 端点读取活动播放会话。"""
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
    """把结构化刷新条目转换为宿主 DTO 后调用 provider。"""
    _ensure_project_import()
    from app.schemas.mediaserver import RefreshMediaItem

    items = [RefreshMediaItem.model_validate(item) for item in _require(arguments, "items")]
    return client.refresh_library_by_items(items)


def _dispatch(client: Any, provider: str, action: str, arguments: Mapping[str, Any]) -> Any:
    """执行 action 注册表中允许的媒体服务器调用。"""
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
        )
    if action == "items.list":
        return _items_list(client, provider, arguments)
    if action == "items.count":
        return _items_count(client, provider, arguments)
    if action == "items.detail":
        return client.get_iteminfo(str(_require(arguments, "item_id")))
    if action == "items.movies.search":
        return client.get_movies(
            title=str(_require(arguments, "title")),
            year=arguments.get("year"),
        )
    if action == "items.music.search":
        return _search_music(client, provider, arguments)
    if action == "items.season_episodes":
        return _season_episodes(client, arguments)
    if action in {"activity.latest", "activity.resume"}:
        return _activity(client, action, arguments)
    if action == "activity.backdrops":
        return client.get_latest_backdrops(
            num=_limit(arguments),
            remote=bool(arguments.get("remote", False)),
        )
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
    """列出媒体服务器实例的非敏感投影。"""
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


def list_capabilities(server_name: Optional[str]) -> dict[str, Any]:
    """返回全部或指定实例支持的 action 清单。"""
    provider = None
    if server_name:
        provider = str(_select_config(server_name).type or "").lower()
    actions = [spec.to_dict(name) for name, spec in ACTIONS.items() if provider is None or provider in spec.providers]
    return {
        "success": True,
        "server": server_name,
        "provider": provider,
        "actions": actions,
    }


def call_action(server_name: Optional[str], action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """校验 action/provider 合同并执行一次受控调用。"""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"未知 media server action: {action}")
    config = _select_config(server_name)
    provider = str(config.type or "").lower()
    if provider not in spec.providers:
        raise ValueError(f"{provider} 不支持 action: {action}")
    for name in spec.required:
        _require(arguments, name)
    result = _dispatch(_build_client(config), provider, action, arguments)
    if spec.effect != "safe_read" and result is False:
        raise RuntimeError("媒体服务器 action 返回失败")
    return {
        "success": True,
        "server": config.name,
        "provider": provider,
        "action": action,
        "effect": spec.effect,
        "data": _jsonable(result),
    }


def _parse_arguments(raw: str) -> dict[str, Any]:
    """解析并限制调用参数为单个 JSON 对象。"""
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("--arguments 必须是 JSON 对象")
    return value


def _build_parser() -> argparse.ArgumentParser:
    """构建固定命令和参数解析器。"""
    parser = argparse.ArgumentParser(description="MoviePilot media server API helper")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("instances", help="list configured instances")
    capabilities = subparsers.add_parser("capabilities", help="list allowed actions")
    capabilities.add_argument("--server")
    call = subparsers.add_parser("call", help="call one allowed action")
    call.add_argument("--server")
    call.add_argument("--action", required=True, choices=sorted(ACTIONS))
    call.add_argument("--arguments", default="{}")
    return parser


def main() -> int:
    """执行 CLI 并以稳定 JSON envelope 返回结果。"""
    parser = _build_parser()
    args = parser.parse_args()
    try:
        if args.command == "instances":
            payload = list_instances()
        elif args.command == "capabilities":
            payload = list_capabilities(args.server)
        elif args.command == "call":
            payload = call_action(args.server, args.action, _parse_arguments(args.arguments))
        else:
            parser.print_help()
            return 0
    except Exception as error:  # noqa: BLE001
        payload = {
            "success": False,
            "error_type": type(error).__name__,
            "message": str(error) if isinstance(error, ValueError) else "媒体服务器调用失败",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
