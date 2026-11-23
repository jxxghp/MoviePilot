"""
自动整理音乐专辑曲目对齐策略配置、识别回落、缓存隔离与刮削联动测试。
"""

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock, Mock

import pytest

from app.application.configuration import ChainRuntimeConfig
from app.chain.media import MediaChain
from app.chain.media.album import _verified_source_album
from app.chain.musicbrainz import MusicBrainzChain
from app.chain.scraping import ScrapingChain
from app.domain.context import MusicAlbumInfo, MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.music import (
    align_music_tracks_by_policy,
    music_album_candidate_matches,
    normalize_auto_music_track_match,
)
from app.modules.musicbrainz import MusicBrainzModule
from app.runtime.config import ConfigModel, Settings, settings
from app.schemas.file import FileItem
from app.schemas.types import MUSIC_ENTITY_RECORDING, MediaSource
from app.startup.composition.configuration import build_chain_runtime_config


def _ost_release_detail(
    release_id: str = "rel-ost-1",
    group_id: str = "rg-ost-1",
    *,
    title: str = "Game OST",
    track2_length_ms: int = 28_000,
    recording2_id: str = "rec-2",
) -> dict:
    """
    构造含隐藏曲目时长差异的 MusicBrainz 发行详情载荷。
    """
    return {
        "id": release_id,
        "title": title,
        "date": "2024-05-01",
        "country": "JP",
        "text-representation": {"script": "Latn"},
        "release-group": {"id": group_id, "primary-type": "Album", "first-release-date": "2024-05-01"},
        "artist-credit": [{"artist": {"id": "artist-1", "name": "OST Composer"}}],
        "media": [
            {
                "position": 1,
                "track-count": 2,
                "tracks": [
                    {
                        "id": f"{release_id}-t1",
                        "position": 1,
                        "title": "Opening Theme",
                        "length": 180_000,
                        "recording": {"id": "rec-1", "title": "Opening Theme", "length": 180_000},
                    },
                    {
                        "id": f"{release_id}-t2",
                        "position": 2,
                        "title": "Hidden Medley",
                        "length": track2_length_ms,
                        "recording": {"id": recording2_id, "title": "Hidden Medley", "length": track2_length_ms},
                    },
                ],
            }
        ],
    }


def _ost_local_metas(*, track2_duration: int = 227) -> tuple[MetaMusic, list[MetaMusic]]:
    """
    构造第 2 首物理时长与线上记录显著不同的本地 OST 曲目线索。
    """
    album_meta = MetaMusic(
        title="Game OST",
        album="Game OST",
        artists=["OST Composer"],
        album_artist="OST Composer",
        year=2024,
        field_sources={"album": "tag", "album_artist": "tag"},
    )
    tracks = [
        MetaMusic(
            title="Opening Theme",
            album="Game OST",
            artists=["OST Composer"],
            album_artist="OST Composer",
            disc_number=1,
            track_number=1,
            duration=180,
            field_sources={"title": "tag", "album": "tag", "artists": "tag"},
        ),
        MetaMusic(
            title="Hidden Medley",
            album="Game OST",
            artists=["OST Composer"],
            album_artist="OST Composer",
            disc_number=1,
            track_number=2,
            duration=track2_duration,
            field_sources={"title": "tag", "album": "tag", "artists": "tag"},
        ),
    ]
    return album_meta, tracks


def test_music_track_match_setting_defaults_and_validation(monkeypatch):
    """
    MUSIC_TRACK_MATCH 默认严格校验，校验并规范化合法值，拒绝顺序盲套与未知策略。
    """
    assert ConfigModel.model_fields["MUSIC_TRACK_MATCH"].default == "strict"
    assert normalize_auto_music_track_match(None) == "strict"
    assert normalize_auto_music_track_match("  IGNORE_DURATION ") == "ignore_duration"
    assert normalize_auto_music_track_match("Fallback_Ignore_Duration") == "fallback_ignore_duration"
    assert normalize_auto_music_track_match("sequential") == "strict"

    monkeypatch.setattr(Settings, "update_env_config", staticmethod(lambda *_args: (True, "")))
    monkeypatch.setattr(settings, "MUSIC_TRACK_MATCH", "strict")

    ok, message = settings.update_setting("MUSIC_TRACK_MATCH", " Fallback_Ignore_Duration ")
    assert ok is True
    assert message == ""
    assert settings.MUSIC_TRACK_MATCH == "fallback_ignore_duration"

    ok, _ = settings.update_setting("MUSIC_TRACK_MATCH", "IGNORE_DURATION")
    assert ok is True
    assert settings.MUSIC_TRACK_MATCH == "ignore_duration"

    ok, _ = settings.update_setting("MUSIC_TRACK_MATCH", "")
    assert ok is True
    assert settings.MUSIC_TRACK_MATCH == "strict"

    for invalid in ("sequential", "force", "auto"):
        ok, err = settings.update_setting("MUSIC_TRACK_MATCH", invalid)
        assert ok is False
        assert "strict" in err
        assert settings.MUSIC_TRACK_MATCH == "strict"

    monkeypatch.setattr(settings, "MUSIC_TRACK_MATCH", "unexpected_env_value")
    snapshot = build_chain_runtime_config(settings)
    assert snapshot.music_track_match == "strict"


def test_domain_alignment_policy_and_source_candidate_guard():
    """
    领域层回落策略只在严格对位不完整时尝试忽略时长，忽略时长仍禁止无曲名目录误匹配。
    """
    album_meta, local_tracks = _ost_local_metas(track2_duration=227)
    remote_tracks = [
        MusicInfo(
            title="Opening Theme",
            artists=["OST Composer"],
            media_source=MediaSource.TheAudioDB,
            media_id="rec-1",
            disc_number=1,
            track_number=1,
            duration=180,
        ),
        MusicInfo(
            title="Hidden Medley",
            artists=["OST Composer"],
            media_source=MediaSource.TheAudioDB,
            media_id="rec-2",
            disc_number=1,
            track_number=2,
            duration=28,
        ),
    ]
    album = MusicAlbumInfo(
        title="Game OST",
        artists=["OST Composer"],
        release_date="2024-05-01",
        media_source=MediaSource.TheAudioDB,
        media_id="album-1",
        tracks=remote_tracks,
    )

    assert align_music_tracks_by_policy(local_tracks, remote_tracks, track_match="strict") == {0: 0}
    assert align_music_tracks_by_policy(local_tracks, remote_tracks, track_match="fallback_ignore_duration") == {
        0: 0,
        1: 1,
    }
    assert align_music_tracks_by_policy(local_tracks, remote_tracks, track_match="ignore_duration") == {0: 0, 1: 1}

    assert music_album_candidate_matches(album, album_meta, local_tracks, track_match="strict") is False
    assert music_album_candidate_matches(album, album_meta, local_tracks, track_match="ignore_duration") is True

    assert _verified_source_album([album], album_meta, local_tracks, track_match="strict") is None
    fallback_album = _verified_source_album([album], album_meta, local_tracks, track_match="fallback_ignore_duration")
    assert fallback_album is not None
    assert fallback_album.raw_data["track_match"] == "ignore_duration"

    # 非 MusicBrainz 来源严格命中多个候选时回落模式保持歧义。
    alt_album = deepcopy(album)
    alt_album.media_id = "album-2"
    _meta, normal_tracks = _ost_local_metas(track2_duration=28)
    assert _verified_source_album([album, alt_album], album_meta, normal_tracks, track_match="fallback_ignore_duration") is None

    weak_album_meta = MetaMusic(title="CD1", album="CD1")
    weak_tracks = [
        MetaMusic(title="Track 01", track_number=1, duration=180),
        MetaMusic(title="Track 02", track_number=2, duration=28),
    ]
    assert music_album_candidate_matches(album, weak_album_meta, weak_tracks, track_match="ignore_duration") is False


@pytest.mark.parametrize("asynchronous", [False, True])
@pytest.mark.parametrize(
    ("policy", "expected_release", "expected_track_match"),
    [
        ("strict", None, None),
        ("fallback_ignore_duration", "rel-ost-1", "ignore_duration"),
        ("ignore_duration", "rel-ost-1", "ignore_duration"),
    ],
)
def test_musicbrainz_match_album_respects_auto_track_match_policy(
    monkeypatch,
    asynchronous,
    policy,
    expected_release,
    expected_track_match,
):
    """
    MusicBrainz 同步和异步专辑匹配按配置执行严格、回落或忽略时长，回落不增加额外网络请求。
    """
    album_meta, tracks = _ost_local_metas(track2_duration=227)
    detail = _ost_release_detail()
    detail_calls: list[str] = []

    def request_json(path, params=None):
        """
        返回模拟的发行详情响应并统计请求次数。
        """
        detail_calls.append(path)
        assert path == "/release/rel-ost-1"
        return deepcopy(detail)

    monkeypatch.setattr(settings, "MUSIC_TRACK_MATCH", policy)
    monkeypatch.setattr(MusicBrainzModule, "_search_release_candidates", Mock(return_value=[{"id": "rel-ost-1"}]))
    monkeypatch.setattr(
        MusicBrainzModule, "_async_search_release_candidates", AsyncMock(return_value=[{"id": "rel-ost-1"}]),
    )
    monkeypatch.setattr(MusicBrainzModule, "_request_json", staticmethod(request_json))
    monkeypatch.setattr(MusicBrainzModule, "_async_request_json", AsyncMock(side_effect=request_json))
    monkeypatch.setattr(MusicBrainzModule, "_lookup_artist_aliases", Mock(return_value=[]))
    monkeypatch.setattr(MusicBrainzModule, "_async_lookup_artist_aliases", AsyncMock(return_value=[]))

    module = MusicBrainzModule()
    result = (
        asyncio.run(module.async_match_music_album(album_meta, tracks))
        if asynchronous
        else module.match_music_album(album_meta, tracks)
    )
    assert len(detail_calls) == 1
    if expected_release is None:
        assert result is None
    else:
        assert result is not None
        assert result.musicbrainz_release_id == expected_release
        assert result.raw_data["track_match"] == expected_track_match


def test_musicbrainz_fallback_never_bypasses_strict_ambiguity_or_unlabeled_tracks():
    """
    严格校验已判定多版本歧义时回落模式保持歧义；忽略时长模式不接受纯无标签曲目目录。
    """
    album_meta, normal_tracks = _ost_local_metas(track2_duration=28)
    ambiguous_details = [
        _ost_release_detail("rel-a", "rg-a", track2_length_ms=28_000, recording2_id="rec-a2"),
        _ost_release_detail("rel-b", "rg-b", track2_length_ms=28_000, recording2_id="rec-b2"),
    ]
    assert (
        MusicBrainzModule._select_release_match(
            album_meta, normal_tracks, ambiguous_details, track_match="fallback_ignore_duration",
        )
        is None
    )

    weak_tracks = [
        MetaMusic(title="Track 01", album="Game OST", artists=["OST Composer"], track_number=1, duration=180),
        MetaMusic(title="Track 02", album="Game OST", artists=["OST Composer"], track_number=2, duration=28),
    ]
    assert (
        MusicBrainzModule._select_release_match(
            album_meta, weak_tracks, [_ost_release_detail()], track_match="ignore_duration",
        )
        is None
    )


@pytest.mark.parametrize("asynchronous", [False, True])
def test_album_directory_recognition_isolates_cache_by_track_match(monkeypatch, tmp_path, asynchronous):
    """
    切换 MUSIC_TRACK_MATCH 后目录识别缓存立即隔离，不会复用旧策略的空结果。
    """
    album_dir = tmp_path / "Game OST"
    album_dir.mkdir()
    file1 = album_dir / "01 - Opening Theme.flac"
    file2 = album_dir / "02 - Hidden Medley.flac"
    file1.write_bytes(b"fLaC1")
    file2.write_bytes(b"fLaC2")
    _album_meta, local_tracks = _ost_local_metas(track2_duration=227)
    detail = _ost_release_detail()
    album = MusicBrainzModule._release_to_album(detail)
    assert album is not None

    current_policy = {"value": "strict"}

    def fake_snapshot():
        """
        返回带当前曲目对齐策略的最小运行时配置快照。
        """
        return ChainRuntimeConfig(
            media_extensions=(".flac",),
            audio_extensions=(".flac",),
            search_source="musicbrainz",
            music_track_match=current_policy["value"],
            music_metadata_to_simplified=False,
        )

    calls: list[str] = []

    def fake_match(self, meta, tracks, **kwargs):
        """
        记录对齐参数并按 MusicBrainz 选择规则返回专辑。
        """
        policy = kwargs.get("track_match", "strict")
        calls.append(policy)
        return MusicBrainzModule._select_release_match(meta, tracks, [detail], track_match=policy)

    async def fake_async_match(self, meta, tracks, **kwargs):
        """
        异步版模拟发行选择。
        """
        return fake_match(self, meta, tracks, **kwargs)

    MediaChain.clear_music_album_cache()
    monkeypatch.setattr("app.chain.media.album.get_chain_runtime_config_snapshot", fake_snapshot)
    monkeypatch.setattr(
        "app.chain.media.album.AudioMetadataHelper.read_many",
        staticmethod(lambda files: deepcopy(local_tracks)),
    )
    monkeypatch.setattr(MusicBrainzChain, "match_music_album", fake_match)
    monkeypatch.setattr(MusicBrainzChain, "async_match_music_album", fake_async_match)
    monkeypatch.setattr(MediaChain, "_finalize_recognition_result", lambda self, info, **_kw: info)
    monkeypatch.setattr(MediaChain, "_async_finalize_recognition_result", AsyncMock(side_effect=lambda info, **_kw: info))

    chain = MediaChain()

    def run_recognize():
        """
        按当前测试模式执行同步或异步目录识别。
        """
        if asynchronous:
            return asyncio.run(chain.async_recognize_music_album_directory(album_dir))
        return chain.recognize_music_album_directory(album_dir)

    strict_result = run_recognize()
    assert len(strict_result) == 0
    assert calls == ["strict"]

    current_policy["value"] = "fallback_ignore_duration"
    fallback_result = run_recognize()
    assert len(fallback_result) == 2
    assert fallback_result[str(file2.resolve())].title == "Hidden Medley"
    assert calls == ["strict", "fallback_ignore_duration"]

    # 同一策略重复调用命中缓存，不重复触发来源匹配。
    cached_result = run_recognize()
    assert len(cached_result) == 2
    assert calls == ["strict", "fallback_ignore_duration"]


def test_scraping_album_alignment_follows_auto_track_match(monkeypatch):
    """
    自动整理入库后的专辑刮削与单轨回查同步遵循 MUSIC_TRACK_MATCH 策略。
    """
    _album_meta, local_tracks = _ost_local_metas(track2_duration=227)
    album = MusicBrainzModule._release_to_album(_ost_release_detail())
    assert album is not None

    current_policy = {"value": "strict"}
    monkeypatch.setattr(
        "app.chain.scraping.get_chain_runtime_config_snapshot",
        lambda: ChainRuntimeConfig(
            media_extensions=(".flac",),
            audio_extensions=(".flac",),
            music_track_match=current_policy["value"],
        ),
    )

    items = [
        FileItem(storage="local", path="/music/01.flac", name="01.flac", type="file", extension="flac"),
        FileItem(storage="local", path="/music/02.flac", name="02.flac", type="file", extension="flac"),
    ]
    file_media = [
        MusicInfo(
            media_source=MediaSource.MusicBrainz,
            media_id="rg-ost-1",
            musicbrainz_release_id="rel-ost-1",
            music_type="album",
            title="Game OST",
        )
        for _ in items
    ]
    evidence_by_path = {items[0].path: local_tracks[0], items[1].path: local_tracks[1]}
    scraper = ScrapingChain()
    monkeypatch.setattr(scraper, "_music_scrape_album", lambda *_args: album)
    monkeypatch.setattr(scraper, "_read_music_track_evidence", lambda item: deepcopy(evidence_by_path[item.path]))

    strict_planned = scraper._plan_music_album_tracks(items, file_media, {}, allow_title_override=False)
    assert len(strict_planned) == 1

    current_policy["value"] = "fallback_ignore_duration"
    fallback_planned = scraper._plan_music_album_tracks(items, file_media, {}, allow_title_override=False)
    assert len(fallback_planned) == 2
    assert fallback_planned[("local", "/music/02.flac")][1].media_id == "rec-2"

    # 单曲回查在标题无曲序且时长差异巨大时，strict 会被时长扣分压到阈值以下，fallback_ignore_duration 可回落命中。
    title_only_meta = MetaMusic(title="Hidden Medley", duration=227)
    current_policy["value"] = "strict"
    assert ScrapingChain._match_music_album_track(title_only_meta, album) is None

    current_policy["value"] = "fallback_ignore_duration"
    matched_track = ScrapingChain._match_music_album_track(title_only_meta, album)
    assert matched_track is not None
    assert matched_track.media_id == "rec-2"
    assert matched_track.music_type == MUSIC_ENTITY_RECORDING
