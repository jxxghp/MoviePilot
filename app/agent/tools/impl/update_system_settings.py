"""统一更新系统设置工具。"""

import copy
import json
from typing import Any, Literal, Optional, Type, Union

from pydantic import BaseModel, Field, PrivateAttr

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._system_setting_utils import (
    SettingSpec,
    get_default_list_match_field,
    is_secret_setting_key,
    redact_secret_value,
    resolve_setting_spec,
    should_redact_setting,
)
from app.runtime.config import settings
from app.runtime.events import eventmanager
from app.runtime.extensions.service_config import service_capability
from app.runtime.extensions.admission.service_config import service_config_write_violation
from app.application.configuration import SystemConfigService
from app.application.service_config import (
    async_write_system_setting,
    read_system_setting,
)
from app.runtime.log import logger
from app.schemas.event import ConfigChangeEventData
from app.schemas.types import EventType

SettingValue = Optional[Union[list, dict, bool, int, float, str]]


class UpdateSystemSettingsInput(BaseModel):
    """更新系统设置工具的输入参数模型。"""

    setting_key: str = Field(
        ...,
        description=(
            "Exact setting key to update. Supports Settings field names, SystemConfigKey values, enum names, and common aliases "
            "such as 'downloaders', 'directories', 'search_sites', 'subscribe_sites', 'site_auth', 'ai_agent', and 'custom_identifiers'."
        ),
    )
    value: SettingValue = Field(
        None,
        description=(
            "The new value or list item payload. For replace: this becomes the entire setting value. For merge_dict: this should be a dict of keys to merge. "
            "For upsert_list_item/remove_list_item: this can be a dict item or a scalar list item."
        ),
    )
    operation: Literal[
        "replace",
        "merge_dict",
        "upsert_list_item",
        "remove_list_item",
    ] = Field(
        "replace",
        description=(
            "Update operation. replace replaces the whole value; merge_dict merges dict keys (optionally with remove_keys); "
            "upsert_list_item inserts or replaces one item inside a list; remove_list_item removes one item from a list."
        ),
    )
    remove_keys: Optional[list[str]] = Field(
        None,
        description="Optional dict keys to delete when operation is merge_dict.",
    )
    match_field: Optional[str] = Field(
        None,
        description=(
            "Optional match field for list item upsert/remove. If omitted, common SystemConfig categories use built-in defaults such as 'name' or 'type'."
        ),
    )
    match_value: SettingValue = Field(
        None,
        description=(
            "Optional explicit value used to locate a list item when operation is upsert_list_item or remove_list_item."
        ),
    )


class UpdateSystemSettingsTool(MoviePilotTool):
    """通过授权配置服务修改可登记系统设置。"""

    name: str = "update_system_settings"
    tags: list[str] = [
        ToolTag.Write,
        ToolTag.System,
        ToolTag.Settings,
        ToolTag.Admin,
    ]
    description: str = (
        "Update system settings across both the basic Settings module and all SystemConfig-backed categories. "
        "Supports full replacement, shallow dict merge, and generic list item upsert/remove so the agent can manage downloaders, media servers, notification channels, storages, directories, search-site ranges, subscribe-site ranges, site auth params, AI agent config, and other system settings through one tool."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = UpdateSystemSettingsInput
    _system_config: Optional[SystemConfigService] = PrivateAttr(default=None)

    def __init__(
        self,
        session_id: str,
        user_id: str,
        *,
        system_config: Optional[SystemConfigService] = None,
        **kwargs,
    ) -> None:
        """注入配置读写服务，并兼容组合根默认装配。"""
        super().__init__(session_id=session_id, user_id=user_id, **kwargs)
        self._system_config = system_config

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据更新参数生成友好的提示消息。"""

        setting_key = kwargs.get("setting_key", "")
        operation = kwargs.get("operation", "replace")
        action_map = {
            "replace": "覆盖系统设置",
            "merge_dict": "合并系统设置",
            "upsert_list_item": "更新列表项",
            "remove_list_item": "移除列表项",
        }
        return f"{action_map.get(operation, '更新系统设置')}: {setting_key}"

    def _load_setting_value(self, spec: SettingSpec):
        """读取指定设置项的当前值。

        显式注入了配置服务时优先使用该服务；否则退回按配置键分流的
        `read_system_setting`，服务实例配置族的事实源才不会被绕过。
        """
        if spec.source == "settings":
            return getattr(settings, spec.key)
        if self._system_config is not None:
            return self._system_config.get(spec.systemconfig_key)
        return read_system_setting(spec.systemconfig_key)

    @staticmethod
    def _normalize_systemconfig_value(value: Any):
        """规范化写入 SystemConfig 的空列表值。"""
        if isinstance(value, list):
            filtered = [item for item in value if item is not None]
            return filtered or None
        return value

    @staticmethod
    def _resolve_list_match(
        spec: SettingSpec,
        operation: str,
        value: Any,
        match_field: Optional[str],
        match_value: Any,
    ) -> tuple[Optional[str], Any]:
        resolved_field = match_field or get_default_list_match_field(spec.key)
        resolved_value = match_value

        if isinstance(value, dict):
            if not resolved_field:
                raise ValueError(
                    f"{operation} 需要提供 match_field，或使用带默认匹配字段的系统配置项"
                )
            if resolved_value is None:
                resolved_value = value.get(resolved_field)
            if resolved_value is None:
                raise ValueError(
                    f"{operation} 缺少匹配值，请在 value.{resolved_field} 或 match_value 中提供"
                )
        else:
            if resolved_value is None:
                resolved_value = value

        return resolved_field, resolved_value

    @classmethod
    def _prepare_next_value(
        cls,
        spec: SettingSpec,
        current_value: Any,
        value: Any,
        operation: str,
        remove_keys: Optional[list[str]] = None,
        match_field: Optional[str] = None,
        match_value: Any = None,
    ) -> Any:
        remove_keys = remove_keys or []
        if operation == "replace":
            return value

        if operation == "merge_dict":
            if remove_keys and not isinstance(remove_keys, list):
                raise ValueError("remove_keys 必须是字符串列表")
            if current_value is not None and not isinstance(current_value, dict):
                raise ValueError("merge_dict 仅支持当前值为 dict 的设置项")
            if value is not None and not isinstance(value, dict):
                raise ValueError("merge_dict 的 value 必须是 dict 或 null")
            next_value = dict(current_value or {})
            if value:
                next_value.update(value)
            for key in remove_keys:
                next_value.pop(key, None)
            return next_value

        if operation not in {"upsert_list_item", "remove_list_item"}:
            raise ValueError(f"不支持的操作: {operation}")

        if current_value is not None and not isinstance(current_value, list):
            raise ValueError(f"{operation} 仅支持当前值为 list 的设置项")

        next_items = list(copy.deepcopy(current_value or []))
        resolved_field, resolved_match_value = cls._resolve_list_match(
            spec, operation, value, match_field, match_value
        )

        if operation == "upsert_list_item":
            if value is None:
                raise ValueError("upsert_list_item 必须提供 value")
            replaced = False
            for index, item in enumerate(next_items):
                if resolved_field:
                    if isinstance(item, dict) and item.get(resolved_field) == resolved_match_value:
                        next_items[index] = value
                        replaced = True
                        break
                elif item == resolved_match_value:
                    next_items[index] = value
                    replaced = True
                    break
            if not replaced:
                next_items.append(value)
            return next_items

        return [
            item
            for item in next_items
            if not (
                isinstance(item, dict)
                and resolved_field
                and item.get(resolved_field) == resolved_match_value
            )
            and not (not resolved_field and item == resolved_match_value)
        ]

    async def run(
        self,
        setting_key: str,
        value: SettingValue = None,
        operation: str = "replace",
        remove_keys: Optional[list[str]] = None,
        match_field: Optional[str] = None,
        match_value: SettingValue = None,
        **kwargs,
    ) -> str:
        logger.info(
            f"执行工具: {self.name}, setting_key={setting_key}, operation={operation}"
        )

        try:
            spec = resolve_setting_spec(setting_key)
            if not spec:
                return json.dumps(
                    {
                        "success": False,
                        "message": f"系统设置项 '{setting_key}' 不存在",
                    },
                    ensure_ascii=False,
                )

            current_value = self._load_setting_value(spec)
            next_value = self._prepare_next_value(
                spec=spec,
                current_value=current_value,
                value=value,
                operation=operation,
                remove_keys=remove_keys,
                match_field=match_field,
                match_value=match_value,
            )

            event_value = next_value
            changed = False
            message = ""
            if spec.source == "settings":
                success, message = settings.update_setting(spec.key, next_value)
                if success is False:
                    return json.dumps(
                        {
                            "success": False,
                            "message": message or f"更新设置 {spec.key} 失败",
                        },
                        ensure_ascii=False,
                    )
                changed = success is True
            else:
                normalized_value = self._normalize_systemconfig_value(next_value)
                # 服务实例配置按声明该类型的扩展给出的契约判定，与设置页写入同一道关卡
                violation = service_config_write_violation(
                    service_capability(spec.key), normalized_value
                )
                if violation:
                    return json.dumps(
                        {"success": False, "message": violation},
                        ensure_ascii=False,
                    )
                event_value = normalized_value
                if self._system_config is not None:
                    changed = await self._system_config.async_set(
                        spec.systemconfig_key,
                        normalized_value,
                    ) is True
                else:
                    changed = await async_write_system_setting(
                        spec.systemconfig_key,
                        normalized_value,
                    )

            if changed:
                await eventmanager.async_send_event(
                    etype=EventType.ConfigChanged,
                    data=ConfigChangeEventData(
                        key=spec.key,
                        value=event_value,
                        change_type="update",
                    ),
                )

            saved_value = self._load_setting_value(spec)
            redact_values = (
                should_redact_setting(spec, saved_value)
                or should_redact_setting(spec, current_value)
            )
            response_previous_value = (
                redact_secret_value(
                    current_value,
                    redact_scalar=is_secret_setting_key(spec.key),
                )
                if redact_values
                else current_value
            )
            response_saved_value = (
                redact_secret_value(
                    saved_value,
                    redact_scalar=is_secret_setting_key(spec.key),
                )
                if redact_values
                else saved_value
            )
            if not changed and not message:
                message = "配置值未发生变化"

            return json.dumps(
                {
                    "success": True,
                    "message": message or f"系统设置 {spec.key} 已更新",
                    "changed": changed,
                    "operation": operation,
                    "setting": {
                        "setting_key": spec.key,
                        "source": spec.source,
                        "group": spec.group,
                        "label": spec.label,
                    },
                    "values_redacted": redact_values,
                    "previous_value": response_previous_value,
                    "saved_value": response_saved_value,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception as e:
            logger.error(f"更新系统设置失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"更新系统设置时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
