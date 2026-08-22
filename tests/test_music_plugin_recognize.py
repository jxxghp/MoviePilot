"""媒体识别插件辅助测试（影视与音乐统一链路）。

覆盖 MediaChain 标题要素插件辅助识别（NameRecognize / MusicNameRecognize 链式事件）
与 ChainBase 媒体识别插件补充（MediaRecognize / MusicMediaRecognize 链式事件）。
"""
import asyncio
from unittest.mock import AsyncMock, Mock, patch

from app.application.orchestration import ChainBase
from app.application.orchestration.media import MediaChain
from app.domain.context import MediaInfo, MusicInfo
from app.runtime.events import Event
from app.domain.meta.metabase import MetaBase
from app.domain.meta.metamusic import MetaMusic
from app.schemas.types import ChainEventType, MediaSource, MediaType


def _fallback_music(title: str = "晴天", **kwargs) -> MusicInfo:
    """构造无远端身份的离线兜底音乐结果。"""
    return MusicInfo(title=title, **kwargs)


def _remote_music() -> MusicInfo:
    """构造带远端身份的标准音乐识别结果。"""
    return MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="晴天",
        artists=["周杰伦"],
        album="叶惠美",
        year=2003,
    )


def test_music_recognize_help_sends_event_and_rematches(monkeypatch):
    """原生识别无远端身份时应发送音乐名称识别事件，并按修正要素重新匹配。"""
    chain = MediaChain()
    meta = MetaMusic(
        title="周杰伦 晴天 FLAC 24bit 48kHz",
        disc_number=1,
        track_number=3,
        total_discs=1,
        total_tracks=11,
        version="原版",
        audio_format="FLAC",
        bit_depth=24,
        sample_rate=48_000,
        bitrate=2_304_000,
        duration=269,
        isrc="TW-A53-03-00003",
    )
    remote = _remote_music()
    recognize_calls = []

    def fake_recognize_media(meta=None, media_source=None, **kwargs):
        recognize_calls.append(meta)
        # 首次返回无身份兜底，辅助识别修正要素后二次识别命中远端
        return remote if len(recognize_calls) > 1 else _fallback_music(title=meta.title)

    monkeypatch.setattr(chain, "recognize_media", fake_recognize_media)

    event = Event(ChainEventType.MusicNameRecognize, {
        "title": meta.title,
        "name": "晴天",
        "artist": "周杰伦",
        "album": "叶惠美",
        "year": "2003",
    })
    with patch("app.application.orchestration.media.eventmanager") as em:
        em.check.return_value = True
        em.send_event.return_value = event
        result = chain.recognize_by_meta(meta, media_source="musicbrainz")

    assert result is remote
    assert em.check.call_args.args[0] == ChainEventType.MusicNameRecognize
    assert em.send_event.call_args.args[0] == ChainEventType.MusicNameRecognize
    # 重新匹配使用辅助识别修正后的要素
    rematch_meta = recognize_calls[-1]
    assert rematch_meta.title == "晴天"
    assert rematch_meta.artists == ["周杰伦"]
    assert rematch_meta.album == "叶惠美"
    assert rematch_meta.year == 2003
    assert rematch_meta.disc_number == 1
    assert rematch_meta.track_number == 3
    assert rematch_meta.total_discs == 1
    assert rematch_meta.total_tracks == 11
    assert rematch_meta.version == "原版"
    assert rematch_meta.audio_format == "FLAC"
    assert rematch_meta.audio_lossless is True
    assert rematch_meta.bit_depth == 24
    assert rematch_meta.sample_rate == 48_000
    assert rematch_meta.bitrate == 2_304_000
    assert rematch_meta.duration == 269
    assert rematch_meta.isrc == "TW-A53-03-00003"


def test_music_recognize_keeps_fallback_without_plugin(monkeypatch):
    """无插件响应音乐名称识别事件时应保留原生兜底结果。"""
    chain = MediaChain()
    meta = MetaMusic(title="未知曲目")
    fallback = _fallback_music(title="未知曲目")
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=fallback))

    with patch("app.application.orchestration.media.eventmanager") as em:
        em.check.return_value = False
        result = chain.recognize_by_meta(meta)

    assert result is fallback
    em.send_event.assert_not_called()


def test_music_recognize_help_same_elements_keeps_fallback(monkeypatch):
    """辅助识别要素与原始一致时不应重新识别。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = _fallback_music(title="晴天", artists=["周杰伦"])
    recognize_mock = Mock(return_value=fallback)
    monkeypatch.setattr(chain, "recognize_media", recognize_mock)

    event = Event(ChainEventType.MusicNameRecognize, {
        "title": "晴天",
        "name": "晴天",
        "artist": "周杰伦",
    })
    with patch("app.application.orchestration.media.eventmanager") as em:
        em.check.return_value = True
        em.send_event.return_value = event
        result = chain.recognize_by_meta(meta)

    assert result is fallback
    assert recognize_mock.call_count == 1


def test_music_recognize_help_keeps_fallback_when_rematch_fails(monkeypatch):
    """辅助要素重新匹配仍无远端身份时应保留原生兜底结果。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天")
    fallback = _fallback_music(title="晴天")
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=fallback))

    event = Event(ChainEventType.MusicNameRecognize, {
        "title": "晴天",
        "name": "另一个晴天",
        "artist": "未知艺术家",
    })
    with patch("app.application.orchestration.media.eventmanager") as em:
        em.check.return_value = True
        em.send_event.return_value = event
        result = chain.recognize_by_meta(meta)

    assert result is fallback


def test_async_music_recognize_help(monkeypatch):
    """异步音乐识别同样应走插件辅助识别并重匹配。"""
    chain = MediaChain()
    meta = MetaMusic(title="周杰伦-晴天", track_number=3, duration=269)
    remote = _remote_music()
    recognize_calls = []

    async def fake_async_recognize_media(meta=None, media_source=None, **kwargs):
        recognize_calls.append(meta)
        return remote if len(recognize_calls) > 1 else _fallback_music(title=meta.title)

    monkeypatch.setattr(chain, "async_recognize_media", fake_async_recognize_media)

    event = Event(ChainEventType.MusicNameRecognize, {
        "title": meta.title,
        "name": "晴天",
        "artist": "周杰伦",
    })
    with patch("app.application.orchestration.media.eventmanager") as em:
        em.check.return_value = True
        em.async_send_event = AsyncMock(return_value=event)
        result = asyncio.run(chain.async_recognize_by_meta(meta))

    assert result is remote
    assert recognize_calls[-1].title == "晴天"
    assert recognize_calls[-1].artists == ["周杰伦"]
    assert recognize_calls[-1].track_number == 3
    assert recognize_calls[-1].duration == 269


def test_plugin_first_keeps_fallback_when_help_unidentified(monkeypatch):
    """插件优先模式下辅助识别未取得身份时，应回退原生识别并保留已有兜底结果。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天")
    fallback = _fallback_music(title="晴天")
    # 辅助识别重匹配仍无身份返回 None，原生兜底不应被丢弃
    monkeypatch.setattr(chain, "recognize_media", Mock(return_value=fallback))

    event = Event(ChainEventType.MusicNameRecognize, {
        "title": "晴天",
        "name": "另一个晴天",
    })
    with patch("app.application.orchestration.media.eventmanager") as em, \
            patch("app.runtime.config.settings.RECOGNIZE_PLUGIN_FIRST", True):
        em.check.return_value = True
        em.send_event.return_value = event
        result = chain.recognize_by_meta(meta)

    assert result is fallback


def test_chain_supplement_music_recognize_uses_plugin_result():
    """音乐媒体识别事件应允许插件按已知要素返回标准音乐信息。"""
    chain = ChainBase()
    meta = MetaMusic(title="晴天", artists=["周杰伦"], album="叶惠美")
    plugin_data = {
        "mediainfo": {
            "media_source": "theaudiodb",
            "media_id": "song-123",
            "title": "晴天",
            "artists": ["周杰伦"],
            "album": "叶惠美",
            "year": 2003,
        }
    }
    event = Event(ChainEventType.MusicMediaRecognize, plugin_data)
    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event) as sender:
        result = chain._supplement_media_recognize(
            meta=meta,
            mtype=None,
            media_source=None,
            media_id=None,
            mediainfo=None,
            music_type="recording",
        )

    assert isinstance(result, MusicInfo)
    assert result.media_source == MediaSource.TheAudioDB
    assert result.media_id == "song-123"
    # 音乐请求使用音乐媒体识别事件，载荷携带已知要素
    assert sender.call_args.args[0] == ChainEventType.MusicMediaRecognize
    payload = sender.call_args.args[1]
    assert payload["title"] == "晴天"
    assert payload["artists"] == ["周杰伦"]
    assert payload["album"] == "叶惠美"
    assert payload["music_type"] == "recording"


def test_chain_supplement_music_rejects_cross_entity_plugin_result():
    """媒体识别插件返回的音乐实体与请求不一致时应保留原结果。"""
    chain = ChainBase()
    fallback = _fallback_music(title="叶惠美")
    event = Event(ChainEventType.MusicMediaRecognize, {
        "mediainfo": {
            "media_source": "theaudiodb",
            "media_id": "song-123",
            "music_type": "recording",
            "title": "叶惠美",
        },
    })

    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event):
        result = chain._supplement_media_recognize(
            meta=MetaMusic(title="叶惠美"),
            mtype=MediaType.MUSIC,
            media_source=MediaSource.TheAudioDB,
            media_id="album-123",
            mediainfo=fallback,
            music_type="album",
        )

    assert result is fallback


def test_chain_supplement_video_recognize_uses_plugin_result():
    """影视媒体识别事件应与音乐对称，允许插件按已知要素返回标准媒体信息。"""
    chain = ChainBase()
    meta = MetaBase("The.Matrix.1999.1080p.BluRay")
    meta.type = MediaType.MOVIE
    plugin_data = {
        "mediainfo": {
            "media_source": "themoviedb",
            "media_id": "603",
            "tmdb_id": 603,
            "title": "黑客帝国",
            "year": "1999",
        }
    }
    event = Event(ChainEventType.MediaRecognize, plugin_data)
    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event) as sender:
        result = chain._supplement_media_recognize(
            meta=meta,
            mtype=MediaType.MOVIE,
            media_source=None,
            media_id=None,
            mediainfo=None,
        )

    assert isinstance(result, MediaInfo)
    assert result.tmdb_id == 603
    # 影视请求使用媒体识别事件，插件未提供类型时使用请求推断类型
    assert sender.call_args.args[0] == ChainEventType.MediaRecognize
    assert result.type == MediaType.MOVIE


def test_chain_supplement_media_recognize_requires_identity():
    """插件返回缺少数据源或远端身份的结果不采信，影视音乐统一。"""
    chain = ChainBase()
    # 音乐缺媒体ID
    meta = MetaMusic(title="晴天")
    fallback = _fallback_music(title="晴天")
    event = Event(ChainEventType.MusicMediaRecognize, {
        "mediainfo": {"media_source": "theaudiodb", "title": "晴天"},
    })
    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event):
        result = chain._supplement_media_recognize(
            meta=meta,
            mtype=None,
            media_source=None,
            media_id=None,
            mediainfo=fallback,
        )
    assert result is fallback

    # 影视缺远端身份
    meta_video = MetaBase("Some.Movie")
    meta_video.type = MediaType.MOVIE
    event_video = Event(ChainEventType.MediaRecognize, {
        "mediainfo": {"media_source": "themoviedb", "title": "某部电影"},
    })
    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event_video):
        result = chain._supplement_media_recognize(
            meta=meta_video, mtype=MediaType.MOVIE,
            media_source=None, media_id=None, mediainfo=None,
        )
    assert result is None


def test_chain_supplement_media_recognize_skips_identified_result():
    """已有远端身份的结果不应触发插件补充事件。"""
    chain = ChainBase()
    meta = MetaMusic(title="晴天")
    remote = _remote_music()
    with patch.object(chain.eventmanager, "check") as checker:
        result = chain._supplement_media_recognize(
            meta=meta,
            mtype=None,
            media_source=None,
            media_id=None,
            mediainfo=remote,
        )

    assert result is remote
    checker.assert_not_called()


def test_chain_recognize_media_music_plugin_supplement():
    """统一识别入口应在原生音乐识别无身份时采信插件补充结果并统一上报。"""
    chain = MediaChain()
    meta = MetaMusic(title="晴天", artists=["周杰伦"])
    fallback = _fallback_music(title="晴天")
    plugin_music = MusicInfo(
        media_source=MediaSource.TheAudioDB,
        media_id="song-123",
        title="晴天",
        artists=["周杰伦"],
    )
    event = Event(ChainEventType.MusicMediaRecognize, {"mediainfo": plugin_music.to_dict()})

    with patch.object(chain, "recognize_music_from_source", return_value=fallback), \
            patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "send_event", return_value=event), \
            patch("app.application.orchestration._recognition.MoviePilotServerHelper.report_recognize_share") as report_mock:
        result = chain.recognize_media(meta=meta, cache=False)

    assert result is not fallback
    assert result.media_source == MediaSource.TheAudioDB
    report_mock.assert_called_once()
    assert report_mock.call_args.kwargs["mediainfo"] is result


def test_chain_async_supplement_media_recognize():
    """异步媒体识别补充应与同步行为一致（音乐）。"""
    chain = ChainBase()
    meta = MetaMusic(title="晴天")
    event = Event(ChainEventType.MusicMediaRecognize, {
        "mediainfo": {
            "media_source": "theaudiodb",
            "media_id": "song-1",
            "title": "晴天",
        },
    })
    with patch.object(chain.eventmanager, "check", return_value=True), \
            patch.object(chain.eventmanager, "async_send_event", AsyncMock(return_value=event)):
        result = asyncio.run(
            chain._async_supplement_media_recognize(
                meta=meta,
                mtype=None,
                media_source=None,
                media_id=None,
                mediainfo=None,
            )
        )

    assert isinstance(result, MusicInfo)
    assert result.media_source == MediaSource.TheAudioDB
    assert result.media_id == "song-1"


def test_chain_supplement_accepts_plugin_media_source():
    """插件返回的规范扩展来源应进入统一识别链。"""
    chain = ChainBase()
    event = Event(
        ChainEventType.MusicMediaRecognize,
        {
            "mediainfo": {
                "media_source": "qqmusic",
                "media_id": "song-1",
                "title": "晴天",
            }
        },
    )
    with patch.object(chain.eventmanager, "check", return_value=True), patch.object(
        chain.eventmanager, "send_event", return_value=event
    ):
        result = chain._supplement_media_recognize(
            meta=MetaMusic(title="晴天"),
            mtype=MediaType.MUSIC,
            media_source=None,
            media_id=None,
            mediainfo=None,
        )

    assert result.media_source == MediaSource("qqmusic")
    assert result.media_id == "song-1"
