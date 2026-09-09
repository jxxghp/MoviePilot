"""公开歌词插件合同从能力注册、候选调度到旁挂文件写入的回归测试。"""

from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from app.application.audio import AudioMetadataHelper
from app.chain.lyrics import LyricsChain
from app.chain.scraping import ScrapingChain
from app.domain import context
from app.modules.lrclib import LrclibModule
from app.runtime.extensions.module.dispatcher import ModuleInvocationDispatcher
from app.runtime.extensions.plugin.projection import PluginProjection
from app.schemas.workflow import FileItem
from app.sdk import media
from app.sdk.media import MetaMusic, MusicInfo, MusicLyrics


class _LyricsPlugin:
    """按插件公开注册协议声明歌词候选实现。"""

    def __init__(self, callback: Callable, enabled: bool = True) -> None:
        """保存候选查询入口和启用状态。"""
        self.callback = callback
        self.enabled = enabled

    def get_name(self) -> str:
        """返回插件能力投影使用的展示名称。"""
        return "测试歌词源"

    def get_state(self) -> bool:
        """只允许启用状态的歌词源参与查询。"""
        return self.enabled

    def get_module(self) -> dict[str, Callable]:
        """使用公开候选方法名注册查询实现。"""
        return {"music_lyrics_candidates": self.callback}


@pytest.fixture
def lyrics_runtime(monkeypatch) -> Callable:
    """构造真实插件投影和模块调度器，只回放 LRCLIB 的外部响应。"""

    def build(plugins: dict[str, _LyricsPlugin]) -> SimpleNamespace:
        """为给定插件建立隔离的歌词链及其宿主来源。"""
        builtin = LrclibModule()
        request = Mock(return_value={
            "id": 1,
            "plainLyrics": "内置歌词",
            "instrumental": False,
        })
        monkeypatch.setattr(builtin, "_request_json", request)
        projection = PluginProjection(plugins)
        plugin_error = Mock()
        system_error = Mock()
        dispatcher = ModuleInvocationDispatcher(
            module_catalog=SimpleNamespace(
                get_running_modules=lambda method: [builtin] if hasattr(builtin, method) else [],
            ),
            plugin_catalog=SimpleNamespace(get_plugin_modules=projection.modules),
            plugin_error_handler=plugin_error,
            system_error_handler=system_error,
            rate_limit_handler=Mock(),
        )
        chain = object.__new__(LyricsChain)
        chain._module_dispatcher = dispatcher
        chain.deadline = None
        chain.budget_exceeded = False
        return SimpleNamespace(
            chain=chain,
            projection=projection,
            request=request,
            plugin_error=plugin_error,
            system_error=system_error,
        )

    return build


def test_lyrics_sdk_exports_preserve_canonical_identity() -> None:
    """插件公开类型必须与宿主候选判断和音乐输入使用同一实现。"""
    assert MusicInfo is context.MusicInfo
    assert MusicLyrics is context.MusicLyrics
    assert {"MusicInfo", "MusicLyrics", "MetaMusic"} <= set(media.__all__)


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize("music_type", [MetaMusic, MusicInfo])
async def test_registered_lyrics_plugins_merge_with_builtin_and_isolate_failures(
    lyrics_runtime: Callable, asynchronous: bool, music_type: type,
) -> None:
    """新歌词接口应聚合多个启用插件和内置源，空值与异常不得阻断后续来源。"""
    music = music_type(title="晴天", artists=["周杰伦"], album="叶惠美", duration=269)
    calls = []
    first_lyrics = MusicLyrics(provider="first", plain_lyrics="插件文本", match_score=100)
    second_lyrics = MusicLyrics(
        provider="second", synced_lyrics="[00:01.00]插件同步歌词", match_score=95,
    )

    def empty(music: Any) -> list[MusicLyrics]:
        """模拟插件未匹配到当前音轨。"""
        calls.append(("empty", music))
        return []

    def broken(music: Any) -> list[MusicLyrics]:
        """模拟单个歌词源下载失败。"""
        calls.append(("broken", music))
        raise RuntimeError("歌词源不可用")

    def first(music: Any) -> list[MusicLyrics]:
        """返回第一个同步插件的纯文本候选。"""
        calls.append(("first", music))
        return [first_lyrics]

    def second(music: Any) -> list[MusicLyrics]:
        """返回第二个插件已下载的同步歌词内容。"""
        calls.append(("second", music))
        return [second_lyrics]

    async def async_second(music: Any) -> list[MusicLyrics]:
        """验证异步入口可以混合同步与协程插件。"""
        return second(music)

    disabled = Mock()
    runtime = lyrics_runtime({
        "Empty": _LyricsPlugin(empty),
        "Broken": _LyricsPlugin(broken),
        "First": _LyricsPlugin(first),
        "Disabled": _LyricsPlugin(disabled, enabled=False),
        "Second": _LyricsPlugin(async_second if asynchronous else second),
    })

    candidates = (
        await runtime.chain.async_get_music_lyrics_candidates(music)
        if asynchronous else runtime.chain.get_music_lyrics_candidates(music)
    )

    assert [candidate.provider for candidate in candidates] == ["lrclib", "first", "second"]
    assert candidates[1:] == [first_lyrics, second_lyrics]
    assert calls == [(name, music) for name in ("empty", "broken", "first", "second")]
    disabled.assert_not_called()
    runtime.plugin_error.assert_called_once()
    assert runtime.plugin_error.call_args.args[1:4] == (
        "Broken", "测试歌词源", "music_lyrics_candidates",
    )
    runtime.system_error.assert_not_called()
    assert runtime.request.call_count == 2
    runtime.request.assert_called_with(
        "/api/get",
        params={"track_name": "晴天", "artist_name": "周杰伦", "album_name": "叶惠美", "duration": 269},
    )


@pytest.mark.parametrize("storage", ["local", "remote"])
@pytest.mark.parametrize(
    ("contents", "extension", "expected"),
    [
        ({"plain_lyrics": "插件纯文本"}, ".txt", "插件纯文本\n"),
        ({"synced_lyrics": "[00:01.00]插件同步歌词"}, ".lrc", "[00:01.00]插件同步歌词\n"),
    ],
)
def test_registered_lyrics_plugin_writes_through_music_scraping(
    lyrics_runtime: Callable, monkeypatch, tmp_path: Path,
    storage: str, contents: dict, extension: str, expected: str,
) -> None:
    """真实音乐刮削应把插件返回的最优歌词保存为本地旁挂文件或上传原存储目录。"""
    music = MusicInfo(title="晴天", artists=["周杰伦"], music_type="recording")
    lyric = MusicLyrics(provider="example", match_score=100, provider_priority=30, **contents)
    callback = Mock(side_effect=lambda music: [lyric])
    runtime = lyrics_runtime({"Example": _LyricsPlugin(callback)})

    def create_lyrics_chain(deadline: float) -> LyricsChain:
        """保留刮削创建的查询预算，并使用隔离注册的真实歌词链。"""
        runtime.chain.deadline = deadline
        return runtime.chain

    monkeypatch.setattr("app.chain.scraping.LyricsChain", create_lyrics_chain)
    monkeypatch.setattr(AudioMetadataHelper, "read_lyrics", lambda _path: None)
    local_path = tmp_path / "晴天.flac"
    local_path.write_bytes(b"audio")
    fileitem = FileItem(
        storage=storage,
        path=local_path.as_posix() if storage == "local" else "/music/晴天.flac",
        name="晴天.flac", type="file", extension="flac",
    )
    parent = FileItem(storage=storage, path="/music", type="dir")
    uploaded = {}

    def upload(target: FileItem, path: Path, new_name: str) -> bool:
        """在远端上传边界读取真实生成文件，保留目录和内容证据。"""
        assert target is parent
        uploaded[new_name] = path.read_text(encoding="utf-8")
        return True

    scraping = object.__new__(ScrapingChain)
    scraping.storagechain = SimpleNamespace(
        download_file=Mock(return_value=local_path),
        get_file_item=Mock(return_value=None),
        get_parent_item=Mock(return_value=parent),
        upload_file=Mock(side_effect=upload),
    )
    scraping.runtime_config = SimpleNamespace(lyrics_batch_timeout=30)
    scraping.scraping_policies = SimpleNamespace(
        option=lambda _target, metadata: SimpleNamespace(
            is_skip=metadata != "lyrics", is_overwrite=False, is_upgrade=True,
        ),
    )

    success, message = scraping.scrape_music_metadata(
        fileitem, mediainfo=music, audio_files=[fileitem],
    )

    assert success is True
    assert "歌词新增 1 首" in message
    callback.assert_called_once_with(music=music)
    runtime.plugin_error.assert_not_called()
    runtime.system_error.assert_not_called()
    if storage == "local":
        assert local_path.with_suffix(extension).read_text(encoding="utf-8") == expected
        scraping.storagechain.upload_file.assert_not_called()
    else:
        assert uploaded == {f"晴天{extension}": expected}
        assert not local_path.with_suffix(extension).exists()
    assert local_path.read_bytes() == b"audio"
