"""向 HTTP/MCP 管理入口暴露自描述的服务操作工具。"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, Type

from pydantic import BaseModel, ConfigDict, Field

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.settings import get_runtime_setting


class DownloaderOperationInput(BaseModel):
    """下载器操作工具的运行时输入模型。"""

    client: Optional[str] = Field(
        default=None,
        description="Configured downloader instance name; omit it to use the default or only enabled instance.",
    )
    action: str = Field(
        description="Exact downloader action. Select one action branch from the MCP input schema.",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured arguments declared by the selected downloader action.",
    )

    model_config = ConfigDict(extra="forbid")


class MediaServerOperationInput(BaseModel):
    """媒体服务器操作工具的运行时输入模型。"""

    server: Optional[str] = Field(
        default=None,
        description="Configured media-server instance name; omit it to use the only enabled instance.",
    )
    action: str = Field(
        description="Exact media-server action. Select one action branch from the MCP input schema.",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured arguments declared by the selected media-server action.",
    )

    model_config = ConfigDict(extra="forbid")


class DatabaseOperationInput(BaseModel):
    """数据库操作工具的运行时输入模型。"""

    action: str = Field(
        description="Exact database action. Select one action branch from the MCP input schema.",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="Structured arguments declared by the selected database action.",
    )

    model_config = ConfigDict(extra="forbid")


@lru_cache(maxsize=3)
def _load_action_contracts(script_path: str) -> dict[str, Any]:
    """从固定 Skill 脚本加载无配置副作用的 action 注册表。"""
    namespace = runpy.run_path(script_path)
    actions = namespace.get("ACTIONS")
    if not isinstance(actions, dict):
        raise RuntimeError("服务操作脚本缺少 ACTIONS 合同")
    return actions


def _compact_type_schema(declared_type: str) -> dict[str, Any]:
    """把 Skill 的紧凑参数类型转换为标准 JSON Schema。"""
    variants: list[dict[str, Any]] = []
    for candidate in declared_type.split("|"):
        if candidate.endswith("[]"):
            variants.append(
                {
                    "type": "array",
                    "items": _compact_type_schema(candidate[:-2]),
                }
            )
        else:
            variants.append({"type": candidate})
    return variants[0] if len(variants) == 1 else {"anyOf": variants}


def _argument_schema(argument: dict[str, Any]) -> dict[str, Any]:
    """构建一个包含类型、说明、默认值和枚举的参数 schema。"""
    schema = _compact_type_schema(str(argument["type"]))
    name = str(argument["name"])
    schema["description"] = str(argument.get("description") or "")
    if "default" in argument:
        schema["default"] = argument["default"]
    if argument.get("enum"):
        schema["enum"] = list(argument["enum"])
    if name == "offset":
        schema["minimum"] = 0
    if name == "limit":
        schema.update({"minimum": 1, "maximum": 200})
    if name in {"upload_limit", "download_limit", "ratio_limit", "seeding_time_limit"}:
        schema["minimum"] = 0
    if name in {
        "task_id",
        "item_id",
        "content",
        "location",
        "category",
    }:
        schema["minLength"] = 1
    if name in {
        "task_ids",
        "wanted_file_ids",
        "unwanted_file_ids",
        "tags",
        "trackers",
        "items",
    }:
        schema["minItems"] = 1
    return schema


def _metadata_refresh_items_schema() -> dict[str, Any]:
    """返回 metadata.refresh.items 的完整嵌套条目合同。"""
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Media title used for recognition."},
                "year": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                    "description": "Release or premiere year used to disambiguate the title.",
                },
                "type": {
                    "type": "string",
                    "enum": ["电影", "电视剧", "音乐"],
                    "description": (
                        "Exact MoviePilot media-type literal: 电影 (movie), 电视剧 (TV), or 音乐 (music)."
                    ),
                },
                "category": {"type": "string", "description": "Optional MoviePilot library category."},
                "target_path": {
                    "type": "string",
                    "description": "Media file or directory path whose metadata should be refreshed.",
                },
            },
            "additionalProperties": False,
        },
    }


def _add_action_argument_rules(action: str, schema: dict[str, Any]) -> None:
    """把跨字段约束补充为机器可校验的 JSON Schema。"""
    task_selector_actions = {
        "tasks.start",
        "tasks.stop",
        "tasks.delete",
        "tasks.recheck",
        "tasks.reannounce",
        "tasks.queue.move",
        "tasks.force_start.set",
        "tasks.tags.set",
    }
    if action in task_selector_actions:
        schema["oneOf"] = [
            {"required": ["task_id"], "not": {"required": ["task_ids"]}},
            {"required": ["task_ids"], "not": {"required": ["task_id"]}},
        ]
    if action == "tasks.files.selection.set":
        schema["anyOf"] = [
            {"required": ["wanted_file_ids"]},
            {"required": ["unwanted_file_ids"]},
        ]
    if action == "tasks.properties.set":
        schema["anyOf"] = [
            {"required": [name]}
            for name in ("upload_limit", "download_limit", "ratio_limit", "seeding_time_limit")
        ]
    if action == "session.speed_limits.set":
        schema["anyOf"] = [
            {"required": ["download_limit"]},
            {"required": ["upload_limit"]},
        ]
    if action == "items.music.search":
        schema["anyOf"] = [
            {"required": ["title"]},
            {"required": ["artist"]},
            {"required": ["album"]},
        ]
    if action == "items.season_episodes":
        schema["anyOf"] = [{"required": ["item_id"]}, {"required": ["title"]}]
    if action in {"query", "write"}:
        schema["oneOf"] = [
            {"required": ["sql"], "not": {"required": ["file"]}},
            {"required": ["file"], "not": {"required": ["sql"]}},
        ]


def _build_arguments_schema(action: str, spec: Any) -> dict[str, Any]:
    """把一个 action 的脚本合同转换为 MCP arguments schema。"""
    contract = spec.to_dict(action)
    properties = {
        argument["name"]: _argument_schema(argument)
        for argument in contract["arguments"]
    }
    if action == "metadata.refresh":
        properties["items"] = _metadata_refresh_items_schema()
        properties["items"]["description"] = contract["arguments"][0]["description"]
    schema: dict[str, Any] = {
        "type": "object",
        "description": " ".join(contract.get("argument_rules") or []),
        "properties": properties,
        "required": list(contract.get("required_arguments") or []),
        "additionalProperties": False,
    }
    _add_action_argument_rules(action, schema)
    return schema


def _build_mcp_input_schema(
    *,
    actions: dict[str, Any],
    selector_name: Optional[str],
    selector_description: Optional[str],
    title: str,
) -> dict[str, Any]:
    """构建按 action 分支且可由外部 MCP Client 直接发现的输入合同。"""
    action_names = sorted(actions)
    branches = []
    for action in action_names:
        contract = actions[action].to_dict(action)
        properties = {
            "action": {"type": "string", "const": action},
            "arguments": _build_arguments_schema(action, actions[action]),
        }
        if selector_name:
            properties[selector_name] = {
                "type": "string",
                "description": selector_description or "",
            }
        branches.append(
            {
                "type": "object",
                "title": action,
                "description": (
                    f"{contract['description']} Effect: {contract['effect']}. "
                    f"Providers: {', '.join(contract['providers'])}."
                ),
                "properties": properties,
                "required": ["action", "arguments"],
                "additionalProperties": False,
            }
        )
    properties = {
        "action": {"type": "string", "enum": action_names},
        "arguments": {
            "type": "object",
            "description": "Arguments must match the oneOf branch selected by action.",
        },
    }
    if selector_name:
        properties[selector_name] = {
            "type": "string",
            "description": selector_description or "",
        }
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": title,
        "type": "object",
        "properties": properties,
        "required": ["action", "arguments"],
        "oneOf": branches,
    }


def _parse_script_payload(stdout: str) -> dict[str, Any]:
    """从可能带启动日志的 stdout 中提取最后一个完整 JSON 对象。"""
    decoder = json.JSONDecoder()
    for index in range(len(stdout) - 1, -1, -1):
        if stdout[index] != "{":
            continue
        try:
            payload, end = decoder.raw_decode(stdout[index:])
        except json.JSONDecodeError:
            continue
        if not stdout[index + end :].strip() and isinstance(payload, dict):
            return payload
    raise RuntimeError("服务操作脚本未返回有效 JSON")


def _run_service_script(
    *,
    relative_script: str,
    selector_flag: str,
    selector_value: Optional[str],
    action: str,
    arguments: Dict[str, Any],
) -> dict[str, Any]:
    """不经 shell 调用固定 Skill 脚本，并只返回其结构化 JSON envelope。"""
    root_path = Path(get_runtime_setting("ROOT_PATH"))
    script_path = root_path / relative_script
    command = [
        sys.executable,
        str(script_path),
        "call",
        "--action",
        action,
        "--arguments",
        json.dumps(arguments, ensure_ascii=False, separators=(",", ":")),
    ]
    if selector_value:
        command.extend([selector_flag, selector_value])
    completed = subprocess.run(
        command,
        cwd=root_path,
        capture_output=True,
        text=True,
        check=False,
    )
    return _parse_script_payload(completed.stdout)


class _ServiceOperationTool(MoviePilotTool):
    """固定 Skill 脚本的 MCP-only 安全包装基类。"""

    require_admin: bool = True
    tags: list[str] = [ToolTag.Admin]
    _relative_script: ClassVar[str]
    _selector_name: ClassVar[Optional[str]]
    _selector_flag: ClassVar[Optional[str]]
    _blocking_bucket: ClassVar[str]

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """返回保留 action 条件分支的完整 MCP JSON Schema。"""
        root_path = Path(get_runtime_setting("ROOT_PATH"))
        actions = _load_action_contracts(str(root_path / self._relative_script))
        selector_description = None
        if self._selector_name:
            selector_description = self.args_schema.model_json_schema()["properties"][self._selector_name][
                "description"
            ]
        return _build_mcp_input_schema(
            actions=actions,
            selector_name=self._selector_name,
            selector_description=selector_description,
            title=self.name,
        )

    async def _run_operation(
        self,
        *,
        selector_value: Optional[str],
        action: str,
        arguments: Dict[str, Any],
    ) -> str:
        """在线程池中运行固定脚本并序列化安全结果。"""
        payload = await self.run_blocking(
            self._blocking_bucket,
            _run_service_script,
            relative_script=self._relative_script,
            selector_flag=self._selector_flag,
            selector_value=selector_value,
            action=action,
            arguments=arguments,
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)


class DownloaderOperationTool(_ServiceOperationTool):
    """外部 HTTP/MCP 调用下载器原生能力的结构化工具。"""

    name: str = "downloader_operation"
    description: str = (
        "Operate a configured qBittorrent, Transmission, or rTorrent instance. "
        "The input schema contains one exact branch per action, including providers, "
        "effects, required fields, types, defaults, enums, and cross-field constraints."
    )
    tags: list[str] = [ToolTag.Download]
    args_schema: Type[BaseModel] = DownloaderOperationInput
    _relative_script = "skills/downloader-operation/scripts/mp-downloader.py"
    _selector_name = "client"
    _selector_flag = "--client"
    _blocking_bucket = "downloader"

    async def run(
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        client: Optional[str] = None,
    ) -> str:
        """执行一次自描述的下载器操作。"""
        return await self._run_operation(
            selector_value=client,
            action=action,
            arguments=arguments or {},
        )


class MediaServerOperationTool(_ServiceOperationTool):
    """外部 HTTP/MCP 调用媒体服务器原生能力的结构化工具。"""

    name: str = "mediaserver_operation"
    description: str = (
        "Operate a configured Emby, Jellyfin, Plex, ZSpace, UGREEN, TrimeMedia, "
        "or Navidrome server. The input schema contains one exact branch per action, "
        "including providers, effects, required fields, types, defaults, enums, nested "
        "item fields, and cross-field constraints."
    )
    tags: list[str] = [ToolTag.Media]
    args_schema: Type[BaseModel] = MediaServerOperationInput
    _relative_script = "skills/mediaserver-operation/scripts/mp-mediaserver.py"
    _selector_name = "server"
    _selector_flag = "--server"
    _blocking_bucket = "mediaserver"

    async def run(
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        server: Optional[str] = None,
    ) -> str:
        """执行一次自描述的媒体服务器操作。"""
        return await self._run_operation(
            selector_value=server,
            action=action,
            arguments=arguments or {},
        )


def _run_database_script(arguments: Dict[str, Any], *, root_path: Path) -> dict[str, Any]:
    """不经 shell 调用数据库 Skill 脚本，并返回结构化结果。"""
    script_path = root_path / "skills/database-operation/scripts/mp-db.py"
    action = str(arguments.get("action") or "")
    action_arguments = arguments.get("arguments") or {}
    if not isinstance(action_arguments, dict):
        raise ValueError("数据库 action arguments 必须是对象")

    contracts = _load_action_contracts(str(script_path))
    contract = contracts.get(action)
    if contract is None:
        raise ValueError(f"未知数据库 action: {action}")
    contract_data = contract.to_dict(action)
    declared_arguments = {item["name"]: item for item in contract_data["arguments"]}
    unknown_arguments = sorted(set(action_arguments) - set(declared_arguments))
    if unknown_arguments:
        raise ValueError(f"数据库 action 存在未知参数: {', '.join(unknown_arguments)}")
    missing_arguments = [
        name
        for name in contract_data["required_arguments"]
        if name not in action_arguments
    ]
    if missing_arguments:
        raise ValueError(f"数据库 action 缺少必填参数: {', '.join(missing_arguments)}")
    if action in {"query", "write"}:
        has_sql = bool(action_arguments.get("sql"))
        has_file = bool(action_arguments.get("file"))
        if has_sql == has_file:
            raise ValueError("数据库 query/write 必须在 sql 与 file 中二选一")
        for field_name in ("sql", "file"):
            if field_name in action_arguments and (
                not isinstance(action_arguments[field_name], str)
                or not action_arguments[field_name].strip()
            ):
                raise ValueError(f"数据库参数 {field_name} 必须是非空 string")
    if action == "schema" and (
        not isinstance(action_arguments.get("table_name"), str)
        or not action_arguments["table_name"].strip()
    ):
        raise ValueError("数据库参数 table_name 必须是非空 string")
    if action == "query":
        limit = action_arguments.get("limit", 100)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("数据库参数 limit 必须是 1 到 200 的 integer")
        write_flag = action_arguments.get("write", False)
        if not isinstance(write_flag, bool):
            raise ValueError("数据库参数 write 必须是 boolean")

    command = [sys.executable, str(script_path), action]
    if action == "schema":
        command.append(str(action_arguments.get("table_name") or ""))
    elif action in {"query", "write"}:
        sql = action_arguments.get("sql")
        sql_file = action_arguments.get("file")
        if sql:
            command.append(str(sql))
        if sql_file:
            command.extend(["--file", str(sql_file)])
        if action == "query":
            command.extend(["--limit", str(action_arguments.get("limit", 100))])
            if action_arguments.get("write") is True:
                command.append("--write")

    completed = subprocess.run(
        command,
        cwd=root_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "数据库脚本执行失败"
        return {"success": False, "error": "database_operation_failed", "message": error_text}
    return _parse_script_payload(completed.stdout)


class DatabaseOperationTool(_ServiceOperationTool):
    """外部 HTTP/MCP 调用 MoviePilot 数据库 Skill 的结构化工具。"""

    name: str = "database_operation"
    description: str = (
        "Inspect or explicitly modify the configured MoviePilot database. The input schema "
        "contains exact branches for tables, schema, query, and write, including field types, "
        "defaults, mutually exclusive SQL sources, and safety rules."
    )
    tags: list[str] = [ToolTag.Admin]
    args_schema: Type[BaseModel] = DatabaseOperationInput
    _relative_script = "skills/database-operation/scripts/mp-db.py"
    _selector_name = None
    _selector_flag = None
    _blocking_bucket = "db"

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """返回数据库 action 的完整 MCP JSON Schema。"""
        root_path = Path(get_runtime_setting("ROOT_PATH"))
        actions = _load_action_contracts(str(root_path / self._relative_script))
        return _build_mcp_input_schema(
            actions=actions,
            selector_name=None,
            selector_description=None,
            title=self.name,
        )

    async def run(
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        执行一次数据库 Skill 操作。

        :param action: tables、schema、query 或 write
        :param arguments: 当前 action 的结构化参数
        :return: 结构化数据库结果
        """
        payload = await self.run_blocking(
            self._blocking_bucket,
            _run_database_script,
            root_path=Path(get_runtime_setting("ROOT_PATH")),
            arguments={"action": action, "arguments": arguments or {}},
        )
        return json.dumps(payload, ensure_ascii=False, indent=2)


__all__ = [
    "DownloaderOperationInput",
    "DownloaderOperationTool",
    "DatabaseOperationInput",
    "DatabaseOperationTool",
    "MediaServerOperationInput",
    "MediaServerOperationTool",
]
