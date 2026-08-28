"""SearchChain 结果处理的输入所有权与生命周期回归测试。"""

import pytest

from app.chain.search import SearchChain
from app.chain.search import result as result_module
from app.domain.context import MediaInfo, TorrentInfo
from app.schemas.types import MediaSource, MediaType


class ProgressProbe:
    """记录搜索进度生命周期，避免测试依赖运行时全局进度状态。"""

    instances = []

    def __init__(self, _key):
        """创建一条尚未启动的进度记录。"""
        self.started = False
        self.ended = False
        self.updates = []
        self.__class__.instances.append(self)

    def start(self):
        """记录进度启动。"""
        self.started = True

    def update(self, **kwargs):
        """记录一次进度更新。"""
        self.updates.append(kwargs)

    def end(self):
        """记录进度结束。"""
        self.ended = True


def _media() -> MediaInfo:
    """构造带有可裁剪详情的目标媒体。"""
    media = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="1",
        tmdb_id=1,
        title="测试电影",
        original_title="Test Movie",
        names=["测试别名"],
        type=MediaType.MOVIE,
        year="2024",
    )
    media.tmdb_info = {"id": 1}
    return media


def _torrent(site: int, title: str, description: str = "描述") -> TorrentInfo:
    """构造一条可稳定匹配的测试资源。"""
    return TorrentInfo(
        site=site,
        site_name=f"站点{site}",
        title=title,
        description=description,
        category=MediaType.MOVIE.value,
    )


def _patch_match(monkeypatch):
    """将匹配和排序边界替换为确定性的离线实现。"""
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "match_torrent",
        staticmethod(lambda **_kwargs: True),
    )
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "requires_identity_disambiguation",
        staticmethod(lambda **_kwargs: False),
    )


def test_parse_result_preserves_caller_inputs(monkeypatch):
    """结果投影应裁剪媒体副本，同时保持调用方对象和资源容器不变。"""
    _patch_match(monkeypatch)
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: contexts),
    )
    target = _media()
    torrents = [_torrent(1, "测试电影 2024 1080p"), _torrent(2, "测试电影 2024 2160p")]
    original_items = tuple(torrents)

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=torrents,
        mediainfo=target,
        rule_groups=[],
    )

    assert tuple(torrents) == original_items
    assert target.names == ["测试别名"]
    assert target.tmdb_info == {"id": 1}
    assert len(contexts) == 2
    assert contexts[0].media_info is contexts[1].media_info
    assert contexts[0].media_info is not target
    assert contexts[0].media_info.names == []
    assert contexts[0].media_info.tmdb_info == {}


def test_parse_result_keeps_sort_and_duplicate_contract(monkeypatch):
    """排序后去重应保留键的首次位置，并采用该键最后出现的资源。"""
    _patch_match(monkeypatch)
    first = _torrent(1, "测试电影 2024 1080p")
    unique = _torrent(2, "测试电影 2024 2160p")
    replacement = _torrent(1, "测试电影 2024 1080p")
    torrents = [first, unique, replacement]
    monkeypatch.setattr(
        result_module.TorrentHelper,
        "sort_torrents",
        staticmethod(lambda contexts: [contexts[1], contexts[2], contexts[0]]),
    )

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=torrents,
        mediainfo=_media(),
        rule_groups=[],
    )

    assert [context.torrent_info for context in contexts] == [unique, first]
    assert torrents == [first, unique, replacement]


def test_progress_ends_when_rule_filter_returns_empty(monkeypatch):
    """规则过滤提前返回空结果时也必须结束已启动的搜索进度。"""
    ProgressProbe.instances.clear()
    monkeypatch.setattr(result_module, "ProgressHelper", ProgressProbe)
    monkeypatch.setattr(SearchChain, "filter_torrents", lambda *_args, **_kwargs: [])

    contexts = object.__new__(SearchChain)._parse_result(
        torrents=[_torrent(1, "测试电影 2024 1080p")],
        mediainfo=_media(),
        rule_groups=["测试规则"],
    )

    assert contexts == []
    assert len(ProgressProbe.instances) == 1
    assert ProgressProbe.instances[0].started
    assert ProgressProbe.instances[0].ended


def test_progress_ends_when_filter_raises(monkeypatch):
    """附加参数过滤异常时应传播异常并结束已启动的搜索进度。"""
    ProgressProbe.instances.clear()
    monkeypatch.setattr(result_module, "ProgressHelper", ProgressProbe)

    def raise_filter(*_args, **_kwargs):
        """模拟过滤边界发生运行时异常。"""
        raise RuntimeError("filter failed")

    monkeypatch.setattr(result_module.TorrentHelper, "filter_torrent", raise_filter)

    with pytest.raises(RuntimeError, match="filter failed"):
        object.__new__(SearchChain)._parse_result(
            torrents=[_torrent(1, "测试电影 2024 1080p")],
            mediainfo=_media(),
            rule_groups=[],
            filter_params={"free": "true"},
        )

    assert len(ProgressProbe.instances) == 1
    assert ProgressProbe.instances[0].started
    assert ProgressProbe.instances[0].ended
