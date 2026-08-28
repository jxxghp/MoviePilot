from unittest.mock import patch

import pytest

from app import schemas
from app.application.history import TransferHistoryMonthlyStatistics
from app.modules.ugreen.ugreen import Ugreen

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
