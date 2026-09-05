"""插件持久化数据查询、投影与写用例。"""

import json
from collections.abc import Callable
from typing import Any, Optional, Protocol

from app.application.security.secrets import is_secret_setting_key
from app.schemas.common import JsonData


class PluginDataQueryRepository(Protocol):
    """Agent 插件数据查询所需的类型化异步端口。"""

    async def get(self, plugin_id: str, key: str) -> JsonData:
        """读取插件指定键的数据。"""
        ...

    async def list(self, plugin_id: str) -> dict[str, JsonData]:
        """读取插件全部键值并在 Session 内完成投影。"""
        ...


class PluginDataMutationRepository(Protocol):
    """插件数据删除命令所需的无提交仓储端口。"""

    def stage_delete(self, plugin_id: str) -> None:
        """暂存目标插件的全部持久化数据删除。"""
        ...


class UnitOfWork(Protocol):
    """插件数据同步写用例所需的事务端口。"""

    def commit(self) -> None:
        """提交当前逻辑操作。"""
        ...

    def rollback(self) -> None:
        """回滚当前逻辑操作。"""
        ...


class DeletePluginDataCommand:
    """在一个显式事务中删除目标插件的全部持久化数据。"""

    def __init__(
        self,
        repository: PluginDataMutationRepository,
        unit_of_work: UnitOfWork,
    ) -> None:
        """保存无提交仓储和事务所有者。"""
        self._repository = repository
        self._unit_of_work = unit_of_work

    def execute(self, plugin_id: str) -> None:
        """暂存并提交删除；任一步失败时回滚并传播原异常。"""
        try:
            self._repository.stage_delete(plugin_id)
            self._unit_of_work.commit()
        except Exception:
            self._unit_of_work.rollback()
            raise


DEFAULT_PLUGIN_DATA_PREVIEW_CHARS = 12_000
MAX_PLUGIN_DATA_PREVIEW_CHARS = 50_000
PLUGIN_DATA_KEY_PREVIEW_LIMIT = 50
PLUGIN_DATA_TRUNCATION_SUFFIX = "\n...(插件数据内容过长，已截断)"


def clamp_preview_chars(max_chars: Optional[int]) -> int:
    """约束插件数据预览长度，避免结果无限膨胀。"""
    if max_chars is None:
        return DEFAULT_PLUGIN_DATA_PREVIEW_CHARS
    return max(512, min(int(max_chars), MAX_PLUGIN_DATA_PREVIEW_CHARS))


def build_preview_payload(value: Any, max_chars: Optional[int]) -> tuple[bool, int, int, str]:
    """稳定序列化插件数据并生成有界预览。"""
    serialized = json.dumps(value, ensure_ascii=False, indent=2, default=str)
    preview_limit = clamp_preview_chars(max_chars)
    if len(serialized) <= preview_limit:
        return False, len(serialized), len(serialized), serialized
    preview = serialized[:preview_limit] + PLUGIN_DATA_TRUNCATION_SUFFIX
    return True, len(serialized), len(preview), preview


class PluginDataQueryService:
    """查询已安装插件数据并对大结果执行稳定裁剪。"""

    def __init__(
        self,
        repository: PluginDataQueryRepository,
        snapshot: Callable[[str], Optional[dict[str, Any]]],
    ) -> None:
        """注入插件数据仓储和安装态快照查询函数。"""
        self._repository = repository
        self._snapshot = snapshot

    async def query(
        self,
        plugin_id: str,
        *,
        key: Optional[str] = None,
        max_chars: Optional[int] = None,
    ) -> dict[str, Any]:
        """读取单键或全部数据，并返回上下文安全的投影。"""
        plugin = self._snapshot(plugin_id)
        if plugin is None:
            raise ValueError(f"插件 {plugin_id} 不存在")
        if key:
            value = await self._repository.get(plugin_id, key)
            if value is None:
                return {
                    **plugin,
                    "key": key,
                    "found": False,
                    "message": f"插件 {plugin_id} 没有数据项 {key}",
                }
            truncated, total_chars, returned_chars, preview = build_preview_payload(value, max_chars)
            result = {
                **plugin,
                "key": key,
                "found": True,
                "truncated": truncated,
                "total_chars": total_chars,
                "returned_chars": returned_chars,
            }
            result["value_preview" if truncated else "value"] = preview if truncated else value
            return result
        data = await self._repository.list(plugin_id)
        keys = list(data)
        result = {
            **plugin,
            "count": len(data),
            "keys": keys[:PLUGIN_DATA_KEY_PREVIEW_LIMIT],
            "keys_truncated": len(keys) > PLUGIN_DATA_KEY_PREVIEW_LIMIT,
        }
        truncated, total_chars, returned_chars, preview = build_preview_payload(data, max_chars)
        result.update(
            {
                "truncated": truncated,
                "total_chars": total_chars,
                "returned_chars": returned_chars,
                "data_preview" if truncated else "data": (preview if truncated else data),
            }
        )
        return result


def plugin_data_value_type(value: JsonData) -> str:
    """把插件 JSON 值映射为不包含内容的稳定类型名称。"""
    if value is None:
        return "null"
    if type(value) is bool:
        return "boolean"
    if type(value) in (int, float):
        return "number"
    if type(value) is str:
        return "string"
    if type(value) is list:
        return "array"
    if type(value) is dict:
        return "object"
    return "unknown"


def plugin_data_serialized_chars(value: JsonData) -> Optional[int]:
    """计算合法 JSON 值的紧凑字符数，异常对象不执行自定义字符串化。"""
    try:
        return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    except TypeError, ValueError:
        return None


class PluginDataSummaryService:
    """构建不包含插件持久化原值的有界诊断摘要。"""

    def __init__(
        self,
        repository: PluginDataQueryRepository,
        snapshot: Callable[[str], Optional[dict[str, Any]]],
    ) -> None:
        """注入插件数据仓储和安装态快照查询函数。"""
        self._repository = repository
        self._snapshot = snapshot

    async def summarize(self, plugin_id: str) -> dict[str, Any]:
        """返回键名、类型、大小和敏感标记，不返回或字符串化数据值。"""
        plugin = self._snapshot(plugin_id)
        if plugin is None:
            raise ValueError(f"插件 {plugin_id} 不存在")

        data = await self._repository.list(plugin_id)
        items = []
        total_chars = 0
        for key, value in list(data.items())[:PLUGIN_DATA_KEY_PREVIEW_LIMIT]:
            serialized_chars = plugin_data_serialized_chars(value)
            if serialized_chars is not None:
                total_chars += serialized_chars
            items.append(
                {
                    "key": str(key),
                    "value_type": plugin_data_value_type(value),
                    "serialized_chars": serialized_chars,
                    "sensitive": is_secret_setting_key(key),
                }
            )

        if len(data) > PLUGIN_DATA_KEY_PREVIEW_LIMIT:
            for value in list(data.values())[PLUGIN_DATA_KEY_PREVIEW_LIMIT:]:
                serialized_chars = plugin_data_serialized_chars(value)
                if serialized_chars is not None:
                    total_chars += serialized_chars

        return {
            **plugin,
            "count": len(data),
            "total_chars": total_chars,
            "keys": items,
            "keys_truncated": len(data) > PLUGIN_DATA_KEY_PREVIEW_LIMIT,
        }
