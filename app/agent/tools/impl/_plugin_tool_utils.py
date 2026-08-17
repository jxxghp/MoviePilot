"""插件 Agent 工具共享辅助方法"""

import json
import shutil
from typing import Any, Optional

from app.runtime.config import settings
from app.runtime.extensions.plugin_manager import PluginManager
from app.application.plugin.install import PluginInstallCommand
from app.db.oper.systemconfig import SystemConfigOper
from app.adapters.external.server import MoviePilotServerHelper
from app.adapters.external.market import PluginHelper
from app.adapters.system.plugin.package import PluginPackageManager
from app.schemas.types import SystemConfigKey

# 默认只向智能体返回一个可读预览，避免超大插件数据挤爆上下文窗口。
DEFAULT_PLUGIN_DATA_PREVIEW_CHARS = 12_000
MAX_PLUGIN_DATA_PREVIEW_CHARS = 50_000
PLUGIN_DATA_KEY_PREVIEW_LIMIT = 50
PLUGIN_DATA_TRUNCATION_SUFFIX = "\n...(插件数据内容过长，已截断)"
DEFAULT_PLUGIN_CANDIDATE_LIMIT = 50
MAX_PLUGIN_CANDIDATE_LIMIT = 200


def get_plugin_snapshot(plugin_id: str) -> Optional[dict[str, Any]]:
    """
    获取已安装插件的基础信息快照。
    """
    plugin_manager = PluginManager()
    for plugin in plugin_manager.get_local_plugins():
        if plugin.id == plugin_id:
            return {
                "plugin_id": plugin.id,
                "plugin_name": plugin.plugin_name,
                "plugin_version": plugin.plugin_version,
                "state": plugin.state,
            }
    return None


def clamp_preview_chars(max_chars: Optional[int]) -> int:
    """
    约束插件数据预览长度，避免工具结果无限膨胀。
    """
    if max_chars is None:
        return DEFAULT_PLUGIN_DATA_PREVIEW_CHARS
    return max(512, min(int(max_chars), MAX_PLUGIN_DATA_PREVIEW_CHARS))


def serialize_for_agent(value: Any) -> str:
    """
    将结果稳定序列化为 JSON 字符串，无法原生序列化的对象退化为字符串。
    """
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def build_preview_payload(value: Any, max_chars: Optional[int]) -> tuple[bool, int, int, str]:
    """
    为可能很大的插件数据生成预览结果。
    """
    serialized = serialize_for_agent(value)
    if len(serialized) <= clamp_preview_chars(max_chars):
        return False, len(serialized), len(serialized), serialized

    preview_limit = clamp_preview_chars(max_chars)
    preview = serialized[:preview_limit] + PLUGIN_DATA_TRUNCATION_SUFFIX
    return True, len(serialized), len(preview), preview


def refresh_plugin_registrations(plugin_id: str) -> None:
    """重新注册插件的定时任务、命令和动态 API 路由。"""
    # 这些依赖只在真正执行重载时才导入，避免普通查询工具引入不必要的初始化开销。
    from app.application.plugins import register_plugin_api
    from app.application.commands import init_commands
    from app.application.scheduling import update_plugin_job

    update_plugin_job(plugin_id)
    init_commands(plugin_id)
    register_plugin_api(plugin_id)


def reload_plugin_runtime(plugin_id: str) -> None:
    """重载插件实例并重新注册其命令、定时任务和 API。"""
    PluginManager().reload_plugin(plugin_id)
    refresh_plugin_registrations(plugin_id)


def summarize_plugin(plugin: Any) -> dict[str, Any]:
    """
    提取插件对象中对 Agent 有价值的摘要字段。
    """
    repo_url = getattr(plugin, "repo_url", None)
    return {
        "id": getattr(plugin, "id", None),
        "plugin_name": getattr(plugin, "plugin_name", None),
        "plugin_desc": getattr(plugin, "plugin_desc", None),
        "plugin_version": getattr(plugin, "plugin_version", None),
        "plugin_author": getattr(plugin, "plugin_author", None),
        "installed": bool(getattr(plugin, "installed", False)),
        "has_update": bool(getattr(plugin, "has_update", False)),
        "system_version_compatible": getattr(plugin, "system_version_compatible", True) is not False,
        "system_version": getattr(plugin, "system_version", None),
        "system_version_message": getattr(plugin, "system_version_message", None),
        "state": bool(getattr(plugin, "state", False)),
        "repo_url": repo_url,
        "source": "local_repo" if PluginHelper.is_local_repo_url(repo_url) else "market",
    }


def _merge_plugin_source_metadata(plugin: Any, source_plugin: Any) -> Any:
    """
    将插件市场或本地仓库中的来源元数据合并到已安装插件对象。
    """
    repo_url = getattr(source_plugin, "repo_url", None)
    if repo_url:
        setattr(plugin, "repo_url", repo_url)

    for attr in (
        "has_update",
        "release",
        "system_version",
        "system_version_compatible",
        "system_version_message",
    ):
        value = getattr(source_plugin, attr, None)
        if value is not None:
            setattr(plugin, attr, value)

    return plugin


def _map_plugins_by_id(plugins: list[Any]) -> dict[str, Any]:
    """
    按插件 ID 建立稳定映射，保留同 ID 首个候选来源。
    """
    plugin_map: dict[str, Any] = {}
    for plugin in plugins:
        plugin_id = getattr(plugin, "id", None)
        if plugin_id and plugin_id not in plugin_map:
            plugin_map[plugin_id] = plugin
    return plugin_map


async def enrich_installed_plugin_sources(
    installed_plugins: list[Any],
    force_refresh: bool = False,
) -> list[Any]:
    """
    为已安装插件补齐安装来源仓库地址。

    本地插件对象只包含运行目录中的静态元数据，通常没有 repo_url。这里按需从
    本地插件仓库和插件市场补齐来源，保证 Agent 后续安装、升级判断可以拿到仓库地址。
    """
    missing_source_plugins = [
        plugin for plugin in installed_plugins if not getattr(plugin, "repo_url", None)
    ]
    if not missing_source_plugins:
        return installed_plugins

    plugin_manager = PluginManager()
    local_repo_map = _map_plugins_by_id(plugin_manager.get_local_repo_plugins())
    for plugin in missing_source_plugins:
        source_plugin = local_repo_map.get(getattr(plugin, "id", None))
        if source_plugin:
            _merge_plugin_source_metadata(plugin, source_plugin)

    missing_source_plugins = [
        plugin for plugin in installed_plugins if not getattr(plugin, "repo_url", None)
    ]
    if not missing_source_plugins:
        return installed_plugins

    market_plugins = await plugin_manager.async_get_online_plugins(force=force_refresh)
    market_map = _map_plugins_by_id(market_plugins or [])
    for plugin in missing_source_plugins:
        source_plugin = market_map.get(getattr(plugin, "id", None))
        if source_plugin:
            _merge_plugin_source_metadata(plugin, source_plugin)

    return installed_plugins


async def load_market_plugins(force_refresh: bool = False) -> list[Any]:
    """
    聚合插件市场与本地插件仓库中的候选插件。
    """
    plugin_manager = PluginManager()
    online_plugins = await plugin_manager.async_get_online_plugins(force=force_refresh)
    local_repo_plugins = plugin_manager.get_local_repo_plugins()
    if not online_plugins and not local_repo_plugins:
        return []
    return plugin_manager.process_plugins_list(online_plugins + local_repo_plugins, [])


def list_installed_plugins() -> list[Any]:
    """
    返回当前已安装插件列表。
    """
    plugin_manager = PluginManager()
    return [plugin for plugin in plugin_manager.get_local_plugins() if plugin.installed]


def _normalize_text(value: Optional[str]) -> str:
    return (value or "").strip().lower()


def is_exact_plugin_match(plugin: Any, query: str) -> bool:
    """
    精确匹配插件 ID 或插件名称，用于安全地自动选择候选。
    """
    normalized_query = _normalize_text(query)
    return normalized_query in {
        _normalize_text(getattr(plugin, "id", None)),
        _normalize_text(getattr(plugin, "plugin_name", None)),
    }


def search_plugin_candidates(query: str, plugins: list[Any]) -> list[dict[str, Any]]:
    """
    按插件 ID、名称、描述和作者搜索候选，并返回打分结果。
    """
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    tokens = [token for token in normalized_query.replace("-", " ").split() if token]
    matches: list[dict[str, Any]] = []

    for plugin in plugins:
        plugin_id = _normalize_text(getattr(plugin, "id", None))
        plugin_name = _normalize_text(getattr(plugin, "plugin_name", None))
        plugin_desc = _normalize_text(getattr(plugin, "plugin_desc", None))
        plugin_author = _normalize_text(getattr(plugin, "plugin_author", None))
        haystack = "\n".join([plugin_id, plugin_name, plugin_desc, plugin_author])

        score = 0
        if normalized_query == plugin_id:
            score = 100
        elif normalized_query == plugin_name:
            score = 95
        elif plugin_id.startswith(normalized_query):
            score = 85
        elif plugin_name.startswith(normalized_query):
            score = 80
        elif normalized_query in plugin_id:
            score = 75
        elif normalized_query in plugin_name:
            score = 70
        elif tokens and all(token in plugin_name for token in tokens):
            score = 68
        elif tokens and all(token in plugin_id for token in tokens):
            score = 66
        elif normalized_query in plugin_desc:
            score = 45
        elif normalized_query in plugin_author:
            score = 40
        elif tokens and all(token in haystack for token in tokens):
            score = 35

        if score <= 0:
            continue

        matches.append(
            {
                "plugin": plugin,
                "score": score,
                "exact": is_exact_plugin_match(plugin, normalized_query),
            }
        )

    return sorted(
        matches,
        key=lambda item: (
            -item["score"],
            not item["exact"],
            -int(bool(getattr(item["plugin"], "has_update", False))),
            -int(bool(getattr(item["plugin"], "installed", False))),
            -int(getattr(item["plugin"], "add_time", 0) or 0),
        ),
    )


def summarize_candidates(matches: list[dict[str, Any]], limit: int = DEFAULT_PLUGIN_CANDIDATE_LIMIT) -> list[dict[str, Any]]:
    """
    压缩候选列表，避免一次性把完整市场数据返回给 Agent。
    """
    return [
        {
            **summarize_plugin(item["plugin"]),
            "score": item["score"],
            "exact": item["exact"],
        }
        for item in matches[:limit]
    ]


async def install_plugin_runtime(
    plugin_id: str, repo_url: Optional[str], force: bool = False
) -> tuple[bool, str, bool]:
    """
    按现有插件接口的行为安装插件，并刷新运行态注册信息。
    """
    plugin_manager = PluginManager()
    plugin_helper = PluginHelper()
    package_manager = PluginPackageManager(plugin_helper)

    from app.agent.tools.base import run_agent_blocking

    async def save_installed_plugins(plugin_ids: list[str]) -> object:
        """保存智能体安装用例确认后的插件列表。"""
        return await SystemConfigOper().async_set(
            SystemConfigKey.UserInstalledPlugins,
            plugin_ids,
        )

    async def install_package(
        target_id: str,
        target_repo: str,
        _release_version: Optional[str],
        force_install: bool,
    ) -> tuple[bool, str]:
        """调用插件包适配器执行异步安装。"""
        return await package_manager.async_install(
            plugin_id=target_id,
            repo_url=target_repo,
            force_install=force_install,
        )

    async def skip_compatibility_check(
        _target_id: str,
        _target_repo: str,
    ) -> None:
        """保持 Agent 旧安装入口不额外执行系统版本预检查。"""
        return None

    async def reload_runtime(target_id: str) -> object:
        """通过 Agent 阻塞任务适配器重建插件实例。"""
        return await run_agent_blocking(
            "plugin",
            plugin_manager.reload_plugin,
            target_id,
        )

    async def refresh_registrations(target_id: str) -> object:
        """通过 Agent 阻塞任务适配器刷新服务、命令和动态路由。"""
        return await run_agent_blocking(
            "plugin",
            refresh_plugin_registrations,
            target_id,
        )

    result = await PluginInstallCommand(
        installed_plugins_reader=lambda: SystemConfigOper().get(
            SystemConfigKey.UserInstalledPlugins
        ) or [],
        installed_plugins_writer=save_installed_plugins,
        plugin_ids_provider=plugin_manager.get_plugin_ids,
        compatibility_checker=skip_compatibility_check,
        package_installer=install_package,
        package_checkpointer=package_manager.async_checkpoint,
        package_committer=package_manager.async_commit,
        package_rollback=package_manager.async_rollback,
        install_reporter=lambda target_id, target_repo: (
            MoviePilotServerHelper.async_install_plugin_reg(
                plugin_id=target_id,
                repo_url=target_repo,
            )
        ),
        plugin_reloader=reload_runtime,
        registration_refresher=refresh_registrations,
    ).execute(
        plugin_id=plugin_id,
        repo_url=repo_url,
        force=force,
    )
    return result.success, result.message, result.refreshed_only


async def uninstall_plugin_runtime(plugin_id: str) -> dict[str, Any]:
    """
    按现有卸载逻辑移除插件，并清理运行态注册与分组信息。
    """
    from app.application.plugins import (
        remove_plugin_api,
        remove_plugin_from_folders,
    )
    from app.application.scheduling import remove_plugin_job

    config_oper = SystemConfigOper()
    install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
    if plugin_id in install_plugins:
        install_plugins = [plugin for plugin in install_plugins if plugin != plugin_id]
        await config_oper.async_set(SystemConfigKey.UserInstalledPlugins, install_plugins)

    remove_plugin_api(plugin_id)
    remove_plugin_job(plugin_id)

    plugin_manager = PluginManager()
    plugin_class = plugin_manager.plugins.get(plugin_id)
    was_clone = bool(getattr(plugin_class, "is_clone", False))
    clone_files_removed = False

    if was_clone:
        plugin_manager.delete_plugin_config(plugin_id)
        plugin_manager.delete_plugin_data(plugin_id)
        plugin_base_dir = settings.ROOT_PATH / "app" / "plugins" / plugin_id.lower()
        if plugin_base_dir.exists():
            try:
                shutil.rmtree(plugin_base_dir)
                plugin_manager.plugins.pop(plugin_id, None)
                clone_files_removed = True
            except Exception:
                clone_files_removed = False

    remove_plugin_from_folders(plugin_id)
    plugin_manager.remove_plugin(plugin_id)

    return {
        "was_clone": was_clone,
        "clone_files_removed": clone_files_removed,
    }
