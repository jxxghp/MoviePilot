"""
逐站刷新以真实缓存序列化和线程屏障验证短锁、合并及识别修正保护。
"""

import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.application.configuration import ChainRuntimeConfig
from app.application.site.observation import report_site_search_outcome
from app.chain import torrents as torrents_module
from app.chain.base import ChainBase
from app.chain.torrents import TorrentsChain
from app.domain.context import Context, MediaInfo, TorrentInfo
from app.runtime.cache import cache_mutation
from app.schemas.types import MediaType


def _site(site_id: int) -> dict:
    """
    构造两个完全隔离的合成站点。
    """
    return {"id": site_id, "name": f"站点{site_id}", "domain": f"https://site{site_id}.example/"}


def _torrent(site_id: int, torrent_id: int = 1) -> TorrentInfo:
    """
    使用稳定详情页 ID，保留影视分类及可轮换的下载地址。
    """
    return TorrentInfo(site=site_id, title=f"Series {site_id} S01E{torrent_id:02d}",
                       category=MediaType.TV.value, enclosure=f"https://site{site_id}.example/download.php?id={torrent_id}",
                       page_url=f"https://site{site_id}.example/details.php?id={torrent_id}")


@pytest.fixture
def chain(monkeypatch):
    """
    仅替换缓存和外部识别边界，保留真实同步序列化和所有刷新逻辑。
    """
    storage = {}
    owner = object.__new__(TorrentsChain)
    owner.filecache = SimpleNamespace(get=lambda key: storage.get(key), set=lambda key, value: storage.__setitem__(key, value))
    owner.runtime_config = ChainRuntimeConfig(media_extensions=())
    owner._build_refresh_context = Mock(side_effect=lambda torrent, _stype: Context(torrent_info=torrent))
    monkeypatch.setattr(TorrentsChain, "_refresh_sites_lock", threading.Lock())
    monkeypatch.setattr(TorrentsChain, "_refreshing_sites", set())
    monkeypatch.setattr(torrents_module.TorrentHelper, "is_invalid", lambda _self, _url: False)
    monkeypatch.setattr(torrents_module, "site_request_cooldown", lambda _site: 0)
    return owner


def test_parallel_sites_preserve_both_caches_and_do_not_hold_lock_during_recognition(chain):
    """
    首站识别阻塞期间第二站仍能保存；首站随后合并不会覆盖第二站的数据。
    """
    started, release = threading.Event(), threading.Event()

    def identify(torrent, _stype):
        """
        仅阻塞首站的识别阶段。
        """
        if torrent.site == 1:
            started.set()
            assert release.wait(5)
        return Context(torrent_info=torrent)

    chain._build_refresh_context.side_effect = identify
    chain.browse = lambda domain, page: [] if page else [_torrent(1 if domain == "site1.example" else 2)]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(chain.refresh_site, _site(1))
        try:
            assert started.wait(2)
            second = workers.submit(chain.refresh_site, _site(2))
            assert second.result(timeout=2)[0] == "site2.example"
            assert not first.done()
            assert chain.load_cache(chain.cache_file)["site2.example"]
        finally:
            release.set()
        first.result(timeout=2)
    assert set(chain.load_cache(chain.cache_file)) == {"site1.example", "site2.example"}
    assert not chain._refreshing_sites


def test_same_site_is_not_reentered_and_busy_call_returns_current_candidates(chain):
    """
    同站在途时不能再发请求，兼容调用仍能读取该站已经存在的候选。
    """
    started, release = threading.Event(), threading.Event()
    chain.save_cache({"site1.example": [Context(torrent_info=_torrent(1, 1))]}, chain.cache_file)

    def identify(torrent, _stype):
        """
        保留已取得的站点执行权直至释放屏障。
        """
        started.set()
        assert release.wait(5)
        return Context(torrent_info=torrent)

    chain._build_refresh_context.side_effect = identify
    chain.browse = Mock(side_effect=lambda **kwargs: [] if kwargs["page"] else [_torrent(1, 2)])
    with ThreadPoolExecutor(max_workers=1) as workers:
        first = workers.submit(chain.refresh_site, _site(1))
        try:
            assert started.wait(2)
            calls = chain.browse.call_count
            _, candidates, fact = chain.refresh_site(_site(1))
            assert chain.browse.call_count == calls
            assert len(candidates) == 1
            assert fact["status"] == "deferred"
            assert fact["attempted"] is False
        finally:
            release.set()
        first.result(timeout=2)


def test_optimistic_merge_preserves_manual_recognition_written_during_fetch(chain):
    """
    识别新资源期间人工修正旧资源，最终合并保留人工身份及最新下载元数据。
    """
    started, release = threading.Event(), threading.Event()
    old = Context(torrent_info=_torrent(1, 1), media_info=MediaInfo(title="原识别"))
    chain.save_cache({"site1.example": [old]}, chain.cache_file)
    latest = _torrent(1, 1)
    latest.seeders = 10
    chain.browse = lambda domain, page: [] if page else [latest, _torrent(1, 2)]

    def identify(torrent, _stype):
        """
        在缓存读取后阻塞新资源识别，为人工修正制造确定窗口。
        """
        started.set()
        assert release.wait(5)
        return Context(torrent_info=torrent)

    chain._build_refresh_context.side_effect = identify
    with ThreadPoolExecutor(max_workers=1) as workers:
        future = workers.submit(chain.refresh_site, _site(1))
        try:
            assert started.wait(2)
            with cache_mutation("torrent-snapshots"):
                corrected = chain.load_cache(chain.cache_file)
                corrected["site1.example"][0].media_info = MediaInfo(title="人工修正")
                chain.save_cache(corrected, chain.cache_file)
        finally:
            release.set()
        future.result(timeout=2)
    cache = chain.load_cache(chain.cache_file)["site1.example"]
    assert len(cache) == 2
    assert cache[0].media_info.title == "人工修正"
    assert cache[0].torrent_info.seeders == 10


@pytest.mark.parametrize("attempted,outcome", [(True, "success"), (True, "failed"), (False, "deferred")])
def test_empty_page_retains_request_outcome_instead_of_inferring_success(chain, monkeypatch, attempted, outcome):
    """
    成功空页、失败和限流虽同样返回空列表，排程仍能得到不同的执行事实。
    """
    def browse(**_kwargs):
        """
        在真实观察上下文内发布索引器事实。
        """
        report_site_search_outcome(attempted=attempted, outcome=outcome)
        return []

    chain.browse = browse
    monkeypatch.setattr(torrents_module, "site_request_cooldown", lambda _site: 600)
    _, _, fact = chain.refresh_site(_site(1))
    assert fact["attempted"] is attempted
    assert fact["status"] == outcome
    assert fact["retry_after"] == 600


def test_failed_cache_write_is_reported_and_site_owner_is_released(chain):
    """
    缓存保存失败必须传播，同时释放真实已结束的站点 owner。
    """
    chain.browse = lambda **_kwargs: []
    chain.filecache.set = Mock(side_effect=OSError("synthetic disk failure"))
    with pytest.raises(OSError, match="synthetic disk failure"):
        chain.refresh_site(_site(1))
    assert not chain._refreshing_sites


def test_music_later_page_deferred_preserves_earlier_request_fact(chain):
    """
    音乐首页成功、后页限流时仍记录已请求，不能把整站误判为未访问而追加预算。
    """
    def browse(*, mtype=None, page=0, **_kwargs):
        """
        普通页成功为空，音乐首页有资源，音乐后页发布未请求的限流事实。
        """
        deferred = mtype is not None and page == 1
        report_site_search_outcome(attempted=not deferred, outcome="deferred" if deferred else "success")
        if mtype is not None and page == 0:
            torrent = _torrent(1)
            torrent.category = MediaType.MUSIC.value
            return [torrent]
        return []

    chain.browse = browse
    indexer = {**_site(1), "search": {"paths": [{"path": "/all"}, {"type": "music", "path": "/music"}]}}
    _, contexts, fact = chain.refresh_site(indexer, include_music=True)
    assert len(contexts) == 1
    assert fact["attempted"] is True
    assert fact["status"] == "deferred"


def test_sync_cache_strict_mode_preserves_legacy_nonthrowing_default(chain):
    """
    确认保存的宿主路径传播失败，原插件调用仍保留记录日志后返回的默认行为。
    """
    chain.filecache.set = Mock(side_effect=OSError("synthetic disk failure"))
    ChainBase.save_cache(chain, {}, "synthetic-cache")
    with pytest.raises(OSError):
        ChainBase.save_cache(chain, {}, "synthetic-cache", strict=True)
