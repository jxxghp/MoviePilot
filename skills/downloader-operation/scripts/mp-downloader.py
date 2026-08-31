#!/usr/bin/env python3
"""通过 MoviePilot 本机配置调用下载器自身 API 的受控命令行工具。"""

from __future__ import annotations

import argparse
import importlib
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
ALL_PROVIDERS = ("qbittorrent", "transmission", "rtorrent")
PROVIDER_CLASSES = {
    "qbittorrent": "app.modules.qbittorrent.qbittorrent:Qbittorrent",
    "transmission": "app.modules.transmission.transmission:Transmission",
    "rtorrent": "app.modules.rtorrent.rtorrent:Rtorrent",
}


@dataclass(frozen=True, slots=True)
class ActionSpec:
    """描述一个允许调用的下载器 action。"""

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
    "tasks.list": ActionSpec("List and filter downloader tasks.", "safe_read"),
    "tasks.files": ActionSpec("List files and priorities for one task.", "safe_read", required=("task_id",)),
    "tasks.files.selection.set": ActionSpec(
        "Select wanted and unwanted files within one task.",
        "reversible_write",
        required=("task_id",),
    ),
    "tasks.trackers": ActionSpec(
        "List trackers for one task.", "safe_read", ("qbittorrent", "transmission"), ("task_id",)
    ),
    "tasks.tags.get": ActionSpec("Read task tags or labels.", "safe_read", required=("task_id",)),
    "tasks.peers": ActionSpec(
        "Read qBittorrent peer synchronization data.", "safe_read", ("qbittorrent",), ("task_id",)
    ),
    "tasks.start": ActionSpec("Start or resume one or more tasks.", "reversible_write", required=("task_ids",)),
    "tasks.stop": ActionSpec("Pause one or more tasks.", "reversible_write", required=("task_ids",)),
    "tasks.delete": ActionSpec("Delete tasks and optionally their data.", "destructive_write", required=("task_ids",)),
    "tasks.recheck": ActionSpec("Force data verification for tasks.", "external_side_effect", required=("task_ids",)),
    "tasks.reannounce": ActionSpec(
        "Force tracker reannounce.", "external_side_effect", ("qbittorrent", "transmission"), ("task_ids",)
    ),
    "tasks.queue.move": ActionSpec(
        "Move tasks to top, up, down, or bottom of the queue.",
        "reversible_write",
        ("qbittorrent", "transmission"),
        ("task_ids", "position"),
    ),
    "tasks.force_start.set": ActionSpec(
        "Enable or disable qBittorrent force-start for tasks.",
        "reversible_write",
        ("qbittorrent",),
        ("task_ids", "enabled"),
    ),
    "tasks.properties.set": ActionSpec(
        "Set task speed, ratio, or seeding-time limits.", "reversible_write", required=("task_id",)
    ),
    "tasks.location.set": ActionSpec(
        "Move or retarget one task to a provider-side path.", "external_side_effect", required=("task_id", "location")
    ),
    "tasks.category.set": ActionSpec(
        "Set qBittorrent category.", "reversible_write", ("qbittorrent",), ("task_id", "category")
    ),
    "tasks.tags.set": ActionSpec("Set or add task tags/labels.", "reversible_write", required=("task_ids", "tags")),
    "tasks.trackers.update": ActionSpec(
        "Add or replace task trackers.", "reversible_write", ("qbittorrent", "transmission"), ("task_id", "trackers")
    ),
    "tasks.add.direct": ActionSpec(
        "Submit a magnet, URL, or local torrent file directly to the provider.",
        "external_side_effect",
        required=("content",),
    ),
    "session.stats": ActionSpec("Read provider transfer/session statistics.", "safe_read"),
    "session.speed_limits.get": ActionSpec("Read global speed limits.", "safe_read", ("qbittorrent", "transmission")),
    "session.speed_limits.set": ActionSpec(
        "Set global speed limits in KB/s.", "reversible_write", ("qbittorrent", "transmission")
    ),
    "session.details": ActionSpec(
        "Read Transmission session configuration and capacity details.",
        "safe_read",
        ("transmission",),
    ),
    "session.content_layout": ActionSpec(
        "Read qBittorrent's default torrent content layout.",
        "safe_read",
        ("qbittorrent",),
    ),
}


def _ensure_project_import() -> None:
    """确保脚本从任意工作目录都可导入 MoviePilot。"""
    project_path = str(PROJECT_ROOT)
    if project_path not in sys.path:
        sys.path.insert(0, project_path)


def _load_configs() -> list[Any]:
    """读取并校验本机下载器配置。"""
    _ensure_project_import()
    from app.db.oper.systemconfig import SystemConfigOper
    from app.runtime.extensions.service import ServiceConfigHelper
    from app.runtime.extensions.service import configure_service_config_reader

    system_config = SystemConfigOper()
    system_config.load_snapshot()
    configure_service_config_reader(system_config.get)
    return ServiceConfigHelper.get_downloader_configs()


def _import_symbol(reference: str) -> type[Any]:
    """按审核过的模块引用惰性导入 provider client。"""
    module_name, symbol_name = reference.split(":", 1)
    return getattr(importlib.import_module(module_name), symbol_name)


def _select_config(client_name: Optional[str]) -> Any:
    """按实例名选择启用配置，省略名称时使用默认或唯一实例。"""
    enabled = [config for config in _load_configs() if config.enabled]
    if client_name:
        for config in enabled:
            if config.name == client_name:
                return config
        raise ValueError(f"未找到已启用下载器实例: {client_name}")
    defaults = [config for config in enabled if config.default]
    if len(defaults) == 1:
        return defaults[0]
    if len(enabled) == 1:
        return enabled[0]
    raise ValueError("存在多个下载器实例，请显式提供 --client")


def _build_client(config: Any) -> Any:
    """使用本机私有配置构造具体下载器 API client。"""
    provider = str(config.type or "").strip().lower()
    reference = PROVIDER_CLASSES.get(provider)
    if not reference:
        raise ValueError(f"暂不支持下载器类型: {provider or 'unknown'}")
    client = _import_symbol(reference)(**dict(config.config or {}))
    if client.is_inactive():
        client.reconnect()
    if client.is_inactive():
        raise RuntimeError("下载器连接不可用")
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
        return _jsonable(dict(value), depth=depth + 1)
    except TypeError, ValueError:
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


def _page(items: Sequence[Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
    """对列表结果执行统一偏移分页。"""
    offset = max(0, int(arguments.get("offset", 0)))
    limit = min(MAX_LIMIT, max(1, int(arguments.get("limit", DEFAULT_LIMIT))))
    materialized = list(items)
    return {
        "total": len(materialized),
        "offset": offset,
        "limit": limit,
        "items": _jsonable(materialized[offset : offset + limit]),
    }


def _task_ids(arguments: Mapping[str, Any]) -> str | list[str]:
    """读取一个或多个任务 ID，并保持 provider 接受的形态。"""
    raw = arguments.get("task_ids", arguments.get("task_id"))
    if isinstance(raw, list):
        values = [str(item) for item in raw if str(item).strip()]
        if not values:
            raise ValueError("task_ids 不能为空")
        return values
    if raw is None or not str(raw).strip():
        raise ValueError("必须提供 task_id 或 task_ids")
    return str(raw)


def _require(arguments: Mapping[str, Any], name: str) -> Any:
    """读取必填 action 参数。"""
    value = arguments.get(name)
    if value is None or value == "" or value == []:
        raise ValueError(f"缺少必填参数: {name}")
    return value


def _tasks_list(client: Any, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """查询并分页返回下载任务。"""
    tasks, error = client.get_torrents(
        ids=arguments.get("task_ids", arguments.get("task_id")),
        status=arguments.get("status"),
        tags=arguments.get("tags"),
    )
    if error:
        raise RuntimeError("下载器任务查询失败")
    return _page(tasks or [], arguments)


def _tags_get(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """按 provider 读取任务标签。"""
    task_id = str(_require(arguments, "task_id"))
    getter = getattr(client, "get_torrent_tags", None)
    if callable(getter):
        return getter(task_id)
    tasks, error = client.get_torrents(ids=task_id)
    if error or not tasks:
        raise RuntimeError("任务标签查询失败")
    task = _jsonable(tasks[0])
    if provider == "qbittorrent":
        tags = task.get("tags") if isinstance(task, dict) else None
        return [item.strip() for item in str(tags or "").split(",") if item.strip()]
    return []


def _tags_set(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """按 provider 设置任务标签。"""
    ids = _task_ids(arguments)
    tags = [str(item) for item in _require(arguments, "tags")]
    if provider == "qbittorrent":
        return client.set_torrents_tag(ids=ids, tags=tags)
    if provider == "transmission":
        return client.set_torrent_tag(ids=ids, tags=tags)
    return client.set_torrents_tag(ids=ids, tags=tags)


def _reannounce(client: Any, provider: str, arguments: Mapping[str, Any]) -> bool:
    """调用 provider SDK 的固定重新汇报动作。"""
    ids = _task_ids(arguments)
    if provider == "qbittorrent":
        client.qbc.torrents_reannounce(torrent_hashes=ids)
    else:
        client.trc.reannounce_torrent(ids=ids)
    return True


def _queue_move(client: Any, provider: str, arguments: Mapping[str, Any]) -> bool:
    """按固定枚举调整任务队列位置。"""
    ids = _task_ids(arguments)
    position = str(_require(arguments, "position")).lower()
    if position not in {"top", "up", "down", "bottom"}:
        raise ValueError("position 仅支持 top、up、down、bottom")
    if provider == "qbittorrent":
        method_name = {
            "top": "torrents_top_priority",
            "up": "torrents_increase_priority",
            "down": "torrents_decrease_priority",
            "bottom": "torrents_bottom_priority",
        }[position]
        method = getattr(client.qbc, method_name)
        method(torrent_hashes=ids)
    else:
        method = getattr(client.trc, f"queue_{position}")
        method(ids=ids)
    return True


def _set_file_selection(client: Any, provider: str, arguments: Mapping[str, Any]) -> bool:
    """按统一 wanted/unwanted 合同设置任务内文件选择。"""
    task_id = str(_require(arguments, "task_id"))
    wanted = [int(item) for item in arguments.get("wanted_file_ids", [])]
    unwanted = [int(item) for item in arguments.get("unwanted_file_ids", [])]
    if not wanted and not unwanted:
        raise ValueError("wanted_file_ids 与 unwanted_file_ids 至少提供一项")
    if set(wanted) & set(unwanted):
        raise ValueError("同一文件不能同时设为 wanted 和 unwanted")
    if provider == "transmission":
        wanted_ok = not wanted or client.set_files(task_id, wanted)
        unwanted_ok = not unwanted or client.set_unwanted_files(task_id, unwanted)
        return bool(wanted_ok and unwanted_ok)
    wanted_ok = not wanted or client.set_files(
        torrent_hash=task_id,
        file_ids=wanted,
        priority=1,
    )
    unwanted_ok = not unwanted or client.set_files(
        torrent_hash=task_id,
        file_ids=unwanted,
        priority=0,
    )
    return bool(wanted_ok and unwanted_ok)


def _add_direct(client: Any, provider: str, arguments: Mapping[str, Any]) -> Any:
    """将显式内容直接提交到下载器，不接收 Cookie 或连接参数。"""
    content = _require(arguments, "content")
    torrent_file = bool(arguments.get("torrent_file"))
    if torrent_file:
        content = Path(str(content)).expanduser().resolve().read_bytes()
    tags = arguments.get("tags")
    common = {
        "content": content,
        "is_paused": bool(arguments.get("paused", False)),
        "download_dir": arguments.get("download_dir"),
    }
    if provider == "qbittorrent":
        return client.add_torrent(
            **common,
            tag=tags,
            category=arguments.get("category"),
        )
    if provider == "transmission":
        return client.add_torrent(**common, labels=tags)
    return client.add_torrent(**common, tags=tags)


def _dispatch(client: Any, provider: str, action: str, arguments: Mapping[str, Any]) -> Any:
    """执行 action 注册表中允许的下载器调用。"""
    if action == "tasks.list":
        return _tasks_list(client, arguments)
    if action == "tasks.files":
        return _page(client.get_files(str(_require(arguments, "task_id"))) or [], arguments)
    if action == "tasks.files.selection.set":
        return _set_file_selection(client, provider, arguments)
    if action == "tasks.trackers":
        return client.get_trackers(str(_require(arguments, "task_id")))
    if action == "tasks.tags.get":
        return _tags_get(client, provider, arguments)
    if action == "tasks.tags.set":
        return _tags_set(client, provider, arguments)
    if action == "tasks.peers":
        return client.qbc.sync_torrent_peers(str(_require(arguments, "task_id")))
    if action == "tasks.start":
        return client.start_torrents(_task_ids(arguments))
    if action == "tasks.stop":
        return client.stop_torrents(_task_ids(arguments))
    if action == "tasks.delete":
        return client.delete_torrents(bool(arguments.get("delete_files", False)), _task_ids(arguments))
    if action == "tasks.recheck":
        return client.recheck_torrents(_task_ids(arguments))
    if action == "tasks.reannounce":
        return _reannounce(client, provider, arguments)
    if action == "tasks.queue.move":
        return _queue_move(client, provider, arguments)
    if action == "tasks.force_start.set":
        client.qbc.torrents_set_force_start(
            enable=bool(_require(arguments, "enabled")),
            torrent_hashes=_task_ids(arguments),
        )
        return True
    if action == "tasks.properties.set":
        properties = {
            "hash_string": str(_require(arguments, "task_id")),
            "upload_limit": arguments.get("upload_limit"),
            "download_limit": arguments.get("download_limit"),
        }
        if provider != "rtorrent":
            properties.update(
                {
                    "ratio_limit": arguments.get("ratio_limit"),
                    "seeding_time_limit": arguments.get("seeding_time_limit"),
                }
            )
        return client.change_torrent(**properties)
    if action == "tasks.location.set":
        return client.set_torrent_location(
            str(_require(arguments, "task_id")),
            str(_require(arguments, "location")),
        )
    if action == "tasks.category.set":
        return client.set_torrent_category(
            str(_require(arguments, "task_id")),
            str(_require(arguments, "category")),
        )
    if action == "tasks.trackers.update":
        return client.update_tracker(
            str(_require(arguments, "task_id")),
            list(_require(arguments, "trackers")),
        )
    if action == "tasks.add.direct":
        return _add_direct(client, provider, arguments)
    if action == "session.stats":
        return client.transfer_info()
    if action == "session.speed_limits.get":
        limits = client.get_speed_limit()
        return {"download_limit": limits[0], "upload_limit": limits[1]} if limits else None
    if action == "session.speed_limits.set":
        return client.set_speed_limit(
            download_limit=arguments.get("download_limit"),
            upload_limit=arguments.get("upload_limit"),
        )
    if action == "session.details":
        return client.get_session()
    if action == "session.content_layout":
        return client.get_content_layout()
    raise ValueError(f"未知 downloader action: {action}")


def list_instances() -> dict[str, Any]:
    """列出下载器实例的非敏感投影。"""
    instances = [
        {
            "name": config.name,
            "provider": config.type,
            "enabled": bool(config.enabled),
            "default": bool(config.default),
            "path_mapping_count": len(config.path_mapping or []),
        }
        for config in _load_configs()
    ]
    return {"success": True, "instances": instances}


def list_capabilities(client_name: Optional[str]) -> dict[str, Any]:
    """返回全部或指定实例支持的 action 清单。"""
    provider = None
    if client_name:
        provider = str(_select_config(client_name).type or "").lower()
    actions = [spec.to_dict(name) for name, spec in ACTIONS.items() if provider is None or provider in spec.providers]
    return {
        "success": True,
        "client": client_name,
        "provider": provider,
        "actions": actions,
    }


def call_action(client_name: Optional[str], action: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    """校验 action/provider 合同并执行一次受控调用。"""
    spec = ACTIONS.get(action)
    if spec is None:
        raise ValueError(f"未知 downloader action: {action}")
    config = _select_config(client_name)
    provider = str(config.type or "").lower()
    if provider not in spec.providers:
        raise ValueError(f"{provider} 不支持 action: {action}")
    for name in spec.required:
        if name == "task_ids":
            _task_ids(arguments)
        else:
            _require(arguments, name)
    result = _dispatch(_build_client(config), provider, action, arguments)
    if spec.effect != "safe_read" and result is False:
        raise RuntimeError("下载器 action 返回失败")
    return {
        "success": True,
        "client": config.name,
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
    parser = argparse.ArgumentParser(description="MoviePilot downloader API helper")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("instances", help="list configured instances")
    capabilities = subparsers.add_parser("capabilities", help="list allowed actions")
    capabilities.add_argument("--client")
    call = subparsers.add_parser("call", help="call one allowed action")
    call.add_argument("--client")
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
            payload = list_capabilities(args.client)
        elif args.command == "call":
            payload = call_action(args.client, args.action, _parse_arguments(args.arguments))
        else:
            parser.print_help()
            return 0
    except Exception as error:  # noqa: BLE001
        payload = {
            "success": False,
            "error_type": type(error).__name__,
            "message": str(error) if isinstance(error, ValueError) else "下载器调用失败",
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
