import asyncio
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.responses import Response

from app import schemas
from app.api.endpoints import plugin as plugin_endpoint
from app.api.endpoints.plugin import (
    plugin_history,
    plugin_releases,
    plugin_static_file,
    reload_plugin,
    reset_plugin,
    runtime_status,
    uninstall_plugin,
)
from app.api.endpoints.system import sync_plugin_market_from_wiki
from app.application.plugin import release as release_module
from app.application.plugin.catalog import PluginCatalogQuery
from app.application.plugin.config import PluginConfigCommand
from app.application.plugin.declaration import PluginDeclaredMetadata
from app.application.plugin.identity import (
    PluginBindingBasis,
    PluginIdentity,
    PluginPayloadSourceType,
    TrustedPluginSourceType,
)
from app.application.plugin.release import PluginReleaseService
from app.foundation.singleton import Singleton
from app.runtime.config import settings
from app.runtime.extensions.plugin.admission import PluginMutationAdmission
from app.runtime.extensions.plugin.manager import PluginManager
from app.runtime.tasks import TaskRegistry
from app.schemas.event import PluginDataResetEventData
from app.schemas.plugin import PluginInstance, PluginRuntimeStatus
from app.schemas.types import ChainEventType, SystemConfigKey

NOW = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
SOURCE_KEY = "github:demo/plugins"
SOURCE_URL = "https://github.com/demo/plugins"


def _plugin_identity(
    *,
    metadata: PluginDeclaredMetadata | None = None,
    plugin_id: str = "DemoPlugin",
    source_type: TrustedPluginSourceType = TrustedPluginSourceType.THIRD_PARTY,
    source_key: str = SOURCE_KEY,
) -> PluginIdentity:
    """构造绑定到测试仓库的插件身份。"""
    has_payload = metadata is not None
    return PluginIdentity(
        plugin_id=plugin_id,
        normalized_plugin_id=plugin_id.lower(),
        trusted_source_type=source_type,
        trusted_source_key=source_key,
        binding_basis=PluginBindingBasis.EXPLICIT_INSTALL,
        payload_source_type=(
            PluginPayloadSourceType.UNKNOWN
            if not has_payload
            else (
                PluginPayloadSourceType.OFFICIAL
                if source_type is TrustedPluginSourceType.OFFICIAL
                else PluginPayloadSourceType.THIRD_PARTY
            )
        ),
        payload_source_key=source_key if has_payload else None,
        declared_version="1.0.0" if has_payload else None,
        package_generation="v3" if has_payload else None,
        declared_metadata=metadata,
        payload_receipt="sha256:" + "0" * 64 if has_payload else None,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
        bound_at=NOW,
        payload_applied_at=NOW if has_payload else None,
    )


def _catalog_query(
    plugin_manager: MagicMock, persistence: MagicMock
) -> PluginCatalogQuery:
    """用运行态和身份端口替身构造插件目录查询。"""
    return PluginCatalogQuery(
        installed_plugins=plugin_manager.get_installed_plugins,
        local_plugins=plugin_manager.get_local_plugins,
        local_repo_plugins=plugin_manager.get_local_repo_plugins,
        online_candidates=plugin_manager.async_get_online_plugin_candidates,
        process_plugins=plugin_manager.process_plugins_list,
        identities=persistence.list_identities,
    )


def test_update_candidates_report_source_and_binding_relationship():
    """市场更新候选应标明仓库类型，并仅把当前绑定仓库视为可直接更新。"""
    plugins = [
        schemas.Plugin(
            id="OfficialBound",
            plugin_version="2.0.0",
            repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
            has_update=True,
        ),
        schemas.Plugin(
            id="ThirdPartyBound",
            plugin_version="2.0.0",
            repo_url="https://github.com/example/plugins",
            has_update=True,
        ),
        schemas.Plugin(
            id="AlternativeOfficial",
            plugin_version="2.0.0",
            repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
            has_update=True,
        ),
        schemas.Plugin(
            id="LocalOnly",
            plugin_version="2.0.0",
            repo_url="local:///plugins",
            has_update=True,
        ),
    ]
    persistence = MagicMock()
    persistence.list_identities = AsyncMock(
        return_value=[
            _plugin_identity(
                plugin_id="OfficialBound",
                source_type=TrustedPluginSourceType.OFFICIAL,
                source_key="github:jxxghp/moviepilot-plugins",
            ),
            _plugin_identity(
                plugin_id="ThirdPartyBound",
                source_key="github:example/plugins",
            ),
            _plugin_identity(
                plugin_id="AlternativeOfficial",
                source_key="github:example/plugins",
            ),
        ]
    )
    plugin_manager = MagicMock()
    plugin_manager.process_plugins_list.side_effect = lambda higher, _base: higher

    result = asyncio.run(
        _catalog_query(plugin_manager, persistence).project_update_candidates(
            plugins,
            plugins,
            [plugin.id for plugin in plugins],
        )
    )

    assert result[0].update_candidate is not None
    assert result[0].update_candidate.source_type == "official"
    assert result[0].update_candidate.is_bound is True
    assert result[1].update_candidate is not None
    assert result[1].update_candidate.source_type == "third_party"
    assert result[1].update_candidate.is_bound is True
    assert result[2].update_candidate is not None
    assert result[2].update_candidate.source_type == "official"
    assert result[2].update_candidate.is_bound is False
    assert result[3].update_candidate is None


def test_bound_repository_update_precedes_a_higher_alternative():
    """绑定仓库仍有更新时先完成可信更新，下一轮再提示其他仓库版本。"""
    bound_update = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="2.0.0",
        repo_url=SOURCE_URL,
        has_update=True,
    )
    alternative_update = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="3.0.0",
        repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
        has_update=True,
    )
    persistence = MagicMock()
    persistence.list_identities = AsyncMock(return_value=[_plugin_identity()])
    plugin_manager = MagicMock()
    plugin_manager.process_plugins_list.return_value = [bound_update]

    result = asyncio.run(
        _catalog_query(plugin_manager, persistence).project_update_candidates(
            [bound_update, alternative_update],
            [alternative_update],
            ["DemoPlugin"],
        )
    )

    assert [plugin.id for plugin in result] == ["DemoPlugin"]
    assert result[0].update_candidate is not None
    assert result[0].update_candidate.version == "2.0.0"
    assert result[0].update_candidate.is_bound is True


def test_market_endpoint_reads_source_preserving_candidates_for_bound_update():
    """市场接口必须在全局版本合并前保留绑定仓库候选。"""
    installed = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.0.0",
        installed=True,
    )
    bound_update = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="2.0.0",
        repo_url=SOURCE_URL,
        has_update=True,
    )
    alternative_update = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="3.0.0",
        repo_url="https://github.com/jxxghp/MoviePilot-Plugins",
        has_update=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed]
    plugin_manager.get_local_plugins.return_value = []
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_online_plugin_candidates = AsyncMock(
        return_value=[bound_update, alternative_update]
    )
    plugin_manager.process_plugins_list.side_effect = (
        lambda higher, base: [
            max(
                higher + base,
                key=lambda plugin: tuple(
                    int(part) for part in plugin.plugin_version.split(".")
                ),
            )
        ]
    )
    persistence = MagicMock()
    persistence.list_identities = AsyncMock(return_value=[_plugin_identity()])

    query = _catalog_query(plugin_manager, persistence)
    with patch(
        "app.api.endpoints.plugin.get_plugin_catalog_query",
        return_value=query,
    ):
        response = Response()
        result = asyncio.run(
            plugin_endpoint.all_plugins(
                None,
                "market",
                False,
                max_results=1,
                response=response,
            )
        )

    assert [plugin.id for plugin in result] == ["DemoPlugin"]
    assert response.headers["X-Total-Count"] == "1"
    assert result[0].update_candidate is not None
    assert result[0].update_candidate.version == "2.0.0"
    assert result[0].update_candidate.is_bound is True
    plugin_manager.async_get_online_plugin_candidates.assert_awaited_once_with(False)


def test_all_plugins_explicit_page_count_overrides_legacy_max_results() -> None:
    """插件列表显式 page/count 应分页，并优先于显式 max_results 限量。"""
    catalog = MagicMock()
    catalog.query = AsyncMock(
        return_value=[
            schemas.Plugin(id=f"Plugin{index}", plugin_version="1.0.0")
            for index in range(1, 4)
        ]
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_catalog_query",
        return_value=catalog,
    ):
        response = Response()
        result = asyncio.run(
            plugin_endpoint.all_plugins(
                None,
                "all",
                False,
                max_results=1,
                page=2,
                count=1,
                response=response,
            )
        )

    assert [plugin.id for plugin in result] == ["Plugin2"]
    assert response.headers["X-Total-Count"] == "3"


def test_all_plugins_without_pagination_or_limit_returns_complete_catalog() -> None:
    """插件列表省略分页和限量参数时应返回完整目录。"""
    catalog = MagicMock()
    catalog.query = AsyncMock(
        return_value=[
            schemas.Plugin(id=f"Plugin{index}", plugin_version="1.0.0")
            for index in range(1, 52)
        ]
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_catalog_query",
        return_value=catalog,
    ):
        response = Response()
        result = asyncio.run(
            plugin_endpoint.all_plugins(
                None,
                "all",
                False,
                response=response,
            )
        )

    assert len(result) == 51
    assert result[0].id == "Plugin1"
    assert result[-1].id == "Plugin51"
    assert response.headers["X-Total-Count"] == "51"


def _persistence(identity: PluginIdentity) -> MagicMock:
    """构造只暴露身份读取合同的异步持久化替身。"""
    persistence = MagicMock()
    persistence.get_identity = AsyncMock(return_value=identity)
    return persistence


def _release_service(
    plugin_manager: MagicMock,
    *,
    persistence: MagicMock | None = None,
    plugin_helper: MagicMock | None = None,
) -> PluginReleaseService:
    """用端口替身构造与启动组合根等价的 Release 查询服务。"""
    if persistence is None:
        persistence = MagicMock()
        persistence.get_identity = AsyncMock(return_value=None)
    if plugin_helper is None:
        plugin_helper = MagicMock()
        plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
        plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[])
    return PluginReleaseService(
        installed_plugins=plugin_manager.get_installed_plugins,
        local_repo_plugins=plugin_manager.get_local_repo_plugins,
        market_plugins=plugin_manager.async_get_plugins_from_market,
        local_version=plugin_manager.get_local_plugin_version,
        identity=persistence.get_identity,
        version_flag=lambda: settings.VERSION_FLAG,
        compatible_flags=lambda flag: ["v2"] if flag == "v3" else [],
        has_release_cache=plugin_helper.async_has_plugin_release_cache,
        releases=plugin_helper.async_get_plugin_release_versions,
        refresh_releases=plugin_helper.async_get_plugin_release_versions,
    )


def test_plugin_release_service_reset_isolates_lifespans(monkeypatch):
    """停机清理后不得继续复用上一 lifespan 的 Release 端口。"""
    manager = MagicMock()
    service = _release_service(manager)
    monkeypatch.setattr(release_module, "_release_service", service)

    release_module.reset_plugin_release_service()

    with pytest.raises(RuntimeError, match="尚未完成初始化"):
        release_module.get_plugin_release_service()


def test_plugin_history_merges_remote_metadata():
    """
    已安装插件点击更新说明时，接口会按需合并远端仓库中的更新记录。
    """
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        installed=True,
        history={},
    )
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        repo_url=SOURCE_URL,
        history={"v1.1.0": "- 新增更新说明"},
        system_version=">=2.0.0",
        system_version_compatible=True,
        has_update=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    persistence = _persistence(_plugin_identity())
    release_service = _release_service(plugin_manager, persistence=persistence)

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.repo_url == "https://github.com/demo/plugins"
    assert result.history == {"v1.1.0": "- 新增更新说明"}
    assert result.system_version == ">=2.0.0"
    assert result.has_update
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        SOURCE_URL, settings.VERSION_FLAG, True
    )


def test_plugin_history_falls_back_to_backward_compatible_package():
    """已绑定来源的插件在当前索引缺失时仍读取向后兼容代际的历史。"""
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        installed=True,
        history={},
    )
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        repo_url=SOURCE_URL,
        history={"v2.0.0": "兼容代际更新说明"},
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_plugins_from_market = AsyncMock(
        side_effect=[[], [market_plugin]]
    )
    persistence = _persistence(_plugin_identity())
    release_service = _release_service(plugin_manager, persistence=persistence)

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.history == {"v2.0.0": "兼容代际更新说明"}
    assert plugin_manager.async_get_plugins_from_market.await_args_list == [
        ((SOURCE_URL, settings.VERSION_FLAG, True), {}),
        ((SOURCE_URL, "v2", True), {}),
    ]


def test_runtime_status_reports_pending_and_terminal_counts():
    """插件页摘要区分后台收敛、准备态和终态失败。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_runtime_statuses.return_value = {
        "SourcePending": PluginRuntimeStatus.SOURCE_MISSING,
        "DependencyPending": PluginRuntimeStatus.DEPENDENCY_PENDING,
        "ActivePlugin": PluginRuntimeStatus.ACTIVE,
        "FailedPlugin": PluginRuntimeStatus.LOAD_FAILED,
    }
    plugin_manager.is_plugin_settling.return_value = True
    plugin_manager.get_plugin_runtime_generation.return_value = 7
    plugin_manager.get_plugin_restart_requirements.return_value = {
        "NativePlugin": ("native-demo",),
        "RemovedPlugin": ("native-removed",),
    }
    config = MagicMock()
    config.get.return_value = ["NativePlugin"]

    with (
        patch("app.api.endpoints.plugin.get_plugin_manager", return_value=plugin_manager),
        patch(
            "app.api.endpoints.plugin.get_configured_system_config",
            return_value=config,
        ),
    ):
        result = asyncio.run(runtime_status(None))

    assert result.ready is False
    assert result.generation == 7
    assert result.pending_count == 2
    assert result.failed_count == 1
    assert result.restart_required_plugin_ids == ["NativePlugin"]


def test_reload_endpoint_reports_load_failure(monkeypatch):
    """插件重载失败时接口返回失败，同时仍刷新旧注册投影。"""
    plugin_manager = MagicMock()
    plugin_manager.reload_plugin.return_value = PluginRuntimeStatus.LOAD_FAILED
    register = MagicMock()
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "register_plugin", register)

    result = reload_plugin("DemoPlugin", None)

    assert result.success is False
    assert result.message == "插件加载失败，请查看插件日志"
    register.assert_called_once_with("DemoPlugin")


def test_plugin_history_returns_installed_plugin_when_remote_missing():
    """
    远端仓库不可用时，接口仍返回本地已安装插件信息，前端可继续展示兜底状态。
    """
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        installed=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[])
    persistence = _persistence(_plugin_identity())
    release_service = _release_service(plugin_manager, persistence=persistence)

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.id == "DemoPlugin"
    assert result.history == {}


def test_plugin_history_uses_bound_repo_without_refreshing_all_markets():
    """
    更新说明只读取持久化绑定仓库，不信任运行态 DTO 中可漂移的来源地址。
    """
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="Demo Plugin",
        plugin_version="1.0.0",
        repo_url="https://github.com/attacker/plugins",
        installed=True,
    )
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        repo_url=SOURCE_URL,
        history={"v1.1.0": "- 新增更新说明"},
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[])
    persistence = _persistence(_plugin_identity())
    release_service = _release_service(plugin_manager, persistence=persistence)

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.history == {"v1.1.0": "- 新增更新说明"}
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        SOURCE_URL, settings.VERSION_FLAG, True
    )
    plugin_manager.async_get_online_plugins.assert_not_awaited()


def test_plugin_history_uses_declared_metadata_when_bound_market_is_unavailable():
    """加载失败且绑定市场不可用时，详情仍返回已提交载荷的展示信息。"""
    installed_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_name="DemoPlugin",
        installed=True,
        runtime_status=PluginRuntimeStatus.LOAD_FAILED,
    )
    metadata = PluginDeclaredMetadata.from_package(
        {
            "name": "Saved Demo",
            "description": "Saved description",
            "author": "Saved author",
            "v3": True,
        },
        declaration_version="1.0.0",
        manifest_matches_payload=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.get_installed_plugins.return_value = [installed_plugin]
    plugin_manager.get_local_repo_plugins.return_value = []
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[])
    persistence = _persistence(_plugin_identity(metadata=metadata))
    release_service = _release_service(plugin_manager, persistence=persistence)

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_history("DemoPlugin", None, True))

    assert result.plugin_name == "Saved Demo"
    assert result.plugin_desc == "Saved description"
    assert result.plugin_author == "Saved author"
    assert result.plugin_version == "1.0.0"
    assert result.runtime_status is PluginRuntimeStatus.LOAD_FAILED


def test_plugin_releases_returns_supported_versions_with_latest_and_current(monkeypatch):
    """
    release 列表接口返回可安装版本，并标记当前 package 最新版本与本地已安装版本。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_online_plugins = AsyncMock(return_value=[market_plugin])
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = "1.2.0"
    plugin_helper = MagicMock()
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
        {"version": "1.2.0", "tag_name": "DemoPlugin_v1.2.0", "asset_name": "demoplugin_v1.2.0.zip"},
    ])
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["release_supported"] is True
    assert result["latest_version"] == "1.2.3"
    assert result["current_version"] == "1.2.0"
    assert result["items"][0]["is_latest"] is True
    assert result["items"][0]["is_current"] is False
    assert result["items"][1]["is_latest"] is False
    assert result["items"][1]["is_current"] is True
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        "https://github.com/demo/plugins", settings.VERSION_FLAG, False
    )
    plugin_manager.async_get_online_plugins.assert_not_awaited()
    plugin_manager.get_local_plugins.assert_not_called()


def test_plugin_releases_does_not_mutate_cached_release_items(monkeypatch):
    """
    接口标记当前/最新版本时不能修改 helper 返回对象，避免污染缓存中的 release 列表。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    release_items = [
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
    ]
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = "1.2.0"
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=release_items)
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["items"][0]["is_latest"] is True
    assert "is_latest" not in release_items[0]
    assert "is_current" not in release_items[0]


def test_plugin_releases_falls_back_to_compatible_base_package(monkeypatch):
    """
    当前版本和向后兼容 package 未包含插件时，再读取基础 package 兼容项。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(
        side_effect=[[], [], [market_plugin]]
    )
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[])
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(
            plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False)
        )

    assert result["latest_version"] == "1.2.3"
    assert plugin_manager.async_get_plugins_from_market.await_args_list == [
        (("https://github.com/demo/plugins", settings.VERSION_FLAG, False), {}),
        (("https://github.com/demo/plugins", "v2", False), {}),
        (("https://github.com/demo/plugins", None, False), {}),
    ]


def test_plugin_releases_uses_force_refresh_for_market_metadata(monkeypatch):
    """
    release 列表接口沿用插件市场的 force 语义，供前端手动刷新时绕过缓存。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[])
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", True))

    assert result["release_supported"] is False
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        "https://github.com/demo/plugins", settings.VERSION_FLAG, True
    )
    assert plugin_helper.async_get_plugin_release_versions.await_args.args == (
        "DemoPlugin",
        "https://github.com/demo/plugins",
    )


def test_plugin_releases_force_uses_cached_release_response_and_schedules_refresh(monkeypatch):
    """
    手动刷新时 package 元数据仍强刷，但 Release 明细先读缓存并后台刷新，避免弹窗阻塞。
    """
    from app.runtime.cache import is_fresh

    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    fresh_states = []
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=True)

    async def fake_releases(*_args):
        fresh_states.append(is_fresh())
        return [
            {
                "version": "1.2.3",
                "tag_name": "DemoPlugin_v1.2.3",
                "asset_name": "demoplugin_v1.2.3.zip",
            }
        ]

    plugin_helper.async_get_plugin_release_versions = fake_releases
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )
    scheduled = []

    def fake_schedule(plugin_id, repo_url, task_registry):
        scheduled.append((plugin_id, repo_url, task_registry))

    with (
        patch(
            "app.api.endpoints.plugin.get_plugin_release_service",
            return_value=release_service,
        ),
        patch.object(plugin_endpoint, "_schedule_plugin_release_refresh", fake_schedule),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", True))

    assert result["release_supported"] is True
    assert fresh_states == [False]
    assert len(scheduled) == 1
    assert scheduled[0][:2] == (
        "DemoPlugin",
        "https://github.com/demo/plugins",
    )
    assert isinstance(scheduled[0][2], TaskRegistry)
    plugin_helper.async_has_plugin_release_cache.assert_awaited_once_with(
        "https://github.com/demo/plugins"
    )
    plugin_manager.async_get_plugins_from_market.assert_awaited_once_with(
        "https://github.com/demo/plugins", settings.VERSION_FLAG, True
    )


def test_plugin_releases_force_skips_background_refresh_without_release_cache(monkeypatch):
    """
    冷缓存 force 请求已在响应路径读取 Release，不能马上再启动一次重复强刷。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=True,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[
        {
            "version": "1.2.3",
            "tag_name": "DemoPlugin_v1.2.3",
            "asset_name": "demoplugin_v1.2.3.zip",
        }
    ])
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )
    scheduled = []

    def fake_schedule(plugin_id, repo_url):
        scheduled.append((plugin_id, repo_url))

    with (
        patch(
            "app.api.endpoints.plugin.get_plugin_release_service",
            return_value=release_service,
        ),
        patch.object(plugin_endpoint, "_schedule_plugin_release_refresh", fake_schedule),
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", True))

    assert result["release_supported"] is True
    assert scheduled == []
    plugin_helper.async_has_plugin_release_cache.assert_awaited_once_with(
        "https://github.com/demo/plugins"
    )


def test_plugin_releases_hides_items_when_market_plugin_does_not_enable_release(monkeypatch):
    """
    接口是否支持 Release 安装要与当前 package 的 release 声明保持一致。
    """
    market_plugin = schemas.Plugin(
        id="DemoPlugin",
        plugin_version="1.2.3",
        repo_url="https://github.com/demo/plugins",
        release=False,
    )
    plugin_manager = MagicMock()
    plugin_manager.async_get_plugins_from_market = AsyncMock(return_value=[market_plugin])
    plugin_manager.get_local_plugin_version.return_value = None
    plugin_helper = MagicMock()
    plugin_helper.async_has_plugin_release_cache = AsyncMock(return_value=False)
    plugin_helper.async_get_plugin_release_versions = AsyncMock(return_value=[
        {"version": "1.2.3", "tag_name": "DemoPlugin_v1.2.3", "asset_name": "demoplugin_v1.2.3.zip"},
    ])
    release_service = _release_service(
        plugin_manager, plugin_helper=plugin_helper
    )

    with patch(
        "app.api.endpoints.plugin.get_plugin_release_service",
        return_value=release_service,
    ):
        result = asyncio.run(plugin_releases("DemoPlugin", None, "https://github.com/demo/plugins", False))

    assert result["release_supported"] is False
    assert result["items"] == []
    plugin_helper.async_get_plugin_release_versions.assert_not_awaited()


def test_sync_plugin_market_from_wiki_merges_and_deduplicates_repos():
    """
    Wiki 同步会提取标记区域内的 GitHub 仓库地址，并与本地配置合并去重后写入。
    """
    markdown = """
<!-- plugin-market-repos:start -->
- https://github.com/local/existing/
- https://github.com/wiki/new-repo/
- https://github.com/wiki/new-repo
<!-- plugin-market-repos:end -->
- https://github.com/wiki/ignored-outside-marker
"""
    response = MagicMock(status_code=200, text=markdown)
    request_utils = MagicMock()
    request_utils.get_res = AsyncMock(return_value=response)
    runtime_settings = MagicMock()
    runtime_settings.get.return_value = "https://github.com/local/existing"
    runtime_settings.update.return_value = (True, "")

    @asynccontextmanager
    async def rule_group_mutation():
        """提供本用例不会进入的规则组事务替身。"""
        yield SimpleNamespace()

    from app.startup.composition.system import compose_system_service

    runtime = SimpleNamespace(
        system=compose_system_service(
            settings=runtime_settings,
            system_config=MagicMock(),
            rule_group_mutation=rule_group_mutation,
        )
    )
    with (
        patch(
            "app.startup.composition.system.AsyncRequestUtils",
            return_value=request_utils,
        ),
        patch(
            "app.startup.composition.system.eventmanager.async_send_event",
            new=AsyncMock(),
        ) as send_event,
    ):
        result = asyncio.run(sync_plugin_market_from_wiki(None, None, runtime))

    assert result.success
    assert result.data["repos"] == [
        "https://github.com/local/existing",
        "https://github.com/wiki/new-repo",
    ]
    assert result.data["added_count"] == 1
    assert result.data["total_count"] == 2
    runtime_settings.update.assert_called_once_with(
        "PLUGIN_MARKET",
        "https://github.com/local/existing,https://github.com/wiki/new-repo",
    )
    send_event.assert_awaited_once()


def test_reset_plugin_sends_pre_reset_chain_event_before_deleting_data():
    """
    插件重置会先触发同步链式事件，让插件在数据被清空前完成自有状态补偿。
    """
    plugin_manager = MagicMock()
    calls = []

    def delete_config(plugin_id, force=False):
        calls.append(("delete_config", plugin_id, force))
        return True

    def delete_data(plugin_id, force=False):
        calls.append(("delete_data", plugin_id, force))
        return True

    def stop_plugin(plugin_id):
        calls.append(("stop", plugin_id))
        return True

    plugin_manager.stop.side_effect = stop_plugin
    plugin_manager.delete_plugin_config.side_effect = delete_config
    plugin_manager.delete_plugin_data.side_effect = delete_data

    def publish_reset(plugin_id):
        """记录重置前事件，验证应用用例保留补偿时序。"""
        calls.append((
            "event",
            ChainEventType.PluginDataReset,
            PluginDataResetEventData(
                plugin_id=plugin_id,
                reset_config=True,
                reset_data=True,
            ),
        ))

    command = PluginConfigCommand(
        save_config=plugin_manager.save_plugin_config,
        initialize=plugin_manager.init_plugin,
        stop=plugin_manager.stop,
        delete_config=plugin_manager.delete_plugin_config,
        delete_data=plugin_manager.delete_plugin_data,
        reload_runtime=plugin_manager.reload_plugin,
        publish_reset=publish_reset,
        refresh_registrations=lambda _plugin_id: None,
        mutation=lambda _operation: nullcontext(),
    )
    result = reset_plugin("SubscribeAssistantEnhanced", None, command)

    assert result.success is True
    assert len(calls) == 4
    event_call = calls[0]
    assert event_call[0] == "event"
    assert event_call[1] is ChainEventType.PluginDataReset
    assert isinstance(event_call[2], PluginDataResetEventData)
    assert event_call[2].plugin_id == "SubscribeAssistantEnhanced"
    assert event_call[2].reset_config is True
    assert event_call[2].reset_data is True
    assert calls[1:] == [
        ("stop", "SubscribeAssistantEnhanced"),
        ("delete_config", "SubscribeAssistantEnhanced", True),
        ("delete_data", "SubscribeAssistantEnhanced", True),
    ]
    plugin_manager.reload_plugin.assert_called_once_with("SubscribeAssistantEnhanced")


def test_delete_plugin_config_can_force_delete_after_plugin_is_stopped():
    """
    重置入口会先停止插件；配置删除需要能处理运行态注册已清理的插件 ID。
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()

    storage = MagicMock()
    storage.delete.return_value = True
    with patch("app.runtime.extensions.plugin.storage._plugin_storage", storage):
        assert manager.delete_plugin_config("DemoPlugin", force=True) is True
    storage.delete.assert_called_once_with("plugin.DemoPlugin")
    Singleton._instances.pop((PluginManager, (), frozenset()), None)


def test_virtual_instance_static_file_reads_from_source_directory(tmp_path, monkeypatch):
    """实例 URL 保持独立，但静态内容直接读取共享的源插件目录。"""
    source_file = tmp_path / "app/plugins/demoplugin/dist/remoteEntry.js"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("export default 'shared'", encoding="utf-8")
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_source_id.return_value = "DemoPlugin"
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_api_runtime_config_snapshot",
        lambda: MagicMock(root_path=tmp_path),
    )

    response = asyncio.run(
        plugin_static_file("DemoPluginwork", "dist/remoteEntry.js", None)
    )

    async def read_body() -> bytes:
        """读取流式响应的全部测试内容。"""
        return b"".join([chunk async for chunk in response.body_iterator])

    assert asyncio.run(read_body()) == b"export default 'shared'"
    assert response.media_type == "application/javascript"
    plugin_manager.get_plugin_source_id.assert_called_once_with("DemoPluginwork")


def test_uninstall_virtual_instance_never_removes_source_package(monkeypatch):
    """卸载虚拟实例只清理实例状态，不触碰源插件安装清单或目录。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_instance.return_value = PluginInstance(
        instance_id="DemoPluginwork",
        source_plugin_id="DemoPlugin",
    )
    plugin_manager.get_plugin_source_instances.return_value = []
    config = MagicMock()
    config.get.return_value = ["DemoPlugin"]
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_api", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_job", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_from_folders", MagicMock())

    result = uninstall_plugin("DemoPluginwork", None)

    assert result.success is True
    config.set.assert_called_once_with(
        SystemConfigKey.UserInstalledPlugins,
        ["DemoPlugin"],
    )
    plugin_manager.delete_plugin_config.assert_called_once_with(
        "DemoPluginwork",
        force=True,
    )
    plugin_manager.delete_plugin_data.assert_called_once_with(
        "DemoPluginwork",
        force=True,
    )
    plugin_manager.delete_plugin_instance.assert_called_once_with("DemoPluginwork")
    plugin_manager.remove_plugin.assert_called_once_with("DemoPluginwork")
    plugin_manager.remove_plugin_package.assert_not_called()


def test_uninstall_clone_delegates_physical_removal_to_package_owner(monkeypatch):
    """HTTP 分身卸载不得自行拼接路径或直接删除目录。"""
    plugin_manager = MagicMock()
    plugin_manager.get_plugin_instance.return_value = None
    plugin_manager.get_plugin_source_instances.return_value = []
    plugin_manager.plugins = {"DemoPluginwork": MagicMock(is_clone=True)}
    plugin_manager.remove_plugin_package.return_value = True
    config = MagicMock()
    config.get.return_value = ["DemoPluginwork"]
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "get_configured_system_config", lambda: config)
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_api", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_job", MagicMock())
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_from_folders", MagicMock())

    result = uninstall_plugin("DemoPluginwork", None)

    assert result.success is True
    plugin_manager.remove_plugin_package.assert_called_once_with("DemoPluginwork")
    assert "DemoPluginwork" not in plugin_manager.plugins


def test_sealed_http_uninstall_rejects_before_first_side_effect(monkeypatch):
    """HTTP 卸载在封口后明确失败，且不读取或写入插件持久化状态。"""
    admission = PluginMutationAdmission()
    admission.seal()
    plugin_manager = MagicMock()
    plugin_manager.mutation.side_effect = admission.hold
    config_provider = MagicMock()
    remove_api = MagicMock()
    remove_job = MagicMock()
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(
        plugin_endpoint,
        "get_configured_system_config",
        config_provider,
    )
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_api", remove_api)
    monkeypatch.setattr(plugin_endpoint, "remove_plugin_job", remove_job)

    result = uninstall_plugin("DemoPlugin", None)

    assert result.success is False
    assert "停机阶段" in result.message
    plugin_manager.get_plugin_instance.assert_not_called()
    config_provider.assert_not_called()
    remove_api.assert_not_called()
    remove_job.assert_not_called()


def test_sealed_http_clone_rejects_before_runtime_and_registration(monkeypatch):
    """HTTP 分身事务未获 admission 时不创建实例、不刷新注册或文件夹。"""
    admission = PluginMutationAdmission()
    admission.seal()
    plugin_manager = MagicMock()
    plugin_manager.mutation.side_effect = admission.hold
    register = MagicMock()
    add_to_folder = MagicMock()
    monkeypatch.setattr(plugin_endpoint, "get_plugin_manager", lambda: plugin_manager)
    monkeypatch.setattr(plugin_endpoint, "register_plugin", register)
    monkeypatch.setattr(plugin_endpoint, "add_clone_to_plugin_folder", add_to_folder)

    result = plugin_endpoint.clone_plugin(
        "DemoPlugin",
        schemas.PluginCloneRequest(suffix="Work"),
        None,
    )

    assert result.success is False
    assert "停机阶段" in result.message
    plugin_manager.clone_plugin.assert_not_called()
    register.assert_not_called()
    add_to_folder.assert_not_called()


def test_sealed_http_folder_update_rejects_before_config_access(monkeypatch):
    """插件文件夹写入口在封口后不读取或改写持久化配置。"""
    admission = PluginMutationAdmission()
    admission.seal()
    plugin_manager = MagicMock()
    plugin_manager.mutation.side_effect = admission.hold
    config_provider = MagicMock()
    monkeypatch.setattr(
        "app.application.plugin.folders.get_plugin_manager",
        lambda: plugin_manager,
    )
    monkeypatch.setattr(
        "app.application.plugin.folders.get_configured_system_config",
        config_provider,
    )

    result = asyncio.run(
        plugin_endpoint.update_folder_plugins("常用", ["DemoPlugin"], None)
    )

    assert result.success is False
    assert "停机阶段" in result.message
    config_provider.assert_not_called()


def test_delete_plugin_data_can_force_delete_after_plugin_is_stopped():
    """
    重置入口会先停止插件；插件数据删除不能依赖运行态注册仍存在。
    """
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
    manager = PluginManager()
    calls = []

    storage = MagicMock()
    storage.delete_data.side_effect = lambda pid: calls.append(pid)
    with patch("app.runtime.extensions.plugin.storage._plugin_storage", storage):
        assert manager.delete_plugin_data("DemoPlugin", force=True) is True

    assert calls == ["DemoPlugin"]
    Singleton._instances.pop((PluginManager, (), frozenset()), None)
