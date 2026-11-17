"""
媒体库查询失败不得被当作缺集：#7444 回归测试。

Issue 背景：Jellyfin 查询失败时 `get_tv_episodes()` 返回 `(None, None)`，
与“未找到”`(None, {})` 在 `media_exists()` 里被合并处理，订阅链随后把整季
判定为缺失（已下载 157..167 集时误判缺失 189 集），并触发重复下载。

修复口径：查询失败以 `MediaServerQueryError` 显式区分，订阅入口在本轮跳过，
不更新缺集、不触发补下；查询正常与真正未找到的行为保持不变。
"""

from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import Mock, patch

import pytest

from app.application.subscription.contract import SubscriptionSnapshot
from app.chain.base import ChainBase
from app.chain.download.existence import DownloadExistenceOwner
from app.chain.subscribe import metadata as metadata_module
from app.chain.subscribe import refresh as refresh_module
from app.chain.subscribe import search as search_module
from app.chain.subscribe.completion import SubscribeCompletionOwner
from app.chain.subscribe.facade import SubscribeChain
from app.chain.subscribe.metadata import prepare_search_target
from app.domain.context import MediaInfo
from app.domain.metainfo import MetaInfo
from app.modules._base.mediaserver import _MediaServerModuleBase
from app.modules.navidrome import NavidromeModule
from app.modules.plex import PlexModule
from app.runtime.extensions.module.dispatcher import (
    ModuleInvocationDispatcher as ModuleDispatcher,
)
from app.schemas.mediaserver import ExistMediaInfo
from app.schemas.types import MediaSource, MediaType

_MEDIA_KEY = "tmdb:12345"


def _query_error_type() -> Any:
    """
    按需引入查询失败异常；修复前该名称不存在。

    只在异常已抛出后或替身触发时调用，避免修复前的 ImportError 掩盖
    调用点本身的 TypeError 等 fail-first 信号。

    :return: 媒体服务器查询失败异常类型
    """
    from app.application.mediaserver import MediaServerQueryError

    return MediaServerQueryError


def _raise_query_error(*_args: Any, **_kwargs: Any) -> Any:
    """
    替身触发的查询失败异常构造器。

    :return: 永不返回
    """
    raise _query_error_type()("Jellyfin 查询失败")


def _media_info() -> MediaInfo:
    """
    构造 issue 中的剧集识别信息：单季 200 集。

    :return: 具有稳定 TMDB 身份的电视剧媒体信息
    """
    return MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="12345",
        tmdb_id=12345,
        type=MediaType.TV,
        title="TestShow",
        year="2023",
        seasons={1: list(range(1, 201))},
        episode_group=None,
    )


def _build_subscribe(**overrides: Any) -> SubscriptionSnapshot:
    """
    构造普通电视剧订阅快照。

    :param overrides: 覆盖默认订阅字段
    :return: 订阅快照
    """
    data: dict[str, Any] = {
        "id": 1,
        "name": "TestShow",
        "season": 1,
        "best_version": 0,
        "best_version_full": 0,
        "type": MediaType.TV.value,
        "start_episode": 1,
        "total_episode": 200,
        "current_priority": None,
        "episode_priority": None,
        "lack_episode": 200,
        "state": "R",
        "note": list(range(157, 168)),
        "manual_total_episode": 0,
        "year": "2023",
        "media_source": "themoviedb",
        "media_id": "12345",
        "episode_group": None,
        "poster": None,
        "backdrop": None,
        "description": None,
        "last_update": None,
        "username": None,
        "custom_words": None,
    }
    data.update(overrides)
    return SubscriptionSnapshot(**data)


def _existence_owner() -> DownloadExistenceOwner:
    """
    构造缺集计算对象，仅隔离媒体服务器查询边界。

    :return: 未初始化的缺集计算对象
    """
    owner = DownloadExistenceOwner.__new__(DownloadExistenceOwner)
    owner.media_server_repository = SimpleNamespace(get_item_id=lambda **_kwargs: None)
    return owner


def _fake_service(result: Any) -> SimpleNamespace:
    """
    构造返回固定剧集查询结果的媒体服务器替身。

    :param result: `get_tv_episodes` 应返回的二元组
    :return: 仅实现剧集查询的服务器替身
    """
    return SimpleNamespace(get_tv_episodes=lambda **_kwargs: result)


class _ConcreteMediaServerModule(_MediaServerModuleBase):
    """
    仅实现抽象生命周期钩子的媒体服务器模块测试替身。
    """

    def init_module(self) -> None:
        """
        测试替身不执行模块初始化。
        """

    def init_setting(self) -> None:
        """
        测试替身不声明启用配置项。

        :return: 始终不启用
        """
        return None

    def stop(self) -> None:
        """
        测试替身不执行模块停止逻辑。
        """


def _media_server_module(services: dict[str, Any]) -> _MediaServerModuleBase:
    """
    构造仅隔离服务实例的媒体服务器模块对象。

    :param services: 服务器名称到服务替身的映射
    :return: 媒体服务器模块对象
    """
    module = _ConcreteMediaServerModule.__new__(_ConcreteMediaServerModule)
    module._query_options_supported = True
    module.get_instances = lambda: services  # type: ignore[method-assign]
    module.get_instance = lambda name: services.get(name)  # type: ignore[method-assign]
    return module


def test_strict_query_failure_raises() -> None:
    """
    严格口径下媒体库查询失败应抛异常，而不是写出整季缺失。
    """
    owner = _existence_owner()
    owner._media_exists_with_server_cache = Mock(side_effect=_raise_query_error)  # type: ignore[method-assign]

    with pytest.raises(Exception) as excinfo:
        owner.get_no_exists_info(
            MetaInfo("TestShow"), _media_info(), totals={1: 200}, raise_on_query_failure=True,
        )

    assert isinstance(excinfo.value, _query_error_type())


def test_query_failure_without_strict_flag_keeps_whole_season_marker() -> None:
    """
    默认口径保持既有行为：查询失败仍按整季缺失处理，供交互下载等入口复用。
    """
    owner = _existence_owner()
    owner._media_exists_with_server_cache = Mock(return_value=None)  # type: ignore[method-assign]

    exist_flag, missing = owner.get_no_exists_info(MetaInfo("TestShow"), _media_info(), totals={1: 200})

    assert exist_flag is False
    assert missing[_MEDIA_KEY][1].episodes == []
    assert missing[_MEDIA_KEY][1].total_episode == 200


def test_not_found_with_strict_flag_keeps_whole_season_marker() -> None:
    """
    严格口径下真正未找到仍应写出整季缺失，只区分查询失败。
    """
    owner = _existence_owner()
    owner._media_exists_with_server_cache = Mock(return_value=None)  # type: ignore[method-assign]

    exist_flag, missing = owner.get_no_exists_info(
        MetaInfo("TestShow"), _media_info(), totals={1: 200}, raise_on_query_failure=True,
    )

    assert exist_flag is False
    assert missing[_MEDIA_KEY][1].episodes == []
    assert missing[_MEDIA_KEY][1].total_episode == 200


def test_query_success_with_strict_flag_returns_only_truly_missing() -> None:
    """
    严格口径下查询正常时只返回真正缺失的集。
    """
    owner = _existence_owner()
    owner._media_exists_with_server_cache = Mock(  # type: ignore[method-assign]
        return_value=ExistMediaInfo(type=MediaType.TV, seasons={1: list(range(1, 168))})
    )

    exist_flag, missing = owner.get_no_exists_info(
        MetaInfo("TestShow"), _media_info(), totals={1: 200}, raise_on_query_failure=True,
    )

    assert exist_flag is False
    assert sorted(missing[_MEDIA_KEY][1].episodes) == list(range(168, 201))


def test_server_cache_raises_when_all_servers_fail(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    全部服务器查询失败时缓存层应抛异常，不返回“未找到”。
    """
    owner = _existence_owner()
    monkeypatch.setattr(
        "app.chain.download.existence.get_mediaserver_configs",
        lambda: [SimpleNamespace(name="Jellyfin")],
    )
    owner.media_exists = Mock(side_effect=_raise_query_error)  # type: ignore[method-assign]

    with pytest.raises(Exception) as excinfo:
        owner._media_exists_with_server_cache(_media_info(), {}, raise_on_query_failure=True)

    assert isinstance(excinfo.value, _query_error_type())


def test_server_cache_returns_confirmed_episodes_when_one_server_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    一台服务器失败、另一台确认存在时，应合并已确认的集，不抛异常。
    """
    owner = _existence_owner()
    monkeypatch.setattr(
        "app.chain.download.existence.get_mediaserver_configs",
        lambda: [SimpleNamespace(name="Jellyfin"), SimpleNamespace(name="Emby")],
    )

    def _media_exists(mediainfo: MediaInfo, **kwargs: Any) -> Optional[ExistMediaInfo]:
        """
        按服务器名分别模拟查询失败与查询成功。

        :param mediainfo: 本次查询的媒体信息
        :param kwargs: 透传的查询选项，含服务器名
        :return: 已确认存在的剧集信息或抛查询失败异常
        """
        if kwargs.get("server") == "Jellyfin":
            return _raise_query_error()
        return ExistMediaInfo(type=MediaType.TV, seasons={1: list(range(1, 168))})

    owner.media_exists = Mock(side_effect=_media_exists)  # type: ignore[method-assign]

    result = owner._media_exists_with_server_cache(_media_info(), {}, raise_on_query_failure=True)

    assert result is not None
    assert result.seasons == {1: list(range(1, 168))}


def test_server_cache_not_found_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    全部服务器确认未找到时缓存层应返回 None。
    """
    owner = _existence_owner()
    monkeypatch.setattr(
        "app.chain.download.existence.get_mediaserver_configs",
        lambda: [SimpleNamespace(name="Jellyfin")],
    )
    owner.media_exists = Mock(return_value=None)  # type: ignore[method-assign]

    assert owner._media_exists_with_server_cache(_media_info(), {}) is None


def test_media_exists_strict_raises_on_query_failure() -> None:
    """
    严格口径下剧集查询返回两项 None 应抛异常，而不是记为“没有在媒体库”。
    """
    module = _media_server_module({"Jellyfin": _fake_service((None, None))})

    with pytest.raises(Exception) as excinfo:
        module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True)

    assert isinstance(excinfo.value, _query_error_type())


def test_media_exists_strict_returns_none_when_not_found() -> None:
    """
    严格口径下剧集查询返回空映射仍应记为未找到。
    """
    module = _media_server_module({"Jellyfin": _fake_service((None, {}))})

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None


def test_media_exists_without_strict_returns_none_on_failure() -> None:
    """
    默认口径保持既有行为：查询失败仍返回 None。
    """
    module = _media_server_module({"Jellyfin": _fake_service((None, None))})

    assert module.media_exists(mediainfo=_media_info()) is None


def test_media_exists_strict_returns_found_episodes() -> None:
    """
    严格口径下查询正常时应返回已确认存在的季集。
    """
    module = _media_server_module({"Jellyfin": _fake_service(("item1", {1: list(range(1, 168))}))})

    result = module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True)

    assert result is not None
    assert result.seasons == {1: list(range(1, 168))}
    assert result.itemid == "item1"


def test_media_exists_strict_tries_other_server_after_failure() -> None:
    """
    严格口径下一台服务器失败时仍应尝试其余服务器，不直接抛异常。
    """
    module = _media_server_module({
        "Jellyfin": _fake_service((None, None)),
        "Emby": _fake_service(("item2", {1: list(range(1, 168))})),
    })

    result = module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True)

    assert result is not None
    assert result.seasons == {1: list(range(1, 168))}
    assert result.server == "Emby"


def test_resolve_subscribe_missing_propagates_query_failure() -> None:
    """
    订阅缺集查询在媒体库查询失败时应抛异常，不把整季写成缺失。
    """
    calls: list[dict[str, Any]] = []

    class _DownloadChain:
        def get_no_exists_info(self, **kwargs: Any) -> Any:
            """
            仅严格口径抛查询失败异常，否则返回预设整季缺失。

            :param kwargs: 缺集查询参数
            :return: 预设整季缺失
            """
            calls.append(kwargs)
            if kwargs.get("raise_on_query_failure"):
                return _raise_query_error()
            return False, {_MEDIA_KEY: {1: SimpleNamespace(
                season=1, episodes=[], total_episode=200, start_episode=1,
                require_complete_coverage=False,
            )}}

    subscribe = _build_subscribe()
    meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)

    with patch.object(refresh_module, "DownloadChain", _DownloadChain):
        with pytest.raises(Exception) as excinfo:
            SubscribeChain().resolve_subscribe_missing(
                subscribe=subscribe, meta=meta, mediainfo=_media_info(), mediakey=_MEDIA_KEY,
            )

    assert isinstance(excinfo.value, _query_error_type())
    assert calls and calls[0].get("raise_on_query_failure") is True


def test_resolve_subscribe_missing_keeps_non_strict_for_movie() -> None:
    """
    电影订阅的缺集查询不使用严格口径，电影服务无法区分查询失败与未找到。
    """
    calls: list[dict[str, Any]] = []

    class _DownloadChain:
        def get_no_exists_info(self, **kwargs: Any) -> Any:
            """
            记录缺集查询参数，并返回未全部存在且无缺集结构。

            :param kwargs: 缺集查询参数
            :return: 未全部存在的标志和空缺集结构
            """
            calls.append(kwargs)
            return False, {}

    movie_subscribe = _build_subscribe(type=MediaType.MOVIE.value, total_episode=1, note=[])
    movie_info = MediaInfo(
        media_source=MediaSource.TMDB,
        media_id="12345",
        tmdb_id=12345,
        type=MediaType.MOVIE,
        title="TestMovie",
        year="2023",
    )
    meta = SimpleNamespace(type=MediaType.MOVIE, begin_season=None, season=None)

    with patch.object(refresh_module, "DownloadChain", _DownloadChain):
        satisfied, no_exists = SubscribeChain().resolve_subscribe_missing(
            subscribe=movie_subscribe, meta=meta, mediainfo=movie_info, mediakey=_MEDIA_KEY,
        )

    assert satisfied is False
    assert no_exists == {}
    assert calls and calls[0].get("raise_on_query_failure") is False


def test_prepare_search_target_returns_none_on_query_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    搜索准备阶段遇到媒体库查询失败应跳过本轮，不生成搜索目标。
    """
    owner = SimpleNamespace(
        check_and_handle_existing_media=Mock(side_effect=_raise_query_error),
    )
    media_chain = SimpleNamespace(recognize_media=lambda **_kwargs: _media_info())
    monkeypatch.setattr(
        metadata_module, "apply_subscription_classification", lambda media, _subscribe: media,
    )

    target = prepare_search_target(
        owner, _build_subscribe(), media_chain, lambda: None,
    )

    assert target is None


def test_refresh_subscribe_progress_reports_failure_on_query_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    进度刷新遇到媒体库查询失败应报告解析失败，不写回缺集进度。
    """
    chain = SubscribeChain()
    monkeypatch.setattr(
        refresh_module, "MediaChain",
        lambda: SimpleNamespace(recognize_media=lambda **_kwargs: _media_info()),
    )
    monkeypatch.setattr(
        chain, "resolve_subscribe_missing", Mock(side_effect=_raise_query_error),
    )

    result = chain.refresh_subscribe_progress(_build_subscribe())

    assert result["updated"] is False
    assert result["reason"] == "media_query_failed"


def test_finish_paged_subscription_skips_finish_on_query_failure() -> None:
    """
    分页搜索结算时遇到媒体库查询失败不得完成订阅。
    """
    subscribe = _build_subscribe()
    finish = Mock()
    owner = SimpleNamespace(
        subscription_repository=SimpleNamespace(get=lambda _subscribe_id: subscribe),
        _SubscribeChain__candidate_contract_changed=lambda _a, _b: False,
        resolve_subscribe_missing=Mock(side_effect=_raise_query_error),
        finish_subscribe_or_not=finish,
    )
    target = SimpleNamespace(
        meta=SimpleNamespace(type=MediaType.TV, begin_season=1, season=1),
        media=_media_info(),
        media_key=_MEDIA_KEY,
    )

    search_module._finish_paged_subscription(owner, subscribe, target)

    finish.assert_not_called()


def test_reconcile_subscription_completion_returns_false_on_query_failure() -> None:
    """
    完成对账遇到媒体库查询失败应返回未完成，不把订阅标为完成。
    """
    owner = SubscribeCompletionOwner.__new__(SubscribeCompletionOwner)
    owner.check_and_handle_existing_media = Mock(  # type: ignore[method-assign]
        side_effect=_raise_query_error
    )
    meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)

    assert owner.reconcile_subscription_completion(_build_subscribe(), meta, _media_info()) is False


def test_media_exists_strict_raises_when_one_server_fails_and_other_not_found() -> None:
    """
    严格口径下一台服务器查询失败、另一台确认未找到时，失败不得被未找到抵消。
    """
    module = _media_server_module({
        "Jellyfin": _fake_service((None, None)),
        "Emby": _fake_service((None, {})),
    })

    with pytest.raises(Exception) as excinfo:
        module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True)

    assert isinstance(excinfo.value, _query_error_type())
    assert "Jellyfin" in str(excinfo.value)


def test_media_exists_strict_returns_none_without_configured_server() -> None:
    """
    未配置媒体服务器时严格口径也应返回 None，不抛异常。
    """
    module = _media_server_module({})

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None


def test_server_cache_without_configured_server_returns_none_on_strict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    未配置媒体服务器时缓存层严格口径应返回 None，不抛异常。
    """
    owner = _existence_owner()
    monkeypatch.setattr(
        "app.chain.download.existence.get_mediaserver_configs",
        lambda: [],
    )
    media_exists = Mock(return_value=None)
    owner.media_exists = media_exists  # type: ignore[method-assign]

    result = owner._media_exists_with_server_cache(_media_info(), {}, raise_on_query_failure=True)

    assert result is None
    media_exists.assert_called_once()
    assert media_exists.call_args.kwargs.get("raise_on_query_failure") is True


def test_server_cache_propagates_raw_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    服务抛原始异常时缓存层应原样传播，不得包进查询失败异常或按未找到处理。
    """
    owner = _existence_owner()
    monkeypatch.setattr(
        "app.chain.download.existence.get_mediaserver_configs",
        lambda: [SimpleNamespace(name="Jellyfin")],
    )
    owner.media_exists = Mock(side_effect=RuntimeError("raw boom"))  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="raw boom"):
        owner._media_exists_with_server_cache(_media_info(), {}, raise_on_query_failure=True)


def test_resolve_subscribe_missing_never_queries_library_for_best_version_tv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    洗版电视剧订阅不得走媒体库缺集查询，严格口径也不应触及媒体库。
    """
    calls: list[dict[str, Any]] = []

    class _DownloadChain:
        def get_no_exists_info(self, **kwargs: Any) -> Any:
            """
            洗版路径调用即为越界，测试应直接失败。

            :param kwargs: 缺集查询参数
            :return: 永不返回
            """
            calls.append(kwargs)
            raise AssertionError("best_version TV must not query the media library")

    subscribe = _build_subscribe(best_version=1)
    meta = SimpleNamespace(type=MediaType.TV, begin_season=1, season=1)
    chain = SubscribeChain()
    monkeypatch.setattr(
        chain, "_SubscribeChain__is_full_best_version_enabled", lambda _subscribe: False,
    )
    monkeypatch.setattr(
        chain, "_get_pending_best_version_episodes",
        lambda _subscribe, **_kwargs: list(range(168, 201)),
    )

    with patch.object(refresh_module, "DownloadChain", _DownloadChain):
        satisfied, no_exists = chain.resolve_subscribe_missing(
            subscribe=subscribe, meta=meta, mediainfo=_media_info(), mediakey=_MEDIA_KEY,
        )

    assert calls == []
    assert satisfied is False
    assert sorted(no_exists[_MEDIA_KEY][1].episodes) == list(range(168, 201))


def test_chain_media_exists_routes_strict_flag_to_strict_dispatch() -> None:
    """
    链路层严格口径必须走严格分发，否则分发器会吞掉提供方异常。
    """
    chain = ChainBase.__new__(ChainBase)
    chain.run_module = Mock(return_value=None)  # type: ignore[method-assign]
    chain.run_module_strict = Mock(return_value=None)  # type: ignore[method-assign]

    assert chain.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None
    chain.run_module_strict.assert_called_once()
    assert chain.run_module_strict.call_args.kwargs.get("raise_on_query_failure") is True
    chain.run_module.assert_not_called()

    chain.run_module.reset_mock()
    chain.run_module_strict.reset_mock()

    assert chain.media_exists(mediainfo=_media_info()) is None
    chain.run_module.assert_called_once()
    assert chain.run_module.call_args.kwargs.get("raise_on_query_failure") is False
    chain.run_module_strict.assert_not_called()


def test_plex_media_exists_accepts_strict_flag() -> None:
    """
    Plex 复写的方法必须接受严格口径参数；Plex 无法区分失败与未找到，参数不改变行为。
    """
    module = PlexModule.__new__(PlexModule)
    module._query_options_supported = True
    module.get_instances = lambda: {}  # type: ignore[method-assign]
    module.get_instance = lambda _name: None  # type: ignore[method-assign]

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None


def test_plex_tv_query_failure_is_treated_as_not_found() -> None:
    """
    Plex 剧集查询失败与未找到均返回空结果，严格口径下也应返回 None 而不是抛异常。
    """
    module = PlexModule.__new__(PlexModule)
    module._query_options_supported = True
    service = SimpleNamespace(get_tv_episodes=lambda **_kwargs: (None, None))
    module.get_instances = lambda: {"Plex": service}  # type: ignore[method-assign]
    module.get_instance = lambda _name: service  # type: ignore[method-assign]

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None


def test_plex_tv_not_found_returns_none_on_strict_flag() -> None:
    """
    Plex 剧集查询确认未找到时严格口径应返回 None。
    """
    module = PlexModule.__new__(PlexModule)
    module._query_options_supported = True
    service = SimpleNamespace(get_tv_episodes=lambda **_kwargs: (None, {}))
    module.get_instances = lambda: {"Plex": service}  # type: ignore[method-assign]
    module.get_instance = lambda _name: service  # type: ignore[method-assign]

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None


class _FakeDispatchModule:
    """
    仅实现分发所需的优先级与剧集存在性查询的宿主模块替身。
    """

    def __init__(self, priority: int, behavior: Any) -> None:
        """
        构造指定优先级与查询行为的模块替身。

        :param priority: 分发排序用的优先级数值
        :param behavior: 查询时调用的行为函数
        """
        self._priority = priority
        self._behavior = behavior

    def get_priority(self) -> int:
        """
        返回分发排序用的优先级数值。

        :return: 优先级数值
        """
        return self._priority

    def media_exists(
        self, mediainfo: Any, itemid: Any = None, server: Any = None, *,
        query: Any = None, raise_on_query_failure: bool = False,
    ) -> Any:
        """
        按构造时给定的行为返回查询结果。

        :param mediainfo: 本次查询的媒体信息
        :param itemid: 缓存条目标识
        :param server: 指定实例名称
        :param query: 可选查询内容投影
        :param raise_on_query_failure: 严格口径标志，替身直接透传给行为函数
        :return: 行为函数返回的存在性信息
        """
        return self._behavior(mediainfo, server)


class _OldSignatureDispatchModule:
    """
    仅实现修复前签名的宿主模块替身，不接受严格口径参数。
    """

    def __init__(self, priority: int) -> None:
        """
        构造指定优先级的旧签名模块替身。

        :param priority: 分发排序用的优先级数值
        """
        self._priority = priority

    def get_priority(self) -> int:
        """
        返回分发排序用的优先级数值。

        :return: 优先级数值
        """
        return self._priority

    def media_exists(
        self, mediainfo: Any, itemid: Any = None, server: Any = None, *, query: Any = None,
    ) -> Any:
        """
        旧签名查询，始终返回未找到。

        :param mediainfo: 本次查询的媒体信息
        :param itemid: 缓存条目标识
        :param server: 指定实例名称
        :param query: 可选查询内容投影
        :return: 始终为 None
        """
        return None


def _make_dispatcher(modules: list[Any], plugins: Any = None) -> ModuleDispatcher:
    """
    构造仅隔离模块目录与插件目录的真实分发器。

    :param modules: 参与分发的宿主模块替身列表
    :param plugins: 插件目录快照，省略时为空
    :return: 可执行严格与宽松分发的分发器对象
    """
    dispatcher = ModuleDispatcher.__new__(ModuleDispatcher)
    dispatcher._module_catalog = SimpleNamespace(
        get_running_modules=lambda _method: list(modules)
    )
    dispatcher._plugin_catalog = SimpleNamespace(
        get_plugin_modules=lambda: plugins or {}
    )
    dispatcher._plugin_error_handler = lambda *args, **kwargs: None
    dispatcher._system_error_handler = lambda *args, **kwargs: None
    dispatcher._rate_limit_handler = lambda *args, **kwargs: None
    return dispatcher


def _strict_dispatch_kwargs() -> dict[str, Any]:
    """
    构造严格口径分发调用的固定参数。

    :return: 含媒体信息与严格标志的分发参数
    """
    return {
        "mediainfo": _media_info(),
        "itemid": None,
        "server": "Jellyfin",
        "raise_on_query_failure": True,
    }


def test_strict_dispatch_propagates_host_module_query_failure() -> None:
    """
    严格分发必须传播宿主模块的查询失败异常，宽松分发则应吞掉并返回 None。
    """
    def _raising(_mediainfo: Any, _server: Any) -> Any:
        """
        模拟媒体服务器查询失败。

        :return: 永不返回
        """
        raise _query_error_type()("Jellyfin 查询失败")

    strict_dispatcher = _make_dispatcher([_FakeDispatchModule(100, _raising)])
    with pytest.raises(Exception) as excinfo:
        strict_dispatcher.dispatch_strict("media_exists", **_strict_dispatch_kwargs())
    assert isinstance(excinfo.value, _query_error_type())

    lenient_dispatcher = _make_dispatcher([_FakeDispatchModule(100, _raising)])
    assert lenient_dispatcher.dispatch("media_exists", **_strict_dispatch_kwargs()) is None


def test_strict_dispatch_raises_after_earlier_none_module() -> None:
    """
    严格分发时排在前面的模块返回 None 不得阻断后续模块的查询失败异常。
    """
    def _returning_none(_mediainfo: Any, _server: Any) -> Any:
        """
        模拟模块确认未找到。

        :return: 始终为 None
        """
        return None

    def _raising(_mediainfo: Any, _server: Any) -> Any:
        """
        模拟媒体服务器查询失败。

        :return: 永不返回
        """
        raise _query_error_type()("Jellyfin 查询失败")

    dispatcher = _make_dispatcher([
        _FakeDispatchModule(50, _returning_none),
        _FakeDispatchModule(100, _raising),
    ])

    with pytest.raises(Exception) as excinfo:
        dispatcher.dispatch_strict("media_exists", **_strict_dispatch_kwargs())
    assert isinstance(excinfo.value, _query_error_type())


def test_strict_dispatch_stops_after_found_result() -> None:
    """
    严格分发时已找到结果应短路，后续模块的查询失败不得再执行。
    """
    def _returning_found(_mediainfo: Any, _server: Any) -> Any:
        """
        模拟模块确认媒体已入库。

        :return: 已确认存在的季集信息
        """
        return SimpleNamespace(seasons={1: [1, 2]})

    def _raising(_mediainfo: Any, _server: Any) -> Any:
        """
        模拟媒体服务器查询失败。

        :return: 永不返回
        """
        raise _query_error_type()("Emby 查询失败")

    dispatcher = _make_dispatcher([
        _FakeDispatchModule(50, _returning_found),
        _FakeDispatchModule(100, _raising),
    ])

    result = dispatcher.dispatch_strict("media_exists", **_strict_dispatch_kwargs())

    assert result is not None
    assert result.seasons == {1: [1, 2]}


def test_strict_dispatch_drops_flag_for_old_signature_provider() -> None:
    """
    严格分发遇到旧签名提供方时应去掉新参数，不得报意外关键字参数。
    """
    dispatcher = _make_dispatcher([_OldSignatureDispatchModule(100)])

    assert dispatcher.dispatch_strict("media_exists", **_strict_dispatch_kwargs()) is None


def test_navidrome_media_exists_accepts_strict_flag() -> None:
    """
    Navidrome 复写的方法必须接受严格口径参数；只处理音乐，参数不改变行为。
    """
    module = NavidromeModule.__new__(NavidromeModule)
    module._query_options_supported = True
    module.get_instances = lambda: {}  # type: ignore[method-assign]
    module.get_instance = lambda _name: None  # type: ignore[method-assign]

    assert module.media_exists(mediainfo=_media_info(), raise_on_query_failure=True) is None
