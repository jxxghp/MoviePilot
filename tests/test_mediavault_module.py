"""MediaVault 自建媒体库客户端的行为契约，全部走假 API 不发真实请求。"""

from pathlib import Path
from typing import Optional

import pytest

from app.modules.mediavault.api import Api, Result
from app.modules.mediavault.mediavault import MediaVault
from app.schemas.mediaserver import RefreshMediaItem
from app.schemas.types import MediaSource, MediaType


class _FakeApi:
    """按路径返回预置数据，并记录调用参数。"""

    def __init__(self, routes: dict, host: str = "http://mv.local"):
        self.routes = routes
        self.calls = []
        self.closed = False
        self._host = host

    @property
    def configured(self) -> bool:
        return True

    def close(self) -> None:
        self.closed = True

    def image_url(self, item_id: str, image_type: str, host: Optional[str] = None) -> str:
        # 复用真实实现，避免假 Api 自行拼装掩盖「URL 不得携带凭据」这一约束
        return Api(host=host or self._host, apikey="k").image_url(item_id, image_type)

    def request(self, api, method=None, params=None, data=None, base_path=None, suppress_log=False):
        self.calls.append({"api": api, "method": method, "params": params or {}, "data": data,
                           "base_path": base_path})
        handler = self.routes.get(api)
        if handler is None:
            return Result(False, None, "not found", 404)
        return handler(params or {}, data) if callable(handler) else handler


def _client(routes: dict, **kwargs) -> MediaVault:
    """构造一个绕过网络探测的客户端。"""
    client = MediaVault.__new__(MediaVault)
    client._host = "http://mv.local"
    client._playhost = kwargs.get("play_host")
    client._apikey = "k"
    client._sync_libraries = kwargs.get("sync_libraries") or []
    client._api = _FakeApi(routes)
    client._active = True
    return client


def _item_row(index: int, kind: str = "Movie", **extra) -> dict:
    row = {"id": f"id-{index}", "library_id": "lib-1", "parent_id": "", "kind": kind,
           "title": f"影片{index}", "year": 2020, "tmdb_id": 1000 + index, "overview": "",
           "genres": [], "has_poster": True, "has_backdrop": False, "season": 0, "episode": 0,
           "duration_ticks": 0, "is_missing": False, "metadata_info": {}, "user_data": {}}
    row.update(extra)
    return row


def _paged_items(total_rows: list):
    """按 page/page_size 切分预置条目，模拟 MediaVault 的分页语义。"""

    def handler(params, _data):
        page = int(params.get("page", 1))
        size = int(params.get("page_size", 40))
        start = (page - 1) * size
        return Result(True, {"items": total_rows[start:start + size], "total": len(total_rows)})

    return handler


# ── 分页 ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("limit", [1, 30, 100, 130, 250, 356, 400])
def test_get_items_limit_matches_full_scan_prefix(limit):
    """限量遍历的结果必须是全量遍历的前缀，不重复也不跳条。"""
    rows = [_item_row(i) for i in range(356)]
    client = _client({"/items": _paged_items(rows)})
    full = [item.item_id for item in client.get_items("lib-1")]
    assert len(full) == len(set(full)) == 356

    client = _client({"/items": _paged_items(rows)})
    got = [item.item_id for item in client.get_items("lib-1", limit=limit)]
    assert got == full[:limit]


@pytest.mark.parametrize("start_index", [0, 30, 100, 130, 250])
def test_get_items_start_index_matches_full_scan_slice(start_index):
    """起始偏移不是页大小整数倍时，也必须精确对齐到全量切片。"""
    rows = [_item_row(i) for i in range(356)]
    full = [item.item_id for item in _client({"/items": _paged_items(rows)}).get_items("lib-1")]

    client = _client({"/items": _paged_items(rows)})
    got = [item.item_id for item in client.get_items("lib-1", start_index=start_index, limit=20)]
    assert got == full[start_index:start_index + 20]


def test_get_items_always_requests_full_pages():
    """页大小恒为上限，页码才能稳定换算成偏移量。"""
    rows = [_item_row(i) for i in range(250)]
    client = _client({"/items": _paged_items(rows)})
    list(client.get_items("lib-1", limit=130))
    sizes = {call["params"]["page_size"] for call in client._api.calls}
    pages = [call["params"]["page"] for call in client._api.calls]
    assert sizes == {MediaVault.PAGE_LIMIT}
    assert pages == [1, 2]


def test_get_items_stops_when_request_fails():
    """中途请求失败时停止产出，不把失败当成遍历结束的空库。"""
    rows = [_item_row(i) for i in range(150)]
    calls = {"n": 0}

    def flaky(params, _data):
        calls["n"] += 1
        if calls["n"] > 1:
            return None
        return _paged_items(rows)(params, None)

    client = _client({"/items": flaky})
    assert len([*client.get_items("lib-1")]) == 100


# ── 媒体库与统计 ────────────────────────────────────────────────────


def test_get_librarys_maps_type_and_builds_image_url():
    """媒体库类型按 MediaVault 的 library_type 映射，封面走带鉴权的图片直链。"""
    routes = {
        "/libraries": Result(True, {"items": [
            {"id": "lib-1", "name": "电影库", "library_type": "movies", "root_paths": ["/mnt/movies"]},
            {"id": "lib-2", "name": "剧集库", "library_type": "tvshows", "root_path": "/mnt/tv"},
            {"id": "lib-3", "name": "未知库", "library_type": "other", "root_paths": []},
        ]}),
        "/items": _paged_items([_item_row(0)]),
    }
    libraries = _client(routes).get_librarys()
    assert [lib.type for lib in libraries] == [
        MediaType.MOVIE.value, MediaType.TV.value, MediaType.UNKNOWN.value
    ]
    assert libraries[0].path == ["/mnt/movies"]
    assert libraries[1].path == "/mnt/tv"
    assert libraries[0].image.endswith("/items/lib-1/image/primary")
    assert libraries[0].server_type == "mediavault"


def test_get_librarys_hidden_respects_sync_selection():
    """开启过滤时只保留已勾选同步的媒体库。"""
    routes = {
        "/libraries": Result(True, {"items": [
            {"id": "lib-1", "name": "A", "library_type": "movies"},
            {"id": "lib-2", "name": "B", "library_type": "movies"},
        ]}),
        "/items": _paged_items([]),
    }
    client = _client(routes, sync_libraries=["lib-2"])
    assert [lib.id for lib in client.get_librarys(hidden=True)] == ["lib-2"]
    assert [lib.id for lib in client.get_librarys(hidden=False)] == ["lib-1", "lib-2"]


def test_get_librarys_returns_none_when_unreachable():
    """连接失败返回 None，与"媒体库为空"区分开。"""
    assert _client({"/libraries": None}).get_librarys() is None


def test_get_medias_count_maps_statistics_fields():
    """统计接口字段映射到 MoviePilot 的统计模型。"""
    routes = {"/statistics": Result(True, {"movie_count": 3030, "series_count": 1501,
                                           "episode_count": 69597, "item_count": 74128})}
    statistic = _client(routes).get_medias_count()
    assert (statistic.movie_count, statistic.tv_count, statistic.episode_count) == (3030, 1501, 69597)


def test_get_items_count_reads_total_not_page_length():
    """条目总数取分页返回的 total，不受页大小影响。"""
    rows = [_item_row(i) for i in range(356)]
    client = _client({"/items": _paged_items(rows)})
    assert client.get_items_count("lib-1") == 356


# ── 存在性判断 ──────────────────────────────────────────────────────


def test_get_movies_filters_by_title_year_and_identity():
    """电影匹配要求标题全等、年份一致且媒体身份不冲突。"""
    rows = [
        _item_row(1, title="沙丘", year=2021, tmdb_id=438631),
        _item_row(2, title="沙丘", year=2024, tmdb_id=693134),
        _item_row(3, title="沙丘前传", year=2021, tmdb_id=111),
    ]
    client = _client({"/items": _paged_items(rows)})
    assert [m.item_id for m in client.get_movies(title="沙丘")] == ["id-1", "id-2"]

    client = _client({"/items": _paged_items(rows)})
    assert [m.item_id for m in client.get_movies(title="沙丘", year="2021")] == ["id-1"]

    client = _client({"/items": _paged_items(rows)})
    matched = client.get_movies(title="沙丘", media_source=MediaSource.TMDB, media_id="693134")
    assert [m.item_id for m in matched] == ["id-2"]


def test_get_movies_requests_movie_kind_only():
    """存在性查询按类型收窄，避免剧集与分集混进电影结果。"""
    client = _client({"/items": _paged_items([])})
    client.get_movies(title="沙丘")
    assert client._api.calls[0]["params"]["kinds"] == "Movie"
    assert client._api.calls[0]["params"]["keyword"] == "沙丘"


def test_get_tv_episodes_by_item_id_returns_season_map():
    """按条目 ID 查询直接返回季集映射。"""
    routes = {
        "/items/series-1": Result(True, _item_row(1, kind="Series", title="剧A", tmdb_id=99)),
        "/items/series-1/episodes": Result(True, {"seasons": {"1": [1, 2, 3], "2": [1]}}),
    }
    item_id, seasons = _client(routes).get_tv_episodes(item_id="series-1")
    assert item_id == "series-1"
    assert seasons == {1: [1, 2, 3], 2: [1]}


def test_get_tv_episodes_filters_requested_season():
    """指定季号时只返回该季。"""
    routes = {
        "/items/series-1": Result(True, _item_row(1, kind="Series")),
        "/items/series-1/episodes": Result(True, {"seasons": {"1": [1, 2], "2": [1]}}),
    }
    _, seasons = _client(routes).get_tv_episodes(item_id="series-1", season=2)
    assert seasons == {2: [1]}


def test_get_tv_episodes_falls_back_to_title_when_cached_id_is_stale():
    """缓存的条目 ID 失效时退回按标题重新定位，不误判整部剧缺失。"""
    routes = {
        "/items/stale-id": Result(False, None, "not found", 404),
        "/items": _paged_items([_item_row(7, kind="Series", title="剧A", year=2020)]),
        "/items/id-7/episodes": Result(True, {"seasons": {"1": [1, 2]}}),
    }
    item_id, seasons = _client(routes).get_tv_episodes(item_id="stale-id", title="剧A", year="2020")
    assert item_id == "id-7"
    assert seasons == {1: [1, 2]}


def test_get_tv_episodes_returns_empty_when_series_absent():
    """剧集不在库中返回空季集：与 Emby 一致用 (None, {}) 表示"查得到但没有"。"""
    routes = {"/items": _paged_items([])}
    assert _client(routes).get_tv_episodes(title="不存在的剧") == (None, {})


def test_get_tv_episodes_returns_none_when_unreachable():
    """服务不可达时返回 None，避免被当成"这部剧一集都没有"。"""
    routes = {"/items": lambda params, data: None}
    assert _client(routes).get_tv_episodes(title="剧A") == (None, None)


def test_get_season_episode_ids_maps_episode_number_to_item_id():
    """季集条目 ID 映射先定位季，再遍历该季的分集。"""

    def items(params, _data):
        if params.get("kinds") == "Season":
            return Result(True, {"items": [_item_row(1, kind="Season", season=1),
                                           _item_row(2, kind="Season", season=2)], "total": 2})
        if params.get("parent_id") == "id-2":
            return Result(True, {"items": [_item_row(10, kind="Episode", season=2, episode=1),
                                           _item_row(11, kind="Episode", season=2, episode=2)],
                                 "total": 2})
        return Result(True, {"items": [], "total": 0})

    assert _client({"/items": items}).get_season_episode_ids("series-1", 2) == {
        1: "id-10", 2: "id-11",
    }


def test_get_season_episode_ids_returns_empty_for_missing_season():
    """季不存在时返回空映射。"""
    client = _client({"/items": _paged_items([_item_row(1, kind="Season", season=1)])})
    assert client.get_season_episode_ids("series-1", 9) == {}


# ── 条目转换 ────────────────────────────────────────────────────────


def test_iteminfo_extracts_identity_and_path_from_sources():
    """条目详情带媒体源时取文件路径，身份按 ProviderIds 优先级解析。"""
    row = _item_row(1, tmdb_id=0, metadata_info={"original_title": "Dune",
                                                 "external_ids": {"imdb_id": "tt1160419"}},
                    sources=[{"id": "s1", "path": "/mnt/movies/Dune/Dune.mkv"}])
    client = _client({"/items/id-1": Result(True, row)})
    item = client.get_iteminfo("id-1")
    assert item.media_source == MediaSource.IMDb
    assert item.media_id == "tt1160419"
    assert item.original_title == "Dune"
    assert item.path == "/mnt/movies/Dune/Dune.mkv"


def test_iteminfo_prefers_tmdb_over_other_providers():
    """同时有 TMDB 与 IMDb 时按统一优先级取 TMDB。"""
    row = _item_row(1, tmdb_id=438631, metadata_info={"external_ids": {"imdb_id": "tt1160419"}})
    item = _client({"/items/id-1": Result(True, row)}).get_iteminfo("id-1")
    assert (item.media_source, item.media_id) == (MediaSource.TMDB, "438631")


def test_iteminfo_returns_none_for_missing_item():
    """条目不存在返回 None。"""
    assert _client({}).get_iteminfo("nope") is None


# ── 展示与图片 ──────────────────────────────────────────────────────


def test_get_resume_builds_episode_subtitle_and_percent():
    """继续观看的分集用剧名做标题，进度按播放位置换算。"""
    row = _item_row(1, kind="Episode", season=2, episode=5, title="第五集",
                    series_id="series-1", series_name="剧A",
                    duration_ticks=1000, user_data={"position_ticks": 250})
    client = _client({"/items": _paged_items([row])})
    played = client.get_resume(num=5)[0]
    assert played.title == "剧A"
    assert played.subtitle == "S2:5 - 第五集"
    assert played.type == MediaType.TV.value
    assert played.percent == 25.0
    # 分集海报取所属剧集，避免每集一张缩略图
    assert "/items/series-1/image/primary" in played.image


def test_get_latest_backdrops_fills_up_to_requested_count():
    """没有背景图的条目不占名额，取满请求数量为止。"""
    rows = [_item_row(i, has_backdrop=(i % 3 == 0)) for i in range(60)]
    client = _client({"/items": _paged_items(rows)})
    assert len(client.get_latest_backdrops(num=10)) == 10


def test_get_latest_backdrops_uses_play_host_when_remote():
    """外网场景改用播放地址拼图片链接。"""
    rows = [_item_row(0, has_backdrop=True)]
    client = _client({"/items": _paged_items(rows)}, play_host="https://mv.example.com")
    assert client.get_latest_backdrops(num=1, remote=True)[0].startswith("https://mv.example.com")
    assert client.get_latest_backdrops(num=1, remote=False)[0].startswith("http://mv.local")


# ── 入库刷新 ────────────────────────────────────────────────────────


def test_refresh_library_by_items_scans_only_matched_libraries():
    """入库路径命中哪个媒体库就只扫哪个，同一媒体库不重复排队。"""
    routes = {
        "/libraries": Result(True, {"items": [
            {"id": "lib-1", "name": "电影", "root_paths": ["/mnt/movies"]},
            {"id": "lib-2", "name": "剧集", "root_paths": ["/mnt/tv"]},
        ]}),
        "/libraries/lib-1/scan-task": Result(True, {}),
        "/libraries/lib-2/scan-task": Result(True, {}),
    }
    client = _client(routes)
    assert client.refresh_library_by_items([
        RefreshMediaItem(title="A", target_path=Path("/mnt/movies/A (2020)")),
        RefreshMediaItem(title="B", target_path=Path("/mnt/movies/B (2021)")),
    ]) is True
    scanned = [call["api"] for call in client._api.calls if call["api"].endswith("scan-task")]
    assert scanned == ["/libraries/lib-1/scan-task"]


def test_refresh_library_by_items_falls_back_to_full_scan_when_unmatched():
    """路径落在所有媒体库之外时退回全库扫描，避免新片一直不入库。"""
    routes = {
        "/libraries": Result(True, {"items": [{"id": "lib-1", "root_paths": ["/mnt/movies"]}]}),
        "/libraries/lib-1/scan-task": Result(True, {}),
    }
    client = _client(routes)
    assert client.refresh_library_by_items([
        RefreshMediaItem(title="C", target_path=Path("/data/other/C")),
    ]) is True
    assert [call["api"] for call in client._api.calls if "scan-task" in call["api"]] == [
        "/libraries/lib-1/scan-task"
    ]


def test_refresh_library_by_items_returns_none_when_unreachable():
    """媒体库列表拿不到时返回 None，交由上层判定为服务不可用。"""
    client = _client({"/libraries": None})
    assert client.refresh_library_by_items([RefreshMediaItem(title="A", target_path=Path("/x"))]) is None


def test_refresh_root_library_queues_every_library():
    """全库刷新对每个媒体库各排一次后台扫描。"""
    routes = {
        "/libraries": Result(True, {"items": [{"id": "lib-1"}, {"id": "lib-2"}]}),
        "/libraries/lib-1/scan-task": Result(True, {}),
        "/libraries/lib-2/scan-task": Result(True, {}),
    }
    client = _client(routes)
    assert client.refresh_root_library() is True
    assert [call["api"] for call in client._api.calls if "scan-task" in call["api"]] == [
        "/libraries/lib-1/scan-task", "/libraries/lib-2/scan-task",
    ]
    assert all(call["method"] == "post" for call in client._api.calls if "scan-task" in call["api"])


# ── 连接与认证 ──────────────────────────────────────────────────────


def test_reconnect_marks_inactive_when_credentials_rejected():
    """凭据被拒时标记为失活，交给定时重连重试。"""
    client = _client({"/libraries": Result(False, None, "unauthorized", 401)})
    assert client.reconnect() is False
    assert client.is_inactive() is True
    assert client.is_authenticated() is False


def test_unconfigured_client_never_reports_inactive():
    """配置不完整的实例不参与重连，避免定时任务空转。"""
    client = _client({})
    client._apikey = None
    assert client.is_configured() is False
    assert client.is_inactive() is False
    assert client.reconnect() is False


def test_authenticate_posts_to_user_auth_and_returns_token():
    """用户认证走 MediaVault 账号体系，返回访问令牌。"""
    routes = {"/login": Result(True, {"access_token": "jwt-token", "token": "legacy"})}
    client = _client(routes)
    assert client.authenticate("someone", "secret") == "jwt-token"
    call = client._api.calls[0]
    assert (call["base_path"], call["method"]) == ("/api/v1/user-auth", "post")
    assert call["data"] == {"username": "someone", "password": "secret"}


def test_authenticate_returns_none_on_rejection():
    """认证失败返回 None，不把错误响应当成令牌。"""
    assert _client({"/login": Result(False, None, "bad credentials", 401)}).authenticate("a", "b") is None
    assert _client({}).authenticate("", "") is None


def test_disconnect_closes_session():
    """断开时释放底层会话。"""
    client = _client({})
    client.disconnect()
    assert client._api.closed is True
    assert client.is_authenticated() is False


# ── PR-Agent 审查修复的回归 ──────────────────────────────────────────


def test_get_movies_pages_past_the_first_page():
    """关键字命中超过单页上限时，仍要能找到排在后面的目标电影。"""
    rows = [_item_row(i, title=f"其它{i}") for i in range(MediaVault.PAGE_LIMIT)]
    rows.append(_item_row(999, title="沙丘", year=2021, tmdb_id=438631))
    client = _client({"/items": _paged_items(rows)})

    matched = client.get_movies(title="沙丘", year="2021")

    assert [m.item_id for m in matched] == ["id-999"]
    assert [call["params"]["page"] for call in client._api.calls] == [1, 2]


def test_find_series_pages_past_the_first_page():
    """按标题定位剧集时同样要翻页，否则整部剧会被误判成未入库。"""
    rows = [_item_row(i, kind="Series", title=f"其它{i}") for i in range(MediaVault.PAGE_LIMIT)]
    rows.append(_item_row(999, kind="Series", title="剧A", year=2020))
    routes = {
        "/items": _paged_items(rows),
        "/items/id-999/episodes": Result(True, {"seasons": {"1": [1, 2]}}),
    }

    item_id, seasons = _client(routes).get_tv_episodes(title="剧A", year="2020")

    assert item_id == "id-999"
    assert seasons == {1: [1, 2]}


def test_latest_marks_series_as_tv_not_movie():
    """最新入库里的剧集条目类型必须是电视剧。"""
    rows = [_item_row(1, kind="Series", title="剧A"), _item_row(2, kind="Movie", title="片B")]
    client = _client({"/items": _paged_items(rows)})

    latest = client.get_latest(num=2)

    assert [(item.title, item.type) for item in latest] == [
        ("剧A", MediaType.TV.value),
        ("片B", MediaType.MOVIE.value),
    ]
    # 剧集不是分集，不应拼出「季:集」副标题
    assert latest[0].subtitle == "2020"


def test_refresh_queues_every_matched_library_even_if_one_fails():
    """同批命中多个媒体库时，前面的扫描失败不能让后面的媒体库被跳过。"""
    routes = {
        "/libraries": Result(True, {"items": [
            {"id": "lib-1", "root_paths": ["/mnt/a"]},
            {"id": "lib-2", "root_paths": ["/mnt/b"]},
        ]}),
        "/libraries/lib-1/scan-task": Result(False, None, "busy", 409),
        "/libraries/lib-2/scan-task": Result(True, {}),
    }
    client = _client(routes)

    ok = client.refresh_library_by_items([
        RefreshMediaItem(title="A", target_path=Path("/mnt/a/A (2020)")),
        RefreshMediaItem(title="B", target_path=Path("/mnt/b/B (2021)")),
    ])

    assert ok is False
    scanned = [call["api"] for call in client._api.calls if call["api"].endswith("scan-task")]
    assert scanned == ["/libraries/lib-1/scan-task", "/libraries/lib-2/scan-task"]


def test_image_url_never_carries_credentials():
    """图片地址会交给浏览器直接加载，绝不能带上管理 API Key。"""
    url = Api(host="http://mv.local", apikey="super-secret-admin-key").image_url("id-1", "primary")

    assert url == "http://mv.local/api/v1/media-library/items/id-1/image/primary"
    assert "super-secret-admin-key" not in url
    assert "api_key" not in url


def test_play_item_and_backdrop_images_carry_no_credentials():
    """展示类接口产出的图片地址同样不得携带凭据。"""
    rows = [_item_row(1, has_backdrop=True), _item_row(2, kind="Episode", series_id="s-1")]
    client = _client({"/items": _paged_items(rows)})

    urls = [item.image for item in client.get_latest(num=2)]
    urls += client.get_latest_backdrops(num=1)
    urls += [lib.image for lib in (client.get_librarys() or [])]

    assert urls
    assert all("api_key" not in url for url in urls)


def _failing_second_page(total_rows: list):
    """第一页正常、后续页请求失败，模拟翻页中途断连。"""
    calls = {"n": 0}

    def handler(params, _data):
        calls["n"] += 1
        if calls["n"] > 1:
            return None
        size = int(params.get("page_size", 40))
        return Result(True, {"items": total_rows[:size], "total": len(total_rows)})

    return handler


def test_get_movies_reports_unreachable_when_a_later_page_fails():
    """翻页中途断连必须返回 None，不能把残缺结果当成「不在库中」。"""
    # total 必须超过一页，否则第一页就判定取完，构造不出「中途失败」
    rows = [_item_row(i, title=f"沙丘{i}") for i in range(MediaVault.PAGE_LIMIT + 1)]
    client = _client({"/items": _failing_second_page(rows)})

    assert client.get_movies(title="沙丘") is None


def test_find_series_reports_unreachable_when_a_later_page_fails():
    """同上：剧集定位不能把断连误判成整部剧未入库。"""
    rows = [_item_row(i, kind="Series", title=f"剧{i}") for i in range(MediaVault.PAGE_LIMIT + 1)]
    client = _client({"/items": _failing_second_page(rows)})

    assert client.get_tv_episodes(title="剧A") == (None, None)


def test_search_stops_at_reported_total_without_an_extra_request():
    """末页恰好满额时用 total 判定取完，不再多发一次可能失败的请求。"""
    rows = [_item_row(i, title="沙丘", year=2021) for i in range(MediaVault.PAGE_LIMIT)]
    client = _client({"/items": _paged_items(rows)})

    matched = client.get_movies(title="沙丘")

    assert len(matched) == MediaVault.PAGE_LIMIT
    assert [call["params"]["page"] for call in client._api.calls] == [1]


def test_find_series_returns_first_page_hit_even_if_a_later_page_fails():
    """第一页已命中就该直接返回，不因后续页失败被误报成服务不可达。"""
    rows = [_item_row(0, kind="Series", title="剧A", year=2020)]
    rows += [_item_row(i, kind="Series", title=f"其它{i}") for i in range(1, MediaVault.PAGE_LIMIT + 1)]
    routes = {
        "/items": _failing_second_page(rows),
        "/items/id-0/episodes": Result(True, {"seasons": {"1": [1]}}),
    }

    item_id, seasons = _client(routes).get_tv_episodes(title="剧A", year="2020")

    assert item_id == "id-0"
    assert seasons == {1: [1]}
