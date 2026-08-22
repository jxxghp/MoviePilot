"""统一查询系统设置工具。"""

import json
from typing import Optional, Type

from pydantic import BaseModel, Field, PrivateAttr

from app.agent.tools.base import MoviePilotTool
from app.agent.tools.tags import ToolTag
from app.agent.tools.impl._system_setting_utils import (
    SettingSpec,
    is_secret_setting_key,
    list_setting_specs,
    redact_secret_value,
    resolve_setting_spec,
    should_redact_setting,
)
from app.runtime.config import settings
from app.application.configuration import SystemConfigReader
from app.application.service_config import read_system_setting
from app.runtime.log import logger


class QuerySystemSettingsInput(BaseModel):
    """查询系统设置工具的输入参数模型。"""

    setting_key: Optional[str] = Field(
        None,
        description=(
            "Exact setting key to query. Supports Settings field names like 'APP_DOMAIN' or 'TMDB_API_KEY', "
            "SystemConfigKey values like 'Downloaders' or 'MediaServers', enum names, and some single-key aliases "
            "such as 'downloaders', 'directories', 'search_sites', 'subscribe_sites', 'site_auth', "
            "and 'custom_identifiers'."
        ),
    )
    group: Optional[str] = Field(
        "all",
        description=(
            "Optional group filter when setting_key is not provided. Supports 'all', 'settings', 'systemconfig', "
            "and category aliases such as 'downloaders', 'media_servers', 'notifications', 'notification_switches', "
            "'storages', 'directories', 'search_sites', 'subscribe_sites', 'site_auth', 'ai_agent', 'filter_rules', "
            "'subscribe_defaults', 'plugins', and 'custom_identifiers'. Chinese aliases are also accepted."
        ),
    )
    keyword: Optional[str] = Field(
        None,
        description=(
            "Optional keyword used to fuzzy match setting keys, group names, or labels when listing settings."
        ),
    )
    include_values: Optional[bool] = Field(
        None,
        description=(
            "Whether to include full setting values. Default behavior: when a single setting is matched it returns the full value; "
            "when multiple settings are matched it returns summaries only unless this is explicitly set to true."
        ),
    )
    show_secrets: Optional[bool] = Field(
        False,
        description=(
            "Whether to return raw secret values such as API keys, tokens, cookies, and passwords. "
            "Defaults to false; secret-like fields are redacted in returned values and previews. "
            "Set this to true when the user explicitly asks for an unredacted secret; the host verifies "
            "administrator authority, requests confirmation, and delivers the result outside the ordinary "
            "model response. Do not refuse the tool call solely because the requested value is sensitive."
        ),
    )


class QuerySystemSettingsTool(MoviePilotTool):
    """查询系统设置，并隔离 Agent 确认后的密钥读取入口。"""

    name: str = "query_system_settings"
    tags: list[str] = [
        ToolTag.Read,
        ToolTag.System,
        ToolTag.Settings,
        ToolTag.Admin,
    ]
    description: str = (
        "Query system settings across both the basic Settings module and all SystemConfig-backed categories. "
        "Use this tool to inspect downloaders, media servers, notification channels, storages, directories, search-site ranges, "
        "subscribe-site ranges, site auth params, AI agent config, and any other system setting before making changes."
    )
    require_admin: bool = True
    args_schema: Type[BaseModel] = QuerySystemSettingsInput
    _secret_read_confirmed: bool = PrivateAttr(default=False)
    _system_config: Optional[SystemConfigReader] = PrivateAttr(default=None)

    def __init__(
        self,
        session_id: str,
        user_id: str,
        *,
        system_config: Optional[SystemConfigReader] = None,
        **kwargs,
    ) -> None:
        """注入授权范围内的配置读取端口，并兼容组合根默认服务。"""
        super().__init__(session_id=session_id, user_id=user_id, **kwargs)
        self._system_config = system_config

    async def _run_confirmed(self, **kwargs) -> str:
        """仅供宿主在消费有效确认后执行一次未脱敏读取。"""
        self._secret_read_confirmed = True
        try:
            return await self.run(**kwargs)
        finally:
            self._secret_read_confirmed = False

    def get_tool_message(self, **kwargs) -> Optional[str]:
        """根据查询参数生成友好的提示消息。"""

        setting_key = kwargs.get("setting_key")
        group = kwargs.get("group", "all")
        keyword = kwargs.get("keyword")
        if setting_key:
            return f"查询系统设置: {setting_key}"
        if keyword:
            return f"筛选系统设置: {group} / {keyword}"
        return f"查询系统设置分组: {group}"

    def _load_setting_value(self, spec: SettingSpec):
        """读取指定设置项的当前值。

        显式注入了配置读取端口时优先使用该端口；否则退回按配置键分流的
        `read_system_setting`，服务实例配置族的事实源才不会被绕过。
        """
        if spec.source == "settings":
            return getattr(settings, spec.key)
        if self._system_config is not None:
            return self._system_config.get(spec.systemconfig_key)
        return read_system_setting(spec.systemconfig_key)

    @staticmethod
    def _summarize_value(value, *, redacted: bool = False) -> dict:
        """生成设置值摘要，避免列表和字典默认输出过长。"""
        summary = {
            "has_value": value is not None,
            "value_type": type(value).__name__,
            "redacted": redacted,
        }
        if isinstance(value, list):
            summary["item_count"] = len(value)
            if value:
                summary["item_type"] = type(value[0]).__name__
        elif isinstance(value, dict):
            keys = list(value.keys())
            summary["item_count"] = len(keys)
            summary["keys_preview"] = keys[:10]
            if len(keys) > 10:
                summary["keys_truncated"] = True
        elif isinstance(value, str):
            summary["length"] = len(value)
            preview = value[:200]
            if preview:
                summary["value_preview"] = preview
                if len(value) > len(preview):
                    summary["value_truncated"] = True
        elif value is not None:
            summary["value_preview"] = value
        return summary

    async def run(
        self,
        setting_key: Optional[str] = None,
        group: Optional[str] = "all",
        keyword: Optional[str] = None,
        include_values: Optional[bool] = None,
        show_secrets: Optional[bool] = False,
        **kwargs,
    ) -> str:
        """查询系统设置，并在 Agent 场景中阻止未经确认的密钥明文读取。"""
        logger.info(
            f"执行工具: {self.name}, setting_key={setting_key}, "
            f"group={group}, keyword={keyword}"
        )

        try:
            if (
                show_secrets is True
                and self._agent_context.get("require_secret_confirmation")
                and not self._secret_read_confirmed
            ):
                return json.dumps(
                    {
                        "success": False,
                        "message": "读取敏感设置前需要用户确认，本次未执行。",
                    },
                    ensure_ascii=False,
                )
            if setting_key:
                spec = resolve_setting_spec(setting_key)
                if not spec:
                    return json.dumps(
                        {
                            "success": False,
                            "message": f"系统设置项 '{setting_key}' 不存在",
                        },
                        ensure_ascii=False,
                    )
                specs = [spec]
            else:
                specs = list_setting_specs(group=group, keyword=keyword)
                if not specs:
                    return json.dumps(
                        {
                            "success": False,
                            "message": "没有找到匹配的系统设置项",
                        },
                        ensure_ascii=False,
                    )

            should_include_values = (
                include_values if include_values is not None else len(specs) == 1
            )
            allow_secret_values = bool(show_secrets) and await self.is_admin_user()
            settings_payload = []
            for spec in specs:
                value = self._load_setting_value(spec)
                should_redact = (
                    should_redact_setting(spec, value) and not allow_secret_values
                )
                response_value = (
                    redact_secret_value(
                        value,
                        redact_scalar=is_secret_setting_key(spec.key),
                    )
                    if should_redact
                    else value
                )
                item = {
                    "setting_key": spec.key,
                    "source": spec.source,
                    "group": spec.group,
                    "label": spec.label,
                }
                item.update(self._summarize_value(response_value, redacted=should_redact))
                if should_include_values:
                    item["value"] = response_value
                settings_payload.append(item)

            return json.dumps(
                {
                    "success": True,
                    "matched_count": len(settings_payload),
                    "include_values": should_include_values,
                    "show_secrets": allow_secret_values,
                    "settings": settings_payload,
                },
                ensure_ascii=False,
                indent=2,
                default=str,
            )
        except Exception as e:
            logger.error(f"查询系统设置失败: {e}", exc_info=True)
            return json.dumps(
                {"success": False, "message": f"查询系统设置时发生错误: {str(e)}"},
                ensure_ascii=False,
            )
