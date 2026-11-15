"""覆盖媒体服务器搜索在 Chain 与各服务端适配器之间的分发契约。"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain.mediaserver import MediaServerChain
from app.modules.ugreen.module import UgreenModule
from app.modules.vyo.module import VyoModule
from app.modules.vyo.vyo import Vyo, _SearchInterrupted
from app.modules.zspace import ZSpaceModule
from app.schemas.mediaserver import MediaServerPlayItem, MediaServerQueryOptions
from app.schemas.types import MediaType


@pytest.mark.parametrize("module_class", [UgreenModule, VyoModule, ZSpaceModule])
def test_provider_modules_tag_search_results_with_config_name(module_class, monkeypatch):
    """各媒体服务模块将实例搜索结果标记为对应的配置名称。"""
    module = object.__new__(module_class)
    first_item = MediaServerPlayItem(item_id="first", type=MediaType.TV.value)
    second_item = MediaServerPlayItem(item_id="second", type=MediaType.MUSIC.value)
    first_server = Mock(search_library=Mock(return_value=[first_item]))
    second_server = Mock(search_library=Mock(return_value=[second_item]))
    monkeypatch.setattr(
        module_class,
        "get_instances",
        lambda _self: {"Server A": first_server, "Server B": second_server},
    )

    results = module.mediaserver_search("keyword", [MediaType.TV.value], 12)

    assert [(item.server, item.item_id) for item in results] == [
        ("Server A", "first"),
        ("Server B", "second"),
    ]
    first_server.search_library.assert_called_once_with("keyword", [MediaType.TV.value], 12)
    second_server.search_library.assert_called_once_with("keyword", [MediaType.TV.value], 12)


def test_find_item_skips_stale_ids_and_finds_matching_enabled_server(monkeypatch):
    """跨服务器查找会跳过失效条目，并返回首个 ID 精确匹配的实例。"""
    chain = object.__new__(MediaServerChain)
    configs = [SimpleNamespace(name=""), SimpleNamespace(name="Server A"), SimpleNamespace(name="Server B")]
    monkeypatch.setattr("app.chain.mediaserver.get_mediaserver_configs", lambda: configs)
    matched_item = SimpleNamespace(item_id=None, id="wanted")
    lookup = Mock(
        side_effect=[
            SimpleNamespace(item_id="stale", id="other"),
            matched_item,
        ]
    )
    monkeypatch.setattr(chain, "iteminfo", lookup)

    assert chain.find_item("", query=MediaServerQueryOptions(fields=frozenset())) is None
    result = chain.find_item("wanted")

    assert result == ("Server B", matched_item)
    assert [call.kwargs for call in lookup.call_args_list] == [
        {"server": "Server A", "item_id": "wanted", "query": None},
        {"server": "Server B", "item_id": "wanted", "query": None},
    ]


def test_find_item_honors_explicit_server_and_query(monkeypatch):
    """显式服务器查找只访问指定实例，并将投影参数传给查询。"""
    chain = object.__new__(MediaServerChain)
    query = MediaServerQueryOptions(fields=frozenset())
    item = SimpleNamespace(item_id=None, id="wanted")
    lookup = Mock(return_value=item)
    monkeypatch.setattr(chain, "iteminfo", lookup)
    monkeypatch.setattr(
        "app.chain.mediaserver.get_mediaserver_configs",
        Mock(side_effect=AssertionError("explicit server should skip config lookup")),
    )

    assert chain.find_item("wanted", server="Server A", query=query) == ("Server A", item)
    lookup.assert_called_once_with(server="Server A", item_id="wanted", query=query)


def test_find_item_returns_none_when_no_server_matches(monkeypatch):
    """所有已启用服务器都未返回同一条目标时返回空结果。"""
    chain = object.__new__(MediaServerChain)
    monkeypatch.setattr(
        "app.chain.mediaserver.get_mediaserver_configs",
        lambda: [SimpleNamespace(name="Server A")],
    )
    monkeypatch.setattr(
        chain,
        "iteminfo",
        lambda **_kwargs: SimpleNamespace(item_id="stale", id="other"),
    )

    assert chain.find_item("wanted") is None


def test_search_library_handles_empty_results_defaults_and_limit_bounds(monkeypatch):
    """搜索会跳过空关键字、填充默认类型并限制适配器请求数量。"""
    chain = object.__new__(MediaServerChain)
    missing_id = MediaServerPlayItem(type=MediaType.TV.value)
    run_module = Mock(side_effect=[None, [missing_id]])
    monkeypatch.setattr(chain, "run_module", run_module)

    assert chain.search_library("  ", limit=20) == []
    assert chain.search_library(" keyword ", types=[], limit=0) == []
    assert chain.search_library("keyword", types=[MediaType.TV.value], limit=100) == []

    assert [call.kwargs for call in run_module.call_args_list] == [
        {"keyword": "keyword", "types": [MediaType.TV.value, MediaType.MUSIC.value], "limit": 1},
        {"keyword": "keyword", "types": [MediaType.TV.value], "limit": 50},
    ]
    assert all(call.args == ("mediaserver_search",) for call in run_module.call_args_list)


def test_search_library_signs_deduplicates_and_limits_results(monkeypatch):
    """搜索结果按服务器和条目标识去重、签名图片并遵守返回上限。"""
    chain = object.__new__(MediaServerChain)
    first_item = MediaServerPlayItem(
        item_id="same", server="Server A", type=MediaType.TV.value, image="poster-a"
    )
    duplicate_item = MediaServerPlayItem(
        item_id="same", server="Server A", type=MediaType.TV.value, image="duplicate-poster"
    )
    second_item = MediaServerPlayItem(
        item_id="music", server="Server A", type=MediaType.MUSIC.value, image="poster-b"
    )
    other_server_item = MediaServerPlayItem(
        item_id="same", server="Server B", type=MediaType.TV.value, image="poster-c"
    )
    invalid_id_item = MediaServerPlayItem(type=MediaType.TV.value)
    invalid_type_item = MediaServerPlayItem(
        item_id="movie", server="Server A", type=MediaType.MOVIE.value
    )
    run_module = Mock(
        return_value=[
            first_item,
            duplicate_item,
            second_item,
            other_server_item,
            invalid_id_item,
            invalid_type_item,
        ]
    )
    sign_image = Mock(side_effect=lambda image: f"signed:{image}")
    monkeypatch.setattr(chain, "run_module", run_module)
    monkeypatch.setattr(chain, "_sign_image_url", sign_image)

    results = chain.search_library("  keyword  ", limit=2)

    assert results == [first_item, second_item]
    assert [item.image for item in results] == ["signed:poster-a", "signed:poster-b"]
    assert [call.args for call in sign_image.call_args_list] == [
        ("poster-a",),
        ("poster-b",),
        ("poster-c",),
    ]
    run_module.assert_called_once_with(
        "mediaserver_search",
        keyword="keyword",
        types=[MediaType.TV.value, MediaType.MUSIC.value],
        limit=2,
    )


def test_vyo_library_search_filters_items_and_maps_playable_results(monkeypatch):
    """Vyo 仅返回有效剧集，并将年份、海报地址和限制映射到统一模型。"""
    client = Vyo.__new__(Vyo)
    client._api = Mock()
    client._api.image_url.side_effect = lambda item_id, _kind: f"https://images.test/{item_id}"
    monkeypatch.setattr(client, "is_configured", lambda: True)
    rows = ["missing", "missing-id", "missing-title", "movie", "series-year", "series-no-year"]
    monkeypatch.setattr(client, "_Vyo__search_rows", Mock(return_value=rows))
    monkeypatch.setattr(
        client,
        "_Vyo__format_item_info",
        Mock(
            side_effect=[
                None,
                SimpleNamespace(item_id=None, title="No ID", item_type="Series", year=2020),
                SimpleNamespace(item_id="no-title", title="", item_type="Series", year=2020),
                SimpleNamespace(item_id="movie", title="Movie", item_type="Movie", year=2020),
                SimpleNamespace(item_id="series-1", title="Series 1", item_type="Series", year=2024),
                SimpleNamespace(item_id="series-2", title="Series 2", item_type="Series", year=None),
            ]
        ),
    )

    results = client.search_library("  Series  ", [MediaType.TV.value], 2)

    assert [(item.item_id, item.title, item.subtitle, item.year) for item in results] == [
        ("series-1", "Series 1", "2024", 2024),
        ("series-2", "Series 2", None, None),
    ]
    assert [item.image for item in results] == [
        "https://images.test/series-1",
        "https://images.test/series-2",
    ]
    client._Vyo__search_rows.assert_called_once_with(keyword="Series", kinds="Series")


def test_vyo_library_search_returns_empty_for_unsupported_or_unavailable_queries(monkeypatch):
    """不支持的类型、空关键字、未配置客户端和搜索失败都返回空列表。"""
    client = Vyo.__new__(Vyo)
    client._api = Mock()
    configured = Mock(side_effect=[False, True])
    rows = Mock(return_value=None)
    monkeypatch.setattr(client, "is_configured", configured)
    monkeypatch.setattr(client, "_Vyo__search_rows", rows)

    assert client.search_library("Series", [MediaType.MUSIC.value], 5) == []
    assert client.search_library("  ", [MediaType.TV.value], 5) == []
    assert client.search_library("Series", [MediaType.TV.value], 5) == []
    assert client.search_library("Series", [MediaType.TV.value], 5) == []

    assert configured.call_count == 2
    rows.assert_called_once_with(keyword="Series", kinds="Series")


def test_vyo_library_search_discards_partial_results_after_page_failure(monkeypatch):
    """Vyo 后续页中断时不把不完整的搜索结果交给播放器。"""
    client = Vyo.__new__(Vyo)
    client._api = Mock()
    client._api.image_url.return_value = "https://images.test/series-1"
    monkeypatch.setattr(client, "is_configured", lambda: True)

    def interrupted_rows():
        """生成一条结果后模拟后续页请求失败。"""
        yield "first"
        raise _SearchInterrupted

    monkeypatch.setattr(client, "_Vyo__search_rows", lambda **_kwargs: interrupted_rows())
    monkeypatch.setattr(
        client,
        "_Vyo__format_item_info",
        lambda _row: SimpleNamespace(
            item_id="series-1", title="Series 1", item_type="Series", year=2024
        ),
    )

    assert client.search_library("Series", [MediaType.TV.value], 5) == []
