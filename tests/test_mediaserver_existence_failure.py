"""
媒体库读取失败不得退化为整季缺失，订阅进度与下载事实必须保留。
"""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from requests import Response

from app.application.mediaserver import MediaServerQueryError
from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.download.existence import DownloadExistenceOwner
from app.chain.subscribe.facade import SubscribeChain
from app.domain.context import MediaInfo
from app.domain.meta.metavideo import MetaVideo
from app.modules.emby import EmbyModule
from app.modules.emby.emby import Emby
from app.modules.jellyfin import JellyfinModule
from app.modules.jellyfin.jellyfin import Jellyfin
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType


def _response(payload: object, status: int = 200) -> Response:
    """
    构造保留真实 HTTP 状态与真假语义的离线响应。
    """
    response = Response()
    response.status_code = status
    response._content = b""
    response._content_consumed = True
    response.json = Mock(return_value=payload)
    return response


@pytest.fixture(params=[(Emby, EmbyModule), (Jellyfin, JellyfinModule)], ids=["emby", "jellyfin"])
def library_query(request, monkeypatch):
    """
    连接真实客户端、模块调度器与缺集链，只替换 HTTP 和持久化出口。
    """
    client_type, module_type = request.param
    client = client_type.__new__(client_type)
    client._host, client._apikey, client.user = "https://media.invalid/", "synthetic-key", "user"
    client._sync_libraries = []
    transport = Mock()
    monkeypatch.setattr(f"{client_type.__module__}.RequestUtils", Mock(return_value=transport))
    module = module_type.__new__(module_type)
    module._instances = {"main": client}
    dispatcher = ModuleInvocationDispatcher(
        module_catalog=Mock(get_running_modules=Mock(return_value=[module])),
        plugin_catalog=Mock(get_plugin_modules=Mock(return_value={})),
        plugin_error_handler=Mock(), system_error_handler=Mock(), rate_limit_handler=Mock(),
    )
    owner = DownloadExistenceOwner.__new__(DownloadExistenceOwner)
    owner._module_dispatcher = dispatcher
    owner.media_server_repository = Mock(get_item_id=Mock(return_value="cached-series"))
    monkeypatch.setattr("app.chain.download.existence.get_mediaserver_configs",
                        lambda: [SimpleNamespace(name="main")])
    media = MediaInfo(type=MediaType.TV, title="示例剧集", year="2023",
                      media_source=MediaSource.TMDB, media_id="123", seasons={1: list(range(1, 201))})
    return owner, transport, media


@pytest.mark.parametrize("phase", ["search", "uncached-search", "detail", "episodes"])
@pytest.mark.parametrize("failure", ["timeout", "http", "malformed", "wrong-items"])
def test_failed_library_query_never_returns_whole_season_missing(library_query, phase, failure):
    """
    标题搜索、详情或分集查询失败都必须中止缺集推断，不能把已有集数变成缺失。
    """
    owner, transport, media = library_query
    failed = {"timeout": None, "http": _response({"Items": []}, 503),
              "malformed": _response({}), "wrong-items": _response({"Items": {}})}[failure]
    series = {"Id": "series", "Type": "Series", "Name": media.title, "ProductionYear": 2023,
              "ProviderIds": {"Tmdb": "123"}}
    if phase == "uncached-search":
        owner.media_server_repository.get_item_id.return_value = None
        transport.get_res.return_value = failed
    elif phase == "search":
        transport.get_res.side_effect = [_response({"Items": []}), failed]
    elif phase == "detail":
        transport.get_res.side_effect = [failed, _response({"Items": [series]}), failed]
    else:
        transport.get_res.side_effect = [_response({"Items": [series]}), failed]
    previous = {"tmdb:other": {}}

    with pytest.raises(MediaServerQueryError, match="媒体库.*查询失败"):
        owner.get_no_exists_info(MetaVideo("示例剧集 S01"), media, no_exists=previous, raise_on_query_failure=True)

    assert previous == {"tmdb:other": {}}


def test_confirmed_absence_still_produces_missing_season(library_query):
    """
    成功查询确认缓存条目与标题均不存在时，继续允许整季补缺。
    """
    owner, transport, media = library_query
    transport.get_res.return_value = _response({"Items": []})

    exists, missing = owner.get_no_exists_info(MetaVideo("示例剧集 S01"), media, raise_on_query_failure=True)

    assert exists is False
    assert missing["tmdb:123"][1].episodes == []
    assert missing["tmdb:123"][1].total_episode == 200


def test_unconfigured_client_cannot_confirm_missing_episodes(library_query):
    """
    启动时连接未就绪或凭据配置缺失，不能放行电视剧补缺。
    """
    owner, _, media = library_query
    module = owner._module_dispatcher._module_catalog.get_running_modules.return_value[0]
    module._instances["main"]._host = None

    with pytest.raises(MediaServerQueryError):
        owner.get_no_exists_info(MetaVideo("示例剧集 S01"), media, raise_on_query_failure=True)


def test_default_existence_call_keeps_error_isolation(library_query):
    """
    未开启严格模式的既有存在性调用保留异常隔离，严格查询则向调用方传递失败。
    """
    owner, transport, media = library_query
    transport.get_res.return_value = None

    assert owner.media_exists(media, server="main") is None
    with pytest.raises(MediaServerQueryError):
        owner.media_exists(media, server="main", raise_on_query_failure=True)


def test_default_server_fallback_also_propagates_query_failures(library_query, monkeypatch):
    """
    不限定媒体服务器的兼容路径同样必须保留查询失败，不能退化成整季缺失。
    """
    owner, transport, media = library_query
    monkeypatch.setattr("app.chain.download.existence.get_mediaserver_configs", lambda: [])
    transport.get_res.return_value = None

    with pytest.raises(MediaServerQueryError):
        owner.get_no_exists_info(MetaVideo("示例剧集 S01"), media, raise_on_query_failure=True)


def test_library_recovery_reuses_real_episodes_without_mutating_download_note(library_query, monkeypatch):
    """
    媒体库故障中断对账，恢复后仍以已有 167 集计算缺集，不改写订阅下载事实。
    """
    owner, transport, media = library_query
    chain = SubscribeChain.__new__(SubscribeChain)
    chain._SubscribeChain__refresh_total_episode_before_completion = Mock(side_effect=lambda **kwargs: kwargs["subscribe"])
    chain._SubscribeChain__refresh_subscribe_progress_with_no_exists = Mock()
    chain.finish_subscribe_or_not = Mock()
    monkeypatch.setattr("app.chain.subscribe.refresh.DownloadChain", lambda: owner)
    subscribe = SubscriptionSnapshot(
        id=7444,
        type=MediaType.TV.value, name=media.title, year="2023", media_source=MediaSource.TMDB,
        media_id="123", season=1, best_version=0, start_episode=1, total_episode=200,
        manual_total_episode=0, lack_episode=33, note=list(range(157, 168)),
    )
    transport.get_res.return_value = None
    meta = MetaVideo("示例剧集 S01")

    with pytest.raises(MediaServerQueryError, match="媒体库.*查询失败"):
        chain.check_and_handle_existing_media(subscribe, meta, media, "tmdb:123")

    chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.assert_not_called()
    chain.finish_subscribe_or_not.assert_not_called()
    assert subscribe.lack_episode == 33
    assert subscribe.note == list(range(157, 168))

    transport.get_res.side_effect = [
        _response({"Items": [{"Id": "series", "Type": "Series", "Name": media.title,
                              "ProviderIds": {"Tmdb": "123"}}]}),
        _response({"Items": [{"ParentIndexNumber": 1, "IndexNumber": ep} for ep in range(1, 168)]}),
    ]
    exists, missing = chain.check_and_handle_existing_media(subscribe, meta, media, "tmdb:123")

    assert exists is False
    assert missing["tmdb:123"][1].episodes == list(range(168, 201))
    chain._SubscribeChain__refresh_subscribe_progress_with_no_exists.assert_called_once()
    assert subscribe.note == list(range(157, 168))


@pytest.mark.parametrize("failed_first", [False, True])
def test_failed_server_cannot_be_hidden_by_another_servers_episodes(library_query, monkeypatch, failed_first):
    """
    任一服务器查询失败时，不得用其他服务器的部分集数做缺集或完成对账。
    """
    owner, _, media = library_query
    monkeypatch.setattr("app.chain.download.existence.get_mediaserver_configs",
                        lambda: [SimpleNamespace(name="main"), SimpleNamespace(name="other")])
    results = [ExistMediaInfo(type=MediaType.TV, seasons={1: [167]}), MediaServerQueryError("查询失败")]
    owner.media_exists = Mock(side_effect=results[::-1] if failed_first else results)

    with pytest.raises(MediaServerQueryError):
        owner.get_no_exists_info(MetaVideo("示例剧集 S01"), media, raise_on_query_failure=True)

    assert all(call.kwargs["raise_on_query_failure"] for call in owner.media_exists.call_args_list)
