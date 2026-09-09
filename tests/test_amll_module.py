"""AMLL 原生歌词接口的严格匹配、请求隔离和缓存合同测试。"""

from collections import deque
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from importlib import import_module
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from requests.exceptions import Timeout

from app.chain.lyrics import LyricsChain
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.modules.amll import module as amll
from app.modules.amll.module import AmllModule
from app.modules.lrclib import LrclibModule
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher

_TTML = """<tt xmlns="http://www.w3.org/ns/ttml" xml:lang="zh">
<body><div><p begin="1" end="2"><span begin="1" end="1.5">你好</span><span begin="1.5" end="2">世界</span></p></div></body>
</tt>"""


class _Response:
    """回放原生接口响应并记录连接关闭行为。"""

    def __init__(self, payload: Any, status: int = 200, headers=None) -> None:
        """保存响应状态、正文及限流提示。"""
        self.payload = payload
        self.status_code = status
        self.headers = headers or {}
        self.text = "response"
        self.closed = False

    def json(self) -> Any:
        """返回预设原生响应，或模拟 JSON 解码失败。"""
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def close(self) -> None:
        """记录请求完成后已释放响应连接。"""
        self.closed = True


def _song(song_id: int = 1, **values: Any) -> dict:
    """构造具有多值名称列表的 AMLL 歌曲记录。"""
    return {
        "id": song_id,
        "filename": f"fixture-{song_id}.ttml",
        "musicNames": ["测试歌曲"],
        "artistNames": ["测试歌手"],
        "albumNames": ["测试专辑"],
        "isrcs": [],
        "format": "ttml",
        **values,
    }


def _search(*songs: dict, has_more: bool = False) -> _Response:
    """回放搜索响应，保持搜索条目不包含完整歌词。"""
    return _Response({"status": 200, "data": {
        "items": list(songs),
        "pagination": {
            "page": 1, "pageSize": 20, "total": len(songs),
            "totalPages": 2 if has_more else 1, "hasMore": has_more,
        },
    }})


def _detail(song_id: int = 1, **values: Any) -> _Response:
    """回放带完整逐词 TTML 的原生详情响应。"""
    return _Response({"status": 200, "data": _song(song_id, **{"lyrics": _TTML, **values})})


@pytest.fixture
def amll_runtime(monkeypatch) -> Iterator[SimpleNamespace]:
    """隔离模块缓存、配置及 HTTP 出口，所有响应在内存中回放。"""
    config = {"AMLL_BASE_URL": "https://api.amll.dev"}
    original_setting = amll.get_runtime_setting
    monkeypatch.setattr(
        amll, "get_runtime_setting",
        lambda key: config[key] if key in config else original_setting(key),
    )
    responses = deque()
    history = []

    def request(url: str, params=None, **_kwargs) -> Any:
        """记录请求参数并禁止任何未声明的外部响应。"""
        history.append((url, params))
        assert responses, f"未声明的 AMLL 请求：{url} {params}"
        response = responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response

    client = Mock()
    client.get_res.side_effect = request
    factory = Mock(return_value=client)
    monkeypatch.setattr(amll, "RequestUtils", factory)
    module = AmllModule()
    module.init_module()
    try:
        yield SimpleNamespace(
            module=module, responses=responses, history=history,
            factory=factory, config=config,
        )
    finally:
        module.init_module()


def test_capability_root_export_preserves_module_identity() -> None:
    """宿主 manifest 使用的惰性包级入口必须返回同一个模块实现类。"""
    assert import_module("app.modules.amll").AmllModule is AmllModule


@pytest.mark.parametrize("music_type", [MusicInfo, MetaMusic])
def test_search_fetches_selected_native_detail_and_preserves_word_lyrics(
    amll_runtime, music_type: type,
) -> None:
    """正确匹配后必须按 ID 取回 TTML，并转换为标准逐词候选。"""
    search = _search(_song())
    detail = _detail()
    amll_runtime.responses.extend([search, detail])
    result = amll_runtime.module.music_lyrics_candidates(music_type(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑", duration=300,
    ))

    assert len(result) == 1
    lyrics = result[0]
    assert lyrics.provider == "amll"
    assert lyrics.provider_id == "1"
    assert lyrics.quality_rank == 4
    assert lyrics.extension == ".lrc"
    assert lyrics.synced_lyrics == "[00:01.00]你好世界"
    assert lyrics.plain_lyrics == "你好世界"
    assert lyrics.lyricsfile
    assert amll_runtime.history == [
        ("https://api.amll.dev/v1/lyrics/search", {
            "musicName": "测试歌曲", "artistName": "测试歌手",
            "albumName": "测试专辑", "page": 1, "pageSize": 20,
        }),
        ("https://api.amll.dev/v1/lyrics/get", {"id": "1"}),
    ]
    assert search.closed and detail.closed
    assert all(call.kwargs["timeout"] == 10 for call in amll_runtime.factory.call_args_list)
    assert not hasattr(amll_runtime.module, "music_lyrics")


@pytest.mark.parametrize("metadata", [
    {}, {"title": "测试歌曲"}, {"artists": ["测试歌手"]},
    {"isrc": "not-an-isrc"},
])
def test_insufficient_identity_skips_network(amll_runtime, metadata: dict) -> None:
    """无合法录音编码且曲名或艺术家缺失时不得模糊下载歌词。"""
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(**metadata)) == []
    assert amll_runtime.history == []


@pytest.mark.parametrize("metadata", [
    {"musicNames": ["测试歌曲 (Live)"]},
    {"musicNames": ["测试歌曲 Remix"]},
    {"musicNames": ["另一首测试歌曲"]},
    {"artistNames": ["另一位测试歌手"]},
    {"albumNames": ["测试专辑 现场版"]},
    {"albumNames": []},
])
def test_search_rejects_fuzzy_title_artist_album_and_versions(
    amll_runtime, metadata: dict,
) -> None:
    """服务端包含匹配结果不得被误当作原曲、原唱或原专辑。"""
    amll_runtime.responses.append(_search(_song(**metadata)))
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []
    assert len(amll_runtime.history) == 1


def test_alias_matching_normalizes_width_case_and_whitespace(amll_runtime) -> None:
    """多值别名中的全半角、大小写及空白差异应允许同一音轨匹配。"""
    metadata = {
        "musicNames": ["别名", "ＡＢＣ Song"],
        "artistNames": ["guest", "ＡＲＴＩＳＴ"],
        "albumNames": ["其他发行", "Album"],
    }
    amll_runtime.responses.extend([_search(_song(**metadata)), _detail(**metadata)])
    result = amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="abc song", artists=[" artist ", "GUEST"], album=" album ",
    ))
    assert len(result) == 1


def test_search_requires_all_supplied_artists(amll_runtime) -> None:
    """已知合作歌手时不得把缺少合作者的同名单人版本当作命中。"""
    amll_runtime.responses.append(_search(_song()))
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手", "合作歌手"], album="测试专辑",
    )) == []
    assert len(amll_runtime.history) == 1


@pytest.mark.parametrize(("title", "studio_title", "live_title"), [
    ("测试歌曲", "测试歌曲", "测试歌曲 (Live)"),
    ("Live Forever", "Live Forever", "Live Forever (Live)"),
    ("Track (Live)", "Track", "Track (Live)"),
])
def test_explicit_version_matches_only_corresponding_title(
    amll_runtime, title: str, studio_title: str, live_title: str,
) -> None:
    """单独版本信息必须参加匹配，不能让原版候选覆盖现场版音轨。"""
    live = {"musicNames": [live_title]}
    amll_runtime.responses.extend([
        _search(_song(musicNames=[studio_title]), _song(2, **live)), _detail(2, **live),
    ])
    result = amll_runtime.module.music_lyrics_candidates(MetaMusic(
        title=title, artists=["测试歌手"], album="测试专辑", version="Live",
    ))
    assert [lyrics.provider_id for lyrics in result] == ["2"]
    assert amll_runtime.history[0][1]["musicName"] == live_title
    assert amll_runtime.history[-1][1] == {"id": "2"}


def test_isrc_lookup_works_without_title_and_skips_search(amll_runtime) -> None:
    """合法 ISRC 精确命中时无需曲名或搜索，规范化编码后直接读取完整歌词。"""
    amll_runtime.responses.append(_detail(isrcs=["USABC2300001"]))
    result = amll_runtime.module.music_lyrics_candidates(MusicInfo(isrc="us-abc-23-00001"))
    assert len(result) == 1
    assert amll_runtime.history == [
        ("https://api.amll.dev/v1/lyrics/get", {"isrc": "USABC2300001"}),
    ]


@pytest.mark.parametrize("isrc_response", ["missing", "wrong-isrc"])
def test_isrc_miss_falls_back_to_strict_search(amll_runtime, isrc_response: str) -> None:
    """录音编码未找到或返回身份不符时必须回退严格元数据匹配。"""
    response = (
        _Response({"status": 404}, status=404)
        if isrc_response == "missing" else _detail(isrcs=["USABC2300002"])
    )
    amll_runtime.responses.extend([response, _search(_song()), _detail()])
    result = amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑", isrc="USABC2300001",
    ))
    assert len(result) == 1
    assert [url.rsplit("/", 1)[-1] for url, _ in amll_runtime.history] == ["get", "search", "get"]


@pytest.mark.parametrize("detail_values", [
    {"musicNames": ["别的歌曲"]}, {"artistNames": ["别的歌手"]},
    {"albumNames": ["别的专辑"]},
])
def test_detail_metadata_is_revalidated(amll_runtime, detail_values: dict) -> None:
    """详情数据与候选身份不符时不得把歌词写入当前音轨。"""
    amll_runtime.responses.extend([_search(_song()), _detail(**detail_values)])
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []


def test_detail_id_must_match_selected_search_item(amll_runtime) -> None:
    """选中歌词 ID 与详情 ID 不一致时必须拒绝该响应。"""
    amll_runtime.responses.extend([_search(_song()), _detail(2)])
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []


@pytest.mark.parametrize("song_id", [0, -1, True, "1", 2 ** 53])
def test_invalid_search_ids_are_not_downloaded(amll_runtime, song_id: Any) -> None:
    """只允许官方合同中的有效整数 ID 进入详情下载，避免异常数据选错身份。"""
    amll_runtime.responses.append(_search(_song(song_id)))
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []
    assert len(amll_runtime.history) == 1


@pytest.mark.parametrize("metadata", [{"format": "lrc"}, {"lyrics": None}, {"lyrics": "<invalid"}])
def test_invalid_detail_content_is_not_returned(amll_runtime, metadata: dict) -> None:
    """缺失、损坏或不支持格式的歌词详情不能成为可落盘的候选。"""
    amll_runtime.responses.extend([_search(_song()), _detail(**metadata)])
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []


def test_search_and_detail_requests_are_bounded(amll_runtime) -> None:
    """即使服务端还有更多页或候选详情失败，也只能查询一页和最多三个详情。"""
    amll_runtime.responses.append(_search(*(_song(i) for i in range(1, 21)), has_more=True))
    amll_runtime.responses.extend(_Response({"status": 404}, status=404) for _ in range(3))
    assert amll_runtime.module.music_lyrics_candidates(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    )) == []
    assert len(amll_runtime.history) == 4


@pytest.mark.parametrize("failure", ["server", "invalid-json", "timeout"])
def test_transient_failure_is_not_cached_and_response_is_closed(amll_runtime, failure: str) -> None:
    """网络或解析失败不能成为永久未匹配结果，下一次请求应能恢复。"""
    response = {
        "server": _Response({"status": 500}, status=500),
        "invalid-json": _Response(ValueError("invalid JSON")),
        "timeout": Timeout("timeout"),
    }[failure]
    amll_runtime.responses.extend([response, _search(_song()), _detail()])
    music = MusicInfo(title="测试歌曲", artists=["测试歌手"], album="测试专辑")
    assert amll_runtime.module.music_lyrics_candidates(music) == []
    assert len(amll_runtime.module.music_lyrics_candidates(music)) == 1
    if isinstance(response, _Response):
        assert response.closed


def test_success_cache_and_configured_base_url_are_isolated(amll_runtime, monkeypatch) -> None:
    """同曲重复查询应复用缓存，切换歌词镜像后不能误用旧地址的响应。"""
    cache = Mock(wraps=amll.Cache(maxsize=1024, ttl=3600))
    monkeypatch.setattr(amll, "Cache", Mock(return_value=cache))
    amll_runtime.responses.extend([_search(_song()), _detail()])
    music = MusicInfo(title="测试歌曲", artists=["测试歌手"], album="测试专辑")
    assert len(amll_runtime.module.music_lyrics_candidates(music)) == 1
    assert len(amll_runtime.module.music_lyrics_candidates(music)) == 1
    assert len(amll_runtime.history) == 2
    assert [call.kwargs["ttl"] for call in cache.set.call_args_list] == [3600, 604800]

    amll_runtime.config["AMLL_BASE_URL"] = "https://mirror.example/"
    amll_runtime.responses.extend([_search(_song(2)), _detail(2)])
    result = amll_runtime.module.music_lyrics_candidates(music)
    assert [lyrics.provider_id for lyrics in result] == ["2"]
    assert amll_runtime.history[-1][0] == "https://mirror.example/v1/lyrics/get"


@pytest.mark.parametrize("status", [200, 404])
def test_not_found_result_uses_negative_cache(amll_runtime, monkeypatch, status: int) -> None:
    """搜索空列表和未找到响应都应仅缓存五分钟，不因分页信息非空而延长。"""
    cache = Mock(wraps=amll.Cache(maxsize=1024, ttl=3600))
    monkeypatch.setattr(amll, "Cache", Mock(return_value=cache))
    response = _search() if status == 200 else _Response({"status": 404}, status=404)
    amll_runtime.responses.append(response)
    music = MusicInfo(title="测试歌曲", artists=["测试歌手"])
    assert amll_runtime.module.music_lyrics_candidates(music) == []
    assert amll_runtime.module.music_lyrics_candidates(music) == []
    assert len(amll_runtime.history) == 1
    assert response.closed
    cache.set.assert_called_once()
    assert cache.set.call_args.kwargs["ttl"] == 300


def test_module_reload_clears_cached_lyrics_and_cooldown(amll_runtime) -> None:
    """标准模块重载入口应正常执行并让下一次查询取得更新后的歌词。"""
    amll_runtime.responses.extend([_search(_song()), _detail()])
    music = MusicInfo(title="测试歌曲", artists=["测试歌手"], album="测试专辑")
    assert len(amll_runtime.module.music_lyrics_candidates(music)) == 1
    amll_runtime.module._cooldown_until = float("inf")

    amll_runtime.module.on_config_changed()
    amll_runtime.responses.extend([_search(_song(2)), _detail(2)])
    result = amll_runtime.module.music_lyrics_candidates(music)
    assert [lyrics.provider_id for lyrics in result] == ["2"]
    assert len(amll_runtime.history) == 4


def test_retry_after_http_date_is_supported(amll_runtime, monkeypatch) -> None:
    """日期形式的限流提示应得到有限冷却时间。"""
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    monkeypatch.setattr(amll, "datetime", SimpleNamespace(now=lambda _timezone: now))
    retry_after = format_datetime(now + timedelta(seconds=60), usegmt=True)
    assert amll_runtime.module._retry_after_seconds(retry_after) == 60


@pytest.mark.parametrize("available", [True, False])
def test_amll_candidates_join_real_lyrics_dispatch_without_blocking_lrclib(
    amll_runtime, monkeypatch, available: bool,
) -> None:
    """真实歌词调度应优先选择逐词候选，AMLL 失败时仍保留内置来源结果。"""
    builtin = LrclibModule()
    monkeypatch.setattr(builtin, "_request_json", Mock(return_value={
        "id": 100, "plainLyrics": "内置纯文本歌词", "instrumental": False,
    }))
    amll_runtime.responses.extend(
        [_search(_song()), _detail()] if available else [Timeout("AMLL timeout")],
    )
    modules = [amll_runtime.module, builtin]
    module_error = Mock()

    def running_modules(method: str) -> list:
        """让真实调度器只看到实现了当前接口的宿主模块。"""
        return [module for module in modules if hasattr(module, method)]

    dispatcher = ModuleInvocationDispatcher(
        module_catalog=SimpleNamespace(
            get_running_modules=running_modules,
        ),
        plugin_catalog=SimpleNamespace(get_plugin_modules=lambda: {}),
        plugin_error_handler=module_error,
        system_error_handler=module_error,
        rate_limit_handler=module_error,
    )
    chain = object.__new__(LyricsChain)
    chain._module_dispatcher = dispatcher
    chain.deadline = None
    chain.budget_exceeded = False

    result = chain.get_music_lyrics(MusicInfo(
        title="测试歌曲", artists=["测试歌手"], album="测试专辑",
    ))

    assert result is not None
    assert result.provider == ("amll" if available else "lrclib")
    assert result.quality_rank == (4 if available else 1)
    assert len(amll_runtime.history) == (2 if available else 1)
    module_error.assert_not_called()


@pytest.mark.parametrize("status", [429, 503])
def test_rate_limit_uses_bounded_cooldown_without_sleep(
    amll_runtime, monkeypatch, status: int,
) -> None:
    """限流立即回退且冷却期不再出站，超长等待提示不能阻塞刮削线程。"""
    clock = [100.0]
    monkeypatch.setattr(amll.time, "monotonic", lambda: clock[0])
    sleep = Mock(side_effect=AssertionError("AMLL 不应主动等待"))
    monkeypatch.setattr(amll.time, "sleep", sleep)
    response = _Response({"status": status}, status=status, headers={"Retry-After": "3600"})
    amll_runtime.responses.append(response)
    music = MusicInfo(title="测试歌曲", artists=["测试歌手"], album="测试专辑")

    assert amll_runtime.module.music_lyrics_candidates(music) == []
    assert amll_runtime.module.music_lyrics_candidates(music) == []
    assert len(amll_runtime.history) == 1
    assert response.closed
    assert 100 < amll_runtime.module._cooldown_until <= 400
    clock[0] = 401.0
    amll_runtime.responses.extend([_search(_song()), _detail()])
    assert len(amll_runtime.module.music_lyrics_candidates(music)) == 1
    sleep.assert_not_called()
