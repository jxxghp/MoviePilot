"""插件目录、生命周期和数据投影应用能力。"""

from typing import Any, Optional

from anyio import to_thread

from app.application.configuration import get_configured_system_config
from app.application.plugin.gateway import get_plugin_install_service
from app.application.plugin.runtime import get_plugin_manager
from app.domain.plugin import is_local_plugin_source
from app.schemas.plugin import PluginRuntimeStatus
from app.schemas.types import SystemConfigKey

DEFAULT_PLUGIN_CANDIDATE_LIMIT = 50
MAX_PLUGIN_CANDIDATE_LIMIT = 200


def get_plugin_snapshot(plugin_id: str) -> Optional[dict[str, Any]]:
    """
    获取已安装插件的基础信息快照。
    """
    plugin_manager = get_plugin_manager()
    for plugin in plugin_manager.get_local_plugins():
        if plugin.id == plugin_id:
            return {
                "plugin_id": plugin.id,
                "plugin_name": plugin.plugin_name,
                "plugin_version": plugin.plugin_version,
                "state": plugin.state,
            }
    return None


def refresh_plugin_registrations(plugin_id: str) -> None:
    """重新注册插件的定时任务、命令和动态 API 路由。"""
    # 这些依赖只在真正执行重载时才导入，避免普通查询工具引入不必要的初始化开销。
    from app.application.commands import init_commands
    from app.application.plugin.routes import register_plugin_api
    from app.application.scheduling import update_plugin_job

    update_plugin_job(plugin_id)
    init_commands(plugin_id)
    register_plugin_api(plugin_id)


def reload_plugin_runtime(plugin_id: str) -> PluginRuntimeStatus:
    """重载插件实例并重新注册其命令、定时任务和 API。"""
    plugin_manager = get_plugin_manager()
    with plugin_manager.mutation(f"重载插件 {plugin_id}"):
        runtime_status = plugin_manager.reload_plugin(plugin_id)
        refresh_plugin_registrations(plugin_id)
        return runtime_status


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
        "source": "local_repo" if is_local_plugin_source(repo_url) else "market",
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
    missing_source_plugins = [plugin for plugin in installed_plugins if not getattr(plugin, "repo_url", None)]
    if not missing_source_plugins:
        return installed_plugins

    plugin_manager = get_plugin_manager()
    local_repo_map = _map_plugins_by_id(plugin_manager.get_local_repo_plugins())
    for plugin in missing_source_plugins:
        source_plugin = local_repo_map.get(getattr(plugin, "id", None))
        if source_plugin:
            _merge_plugin_source_metadata(plugin, source_plugin)

    missing_source_plugins = [plugin for plugin in installed_plugins if not getattr(plugin, "repo_url", None)]
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
    plugin_manager = get_plugin_manager()
    online_plugins = await plugin_manager.async_get_online_plugins(force=force_refresh)
    local_repo_plugins = plugin_manager.get_local_repo_plugins()
    if not online_plugins and not local_repo_plugins:
        return []
    return plugin_manager.process_plugins_list(online_plugins + local_repo_plugins, [])


def list_installed_plugins() -> list[Any]:
    """
    返回当前已安装插件列表。
    """
    plugin_manager = get_plugin_manager()
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


def summarize_candidates(
    matches: list[dict[str, Any]], limit: int = DEFAULT_PLUGIN_CANDIDATE_LIMIT
) -> list[dict[str, Any]]:
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
    plugin_id: str,
    repo_url: Optional[str],
    force: bool = False,
    *,
    explicit_source: bool = False,
) -> tuple[bool, str, bool]:
    """
    按现有插件接口的行为安装插件，并刷新运行态注册信息。
    """
    result = await get_plugin_install_service().install(
        plugin_id=plugin_id,
        repo_url=repo_url or None,
        force=force,
        explicit_source=explicit_source,
    )
    return result.success, result.message, result.refreshed_only


async def inspect_plugin_sources(
    plugin_id: str,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """返回 Agent 可展示的脱敏来源候选与当前准入状态。"""
    inspection = await get_plugin_install_service().inspect_source(
        plugin_id=plugin_id,
        force=force,
    )
    candidates = [candidate.public_dict() for candidate in inspection.online_candidates]
    if inspection.local_candidate is not None:
        candidates.append(inspection.local_candidate.public_dict())
    return {
        "selection_status": inspection.selection.status.value,
        "selection_reason": inspection.selection.reason,
        "inventory_complete": inspection.inventory_complete,
        "candidates": candidates,
    }


async def uninstall_plugin_runtime(plugin_id: str) -> dict[str, Any]:
    """
    按现有卸载逻辑移除插件，并清理运行态注册与分组信息。
    """
    from app.application.plugin.folders import remove_plugin_from_folders
    from app.application.plugin.routes import remove_plugin_api
    from app.application.scheduling import remove_plugin_job

    plugin_manager = get_plugin_manager()
    with plugin_manager.mutation(f"卸载插件 {plugin_id}"):
        virtual_instance = plugin_manager.get_plugin_instance(plugin_id)
        source_instances = plugin_manager.get_plugin_source_instances(plugin_id)
        if not virtual_instance and source_instances:
            instance_ids = "、".join(item.instance_id for item in source_instances)
            raise ValueError(f"请先卸载该插件的分身：{instance_ids}")

        config_oper = get_configured_system_config()
        install_plugins = config_oper.get(SystemConfigKey.UserInstalledPlugins) or []
        if plugin_id in install_plugins:
            install_plugins = [plugin for plugin in install_plugins if plugin != plugin_id]
            await config_oper.async_set(
                SystemConfigKey.UserInstalledPlugins,
                install_plugins,
            )

        remove_plugin_api(plugin_id)
        remove_plugin_job(plugin_id)

        plugin_class = plugin_manager.plugins.get(plugin_id)
        was_clone = bool(getattr(plugin_class, "is_clone", False))
        clone_files_removed = False

        # 删除数据必须发生在插件停止之后：插件的停机钩子只要取一次自有库句柄，就会把刚
        # 删除的数据重新建出来。停止同时注销插件类，故其后的删除一律按 force 执行
        plugin_manager.stop(plugin_id)

        if virtual_instance:
            plugin_manager.delete_plugin_config(plugin_id, force=True)
            plugin_manager.delete_plugin_data(plugin_id, force=True)
            plugin_manager.delete_plugin_instance(plugin_id)
        elif was_clone:
            plugin_manager.delete_plugin_config(plugin_id, force=True)
            plugin_manager.delete_plugin_data(plugin_id, force=True)
            try:
                clone_files_removed = await to_thread.run_sync(
                    plugin_manager.remove_plugin_package,
                    plugin_id,
                )
                if clone_files_removed:
                    plugin_manager.plugins.pop(plugin_id, None)
            except Exception:
                clone_files_removed = False

        remove_plugin_from_folders(plugin_id)
        plugin_manager.remove_plugin(plugin_id)

        return {
            "was_clone": was_clone,
            "clone_files_removed": clone_files_removed,
        }
