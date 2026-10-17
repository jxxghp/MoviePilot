"""
换票资源跨批次复用内容与真实限流冷却的离线回归。
"""

import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response

import app.application.torrent.download as torrent_download
import app.chain.download.submission as submission
from app.adapters.cache.backends import FileBackend
from app.chain.download import DownloadChain
from app.chain.download.batch import _BatchDownloadRun
from app.chain.download.failure import DownloadFailureOwner
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo
from app.schemas.types import MediaType


def _content(filename: str) -> bytes:
    """
    构造可由真实解析器读取的单文件种子。
    """
    name = filename.encode()
    return b"d4:infod4:name" + str(len(name)).encode() + b":" + name + b"6:lengthi1eee"


def _response(content: bytes) -> Response:
    """
    构造 HTTP 成功响应，内容可以是种子或站点限流 JSON。
    """
    response = Response()
    response.status_code = 200
    response._content = content
    response.encoding = "utf-8"
    return response


def _context(**fields) -> Context:
    """
    构造同站同资源的多集包候选，识别结果保持空集数。
    """
    values = dict(
        site=1, site_name="馒头", title="Series S05",
        page_url="https://tracker.example/detail/793424",
        enclosure="[encoded]https://tracker.example/token", site_cookie="session=first",
    )
    values.update(fields)
    return Context(
        meta_info=MetaInfo("Series S05"),
        media_info=MediaInfo(title="Series", type=MediaType.TV),
        torrent_info=TorrentInfo(**values),
    )


def _batch(chain) -> _BatchDownloadRun:
    """
    每轮重新创建批次，验证复用不依赖上轮批次状态。
    """
    options = dict(
        save_path=None, channel=None, source="Subscribe|48", userid=None,
        username=None, downloader="test", custom_words=None, governance=None,
    )
    return _BatchDownloadRun(chain, [], {}, options, None)


@pytest.fixture
def boundary(monkeypatch, tmp_path):
    """
    仅替换 HTTP、换票与持久化边界，使用真实种子解析及临时文件缓存。
    """
    chain = object.__new__(DownloadChain)
    chain.post_message = Mock()
    chain.download_single = Mock(return_value="download-id")
    chain._record_download_failure = Mock()
    chain._resolve_indirect_download_url = Mock(side_effect=[
        f"https://tracker.example/download?id=793424&t={number}&sign=ticket{number}"
        for number in range(30)
    ])
    cache = FileBackend(tmp_path)
    factories = [Mock(return_value=cache), Mock(return_value=cache)]
    monkeypatch.setattr(submission, "FileCache", factories[0])
    monkeypatch.setattr(torrent_download, "FileCache", factories[1])
    port = Mock()
    port.request.return_value = _response(_content("Series.S05E001.mkv"))
    monkeypatch.setattr(torrent_download, "_require_torrent_port", Mock(return_value=port))
    return SimpleNamespace(chain=chain, cache=cache, port=port, factories=factories)


@pytest.mark.parametrize("filename", ["Series.S05E001.mkv", "Series.mkv"])
def test_repeated_partial_pack_rounds_fetch_content_once(boundary, filename):
    """
    无需要集和无法识别集数均可跨十二轮复用，避免耗尽每日配额。
    """
    boundary.port.request.return_value = _response(_content(filename))
    for _ in range(12):
        assert _batch(boundary.chain)._submit_partial_pack(_context(), {196, 197}) == set()
    boundary.port.request.assert_called_once()
    boundary.chain._resolve_indirect_download_url.assert_called_once()
    boundary.chain.download_single.assert_not_called()
    boundary.chain._record_download_failure.assert_not_called()
    boundary.chain.post_message.assert_not_called()
    assert all(factory.call_args.kwargs == {"local_only": True} for factory in boundary.factories)


def test_new_missing_episode_uses_cached_content(boundary):
    """
    缓存正向文件事实，缺集变化后立即选中需要的集，不记资源失败。
    """
    assert _batch(boundary.chain)._submit_partial_pack(_context(), {196}) == set()
    context = _context()
    assert _batch(boundary.chain)._submit_partial_pack(context, {1, 196}) == {1}
    boundary.port.request.assert_called_once()
    assert boundary.chain.download_single.call_args.kwargs["episodes"] == {1}
    assert boundary.chain.download_single.call_args.kwargs["torrent_content"] == _content("Series.S05E001.mkv")
    boundary.chain._record_download_failure.assert_not_called()


@pytest.mark.parametrize("fields", [
    {"site": 2}, {"page_url": "https://tracker.example/detail/917009"},
    {"site_cookie": "session=second"}, {"enclosure": "[other-key]https://tracker.example/token"},
])
def test_resource_and_credentials_isolate_cached_content(boundary, fields):
    """
    同种子 ID 的其他站点、其他种子与新换票凭据都必须重新取种子。
    """
    first = _context().torrent_info
    boundary.chain.download_torrent(first)
    boundary.chain.download_torrent(replace(first, **fields))
    assert boundary.port.request.call_count == 2
    assert boundary.chain._resolve_indirect_download_url.call_count == 2


def test_corrupt_or_cleared_resource_cache_refetches_content(boundary):
    """
    损坏的资源缓存回退到换票，清理种子缓存后也不再复用。
    """
    torrent = _context().torrent_info
    key = boundary.chain._indirect_torrent_cache_key(torrent)
    boundary.cache.set(key, b"invalid", region="torrents")
    assert boundary.chain.download_torrent(torrent)[0] == _content("Series.S05E001.mkv")
    boundary.cache.clear(region="torrents")
    assert boundary.chain.download_torrent(torrent)[0] == _content("Series.S05E001.mkv")
    assert boundary.port.request.call_count == 2


@pytest.mark.parametrize("partial", [False, True])
def test_batch_keeps_daily_limit_reason_and_cooldown(boundary, partial):
    """
    完整包和拆包均记录真实每日限流，交给既有策略冷却 24 小时。
    """
    reason = "相同種子當天最多下載10次"
    boundary.port.request.return_value = _response(json.dumps({"message": reason}).encode())
    context = _context()
    assert _batch(boundary.chain)._read_torrent(context, partial=partial) is None
    recorded = boundary.chain._record_download_failure.call_args.kwargs
    assert recorded["error_msg"] == f"站点限流：{reason}"
    assert recorded["context"] is context
    assert recorded["source"] == "Subscribe|48"
    assert DownloadFailureOwner._download_failure_ttl(recorded["error_msg"]) == 86400
    boundary.port.request.assert_called_once()
    key = boundary.chain._indirect_torrent_cache_key(context.torrent_info)
    assert boundary.cache.get(key, region="torrents") is None


def test_resource_without_stable_identity_does_not_share_cache(boundary):
    """
    无法确认种子身份时只保留既有 URL 缓存，不按标题共享资源内容。
    """
    torrent = _context(page_url=None).torrent_info
    for _ in range(2):
        boundary.chain.download_torrent(torrent)
    assert boundary.port.request.call_count == 2
    assert boundary.chain._indirect_torrent_cache_key(torrent) is None
