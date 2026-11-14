"""
验证CUE内容变化使同步和异步专辑目录缓存失效，并保持测试owner隔离。
"""



import asyncio
import os
from unittest.mock import AsyncMock, Mock

import pytest

from app.chain.media import MediaChain
from app.chain.media.cache import AlbumDirectoryCache
from app.modules.musicbrainz import MusicBrainzModule
from tests.test_music_album_match import _release_detail
from tests.test_music_cue import _image_pair


@pytest.fixture
def album_chain():
    """
    提供本例专用的真实媒体 owner，不登记单例或启动无关模块与插件资源。

    只注入目录缓存；文件读取、CUE解析、目录匹配及同步/异步识别仍走真实实现。
    既有 MediaChain 实例、实例属性存在性和原缓存内容完全不接触。

    :return: 持有本例独立缓存的媒体处理实例
    """
    chain = object.__new__(MediaChain)
    chain._album_dir_cache = AlbumDirectoryCache(16)
    return chain


@pytest.mark.parametrize("asynchronous", [False, True])
def test_album_recognition_refreshes_same_size_cue_with_restored_mtime(tmp_path, monkeypatch, asynchronous, album_chain):
    """
    同步与异步完整目录识别都不得复用内容变更而大小、mtime相同的旧 CUE 结果。
    """
    audio, cue = _image_pair(tmp_path)
    old = MusicBrainzModule._release_to_album(_release_detail("old", "专辑示例", "测试歌手", [("第一首歌曲", 1), ("第二首歌曲", 2)]))
    new = MusicBrainzModule._release_to_album(_release_detail("new", "专辑示例", "测试歌手", [("第一首歌曲", 1), ("另一首歌曲", 2)]))
    request = AsyncMock(side_effect=[old, new]) if asynchronous else Mock(side_effect=[old, new])
    source = Mock(**{"async_match_music_album" if asynchronous else "match_music_album": request})
    monkeypatch.setattr("app.chain.media.album.MusicBrainzChain", Mock(return_value=source))
    chain = album_chain

    def recognize():
        """
        调用当前参数选择的真实目录识别入口。
        """
        return (asyncio.run(chain.async_recognize_music_album_directory(audio.parent)) if asynchronous
                else chain.recognize_music_album_directory(audio.parent))

    assert recognize()
    before = cue.stat()
    cue.write_text(cue.read_text().replace("第二首歌曲", "另一首歌曲"))
    os.utime(cue, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert cue.stat().st_size == before.st_size
    assert cue.stat().st_mtime_ns == before.st_mtime_ns
    assert recognize()
    assert request.call_count == 2
    assert [track.title for track in request.call_args.args[1]] == ["第一首歌曲", "另一首歌曲"]
