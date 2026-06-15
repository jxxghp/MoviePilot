from unittest.mock import patch

from app.modules.zspace.zspace import ZSpace


class _FakeResponse:
    """模拟极影视 HTTP 响应。"""

    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def json(self) -> dict:
        """返回模拟的 JSON 数据。"""
        return self._payload


def _build_client() -> ZSpace:
    """构造不触发登录流程的极影视客户端。"""
    client = ZSpace.__new__(ZSpace)
    client._host = "http://zspace.local/"
    client._apikey = "zspace-token"
    client.user = "user-id"
    return client


def test_get_items_fetches_all_recursive_pages() -> None:
    """全量同步应递归查询并持续翻页，且忽略非媒体条目。"""
    client = _build_client()
    responses = [
        _FakeResponse({
            "Items": [
                {
                    "Id": "movie-1",
                    "Type": "Movie",
                    "Name": "电影一",
                    "ProductionYear": 2025,
                    "ProviderIds": {"Tmdb": "101"},
                    "Path": "/media/movie-1.mkv",
                },
                {
                    "Id": "audio-1",
                    "Type": "Audio",
                    "Name": "音频一",
                },
            ],
            "TotalRecordCount": 3,
        }),
        _FakeResponse({
            "Items": [
                {
                    "Id": "series-1",
                    "Type": "Series",
                    "Name": "剧集一",
                    "ProductionYear": 2024,
                    "ProviderIds": {"Tmdb": "202"},
                    "Path": "/media/series-1",
                },
            ],
            "TotalRecordCount": 3,
        }),
    ]

    with patch("app.modules.zspace.zspace.DEFAULT_ITEMS_PAGE_SIZE", 2), patch(
        "app.modules.zspace.zspace.RequestUtils"
    ) as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = responses
        items = list(client.get_items(parent="library-id"))

    assert [item.item_id for item in items] == ["movie-1", "series-1"]
    calls = request_utils_cls.return_value.get_res.call_args_list
    assert calls[0].kwargs["params"]["Recursive"] == "true"
    assert calls[0].kwargs["params"]["StartIndex"] == 0
    assert calls[0].kwargs["params"]["Limit"] == 2
    assert calls[1].kwargs["params"]["StartIndex"] == 2
    assert calls[1].kwargs["params"]["Limit"] == 2


def test_get_items_expands_boxset_movies() -> None:
    """电影合集应向下查询并返回全部子电影。"""
    client = _build_client()
    responses = [
        _FakeResponse({
            "Items": [{
                "Id": "collection-1",
                "Type": "BoxSet",
                "Name": "疯狂动物城（系列）",
            }],
            "TotalRecordCount": 1,
        }),
        _FakeResponse({
            "Items": [
                {
                    "Id": "movie-1",
                    "Type": "Movie",
                    "Name": "疯狂动物城",
                    "ProductionYear": None,
                    "ProviderIds": {},
                    "Path": "",
                },
                {
                    "Id": "movie-2",
                    "Type": "Movie",
                    "Name": "疯狂动物城2",
                    "ProductionYear": 2025,
                    "ProviderIds": {"Tmdb": "1084242"},
                    "Path": "/media/疯狂动物城2.mkv",
                },
            ],
            "TotalRecordCount": 2,
        }),
        _FakeResponse({
            "Id": "movie-1",
            "ParentId": "collection-1",
            "Type": "Movie",
            "Name": "疯狂动物城",
            "ProductionYear": 2016,
            "ProviderIds": {"Tmdb": "269149"},
            "Path": "/media/疯狂动物城.mkv",
        }),
    ]

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = responses
        items = list(client.get_items(parent="library-id"))

    assert [item.item_id for item in items] == ["movie-1", "movie-2"]
    assert [item.tmdbid for item in items] == [269149, 1084242]
    calls = request_utils_cls.return_value.get_res.call_args_list
    assert calls[0].kwargs["params"]["Recursive"] == "true"
    assert calls[1].kwargs["params"]["ParentId"] == "collection-1"


def test_get_items_expands_boxset_series() -> None:
    """电视剧合集应向下展开并返回全部子 Series。"""
    client = _build_client()
    responses = [
        _FakeResponse({
            "Items": [{
                "Id": "collection-1",
                "Type": "BoxSet",
                "Name": "绝命毒师",
            }],
            "TotalRecordCount": 1,
        }),
        _FakeResponse({
            "Items": [
                {
                    "Id": "series-1",
                    "Type": "Series",
                    "Name": "绝命毒师 第 1 季",
                    "ProductionYear": 2008,
                    "ProviderIds": {"Tmdb": "1396"},
                    "Path": "/media/绝命毒师/Season 1",
                },
                {
                    "Id": "series-2",
                    "Type": "Series",
                    "Name": "绝命毒师 第 2 季",
                    "ProductionYear": 2009,
                    "ProviderIds": {"Tmdb": "1396"},
                    "Path": "/media/绝命毒师/Season 2",
                },
            ],
            "TotalRecordCount": 2,
        }),
    ]

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = responses
        items = list(client.get_items(parent="library-id"))

    assert [item.item_id for item in items] == ["series-1", "series-2"]
    assert [item.tmdbid for item in items] == [1396, 1396]


def test_get_items_loads_detail_when_list_metadata_is_incomplete() -> None:
    """列表项缺少关键元数据时应使用详情接口补全。"""
    client = _build_client()
    responses = [
        _FakeResponse({
            "Items": [
                {
                    "Id": "movie-1",
                    "Type": "Movie",
                    "Name": "疯狂动物城",
                    "ProductionYear": None,
                    "ProviderIds": {},
                    "Path": "",
                }
            ],
            "TotalRecordCount": 1,
        }),
        _FakeResponse({
            "Id": "movie-1",
            "ParentId": "library-id",
            "Type": "Movie",
            "Name": "疯狂动物城",
            "OriginalTitle": "Zootopia",
            "ProductionYear": 2016,
            "ProviderIds": {
                "Tmdb": "269149",
                "Imdb": "tt2948356",
            },
            "Path": "/media/疯狂动物城 (2016)/疯狂动物城.mkv",
        }),
    ]

    with patch("app.modules.zspace.zspace.RequestUtils") as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = responses
        items = list(client.get_items(parent="library-id"))

    assert len(items) == 1
    assert items[0].item_id == "movie-1"
    assert items[0].tmdbid == 269149
    assert items[0].imdbid == "tt2948356"
    assert items[0].year == 2016
    assert items[0].path.endswith("疯狂动物城.mkv")
    assert request_utils_cls.return_value.get_res.call_args_list[1].args[0] == (
        "http://zspace.local/emby/Users/user-id/Items/movie-1"
    )


def test_get_items_uses_total_count_when_server_returns_short_pages() -> None:
    """服务端单页少于请求数量时应以总记录数决定是否继续翻页。"""
    client = _build_client()
    responses = [
        _FakeResponse({
            "Items": [
                {
                    "Id": "movie-1",
                    "Type": "Movie",
                    "Name": "电影一",
                    "ProductionYear": 2025,
                    "ProviderIds": {"Tmdb": "101"},
                    "Path": "/media/movie-1.mkv",
                }
            ],
            "TotalRecordCount": 2,
        }),
        _FakeResponse({
            "Items": [
                {
                    "Id": "movie-2",
                    "Type": "Movie",
                    "Name": "电影二",
                    "ProductionYear": 2024,
                    "ProviderIds": {"Tmdb": "102"},
                    "Path": "/media/movie-2.mkv",
                }
            ],
            "TotalRecordCount": 2,
        }),
    ]

    with patch("app.modules.zspace.zspace.DEFAULT_ITEMS_PAGE_SIZE", 100), patch(
        "app.modules.zspace.zspace.RequestUtils"
    ) as request_utils_cls:
        request_utils_cls.return_value.get_res.side_effect = responses
        items = list(client.get_items(parent="library-id"))

    assert [item.item_id for item in items] == ["movie-1", "movie-2"]
    calls = request_utils_cls.return_value.get_res.call_args_list
    assert calls[0].kwargs["params"]["StartIndex"] == 0
    assert calls[1].kwargs["params"]["StartIndex"] == 1
