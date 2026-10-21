"""
向 HTTP/MCP 管理入口暴露自描述的服务操作工具。
"""

from __future__ import annotations

import json
import runpy
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, ClassVar, Dict, Optional, Type, cast

from pydantic import BaseModel, ConfigDict, Field

from app.agent.shell import agent_text_subprocess_kwargs
from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.runtime.settings import get_runtime_setting
from app.schemas.mediaserver import MediaServerQueryOptions


class DownloaderOperationInput(BaseModel):
    """
    下载器操作工具的运行时输入模型。
    """

    client: Optional[str] = Field(
        default=None,
        description="已配置下载器实例名称；省略时使用默认或唯一启用实例",
    )
    action: str = Field(
        description="精确下载器动作，须从 MCP 输入 schema 选择对应分支",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="所选下载器动作声明的结构化参数",
    )

    model_config = ConfigDict(extra="forbid")


class MediaServerOperationInput(BaseModel):
    """
    媒体服务器操作工具的运行时输入模型。
    """

    server: Optional[str] = Field(
        default=None,
        description="已配置媒体服务器实例名称；省略时使用唯一启用实例",
    )
    action: str = Field(
        description="精确媒体服务器动作，须从 MCP 输入 schema 选择对应分支",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="所选媒体服务器动作声明的结构化参数",
    )

    model_config = ConfigDict(extra="forbid")


class DatabaseOperationInput(BaseModel):
    """
    数据库操作工具的运行时输入模型。
    """

    action: str = Field(
        description="精确数据库动作，须从 MCP 输入 schema 选择对应分支",
    )
    arguments: Dict[str, Any] = Field(
        default_factory=dict,
        description="所选数据库动作声明的结构化参数",
    )

    model_config = ConfigDict(extra="forbid")


@lru_cache(maxsize=3)
def _load_action_contracts(script_path: str) -> dict[str, Any]:
    """
    从固定 Skill 脚本加载无配置副作用的 action 注册表。
    """
    namespace = runpy.run_path(script_path)
    actions = namespace.get("ACTIONS")
    if not isinstance(actions, dict):
        raise RuntimeError("服务操作脚本缺少 ACTIONS 合同")
    return actions


def _compact_type_schema(declared_type: str) -> dict[str, Any]:
    """
    把 Skill 的紧凑参数类型转换为标准 JSON Schema。
    """
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
    """
    构建一个包含类型、说明、默认值和枚举的参数 schema。
    """
    schema = _compact_type_schema(str(argument["type"]))
    name = str(argument["name"])
    if name == "query" and argument["type"] == "object":
        schema = _media_query_schema()
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


def _media_query_schema() -> dict[str, Any]:
    """
    从统一投影模型生成内联参数结构，避免工具另维护一份字段列表。

    :return: 不依赖外层 definitions 路径的投影 JSON Schema
    """
    schema: dict[str, Any] = MediaServerQueryOptions.model_json_schema()
    definitions = schema.pop("$defs")
    for alternative in schema["properties"]["fields"]["anyOf"]:
        if "items" in alternative:
            alternative["items"] = definitions["MediaServerField"]
    return schema


def _metadata_refresh_items_schema() -> dict[str, Any]:
    """
    返回 metadata.refresh.items 的完整嵌套条目合同。
    """
    return {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "用于媒体识别的标题。"},
                "year": {
                    "anyOf": [{"type": "string"}, {"type": "integer"}],
                    "description": "发行或首映年份，用于区分同名媒体。",
                },
                "type": {
                    "type": "string",
                    "enum": ["电影", "电视剧", "音乐"],
                    "description": (
                        "MoviePilot 媒体类型的精确值：电影、电视剧或音乐。"
                    ),
                },
                "category": {"type": "string", "description": "可选的 MoviePilot 媒体库分类。"},
                "target_path": {
                    "type": "string",
                    "description": "需要刷新元数据的媒体文件或目录路径。",
                },
            },
            "additionalProperties": False,
        },
    }


def _add_action_argument_rules(action: str, schema: dict[str, Any]) -> None:
    """
    把跨字段约束补充为机器可校验的 JSON Schema。
    """
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
    """
    把指定 action 的脚本参数契约转换为 MCP 参数结构。
    """
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
    """
    构建按 action 分支且可由外部 MCP Client 直接发现的输入合同。
    """
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
                    f"{contract['description']} 操作影响：{contract['effect']}。 "
                    f"支持的服务：{', '.join(contract['providers'])}。"
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
            "description": "arguments 必须匹配 action 选中的 oneOf 分支。",
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
    """
    从可能带启动日志的 stdout 中提取最后一个完整 JSON 对象。
    """
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
    selector_flag: Optional[str],
    selector_value: Optional[str],
    action: str,
    arguments: Dict[str, Any],
) -> dict[str, Any]:
    """
    不经 shell 调用固定 Skill 脚本，并只返回其结构化 JSON envelope。
    """
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
    if selector_value and selector_flag:
        command.extend([selector_flag, selector_value])
    completed = subprocess.run(
        command,
        cwd=root_path,
        capture_output=True,
        text=True,
        check=False,
        **agent_text_subprocess_kwargs(),
    )
    return _parse_script_payload(completed.stdout)


class _ServiceOperationTool(MoviePilotTool):
    """
    固定 Skill 脚本的 MCP-only 安全包装基类。

    :cvar _relative_script: 相对于项目根目录的固定 Skill 脚本路径
    :cvar _selector_name: 输入模型中的服务选择字段；不选择实例时为 None
    :cvar _selector_flag: 传给固定脚本的实例选择选项；不选择实例时为 None
    :cvar _blocking_bucket: 阻塞执行隔离桶的名称，用于限制同类操作并发
    """

    require_admin: bool = Field(
        default=True,
        description="是否仅允许系统管理员调用",
    )
    tags: list[str] = Field(
        default=[ToolTag.Admin],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    _relative_script: ClassVar[str]
    _selector_name: ClassVar[Optional[str]]
    _selector_flag: ClassVar[Optional[str]]
    _blocking_bucket: ClassVar[str]

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """
        返回保留 action 条件分支的完整 MCP JSON Schema。
        """
        root_path = Path(get_runtime_setting("ROOT_PATH"))
        actions = _load_action_contracts(str(root_path / self._relative_script))
        selector_description = None
        if self._selector_name:
            selector_description = cast(type[BaseModel], self.get_input_schema()).model_json_schema()["properties"][self._selector_name][
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
        """
        在线程池中运行固定脚本并序列化安全结果。
        """
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
    """
    外部 HTTP/MCP 调用下载器原生能力的结构化工具。

    :cvar _relative_script: 相对于项目根目录的固定 Skill 脚本路径
    :cvar _selector_name: 输入模型中的服务选择字段；不选择实例时为 None
    :cvar _selector_flag: 传给固定脚本的实例选择选项；不选择实例时为 None
    :cvar _blocking_bucket: 阻塞执行隔离桶的名称，用于限制同类操作并发
    """

    name: str = Field(
        default="downloader_operation",
        description="供模型调用的稳定工具名称",
    )
    description: str = Field(
        default=(
            "操作已配置的 qBittorrent、Transmission 或 rTorrent 实例。"
            "输入 schema 为每个动作提供精确分支，包含支持的服务、操作影响、必填字段、类型、默认值、枚举和跨字段约束。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    tags: list[str] = Field(
        default=[ToolTag.Download],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    args_schema: Type[BaseModel] = Field(
        default=DownloaderOperationInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    _relative_script: ClassVar[str] = "skills/downloader-operation/scripts/mp-downloader.py"
    _selector_name: ClassVar[Optional[str]] = "client"
    _selector_flag: ClassVar[Optional[str]] = "--client"
    _blocking_bucket: ClassVar[str] = "downloader"

    # 具体参数由 args_schema 验证，执行入口保留与工具基类的动态分发兼容。
    async def run(  # type: ignore[override]
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        client: Optional[str] = None,
    ) -> str:
        """
        执行一次自描述的下载器操作。
        """
        return await self._run_operation(
            selector_value=client,
            action=action,
            arguments=arguments or {},
        )


class MediaServerOperationTool(_ServiceOperationTool):
    """
    外部 HTTP/MCP 调用媒体服务器原生能力的结构化工具。

    :cvar _relative_script: 相对于项目根目录的固定 Skill 脚本路径
    :cvar _selector_name: 输入模型中的服务选择字段；不选择实例时为 None
    :cvar _selector_flag: 传给固定脚本的实例选择选项；不选择实例时为 None
    :cvar _blocking_bucket: 阻塞执行隔离桶的名称，用于限制同类操作并发
    """

    name: str = Field(
        default="mediaserver_operation",
        description="供模型调用的稳定工具名称",
    )
    description: str = Field(
        default=(
            "操作已配置的 Emby、Jellyfin、Plex、ZSpace、UGREEN、TrimeMedia、Navidrome 或 Vyo 服务。"
            "输入 schema 为每个动作提供精确分支，包含支持的服务、操作影响、必填字段、类型、默认值、枚举、嵌套字段和跨字段约束。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    tags: list[str] = Field(
        default=[ToolTag.Media],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    args_schema: Type[BaseModel] = Field(
        default=MediaServerOperationInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    _relative_script: ClassVar[str] = "skills/mediaserver-operation/scripts/mp-mediaserver.py"
    _selector_name: ClassVar[Optional[str]] = "server"
    _selector_flag: ClassVar[Optional[str]] = "--server"
    _blocking_bucket: ClassVar[str] = "mediaserver"

    # 具体参数由 args_schema 验证，执行入口保留与工具基类的动态分发兼容。
    async def run(  # type: ignore[override]
        self,
        action: str,
        arguments: Optional[Dict[str, Any]] = None,
        server: Optional[str] = None,
    ) -> str:
        """
        执行一次自描述的媒体服务器操作。
        """
        return await self._run_operation(
            selector_value=server,
            action=action,
            arguments=arguments or {},
        )


def _run_database_script(arguments: Dict[str, Any], *, root_path: Path) -> dict[str, Any]:
    """
    不经 shell 调用数据库 Skill 脚本，并返回结构化结果。
    """
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
        **agent_text_subprocess_kwargs(),
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "数据库脚本执行失败"
        return {"success": False, "error": "database_operation_failed", "message": error_text}
    return _parse_script_payload(completed.stdout)


class DatabaseOperationTool(_ServiceOperationTool):
    """
    外部 HTTP/MCP 调用 MoviePilot 数据库 Skill 的结构化工具。

    :cvar _relative_script: 相对于项目根目录的固定 Skill 脚本路径
    :cvar _selector_name: 输入模型中的服务选择字段；不选择实例时为 None
    :cvar _selector_flag: 传给固定脚本的实例选择选项；不选择实例时为 None
    :cvar _blocking_bucket: 阻塞执行隔离桶的名称，用于限制同类操作并发
    """

    name: str = Field(
        default="database_operation",
        description="供模型调用的稳定工具名称",
    )
    description: str = Field(
        default=(
            "检查或显式修改已配置的 MoviePilot 数据库。"
            "输入 schema 提供 tables、schema、query 和 write 的精确分支，包含字段类型、默认值、互斥的 SQL 来源及安全规则。"
        ),
        description="供模型理解工具用途、输入约束和调用边界的说明",
    )
    tags: list[str] = Field(
        default=[ToolTag.Admin],
        description="工具的能力和权限分类标签，初始化时会统一归一化",
    )
    args_schema: Type[BaseModel] = Field(
        default=DatabaseOperationInput,
        description="工具输入参数的验证模型或 JSON Schema",
    )
    _relative_script: ClassVar[str] = "skills/database-operation/scripts/mp-db.py"
    _selector_name: ClassVar[Optional[str]] = None
    _selector_flag: ClassVar[Optional[str]] = None
    _blocking_bucket: ClassVar[str] = "db"

    def get_mcp_input_schema(self) -> dict[str, Any]:
        """
        返回数据库 action 的完整 MCP JSON Schema。
        """
        root_path = Path(get_runtime_setting("ROOT_PATH"))
        actions = _load_action_contracts(str(root_path / self._relative_script))
        return _build_mcp_input_schema(
            actions=actions,
            selector_name=None,
            selector_description=None,
            title=self.name,
        )

    # 具体参数由 args_schema 验证，执行入口保留与工具基类的动态分发兼容。
    async def run(  # type: ignore[override]
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
