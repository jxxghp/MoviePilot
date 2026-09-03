"""Agent 应用服务的纯逻辑、端口编排和安全投影测试。"""

from contextlib import asynccontextmanager, nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import app.application.commands as command_application
import app.application.filtering as filtering
import app.application.plugin.management as plugin_management
import app.application.settings as settings_module
from app.application.configuration import SystemConfigWriteResult
from app.application.download.tasks import DownloadTaskMutationService, DownloadTaskService
from app.application.music.projection import simplify_music_album, simplify_music_artist, simplify_music_info
from app.application.plugin.data import (
    DeletePluginDataCommand,
    PluginDataQueryService,
    build_preview_payload,
    clamp_preview_chars,
)
from app.application.security.secrets import is_secret_setting_key
from app.domain.context import MusicAlbumInfo, MusicArtistInfo, MusicInfo, MusicRelease
from app.domain.projection.douban import project as project_douban
from app.schemas.rule import CustomRule, FilterRuleGroup
from app.schemas.types import EventType, MediaSource, SystemConfigKey


def run_async(awaitable):
    """在普通 pytest 函数中执行一个短异步断言。"""
    import asyncio
    return asyncio.run(awaitable)


def test_filtering_primitives_and_projection():
    """过滤规则基础函数应统一输入、解析引用并隔离可变输出。"""
    assert filtering.normalize_optional_text(None) is None
    assert filtering.normalize_optional_text("  value  ") == "value"
    assert filtering.normalize_media_type("movie") == "电影"
    assert filtering.normalize_media_type("电视剧") == "电视剧"
    assert filtering.normalize_media_type("") is None
    with pytest.raises(ValueError, match="media_type"):
        filtering.normalize_media_type("documentary")
    assert filtering.validate_numeric_range("size_range", None) is None
    assert filtering.validate_numeric_range("size_range", "1000 - 5000") == "1000 - 5000"
    with pytest.raises(ValueError, match="格式无效"):
        filtering.validate_numeric_range("size_range", "abc")
    with pytest.raises(ValueError, match="起始值"):
        filtering.validate_numeric_range("size_range", "5-2")
    assert filtering.validate_seeders(" 12 ") == "12"
    with pytest.raises(ValueError, match="非负整数"):
        filtering.validate_seeders("1.2")
    builtin = filtering.get_builtin_rules()
    builtin["4K"]["_test"] = "changed"
    assert "_test" not in filtering.get_builtin_rules()["4K"]
    custom = CustomRule(id="CUSTOM", name="Custom", include="x")
    group = FilterRuleGroup(name="main", rule_string="CUSTOM & 4K")
    assert filtering.build_custom_rule_map([custom, CustomRule(id=None)]) == {"CUSTOM": custom}
    assert filtering.build_rule_group_map([group, FilterRuleGroup(name=None)]) == {"main": group}
    assert filtering.extract_rule_tokens("CUSTOM & 4K & CUSTOM") == ["CUSTOM", "4K"]
    assert filtering.extract_rule_tokens(None) == []
    assert filtering.parse_rule_string("CUSTOM & 4K > !BLU")["levels"][0]["priority"] == 1
    with pytest.raises(ValueError, match="不能为空"):
        filtering.parse_rule_string(" ")
    with pytest.raises(ValueError, match="空层级"):
        filtering.parse_rule_string("4K > ")
    assert filtering.validate_rule_string("CUSTOM & 4K", ["CUSTOM", "4K"])["levels"]
    with pytest.raises(ValueError, match="不存在"):
        filtering.validate_rule_string("MISSING", ["4K"])
    assert filtering.serialize_builtin_rule("4K", {"name": "4K"})["source"] == "builtin"
    assert filtering.serialize_custom_rule(custom, ["main"])["referenced_by_rule_groups"] == ["main"]
    assert filtering.serialize_rule_group(group)["syntax_valid"] is True
    assert filtering.serialize_rule_group(FilterRuleGroup(name="bad", rule_string="4K > "))["syntax_valid"] is False
    assert filtering.serialize_rule_group(FilterRuleGroup(name="empty"))["syntax_valid"] is False
    assert filtering.replace_rule_id_in_rule_string("OLD & OLDER", "OLD", "NEW") == "NEW & OLDER"


def test_filtering_normalizers_and_usage_collection(monkeypatch):
    """规则实体校验应拒绝重复项，并能汇总全局与订阅引用。"""
    rule = filtering.normalize_custom_rule("NEW", "New rule", "inc", None, "1-2", "3", "2024", [])
    assert rule.id == "NEW"
    with pytest.raises(ValueError, match="不能为空"):
        filtering.normalize_custom_rule("", "New", None, None, None, None, None, [])
    with pytest.raises(ValueError, match="不能为空"):
        filtering.normalize_custom_rule("NEW", "", None, None, None, None, None, [])
    with pytest.raises(ValueError, match="内置"):
        filtering.normalize_custom_rule("4K", "Built-in", None, None, None, None, None, [])
    with pytest.raises(ValueError, match="已存在"):
        filtering.normalize_custom_rule("NEW", "Other", None, None, None, None, None, [rule])
    with pytest.raises(ValueError, match="规则名称"):
        filtering.normalize_custom_rule("OTHER", "New rule", None, None, None, None, None, [rule])
    with pytest.raises(ValueError, match="rule_id"):
        filtering.normalize_custom_rule("bad-id", "Bad", None, None, None, None, None, [])
    with pytest.raises(ValueError, match="规则组名称"):
        filtering.normalize_rule_group("", "4K", None, None, [], ["4K"])
    with pytest.raises(ValueError, match="category"):
        filtering.normalize_rule_group("group", "4K", None, "movie", [], ["4K"])
    normalized_group, parsed = filtering.normalize_rule_group("group", "4K", "movie", "action", [], ["4K"])
    assert normalized_group.media_type == "电影" and parsed["levels"]
    with pytest.raises(ValueError, match="已存在"):
        filtering.normalize_rule_group("group", "4K", None, None, [normalized_group], ["4K"])
    config = MagicMock()
    config.get.side_effect = lambda key: {
        SystemConfigKey.SearchFilterRuleGroups: ["search"],
        SystemConfigKey.SubscribeFilterRuleGroups: ["subscribe"],
        SystemConfigKey.BestVersionFilterRuleGroups: ["best"],
    }.get(key, [])
    monkeypatch.setattr(filtering, "get_configured_system_config", lambda: config)

    class SubscriptionPort:
        """提供规则组使用统计所需的最小订阅端口。"""

        async def async_list(self):
            """返回带规则组引用的订阅快照。"""
            return [SimpleNamespace(id=1, name="Sub", season=1, type="电影", username="u", best_version=True, filter_groups=["search", "custom"]), SimpleNamespace(filter_groups=None)]

    usage = run_async(filtering.collect_rule_group_usages(SubscriptionPort(), ["search", "custom"]))
    assert usage["search"]["used_in_global_search"] is True
    assert usage["search"]["subscribes"][0]["subscribe_id"] == 1
    refs = filtering.collect_custom_rule_group_refs([FilterRuleGroup(name="main", rule_string="CUSTOM & 4K"), FilterRuleGroup(name="none")], ["CUSTOM"])
    assert refs["CUSTOM"] == ["main"]


@pytest.mark.asyncio
async def test_filter_rule_service_queries_and_mutations(monkeypatch):
    """规则服务应覆盖查询、增删改和重命名引用的事务边界。"""
    old_rule = CustomRule(id="OLD", name="Old", include="old")
    old_group = FilterRuleGroup(name="group", rule_string="OLD & 4K")
    monkeypatch.setattr(filtering, "get_custom_rules", lambda: [old_rule])
    monkeypatch.setattr(filtering, "get_rule_groups", lambda: [old_group])
    config = MagicMock()
    config.async_set = AsyncMock(return_value=True)
    monkeypatch.setattr(filtering, "get_configured_system_config", lambda: config)
    publish = AsyncMock()
    mutation = MagicMock()
    mutation.apply = AsyncMock(return_value=SimpleNamespace(to_dict=lambda: {"changed": True}))

    @asynccontextmanager
    async def mutation_scope():
        """提供可观测的规则组异步事务。"""
        yield mutation

    class SubscriptionPort:
        """提供空订阅列表的查询端口。"""

        async def async_list(self):
            """返回空订阅集合。"""
            return []

    service = filtering.FilterRuleService(SubscriptionPort(), mutation_scope, publish)
    assert filtering.FilterRuleService.query_builtin(["4K"])["count"] == 1
    assert filtering.FilterRuleService.query_custom(["OLD"])["count"] == 1
    assert (await service.query_groups(include_usage=False))["count"] == 1
    assert (await service.query_groups(["missing"], include_usage=False))["count"] == 0
    added = await service.add_custom(rule_id="NEW", name="New", include="new", exclude=None, size_range=None, seeders=None, publish_time=None)
    assert added["custom_rule"]["id"] == "NEW"
    updated = await service.update_custom(current_rule_id="OLD", new_rule_id="RENAMED")
    assert updated["rule_groups_updated_for_rule_id_rename"] == ["group"]
    monkeypatch.setattr(filtering, "get_custom_rules", lambda: [CustomRule(id="DELETE", name="Delete")])
    monkeypatch.setattr(filtering, "get_rule_groups", lambda: [])
    assert (await service.delete_custom("DELETE"))["count"] == 0
    assert (await service.add_group(name="new-group", rule_string="4K"))["rule_group"]["name"] == "new-group"
    monkeypatch.setattr(filtering, "get_rule_groups", lambda: [FilterRuleGroup(name="old", rule_string="4K")])
    assert (await service.update_group(current_name="old", new_name="new"))["rule_group"]["name"] == "new"
    assert (await service.delete_group("old"))["count"] == 0
    monkeypatch.setattr(filtering, "get_rule_groups", lambda: [])
    with pytest.raises(ValueError, match="不存在"):
        await service.delete_group("old")
    with pytest.raises(ValueError, match="不存在"):
        await service.delete_custom("missing")


@pytest.mark.asyncio
async def test_save_system_config_and_settings_service(monkeypatch):
    """系统设置服务应处理摘要、脱敏、合并、列表更新和两类配置源。"""
    runtime = MagicMock()
    runtime.get.side_effect = lambda key: {"LLM_MODEL": "model", "PLUGIN_MARKET": "a"}.get(key)
    runtime.update.return_value = (True, "updated")
    system = MagicMock()
    system.get.side_effect = lambda key: [{"name": "qb", "token": "secret"}] if key == SystemConfigKey.Downloaders else {"a": 1}
    system.normalize_value.side_effect = lambda _key, value: value
    system.async_set = AsyncMock(return_value=True)
    system.async_set_with_normalized_value = AsyncMock(
        side_effect=lambda _key, value: SystemConfigWriteResult(
            changed=True,
            normalized_value=value,
        )
    )
    publish = AsyncMock()
    filter_config = MagicMock()
    filter_config.async_set = AsyncMock(return_value=True)
    monkeypatch.setattr(filtering, "get_configured_system_config", lambda: filter_config)
    monkeypatch.setattr(settings_module, "plugin_system_config_mutation", lambda _key: nullcontext())
    service = settings_module.SystemSettingsService(runtime, system, publish)
    await filtering.save_system_config(SystemConfigKey.CustomFilterRules, [None, ""], publish)
    assert filter_config.async_set.await_count == 1
    secret = service.query(setting_key=SystemConfigKey.Downloaders.value, include_values=True)
    assert secret["settings"][0]["value"][0]["token"] == "***"
    definition = secret["settings"][0]["definition"]
    assert definition == {
        "declared_type": "list[object]",
        "value_shape": "list",
        "nullable": False,
        "sensitive": True,
        "update_operations": ["replace", "upsert_list_item", "remove_list_item"],
        "default_match_field": "name",
        "persistence": "database:systemconfig",
    }
    shown = service.query(setting_key=SystemConfigKey.Downloaders.value, include_values=True, show_secrets=True)
    assert shown["settings"][0]["value"][0]["token"] == "secret"
    assert service.query(group="ai_agent")["include_values"] is False
    runtime_definition = service.query(setting_key="LLM_MODEL")["settings"][0]["definition"]
    assert runtime_definition["declared_type"]
    assert runtime_definition["value_shape"] == "str"
    assert runtime_definition["update_operations"] == ["replace"]
    assert runtime_definition["persistence"] == "app.env"
    spec = settings_module.resolve_setting_spec(SystemConfigKey.Downloaders.value)
    assert spec
    assert service._prepare_next_value(spec, {"name": "old", "x": 1}, {"name": "old", "y": 2}, "merge_dict", ["x"], None, None) == {"name": "old", "y": 2}
    assert service._prepare_next_value(spec, [{"name": "old"}], {"name": "old", "x": 2}, "upsert_list_item", None, None, None) == [{"name": "old", "x": 2}]
    assert service._prepare_next_value(spec, [{"name": "old"}], {"name": "old"}, "remove_list_item", None, None, None) == []
    with pytest.raises(ValueError, match="不支持"):
        service._prepare_next_value(spec, None, None, "bad", None, None, None)
    system.get.side_effect = [[], [{"name": "new"}]]
    result = await service.update(setting_key=SystemConfigKey.Downloaders.value, value={"name": "new"}, operation="upsert_list_item")
    assert result["changed"] is True
    runtime.get.side_effect = lambda key: "old"
    assert (await service.update(setting_key="PLUGIN_MARKET", value="new"))["changed"] is True


@pytest.mark.asyncio
async def test_settings_service_publishes_normalized_directory_value(monkeypatch):
    """新设置入口写库与配置事件必须共享同一份目录规范化结果。"""
    runtime = MagicMock()
    system = MagicMock()
    system.get.side_effect = [[], [{"name": "动漫", "media_category": "动漫/日番"}]]
    normalized = [{"name": "动漫", "media_category_id": "tv.anime.jp", "media_category": "动漫/日番"}]
    system.async_set_with_normalized_value = AsyncMock(
        return_value=SystemConfigWriteResult(
            changed=True,
            normalized_value=normalized,
        )
    )
    publish = AsyncMock()
    monkeypatch.setattr(settings_module, "plugin_system_config_mutation", lambda _key: nullcontext())
    service = settings_module.SystemSettingsService(runtime, system, publish)

    result = await service.update(
        setting_key=SystemConfigKey.Directories.value,
        value={"name": "动漫", "media_category_id": "tv.anime.jp"},
        operation="upsert_list_item",
    )

    assert result["saved_value"] == [{"name": "动漫", "media_category": "动漫/日番"}]
    system.async_set_with_normalized_value.assert_awaited_once_with(
        SystemConfigKey.Directories,
        [{"name": "动漫", "media_category_id": "tv.anime.jp"}],
    )
    publish.assert_awaited_once_with(SystemConfigKey.Directories.value, normalized)


def test_settings_catalog_redaction_and_projection():
    """设置目录应支持分类别名、匹配字段和递归敏感值脱敏。"""
    assert settings_module.normalize_group("基础配置") == "settings"
    assert settings_module.normalize_group("全部") == "all"
    with pytest.raises(ValueError, match="group"):
        settings_module.normalize_group("unknown")
    assert settings_module.resolve_setting_spec("Downloaders") is not None
    assert settings_module.list_setting_specs("downloaders")[0].group == "downloaders"
    assert settings_module.list_setting_specs("ai_agent", keyword="llm")
    assert settings_module.get_default_list_match_field(SystemConfigKey.Downloaders.value) == "name"
    assert settings_module.redact_secret_value({"apiKey": "secret", "url": "x", "nested": ["plain"]})["apiKey"] == "***"
    assert settings_module.normalize_group(None) == "all"
    assert settings_module.resolve_setting_spec(None) is None
    spec = settings_module.resolve_setting_spec(SystemConfigKey.UserSiteAuthParams.value)
    assert spec and settings_module.should_redact_setting(spec, [{"token": "secret"}])
    assert is_secret_setting_key("accessToken") and not is_secret_setting_key("token_count")


@pytest.mark.asyncio
async def test_plugin_management_and_data_services(monkeypatch):
    """插件管理、来源补齐和数据预览应覆盖成功与安全失败路径。"""
    plugin = SimpleNamespace(id="Demo", plugin_name="Demo Plugin", plugin_desc="desc", plugin_version="1", plugin_author="author", installed=True, has_update=True, state=True, repo_url=None, add_time=1)
    class SourceCandidate:
        """提供插件来源检查所需的公开投影。"""

        id = "Demo"
        plugin_name = "Demo Plugin"
        repo_url = "https://github.com/demo/repo"
        has_update = True
        release = "r1"
        system_version = "3"
        system_version_compatible = True
        system_version_message = None

        def public_dict(self):
            """返回来源候选的脱敏字典。"""
            return {"id": self.id, "repo_url": self.repo_url}

    source = SourceCandidate()
    manager = MagicMock()
    manager.get_local_plugins.return_value = [plugin]
    manager.get_local_repo_plugins.return_value = [source]
    manager.async_get_online_plugins = AsyncMock(return_value=[])
    manager.process_plugins_list.return_value = [source]
    monkeypatch.setattr(plugin_management, "get_plugin_manager", lambda: manager)
    assert plugin_management.get_plugin_snapshot("Demo")["plugin_id"] == "Demo"
    assert plugin_management.summarize_plugin(plugin)["source"] == "market"
    assert plugin_management.is_exact_plugin_match(plugin, "demo plugin")
    assert plugin_management.search_plugin_candidates("demo", [plugin])[0]["exact"] is True
    assert plugin_management.summarize_candidates(plugin_management.search_plugin_candidates("demo", [plugin]), 1)[0]["id"] == "Demo"
    assert await plugin_management.enrich_installed_plugin_sources([plugin]) == [plugin]
    assert plugin.repo_url == source.repo_url
    assert await plugin_management.load_market_plugins() == [source]
    assert plugin_management.list_installed_plugins() == [plugin]
    install_service = MagicMock()
    install_service.install = AsyncMock(return_value=SimpleNamespace(success=True, message="ok", refreshed_only=False))
    install_service.inspect_source = AsyncMock(return_value=SimpleNamespace(online_candidates=[source], local_candidate=None, selection=SimpleNamespace(status=SimpleNamespace(value="selected"), reason="exact"), inventory_complete=True))
    monkeypatch.setattr(plugin_management, "get_plugin_install_service", lambda: install_service)
    assert await plugin_management.install_plugin_runtime("Demo", source.repo_url) == (True, "ok", False)
    assert (await plugin_management.inspect_plugin_sources("Demo"))["selection_status"] == "selected"
    repo = MagicMock()
    unit = MagicMock()
    DeletePluginDataCommand(repo, unit).execute("Demo")
    unit.commit.assert_called_once()
    assert clamp_preview_chars(1) == 512
    truncated, total, returned, preview = build_preview_payload({"value": "x" * 1000}, 512)
    assert truncated and total > returned and "截断" in preview
    query_repo = MagicMock()
    query_repo.get = AsyncMock(return_value={"key": "value"})
    query_repo.list = AsyncMock(return_value={"a": 1})
    query = PluginDataQueryService(query_repo, lambda _id: {"plugin_id": "Demo"})
    assert (await query.query("Demo", key="config"))["found"]
    query_repo.get.return_value = None
    assert not (await query.query("Demo", key="missing"))["found"]
    assert (await query.query("Demo"))["count"] == 1
    with pytest.raises(ValueError, match="不存在"):
        await PluginDataQueryService(query_repo, lambda _id: None).query("Demo")


def test_remaining_application_guard_paths(monkeypatch):
    """应用服务的空结果、回滚和匹配参数保护应保持可观测。"""
    helper = MagicMock()
    helper.get_custom_rules.return_value = []
    helper.get_rule_groups.return_value = []
    monkeypatch.setattr(filtering, "RuleHelper", lambda: helper)
    assert filtering.get_custom_rules() == []
    assert filtering.get_rule_groups() == []
    assert settings_module.SystemSettingsService._normalize_systemconfig_value([]) is None
    with pytest.raises(ValueError, match="匹配字段"):
        settings_module.SystemSettingsService._resolve_list_match(
            settings_module.SettingSpec("CUSTOM", "settings", "x", "x"),
            "upsert_list_item",
            {"value": 1},
            None,
            None,
        )
    empty_service = DownloadTaskService(
        MagicMock(return_value=[]),
        lambda _hashes: {},
        MagicMock(),
        MagicMock(),
        MagicMock(),
    )
    assert empty_service.downloading() == []


def test_command_application_facade_and_dispatch(monkeypatch):
    """命令应用门面应支持注册、查询、初始化和事件派发。"""

    class CommandRegistry:
        """提供命令门面所需的最小注册表实现。"""

        calls = []

        def get_commands(self):
            """返回一个可触发的命令定义。"""
            return {"/demo": {"description": "Demo", "pid": "plugin"}}

        def get(self, name):
            """按名称返回命令定义。"""
            return self.get_commands().get(name)

        def init_commands(self, plugin_id=None):
            """记录命令初始化范围。"""
            self.calls.append(plugin_id)

    command_application.register_command_class(CommandRegistry)
    try:
        assert isinstance(command_application.get_command_object(), CommandRegistry)
        assert command_application.get_commands()["/demo"]["description"] == "Demo"
        assert command_application.get_command("/demo")["pid"] == "plugin"
        command_application.init_commands("plugin")
        published = []
        result = command_application.dispatch_command(
            "demo --flag",
            user_id="1",
            source="test",
            publish_event=lambda event_type, payload: published.append((event_type, payload)),
        )
        assert result["command"] == "/demo --flag"
        assert result["plugin_id"] == "plugin"
        assert published == [
            (
                EventType.CommandExcute,
                {"cmd": "/demo --flag", "user": "1", "channel": None, "source": "test"},
            )
        ]
        with pytest.raises(ValueError, match="命令不能为空"):
            command_application.dispatch_command("  ", user_id="1", publish_event=lambda *_args: None)
        with pytest.raises(ValueError, match="不存在"):
            command_application.dispatch_command("/missing", user_id="1", publish_event=lambda *_args: None)
    finally:
        command_application.reset_command_class()

    with pytest.raises(RuntimeError, match="未初始化"):
        command_application.get_command_object()


def test_music_projection_and_domain_projection():
    """音乐和豆瓣投影应保留稳定身份并裁剪大字段。"""
    track = MusicInfo(media_source=MediaSource.MusicBrainz, media_id="track", title="Track", artists=["Artist"], year=2024)
    assert simplify_music_info(track)["title"] == "Track"
    album = MusicAlbumInfo(media_source=MediaSource.MusicBrainz, media_id="album", title="Album", artists=["Artist"], tracks=[track] * 3, releases=[MusicRelease(media_id="release", title="Release")])
    assert simplify_music_album(album, track_limit=2)["tracks_truncated"] is True
    artist = MusicArtistInfo(media_source=MediaSource.MusicBrainz, media_id="artist", name="Artist", raw_data={"secret": 1})
    assert simplify_music_artist(artist)["subscribable"] is False
    projected = project_douban({}, {"id": "1", "title": "Movie", "subtype": "movie", "rating": {"value": 8.5}, "pic": {"large": "poster"}})
    assert projected["poster_path"] == "poster"
    assert projected["douban_id"] == "1"
    assert project_douban({}, {}) == {}


def test_download_task_services_validate_and_delegate(monkeypatch):
    """下载任务服务应补齐历史媒体并严格校验高级修改。"""
    torrent = SimpleNamespace(hash="a" * 40, downloader="qb")
    history = SimpleNamespace(media_source=MediaSource.TMDB, media_id="1", type="电影", title="Movie", seasons="1", episodes="2", poster="p", image="b", torrent_site="site", userid="u", username="name")
    list_torrents = MagicMock(return_value=[torrent])
    service = DownloadTaskService(list_torrents, lambda _hashes: {torrent.hash: history}, MagicMock(return_value=True), MagicMock(return_value=True), MagicMock(return_value=True))
    assert service.downloading()[0].media.title == "Movie"
    assert service.set_downloading(torrent.hash, "start") is True
    assert service.set_downloading(torrent.hash, "bad") is False
    assert service.remove_downloading(torrent.hash) is True
    mutation = DownloadTaskMutationService(list_torrents=lambda **_kwargs: [torrent], set_tags=MagicMock(return_value=True), set_downloading=MagicMock(return_value=True), update_torrent=MagicMock(return_value={"limits": True, "trackers": False}))
    assert mutation.update(hash_value=torrent.hash, action="start", tags=["tag"], download_limit=1)["results"]
    with pytest.raises(ValueError, match="hash"):
        mutation.update(hash_value="bad", action="start")
    with pytest.raises(ValueError, match="至少"):
        mutation.update(hash_value=torrent.hash)
    with pytest.raises(ValueError, match="action"):
        mutation.update(hash_value=torrent.hash, action="pause")
