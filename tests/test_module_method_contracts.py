"""模块字符串方法契约清单的架构测试。"""

import json
from pathlib import Path

from app.runtime.extensions.module.contracts import (
    ModuleErrorPolicy,
    ModuleExecutionMode,
    ModuleResultAggregation,
    ModuleResultShape,
    diagnose_module_callable,
    diagnose_module_result,
    get_module_method_contract,
    is_explicit_module_method,
    list_explicit_module_contracts,
)

RUNTIME_BASELINE = Path(__file__).parent / "fixtures" / "architecture" / "runtime-contract-baseline.json"


def test_all_scanned_host_module_methods_have_explicit_v2_contracts() -> None:
    """架构快照中的全部宿主字符串方法都必须显式登记 V2 契约。"""
    payload = json.loads(RUNTIME_BASELINE.read_text(encoding="utf-8"))
    methods = set(payload["run_module"]["methods"])
    contracts = list_explicit_module_contracts()

    assert methods
    assert methods <= contracts.keys()
    for method in methods:
        contract = contracts[method]
        assert isinstance(contract.aggregation, ModuleResultAggregation)
        assert contract.input_contract != "legacy_args"
        assert contract.result_contract != "Any"
        assert isinstance(contract.plugin_short_circuit, bool)


def test_high_frequency_capability_families_are_explicit() -> None:
    """媒体发现、识别、存储和消息族不能退回未分类 legacy 契约。"""
    expected_families = {
        "async_tmdb_discover": "tmdb",
        "async_douban_discover": "douban",
        "bangumi_info": "bangumi",
        "anilist_info": "anilist",
        "recognize_media": "media-recognition",
        "mediaserver_items": "media-server",
        "list_files": "storage",
        "finalize_message": "messaging",
        "scheduler_job": "scheduling",
    }

    for method, family in expected_families.items():
        assert is_explicit_module_method(method)
        assert get_module_method_contract(method).family == family


def test_unknown_plugin_method_keeps_legacy_compatibility() -> None:
    """第三方插件自定义方法仍应落入开放的 legacy 调度协议。"""
    contract = get_module_method_contract("third_party_custom_method")

    assert contract.family == "legacy"
    assert contract.supports_sync is True
    assert contract.supports_async is True


def test_contract_v2_freezes_every_observed_host_method() -> None:
    """全部已观察宿主能力必须具备可生成文档和诊断的完整 V2 字段。"""
    contracts = list_explicit_module_contracts()

    assert len(contracts) >= 211
    host_internal_methods = {"plan_transfer", "execute_transfer_plan"}
    for method_name, contract in contracts.items():
        assert contract.version == 1
        assert contract.input_contract != "legacy_args"
        assert contract.result_contract
        assert contract.execution is ModuleExecutionMode.SYNC_OR_ASYNC
        assert contract.timeout_policy == "caller_budget"
        assert contract.error_policy is ModuleErrorPolicy.ISOLATE_PROVIDER
        assert contract.public_to_plugins is (method_name not in host_internal_methods)


def test_signature_diagnostics_do_not_reject_legacy_callable() -> None:
    """无法检查的旧插件 callable 只产生诊断，仍由 dispatcher 决定是否执行。"""

    class _OpaqueCallable:
        """模拟 inspect 无法解析签名的第三方 callable。"""

        @property
        def __signature__(self):
            """模拟扩展对象不提供 Python signature。"""
            raise ValueError("opaque")

        def __call__(self):
            """保留可调用行为。"""
            return "ok"

    assert diagnose_module_callable("recognize_media", _OpaqueCallable()) == ("signature-unavailable",)
    assert _OpaqueCallable()() == "ok"


def test_signature_diagnostics_report_missing_contract_parameters() -> None:
    """显式 Contract 应能指出 provider 遗漏的宿主调用参数。"""

    def incomplete_storage_provider(fileitem):
        """模拟仍未接受 recursion 参数的旧存储 provider。"""
        return [fileitem]

    assert diagnose_module_callable("list_files", incomplete_storage_provider) == ("missing-parameter:recursion",)


def test_signature_diagnostics_accept_keyword_compatibility_provider() -> None:
    """带 **kwargs 的第三方 provider 继续兼容逐步扩展的输入契约。"""

    def compatible_provider(**kwargs):
        """模拟通过关键字参数保持前向兼容的第三方 provider。"""
        return kwargs

    assert diagnose_module_callable("snapshot_storage", compatible_provider) == ()


def test_result_diagnostics_check_only_enabled_basic_shapes() -> None:
    """高频方法检查基础结果形状，业务对象合同仍留给逐族适配器。"""
    assert get_module_method_contract("list_files").result_shape is ModuleResultShape.LIST
    assert diagnose_module_result("list_files", [object()]) == ()
    assert diagnose_module_result("list_files", None) == ()
    assert diagnose_module_result("list_files", "legacy-value") == ("unexpected-result:list:str",)
    assert diagnose_module_result("mediaserver_play_url", "https://example.test") == ()
    assert diagnose_module_result("mediaserver_play_url", 7) == ("unexpected-result:string:int",)


def test_message_attachment_contracts_use_messaging_family() -> None:
    """消息附件下载能力不得因 download 前缀误归入下载器能力族。"""
    expected_parameters = {
        "download_telegram_file_bytes": ("file_id", "source"),
        "download_wechat_media_bytes": ("media_ref", "source"),
        "download_slack_file_to_data_url": ("file_url", "source"),
        "download_feishu_image_to_data_url": ("image_ref", "source"),
    }

    for method, parameters in expected_parameters.items():
        contract = get_module_method_contract(method)
        assert contract.family == "messaging"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.required_parameters == parameters


def test_message_route_contracts_use_first_matching_provider() -> None:
    """消息路由能力应保留渠道筛选语义，并停止依赖 legacy 结果接力。"""
    expected_shapes = {
        "channel_manage": ModuleResultShape.MAPPING,
        "delete_message": ModuleResultShape.BOOLEAN,
        "edit_message": ModuleResultShape.BOOLEAN,
        "mark_message_processing_started": ModuleResultShape.MAPPING,
        "mark_message_processing_finished": ModuleResultShape.BOOLEAN,
        "message_parser": ModuleResultShape.ANY,
        "send_direct_message": ModuleResultShape.ANY,
    }

    for method, result_shape in expected_shapes.items():
        contract = get_module_method_contract(method)
        assert contract.family == "messaging"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.result_shape is result_shape
        assert contract.required_parameters


def test_refresh_and_tmdb_cache_contracts_match_host_result_shapes() -> None:
    """刷新种子与 TMDB 缓存入口应声明列表合并或目标值路由语义。"""
    refresh = get_module_method_contract("refresh_torrents")
    async_refresh = get_module_method_contract("async_refresh_torrents")
    cache_items = get_module_method_contract("tmdb_cache_items")
    cache_delete = get_module_method_contract("tmdb_cache_delete")

    assert refresh is async_refresh
    assert refresh.family == "downloader"
    assert refresh.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
    assert refresh.result_shape is ModuleResultShape.LIST
    assert cache_items.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
    assert cache_items.result_shape is ModuleResultShape.LIST
    assert cache_delete.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
    assert cache_delete.result_shape is ModuleResultShape.MAPPING


def test_downloader_query_contracts_merge_provider_lists() -> None:
    """下载器查询能力应显式合并各 provider 的有序列表结果。"""
    for method in ("list_torrents", "downloader_info"):
        contract = get_module_method_contract(method)
        assert contract.family == "downloader"
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST


def test_media_server_contracts_distinguish_streams_lists_and_scalar_routes() -> None:
    """媒体服务器能力应按真实返回形状选择合并或目标路由语义。"""
    list_methods = {
        "media_statistic",
        "mediaserver_latest",
        "mediaserver_latest_images",
        "mediaserver_librarys",
        "mediaserver_playing",
    }
    scalar_methods = {
        "media_exists",
        "mediaserver_image_cookies",
        "mediaserver_items_count",
        "mediaserver_season_episode_ids",
    }

    for method in list_methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST
    for method in scalar_methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY

    items = get_module_method_contract("mediaserver_items")
    assert items.result_contract == "Iterable[MediaServerItem] | None"
    assert items.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
    assert items.result_shape is ModuleResultShape.ANY


def test_storage_value_contracts_use_explicit_provider_semantics() -> None:
    """存储值查询和动作应停止 legacy 接力，仅文件清单合并 provider 结果。"""
    for method in ("any_files", "create_folder", "delete_file", "transfer"):
        contract = get_module_method_contract(method)
        assert contract.family == "storage"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.required_parameters

    media_files = get_module_method_contract("media_files")
    assert media_files.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
    assert media_files.result_shape is ModuleResultShape.LIST
    assert media_files.required_parameters == ("mediainfo",)


def test_lookup_contracts_separate_value_routes_from_list_aggregation() -> None:
    """站点、元数据、认证与 TVDB 查询应声明真实的值或列表语义。"""
    list_methods = {"site_subtitle_links", "search_tvdb"}
    value_methods = {
        "get_search_page_size",
        "refresh_userdata",
        "metadata_img",
        "metadata_nfo",
        "obtain_specific_image",
        "recommend_name",
        "user_authenticate",
        "tvdb_info",
        "tvdb_slug",
    }

    for method in list_methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST
    for method in value_methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.result_contract


def test_legacy_category_module_methods_are_not_public_contracts() -> None:
    """旧 YAML 分类读写和求值方法不得继续成为宿主模块协议。"""
    contracts = list_explicit_module_contracts()

    assert {
        "media_category",
        "load_category_config",
        "save_category_config",
    }.isdisjoint(contracts)


def test_recognition_match_and_cache_contracts_share_sync_async_semantics() -> None:
    """媒体匹配与缓存回填的同步、异步入口必须复用同一目标路由契约。"""
    pairs = {
        "match_doubaninfo": "async_match_doubaninfo",
        "match_tmdbinfo": "async_match_tmdbinfo",
        "update_recognize_cache": "async_update_recognize_cache",
    }

    for sync_method, async_method in pairs.items():
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(async_method)
        assert contract.family == "media-recognition"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.required_parameters


def test_media_auxiliary_contract_merges_every_enabled_provider() -> None:
    """附加信息能力应合并全部 provider 列表，不能被首个插件结果短路。"""
    contract = get_module_method_contract("get_media_auxiliary_info")

    assert contract is get_module_method_contract("async_get_media_auxiliary_info")
    assert contract.family == "media-recognition"
    assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
    assert contract.result_shape is ModuleResultShape.LIST
    assert contract.plugin_short_circuit is False
    assert contract.required_parameters == ("mediainfo", "media_source", "metainfo")


def test_classification_enrichment_contract_is_sync_fan_out() -> None:
    """缺失事实 provider 使用同步方法，由专用服务负责并发和结果隔离。"""
    contract = get_module_method_contract("get_media_classification_facts")

    assert contract.family == "media-classification"
    assert contract.aggregation is ModuleResultAggregation.FAN_OUT
    assert contract.result_shape is ModuleResultShape.MAPPING
    assert contract.required_parameters == ("request",)
    assert contract.supports_sync is True
    assert contract.supports_async is False
    assert contract.plugin_short_circuit is False


def test_torrent_filter_contract_preserves_original_argument_list_merge() -> None:
    """种子过滤 provider 应接收原始参数并有序合并结果，不得误用单参数接力。"""
    contract = get_module_method_contract("filter_torrents")

    assert contract.family == "downloader"
    assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
    assert contract.result_shape is ModuleResultShape.LIST
    assert contract.required_parameters == (
        "rule_groups",
        "torrent_list",
        "mediainfo",
    )


def test_side_effect_hooks_use_non_short_circuiting_fan_out_contracts() -> None:
    """副作用钩子必须执行全部 provider，不能被任意返回值提前截断。"""
    methods = {
        "clear_cache",
        "download_added",
        "music_cache_clear",
        "register_commands",
        "scheduler_job",
        "tmdb_cache_clear",
        "transfer_completed",
    }

    for method in methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.FAN_OUT
        assert contract.result_contract == "None"
        assert contract.plugin_short_circuit is False


def test_torrent_files_uses_normalized_first_provider_result() -> None:
    """下载器文件项归一后应按目标 provider 返回宿主 DTO 列表。"""
    contract = get_module_method_contract("torrent_files")

    assert contract.required_parameters == ("tid", "downloader")
    assert contract.result_contract == "list[DownloaderFile]"
    assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
    assert contract.result_shape is ModuleResultShape.LIST


def test_torrent_tracker_contract_merges_downloader_mappings() -> None:
    """Tracker 查询应登记跨下载器有序映射合并，而不是首个字典短路。"""
    contract = get_module_method_contract("get_torrent_trackers")

    assert contract.required_parameters == ("hash_string", "downloader")
    assert contract.aggregation is ModuleResultAggregation.ORDERED_MAPPING_MERGE
    assert contract.result_shape is ModuleResultShape.MAPPING


def test_downloader_action_contracts_freeze_shared_provider_signatures() -> None:
    """三种宿主下载器的目标选择动作应使用一致的首个非空契约。"""
    expected_parameters = {
        "download": (
            "content",
            "download_dir",
            "cookie",
            "episodes",
            "category",
            "label",
            "downloader",
        ),
        "remove_torrents": ("hashs", "delete_file", "downloader"),
        "set_torrents_tag": ("hashs", "tags", "downloader"),
        "start_torrents": ("hashs", "downloader"),
        "stop_torrents": ("hashs", "downloader"),
        "update_torrent": (
            "hash_string",
            "downloader",
            "download_limit",
            "upload_limit",
            "tracker_list",
            "save_path",
            "category",
            "ratio_limit",
            "seeding_time_limit",
        ),
    }

    for method, parameters in expected_parameters.items():
        contract = get_module_method_contract(method)
        assert contract.family == "downloader"
        assert contract.aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
        assert contract.required_parameters == parameters


def test_sync_and_async_media_capabilities_share_contracts() -> None:
    """同一识别能力的同步与异步入口必须复用完全相同的契约。"""
    for sync_method, async_method in (
        ("recognize_media", "async_recognize_media"),
        ("search_medias", "async_search_medias"),
        ("obtain_images", "async_obtain_images"),
    ):
        assert get_module_method_contract(sync_method) is get_module_method_contract(async_method)

    assert get_module_method_contract("obtain_images").aggregation is ModuleResultAggregation.PIPELINE_RELAY


def test_sync_and_async_discovery_lists_share_contracts() -> None:
    """媒体榜单与搜索的同步、异步入口必须共享有序列表契约。"""
    sync_methods = (
        "movie_hot",
        "movie_showing",
        "movie_top250",
        "search_collections",
        "search_persons",
        "search_subtitles",
        "search_torrents",
        "tv_animation",
        "tv_hot",
        "tv_weekly_chinese",
        "tv_weekly_global",
    )

    for sync_method in sync_methods:
        async_method = f"async_{sync_method}"
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(async_method)
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST
        assert contract.required_parameters


def test_sync_and_async_douban_capabilities_share_contracts() -> None:
    """Douban 同步与异步 provider 应共享参数和结果聚合契约。"""
    list_methods = {
        "douban_discover",
        "douban_movie_credits",
        "douban_movie_recommend",
        "douban_person_credits",
        "douban_tv_credits",
        "douban_tv_recommend",
    }
    value_methods = {"douban_info", "douban_person_detail"}

    for sync_method in list_methods | value_methods:
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(f"async_{sync_method}")
        assert contract.required_parameters
        expected = (
            ModuleResultAggregation.ORDERED_LIST_MERGE
            if sync_method in list_methods
            else ModuleResultAggregation.FIRST_NON_EMPTY
        )
        assert contract.aggregation is expected


def test_sync_and_async_bangumi_capabilities_share_contracts() -> None:
    """Bangumi 同步与异步 provider 应共享列表或首值契约。"""
    list_methods = {
        "bangumi_calendar",
        "bangumi_credits",
        "bangumi_discover",
        "bangumi_person_credits",
        "bangumi_recommend",
    }
    value_methods = {"bangumi_info", "bangumi_person_detail"}

    for sync_method in list_methods | value_methods:
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(f"async_{sync_method}")
        expected = (
            ModuleResultAggregation.ORDERED_LIST_MERGE
            if sync_method in list_methods
            else ModuleResultAggregation.FIRST_NON_EMPTY
        )
        assert contract.aggregation is expected


def test_sync_and_async_anilist_capabilities_share_contracts() -> None:
    """AniList 同步与异步 provider 应共享列表或首值契约。"""
    list_methods = {
        "anilist_credits",
        "anilist_discover",
        "anilist_person_credits",
        "anilist_popular_this_season",
        "anilist_recommendations",
        "anilist_trending",
    }
    value_methods = {"anilist_info", "anilist_person_detail"}

    for sync_method in list_methods | value_methods:
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(f"async_{sync_method}")
        expected = (
            ModuleResultAggregation.ORDERED_LIST_MERGE
            if sync_method in list_methods
            else ModuleResultAggregation.FIRST_NON_EMPTY
        )
        assert contract.aggregation is expected


def test_sync_and_async_tmdb_capabilities_share_contracts() -> None:
    """TMDB 同步与异步查询应共享列表或首值契约。"""
    list_methods = {
        "tmdb_collection",
        "tmdb_discover",
        "tmdb_episodes",
        "tmdb_group_seasons",
        "tmdb_movie_credits",
        "tmdb_movie_recommend",
        "tmdb_movie_similar",
        "tmdb_person_credits",
        "tmdb_seasons",
        "tmdb_trending",
        "tmdb_tv_credits",
        "tmdb_tv_recommend",
        "tmdb_tv_similar",
    }
    value_methods = {"tmdb_info", "tmdb_person_detail"}

    for sync_method in list_methods | value_methods:
        contract = get_module_method_contract(sync_method)
        assert contract is get_module_method_contract(f"async_{sync_method}")
        expected = (
            ModuleResultAggregation.ORDERED_LIST_MERGE
            if sync_method in list_methods
            else ModuleResultAggregation.FIRST_NON_EMPTY
        )
        assert contract.aggregation is expected
        assert contract.required_parameters


def test_music_capabilities_distinguish_lists_values_and_async_aliases() -> None:
    """音乐查询应声明真实聚合，独立异步方法名必须复用同步契约。"""
    list_methods = {
        "music_album_related",
        "music_artist_albums",
        "music_artist_related",
        "music_cache_items",
        "music_chart",
        "music_discover",
        "music_fresh_releases",
        "search_music",
    }
    value_methods = {
        "identify_music_by_fingerprint",
        "match_music_album",
        "music_album",
        "music_artist",
        "music_cache_delete",
        "music_lyrics",
    }

    for method in list_methods:
        contract = get_module_method_contract(method)
        assert contract.aggregation is ModuleResultAggregation.ORDERED_LIST_MERGE
        assert contract.result_shape is ModuleResultShape.LIST
    for method in value_methods:
        assert get_module_method_contract(method).aggregation is ModuleResultAggregation.FIRST_NON_EMPTY
    for sync_method in ("identify_music_by_fingerprint", "match_music_album"):
        assert get_module_method_contract(sync_method) is get_module_method_contract(f"async_{sync_method}")


def test_attachment_result_diagnostics_distinguish_bytes_and_strings() -> None:
    """附件契约应区分二进制内容和可展示字符串，偏差仍仅供诊断。"""
    assert diagnose_module_result("download_qq_file_bytes", b"content") == ()
    assert diagnose_module_result("download_qq_file_bytes", "content") == ("unexpected-result:bytes:str",)
    assert (
        diagnose_module_result(
            "download_wechat_image_to_data_url",
            "data:image/png;base64,AA==",
        )
        == ()
    )


def test_unknown_plugin_result_keeps_unchecked_legacy_compatibility() -> None:
    """未知第三方方法的任意返回值继续不做结果形状诊断。"""
    assert diagnose_module_result("third_party_custom_method", object()) == ()
