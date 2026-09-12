from unittest.mock import patch

import pytest

from app import schemas
from app.application.history import TransferHistoryMonthlyStatistics
from app.modules.ugreen.ugreen import Ugreen
from app.schemas.types import MediaSource

try:
    from app.api.endpoints import dashboard as dashboard_endpoint
except Exception:
    dashboard_endpoint = None


class _FakeUgreenApi:
    host = "http://127.0.0.1:9999"
    token = "test-token"

    @staticmethod
    def video_all(classification: int, page: int = 1, page_size: int = 1):
        if classification == -102:
            return {"total_num": 12}
        if classification == -103:
            return {"total_num": 34}
        return {"total_num": 0}


class _FakeReconnectApi:
    token = "test-token"

    @staticmethod
    def login(_username, _password):
        return "test-token"

    @staticmethod
    def current_user():
        return {"name": "tester"}

    @staticmethod
    def close():
        return None

    @staticmethod
    def export_session_state():
        return {"token": "test-token", "public_key": "public-key"}


class _PagedFolderApi:
    def __init__(self, stop_after: int | None = None):
        self.calls = 0
        self.pages = []
        self.stop_after = stop_after

    def poster_wall_get_folder(self, page: int, page_size: int = 100):
        self.calls += 1
        self.pages.append(page)
        if self.stop_after is not None and page >= self.stop_after:
            return {
                "folder_arr": [
                    {"media_lib_set_id": page, "path": f"/library/{page}"},
                ],
                "is_last_page": True,
            }
        return {
            "folder_arr": [
                {"media_lib_set_id": page, "path": f"/library/{page}"},
            ],
            "is_last_page": False,
        }


class _FakeMediaApi:
    """媒体库服务接口的最小伪实现。"""

    host = "https://nas.example:9443"
    token = "media-token"
    user_id = "user-id"
    server_id = "server-id"

    def __init__(self) -> None:
        """初始化可记录请求参数的伪媒体接口。"""
        self.calls: list[tuple[str, dict]] = []

    @staticmethod
    def _movie() -> dict:
        """返回电影条目样本。"""
        return {
            "Id": "movie-id",
            "Type": "Movie",
            "Name": "示例电影",
            "OriginalTitle": "Example Movie",
            "ProductionYear": 2024,
            "ProviderIds": {"Tmdb": "100"},
            "ParentId": "movie-library",
            "Path": "/media/movies/example",
            "BackdropImageTags": ["movie-backdrop"],
        }

    @staticmethod
    def _series() -> dict:
        """返回电视剧条目样本。"""
        return {
            "Id": "series-id",
            "Type": "Series",
            "Name": "示例剧集",
            "OriginalTitle": "Example Series",
            "ProductionYear": 2023,
            "ProviderIds": {"Tmdb": "200"},
            "ParentId": "tv-library",
            "Path": "/media/tv/example",
        }

    @staticmethod
    def _episode() -> dict:
        """返回剧集条目样本。"""
        return {
            "Id": "episode-id",
            "Type": "Episode",
            "Name": "第二集",
            "SeriesName": "示例剧集",
            "SeriesId": "series-id",
            "ParentId": "season-id",
            "ParentIndexNumber": 1,
            "IndexNumber": 2,
            "ProviderIds": {"Tmdb": "200"},
            "UserData": {"PlayedPercentage": 25.0},
        }

    def current_user(self) -> dict:
        """返回有效媒体库用户。"""
        return {"Id": self.user_id, "Name": "tester"}

    def views(self) -> list[dict]:
        """返回电影和电视剧媒体库视图。"""
        return [
            {
                "Id": "movie-library",
                "Name": "电影",
                "CollectionType": "movies",
                "Path": "/media/movies",
            },
            {
                "Id": "tv-library",
                "Name": "剧集",
                "CollectionType": "tvshows",
                "Path": "/media/tv",
            },
        ]

    def items(self, **kwargs: object) -> dict:
        """按查询条件返回预置的用户条目。"""
        self.calls.append(("items", dict(kwargs)))
        parent_id = kwargs.get("parent_id")
        ids = kwargs.get("ids")
        include_item_types = kwargs.get("include_item_types")
        if ids == "movie-id":
            return {"Items": [self._movie()], "TotalRecordCount": 1}
        if ids == "series-id":
            return {"Items": [self._series()], "TotalRecordCount": 1}
        if parent_id == "movie-library":
            return {"Items": [self._movie()], "TotalRecordCount": 1}
        if parent_id == "tv-library":
            return {"Items": [self._series()], "TotalRecordCount": 1}
        if include_item_types == "Movie":
            return {"Items": [self._movie()], "TotalRecordCount": 1}
        if include_item_types == "Series":
            return {"Items": [self._series()], "TotalRecordCount": 1}
        if include_item_types == "Movie,Series":
            return {
                "Items": [self._movie(), self._series()],
                "TotalRecordCount": 2,
            }
        if include_item_types == "Movie,Series,MusicAlbum":
            return {
                "Items": [self._movie(), self._series()],
                "TotalRecordCount": 2,
            }
        return {"Items": [], "TotalRecordCount": 0}

    def item(self, item_id: str) -> dict | None:
        """返回指定媒体条目详情。"""
        return {
            "movie-id": self._movie(),
            "series-id": self._series(),
        }.get(item_id)

    def counts(self) -> dict:
        """返回媒体库数量统计。"""
        return {
            "MovieCount": 11,
            "SeriesCount": 3,
            "EpisodeCount": 27,
            "MusicAlbumCount": 5,
        }

    def resume(self, limit: int = 20) -> list[dict]:
        """返回继续观看条目。"""
        _ = limit
        return [self._movie() | {"UserData": {"PlayedPercentage": 50.0}}, self._episode()]

    def episodes(self, series_id: str, season: int | None = None) -> list[dict]:
        """返回指定剧集的已入库集数。"""
        assert series_id == "series-id"
        assert season in (None, 1)
        return [self._episode()]

    def image_url(self, item_id: str, image_type: str = "Primary") -> str:
        """返回伪造的媒体图片地址。"""
        return f"{self.host}/emby/Items/{item_id}/Images/{image_type}?api_key={self.token}"

    def close(self) -> None:
        """关闭伪媒体接口。"""
        return None


def _build_media_service() -> tuple[Ugreen, _FakeMediaApi]:
    """构造已认证的媒体库服务模式绿联实例。"""
    media_api = _FakeMediaApi()
    ugreen = Ugreen.__new__(Ugreen)
    ugreen._host = media_api.host
    ugreen._username = "tester"
    ugreen._password = "secret"
    ugreen._userinfo = {"Id": media_api.user_id, "Name": "tester"}
    ugreen._verify_ssl = True
    ugreen._connection_mode = Ugreen.MEDIA_SERVICE_CONNECTION_MODE
    ugreen._media_api = media_api
    ugreen._api = None
    ugreen._playhost = "https://play.example"
    ugreen._libraries = {}
    ugreen._library_paths = {}
    ugreen._sync_libraries = []
    return ugreen, media_api


def test_resolve_scan_type():
    resolve = Ugreen._Ugreen__resolve_scan_type

    assert resolve(scan_mode="new_and_modified") == 1
    assert resolve(scan_mode="supplement_missing") == 2
    assert resolve(scan_mode="full_override") == 3
    assert resolve(scan_mode="1") == 1
    assert resolve(scan_mode="2") == 2
    assert resolve(scan_mode="3") == 3
    assert resolve(scan_type=1) == 1
    assert resolve(scan_type=2) == 2
    assert resolve(scan_type=3) == 3
    assert resolve(scan_mode="unknown") == 2
    assert resolve() == 2


def test_resolve_verify_ssl():
    resolve = Ugreen._Ugreen__resolve_verify_ssl
    assert resolve(True) is True
    assert resolve(False) is False
    assert resolve("true") is True
    assert resolve("1") is True
    assert resolve("false") is False
    assert resolve("0") is False
    assert resolve(None) is True


def test_resolve_connection_mode_uses_media_service_port():
    """9443 默认使用媒体库服务，显式 internal 可保留旧接口行为。"""
    resolve = Ugreen._Ugreen__resolve_connection_mode

    assert resolve("https://nas.example:9443") == Ugreen.MEDIA_SERVICE_CONNECTION_MODE
    assert resolve("nas.example:9443") == Ugreen.MEDIA_SERVICE_CONNECTION_MODE
    assert resolve("https://nas.example:9443", "internal") == Ugreen.INTERNAL_CONNECTION_MODE
    assert resolve("https://nas.example:9999", "media_service") == Ugreen.MEDIA_SERVICE_CONNECTION_MODE


def test_media_service_supports_library_query_and_core_media_operations():
    """媒体库服务模式应覆盖媒体库、搜索、详情、播放和同步查询能力。"""
    ugreen, media_api = _build_media_service()

    libraries = ugreen.get_librarys()
    assert libraries is not None
    assert [library.id for library in libraries] == ["movie-library", "tv-library"]
    assert libraries[0].type == "电影"
    assert libraries[0].item_count == 1

    stat = ugreen.get_medias_count()
    assert stat.movie_count == 11
    assert stat.tv_count == 3
    assert stat.episode_count == 27
    assert stat.music_count == 5
    assert ugreen.get_user_count() == 1

    movies = ugreen.get_movies(
        "示例电影",
        year="2024",
        media_source=MediaSource.TMDB,
        media_id="100",
    )
    assert movies is not None
    assert [movie.item_id for movie in movies] == ["movie-id"]
    assert ugreen.get_iteminfo("movie-id").title == "示例电影"

    series_id, season_episodes = ugreen.get_tv_episodes(
        item_id="series-id",
        title="示例剧集",
        year="2023",
        media_source=MediaSource.TMDB,
        media_id="200",
        season=1,
    )
    assert series_id == "series-id"
    assert season_episodes == {1: [2]}

    items = list(ugreen.get_items("movie-library", limit=1))
    assert [item.item_id for item in items] == ["movie-id"]
    assert ugreen.get_items_count("movie-library") == 1

    play_url = ugreen.get_play_url("movie-id")
    assert play_url == ("https://play.example/web/index.html#!/item?id=movie-id&context=home&serverId=server-id")
    resume = ugreen.get_resume(num=2)
    assert resume is not None
    assert [item.item_id for item in resume] == ["movie-id", "episode-id"]
    assert resume[0].percent == 50.0
    latest = ugreen.get_latest(num=2)
    assert latest is not None
    assert [item.item_id for item in latest] == ["movie-id", "series-id"]
    assert ugreen.get_latest_backdrops(num=1, remote=True) == [
        "https://play.example/emby/Items/movie-id/Images/Backdrop?api_key=media-token"
    ]
    assert ugreen.refresh_root_library() is False
    assert any(name == "items" for name, _ in media_api.calls)


def test_get_medias_count_episode_is_none():
    ugreen = Ugreen.__new__(Ugreen)
    ugreen._host = "http://127.0.0.1:9999"
    ugreen._username = "tester"
    ugreen._password = "secret"
    ugreen._userinfo = {"name": "tester"}
    ugreen._api = _FakeUgreenApi()

    stat = ugreen.get_medias_count()
    assert stat.movie_count == 12
    assert stat.tv_count == 34
    assert stat.episode_count is None


def test_reconnect_does_not_eagerly_load_libraries():
    ugreen = Ugreen.__new__(Ugreen)
    ugreen._host = "http://127.0.0.1:9999"
    ugreen._username = "tester"
    ugreen._password = "secret"
    ugreen._verify_ssl = True
    ugreen._libraries = {"old": {"id": "old"}}
    ugreen._library_paths = {"old": "/old"}
    ugreen._api = None
    ugreen._userinfo = None

    with patch.object(Ugreen, "_Ugreen__restore_persisted_session", return_value=False), patch(
        "app.modules.ugreen.ugreen.Api", return_value=_FakeReconnectApi()
    ), patch.object(Ugreen, "_Ugreen__save_persisted_session", return_value=None), patch.object(
        Ugreen, "disconnect", wraps=ugreen.disconnect
    ), patch.object(Ugreen, "get_librarys") as mocked_get_librarys:
        assert ugreen.reconnect() is True

    mocked_get_librarys.assert_not_called()
    assert ugreen._libraries == {}
    assert ugreen._library_paths == {}


def test_load_library_paths_stops_at_last_page():
    ugreen = Ugreen.__new__(Ugreen)
    ugreen._username = "tester"
    ugreen._api = _PagedFolderApi(stop_after=3)

    paths = ugreen._Ugreen__load_library_paths()

    assert ugreen._api.pages == [1, 2, 3]
    assert paths["3"] == "/library/3"


def test_load_library_paths_respects_page_limit():
    ugreen = Ugreen.__new__(Ugreen)
    ugreen._username = "tester"
    ugreen._api = _PagedFolderApi()

    paths = ugreen._Ugreen__load_library_paths()

    assert ugreen._api.calls == Ugreen.LIBRARY_PATH_PAGE_LIMIT
    assert len(paths) == Ugreen.LIBRARY_PATH_PAGE_LIMIT
    assert str(Ugreen.LIBRARY_PATH_PAGE_LIMIT) in paths


class _DashboardRepository:
    """Dashboard 汇总测试使用的零增量历史仓储。"""

    @staticmethod
    def monthly_media_statistics() -> TransferHistoryMonthlyStatistics:
        """返回全零月度统计，隔离媒体服务汇总断言。"""
        return TransferHistoryMonthlyStatistics(
            movies=0,
            tv_shows=0,
            episodes=0,
            music=0,
        )


@pytest.mark.skipif(
    dashboard_endpoint is None,
    reason="dashboard endpoint dependencies are missing",
)
def test_statistic_all_episode_missing():
    mocked_stats = [
        schemas.Statistic(movie_count=10, tv_count=20, episode_count=None, user_count=2),
        schemas.Statistic(movie_count=1, tv_count=2, episode_count=None, user_count=1),
    ]
    from app.application.dashboard import DashboardQueryService

    service = DashboardQueryService(
        repository=_DashboardRepository(),
        media_statistics=lambda _name: mocked_stats,
    )
    ret = dashboard_endpoint.statistic(name="ugreen", service=service, _=None)

    assert ret.movie_count == 11
    assert ret.tv_count == 22
    assert ret.user_count == 3
    assert ret.episode_count is None


@pytest.mark.skipif(
    dashboard_endpoint is None,
    reason="dashboard endpoint dependencies are missing",
)
def test_statistic_mixed_episode_count():
    mocked_stats = [
        schemas.Statistic(movie_count=10, tv_count=20, episode_count=None, user_count=2),
        schemas.Statistic(movie_count=1, tv_count=2, episode_count=6, user_count=1),
    ]
    from app.application.dashboard import DashboardQueryService

    service = DashboardQueryService(
        repository=_DashboardRepository(),
        media_statistics=lambda _name: mocked_stats,
    )
    ret = dashboard_endpoint.statistic(name="all", service=service, _=None)

    assert ret.movie_count == 11
    assert ret.tv_count == 22
    assert ret.user_count == 3
    assert ret.episode_count == 6
