import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from mutagen.id3 import SYLT, USLT

from app.application.audio import AudioMetadataHelper
from app.chain.lyrics import LyricsChain
from app.chain.scraping import ScrapingChain
from app.domain.context import MusicInfo, MusicLyrics
from app.domain.meta.metamusic import MetaMusic
from app.schemas.workflow import FileItem


def test_lyricsfile_derives_lrc_plain_language_and_word_quality() -> None:
    """Lyricsfile 应保留原文，并派生播放器兼容内容和逐字同步质量。"""
    lyrics = MusicLyrics(
        provider="lrclib",
        lyricsfile="""
version: '1.0'
metadata:
  title: 晴天
  artist: 周杰伦
  language: zh
lines:
  - start_ms: 1250
    text: 晴天
    words:
      - {start_ms: 1250, text: 晴}
      - {start_ms: 1500, text: 天}
""",
    )

    assert lyrics.language == "zh"
    assert lyrics.synced_lyrics == "[00:01.25]晴天"
    assert lyrics.plain_lyrics == "晴天"
    assert lyrics.quality_rank == 4


def test_lyricsfile_rejects_yaml_aliases() -> None:
    """外部 Lyricsfile 不得通过 YAML 锚点别名制造共享或膨胀结构。"""
    lyrics = MusicLyrics(
        provider="lrclib",
        lyricsfile="""
version: '1.0'
metadata: {title: Track, artist: Artist}
base: &line {start: 1000, text: unsafe}
lines: [*line]
""",
    )

    assert lyrics.content is None
    assert lyrics.quality_rank == 0


def test_lyrics_chain_prefers_synced_candidate_over_exact_plain_fallback(monkeypatch) -> None:
    """可信候选中应优先同步质量，再以匹配度和来源优先级打破平局。"""
    chain = LyricsChain()
    responses = iter([
        MusicLyrics(provider="legacy", plain_lyrics="plain", match_score=100),
        [MusicLyrics(provider="licensed", synced_lyrics="[00:01]sync", match_score=95)],
    ])
    monkeypatch.setattr(chain, "run_module", lambda *_args, **_kwargs: next(responses))

    result = chain.get_music_lyrics(MusicInfo(title="Track", artists=["Artist"]))

    assert result is not None
    assert result.provider == "licensed"


def test_lyrics_chain_selects_async_candidates(monkeypatch) -> None:
    """通用歌词链异步入口应返回候选中的最优结果。"""
    chain = LyricsChain()
    run_module = AsyncMock(side_effect=[
        MusicLyrics(provider="legacy", plain_lyrics="plain", match_score=100),
        [MusicLyrics(provider="lrclib", synced_lyrics="[00:01]sync", match_score=100)],
    ])
    monkeypatch.setattr(chain, "async_run_module", run_module)

    result = asyncio.run(chain.async_get_music_lyrics(
        MusicInfo(title="Track", artists=["Artist"])
    ))

    assert result is not None
    assert result.synced_lyrics == "[00:01]sync"


def test_audio_helper_reads_id3_synced_and_plain_lyrics(monkeypatch, tmp_path) -> None:
    """ID3 SYLT 和 USLT 应转换为本地高置信歌词候选。"""
    tags = Mock()
    tags.getall.side_effect = lambda name: {
        "SYLT": [SYLT(encoding=3, lang="zho", format=2, type=1, text=[("晴天", 1250)])],
        "USLT": [USLT(encoding=3, lang="zho", text="晴天")],
    }[name]
    tags.items.return_value = []
    monkeypatch.setattr("app.application.audio.MutagenFile", lambda *_args, **_kwargs: SimpleNamespace(tags=tags))

    lyrics = AudioMetadataHelper.read_lyrics(tmp_path / "track.mp3")

    assert lyrics is not None
    assert lyrics.provider == "embedded"
    assert lyrics.synced_lyrics == "[00:01.25]晴天"
    assert lyrics.plain_lyrics == "晴天"


def test_scrape_never_downgrades_existing_lrc_to_plain_text(monkeypatch, tmp_path) -> None:
    """即使调用方要求覆盖，也不能用纯文本替换已有同步歌词。"""
    chain = object.__new__(ScrapingChain)
    chain.storagechain = Mock()
    existing = FileItem(storage="local", path=(tmp_path / "track.lrc").as_posix(), type="file")
    chain.storagechain.get_file_item.side_effect = lambda storage, path: (
        existing if str(path).endswith(".lrc") else None
    )
    lyrics_chain = Mock()
    lyrics_chain.budget_exceeded = False
    lyrics_chain.get_music_lyrics.return_value = MusicLyrics(
        provider="theaudiodb",
        plain_lyrics="plain",
        match_score=100,
    )
    monkeypatch.setattr(AudioMetadataHelper, "read_lyrics", lambda _path: None)
    write = Mock(return_value=True)
    monkeypatch.setattr(chain, "_write_music_lyrics_sidecar", write)

    status = chain._scrape_music_lyrics(
        fileitem=FileItem(storage="local", path=(tmp_path / "track.flac").as_posix(), type="file"),
        local_path=tmp_path / "track.flac",
        scrape_info=MetaMusic(title="Track", artists=["Artist"]),
        lyrics_option=SimpleNamespace(is_skip=False, is_upgrade=False),
        overwrite=True,
        lyrics_chain=lyrics_chain,
        album_info=None,
    )

    assert status == "protected"
    write.assert_not_called()


def test_music_lyrics_sidecars_match_only_same_stem_audio() -> None:
    """整理链只关联同目录同主干名歌词，Lyricsfile 双扩展名也应正确剥离。"""
    audio = FileItem(storage="local", path="/music/Track.flac", name="Track.flac", type="file", extension="flac")
    lrc = FileItem(storage="local", path="/music/Track.lrc", name="Track.lrc", type="file", extension="lrc")
    lyricsfile = FileItem(
        storage="local",
        path="/music/Track.lyricsfile.yaml",
        name="Track.lyricsfile.yaml",
        type="file",
        extension="yaml",
    )
    other = FileItem(storage="local", path="/music/Notes.txt", name="Notes.txt", type="file", extension="txt")

    from app.chain.transfer import TransferChain

    transfer_chain = object.__new__(TransferChain)
    transfer_chain._subtitle_exts = ()
    transfer_chain._audio_exts = (".flac",)
    assert transfer_chain._get_related_main_file_key(lrc, [audio]) == ("local", "/music/Track.flac")
    assert transfer_chain._get_related_main_file_key(lyricsfile, [audio]) == ("local", "/music/Track.flac")
    assert transfer_chain._get_related_main_file_key(other, [audio]) is None
