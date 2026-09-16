"""音乐实站审计发现的名称匹配和刮削身份回归。"""

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.adapters.system import rust
from app.application.audio import AudioMetadataHelper
from app.chain.scraping import ScrapingChain
from app.domain.context import MusicInfo
from app.domain.meta import runtime
from app.domain.meta.metamusic import MetaMusic
from app.domain.metainfo import MetaInfo
from app.domain.music import match_music_resource
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.types import MediaSource, MediaType
from scripts import music_recognize_batch_test as batch


@pytest.mark.parametrize("title,artist,annotation", [
    ("一起飙高音", "黄明志", "(feat. 李佳薇)"),
    ("黎明所愿", "李佳薇", "(电视剧《暗夜与黎明》插曲)"),
    ("穿越星海", "李佳薇", "(《勿扰飞升》影视剧主题曲)"),
    ("我要闭上眼睛", "李佳薇", "(《仙剑四》影视剧片尾曲)"),
])
def test_credited_recording_matches_without_marketing_annotation(title, artist, annotation):
    """明确署名或影视用途注释不改变录音名称，检索与订阅匹配保持一致。"""
    resource = f"{artist} - {title} {annotation} (2024) - WEB-DL - 24bit ALAC-HHWEB"
    meta = MetaMusic.parse_resource(resource)
    artists = [artist, "李佳薇"] if "feat." in annotation else [artist]
    candidate = MusicInfo(media_source="musicbrainz", media_id="recording", title=title, artists=artists)

    assert MusicBrainzModule._select_candidate(meta, [candidate], MediaSource.MusicBrainz) is candidate
    assert match_music_resource(candidate, resource).status == "exact"


@pytest.mark.parametrize("annotation", ["(Part Two)", "(Live)", "(Remix)", "(feature presentation)"])
def test_music_annotation_cleanup_preserves_distinct_works(annotation):
    """未知副标题和录音版本仍须拒绝，不能靠删除所有括号提升命中率。"""
    candidate = MusicInfo(media_source="musicbrainz", media_id="recording", title="Song", artists=["Artist"])
    meta = MetaMusic.parse_resource(f"Artist - Song {annotation} FLAC")

    assert MusicBrainzModule._select_candidate(meta, [candidate], MediaSource.MusicBrainz) is None
    assert match_music_resource(candidate, meta.org_string).status != "exact"


def test_featured_recording_does_not_match_solo_candidate():
    """客串署名缺少来源佐证时，去注释不能把合作录音误配成独唱。"""
    candidate = MusicInfo(media_source="musicbrainz", media_id="recording", title="Song", artists=["Artist"])
    meta = MetaMusic.parse_resource("Artist - Song (feat. Guest) FLAC")

    assert MusicBrainzModule._select_candidate(meta, [candidate], MediaSource.MusicBrainz) is None
    assert match_music_resource(candidate, meta.org_string).status != "exact"


@pytest.mark.parametrize("recording_id", [None, "f4f1b7b2-22b0-4ce0-9ac6-31c336c52812"])
def test_album_scrape_never_writes_release_group_as_recording(recording_id):
    """专辑合并一直验证到标签投影，避免 release-group ID 污染下一次单曲识别。"""
    local = MetaMusic(title="晴天", artists=["周杰伦"], track_number=3,
                      media_source=MediaSource.MusicBrainz if recording_id else None, media_id=recording_id)
    album = MusicInfo(media_source="musicbrainz", media_id="album-id", music_type="album",
                      title="叶惠美", album="叶惠美", artists=["周杰伦"], total_tracks=11)

    merged = ScrapingChain._merge_music_album_metadata(local, album)
    tags = AudioMetadataHelper._tag_values(merged)

    assert tags["musicbrainz_trackid"] == recording_id
    assert tags["title"] == "晴天"
    assert tags["album"] == "叶惠美"
    assert local.album is None


@pytest.fixture(params=["python", "rust"])
def music_parser(request, monkeypatch):
    """使用真实两种引擎入口验证共享资源预处理，避免只修到 Python 回退。"""
    if request.param == "rust":
        if not rust.is_available():
            pytest.skip("moviepilot_rust 扩展未安装")
        monkeypatch.setattr(rust, "is_enabled", lambda: True)
    monkeypatch.setattr(runtime, "_metainfo_accelerator", rust if request.param == "rust" else None)
    return MetaInfo


def test_resource_release_catalog_does_not_pollute_album_name(music_parser):
    """标准 CD 抓轨尾链只提供发行和音质信息，不应进入专辑名称。"""
    title = ("周杰伦 - Jay  (2000) - CD - [WAV整轨+CUE] - 16bit - 44.1kHz - "
             "{TW - Alfa Music 74321838942} - CFQG@HDFans")
    meta = music_parser(title, mtype=MediaType.MUSIC)

    assert (meta.title, meta.artists, meta.year) == ("Jay", ["周杰伦"], 2000)
    assert meta.audio_format == "WAV"
    assert meta.org_string == title


def test_vertical_separator_does_not_turn_record_label_into_artist(music_parser):
    """站点竖线分隔的厂牌和编号不能被拼接为 artist-title 署名。"""
    meta = music_parser(
        "Duo.Improbabile.Improbabile.Genesi.2016.DSF.FLAC",
        "From NativeDSD丨VDM Records - VDM038009丨DSD256",
        mtype=MediaType.MUSIC,
    )

    assert meta.artists == []


def test_batch_uses_subtitle_and_does_not_count_display_fallback():
    """评测必须复用正式入口并区分本地展示对象和远端身份命中。"""
    module = Mock()
    module.recognize_media.return_value = MusicInfo(title="晴天")

    result = batch.recognize_one(module, "晴天 FLAC", "演唱：周杰伦；专辑：叶惠美")

    assert result["status"] == "未命中"
    assert result["parsed_artists"] == "周杰伦"
    assert result["parsed_album"] == "叶惠美"
    assert module.recognize_media.call_args.kwargs["cache"] is False


def test_batch_reads_multiline_subtitles_and_legacy_rows(tmp_path, monkeypatch):
    """TSV 正确还原带换行的副标题，同时支持旧版只有主标题的采样。"""
    path = tmp_path / "titles.tsv"
    path.write_text('站点\t晴天\t"歌手：周杰伦\n专辑：叶惠美"\n旧站\t单曲\n旧标题\n', encoding="utf-8")
    monkeypatch.setattr(batch, "TITLES_FILE", path)

    assert batch.read_titles_file() == [
        ("站点", "晴天", "歌手：周杰伦\n专辑：叶惠美"),
        ("旧站", "单曲", ""), ("憨憨", "旧标题", ""),
    ]


def test_batch_matches_registered_site_domain(tmp_path, monkeypatch):
    """索引中的 pt 子域须按生产库注册域查找，不能把已有 Cookie 误报成缺失。"""
    path = tmp_path / "user.db"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE site (domain TEXT, cookie TEXT, ua TEXT, proxy INTEGER)")
        connection.execute("INSERT INTO site VALUES (?, ?, ?, ?)", ("example.org", "fixture", "test", 0))
    monkeypatch.setattr(batch, "DB_PATH", path)

    result = batch.load_site_credentials(["pt.example.org"])

    assert result["pt.example.org"]["cookie"] == "fixture"


@pytest.mark.parametrize("asynchronous", [False, True])
def test_real_recording_response_matches_tie_in_title(asynchronous, monkeypatch):
    """实站名称和公共响应离线重放，确认修复能识别单曲而非回退到同名专辑。"""
    fixture = json.loads((Path(__file__).parent / "fixtures/music_lifecycle_recording.json").read_text(encoding="utf-8"))
    module = MusicBrainzModule()

    def request(path, params=None):
        """只接受已录制请求，意外新增出站路径直接使回归失败。"""
        return next(item["payload"] for item in fixture["requests"] if item["path"] == path and item["params"] == params)

    async def async_request(path, params=None):
        """异步入口消费相同公共响应。"""
        return request(path, params)

    monkeypatch.setattr(module, "_request_json", request)
    monkeypatch.setattr(module, "_async_request_json", async_request)
    kwargs = dict(meta=MetaMusic.parse_resource(fixture["title"], fixture["description"]),
                  media_source=MediaSource.MusicBrainz, mtype=MediaType.MUSIC, music_type="recording", cache=False)
    result = asyncio.run(module.async_recognize_media(**kwargs)) if asynchronous else module.recognize_media(**kwargs)

    assert result.media_id == fixture["expected_id"]
    assert result.music_type == "recording"
    assert result.artists == ["李佳薇"]
