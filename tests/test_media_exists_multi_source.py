"""多来源媒体存量判定协议的行为测试。"""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

import app.application.orchestration.download as download_module
from app.application.orchestration.download import DownloadChain
from app.application.orchestration.ports.library import LibraryPorts
from app.modules.medialibrary import MediaLibraryModule
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType


class _RecordingDispatch:
    """记录端口选用的发行方式，并按预设答案应答。"""

    def __init__(self, multicast_results=None, unicast_result=None):
        """
        :param multicast_results: 多播时返回的各来源答案
        :param unicast_result: 单播时返回的仲裁答案
        """
        self.multicast_results = multicast_results or []
        self.unicast_result = unicast_result
        self.multicast_calls = []
        self.unicast_calls = []

    def multicast(self, method, **kwargs):
        """记录多播调用并返回预设的各来源答案。"""
        self.multicast_calls.append((method, kwargs))
        return list(self.multicast_results)

    def unicast(self, method, **kwargs):
        """记录单播调用并返回预设的仲裁答案。"""
        self.unicast_calls.append((method, kwargs))
        return self.unicast_result


def _tv_mediainfo(seasons=None):
    """构造电视剧媒体信息替身。"""
    return SimpleNamespace(
        type=MediaType.TV,
        title="Test Show",
        title_year="Test Show (2026)",
        year="2026",
        season=1,
        seasons=seasons or {1: [1, 2, 3, 4, 5]},
        media_source=MediaSource.TMDB,
        media_id="1",
        episode_group=None,
    )


def _server_exists(seasons):
    """构造媒体服务器来源的存量答案。"""
    return ExistMediaInfo(
        type=MediaType.TV,
        seasons=seasons,
        server_type="emby",
        server="Emby1",
        itemid="emby-series",
    )


def _filesystem_exists(seasons):
    """构造文件系统来源的存量答案。"""
    return ExistMediaInfo(type=MediaType.TV, seasons=seasons)


def test_tv_merges_episodes_from_media_server_and_file_system():
    """媒体服务器与文件系统各报一部分集时，端口须给出并集。"""
    dispatch = _RecordingDispatch(
        multicast_results=[
            _server_exists({1: [1, 2, 3]}),
            _filesystem_exists({1: [1, 2, 3, 4, 5]}),
        ]
    )

    exists = LibraryPorts(dispatch).media_exists(mediainfo=_tv_mediainfo())

    assert exists.seasons == {1: [1, 2, 3, 4, 5]}
    assert dispatch.unicast_calls == []


def test_tv_merge_keeps_identity_of_highest_priority_source():
    """合并结果的媒体库标识须沿用最高优先级来源。"""
    dispatch = _RecordingDispatch(
        multicast_results=[
            _server_exists({1: [1]}),
            _filesystem_exists({1: [2]}),
        ]
    )

    exists = LibraryPorts(dispatch).media_exists(mediainfo=_tv_mediainfo())

    assert exists.server == "Emby1"
    assert exists.server_type == "emby"
    assert exists.itemid == "emby-series"


def test_tv_merge_accepts_seasons_claimed_by_different_sources():
    """某来源只报季 1、另一来源只报季 2 时，两季都要保留。"""
    dispatch = _RecordingDispatch(
        multicast_results=[
            _server_exists({1: [1, 2]}),
            _filesystem_exists({2: [1]}),
        ]
    )

    exists = LibraryPorts(dispatch).media_exists(
        mediainfo=_tv_mediainfo(seasons={1: [1, 2], 2: [1]})
    )

    assert exists.seasons == {1: [1, 2], 2: [1]}


def test_tv_returns_none_when_no_source_claims():
    """无人认领时电视剧仍返回 None。"""
    dispatch = _RecordingDispatch(multicast_results=[])

    assert LibraryPorts(dispatch).media_exists(mediainfo=_tv_mediainfo()) is None


def test_specified_server_narrows_to_single_media_server():
    """指定媒体服务器时收窄为单播，不得改成多来源合并。"""
    dispatch = _RecordingDispatch(unicast_result=_server_exists({1: [1]}))

    exists = LibraryPorts(dispatch).media_exists(
        mediainfo=_tv_mediainfo(), server="Emby1"
    )

    assert exists.seasons == {1: [1]}
    assert dispatch.multicast_calls == []
    assert dispatch.unicast_calls[0][1]["server"] == "Emby1"


@pytest.mark.parametrize("media_type", [MediaType.MOVIE, MediaType.MUSIC])
def test_movie_and_music_take_first_non_empty_answer(media_type):
    """电影与音乐仍按首个非空答案判定，不参与合并。"""
    dispatch = _RecordingDispatch(
        unicast_result=ExistMediaInfo(type=media_type, server="Emby1")
    )
    mediainfo = SimpleNamespace(
        type=media_type,
        title="Demo",
        title_year="Demo (2026)",
        media_source=MediaSource.TMDB,
        media_id="1",
    )

    exists = LibraryPorts(dispatch).media_exists(mediainfo=mediainfo, itemid="item-1")

    assert exists.type == media_type
    assert dispatch.multicast_calls == []
    assert dispatch.unicast_calls[0][1]["itemid"] == "item-1"


def test_file_system_source_yields_when_local_exists_search_disabled():
    """关闭本地检索开关后，文件系统来源不得扫描媒体库。"""
    module = MediaLibraryModule()

    with patch("app.modules.medialibrary.settings.LOCAL_EXISTS_SEARCH", False), \
            patch.object(module, "media_files") as media_files:
        result = module.media_exists(_tv_mediainfo())

    assert result is None
    media_files.assert_not_called()


def test_file_system_source_yields_when_server_specified():
    """指定媒体服务器后，文件系统来源须整体让出。"""
    module = MediaLibraryModule()

    with patch("app.modules.medialibrary.settings.LOCAL_EXISTS_SEARCH", True), \
            patch.object(module, "media_files") as media_files:
        result = module.media_exists(_tv_mediainfo(), server="Emby1")

    assert result is None
    media_files.assert_not_called()


class _FakeMediaServerOper:
    """避免缺集检查触碰媒体服务器条目库。"""

    def get_item_id(self, **_kwargs):
        """返回空条目 ID。"""
        return None


def _no_exists_chain(multicast_results):
    """构造只接管分发的下载链，用于缺集检查。"""
    chain = DownloadChain.__new__(DownloadChain)
    dispatch = _RecordingDispatch(multicast_results=multicast_results)
    chain.multicast = dispatch.multicast
    chain.unicast = dispatch.unicast
    return chain


def test_no_exists_excludes_episodes_only_present_on_disk(monkeypatch):
    """媒体服务器有 E1-3、磁盘有 E1-5 时，缺失集不应包含 E4、E5。"""
    monkeypatch.setattr(download_module, "MediaServerOper", _FakeMediaServerOper)
    chain = _no_exists_chain(
        [
            _server_exists({1: [1, 2, 3]}),
            _filesystem_exists({1: [1, 2, 3, 4, 5]}),
        ]
    )
    meta = SimpleNamespace(sea=None, season_list=[])

    exist, no_exists = chain.get_no_exists_info(meta=meta, mediainfo=_tv_mediainfo())

    assert exist is True
    assert no_exists == {}


def test_no_exists_reports_missing_episodes_when_no_source_has_them(monkeypatch):
    """所有来源都只有 E1-3 时，E4、E5 仍须判定为缺失。"""
    monkeypatch.setattr(download_module, "MediaServerOper", _FakeMediaServerOper)
    chain = _no_exists_chain([_server_exists({1: [1, 2, 3]})])
    meta = SimpleNamespace(sea=None, season_list=[])

    exist, no_exists = chain.get_no_exists_info(meta=meta, mediainfo=_tv_mediainfo())

    assert exist is False
    assert sorted(no_exists["tmdb:1"][1].episodes) == [4, 5]
