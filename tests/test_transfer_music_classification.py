from app.chain.media import MediaChain
from app.chain.transfer.filter import FileFilterMixin
from app.domain.context import MusicInfo
from app.domain.meta.metamusic import MetaMusic
from app.schemas.category import ClassificationResult, ClassificationSelection
from app.schemas.file import FileItem


def test_album_context_preserves_classification_and_remote_music_facts(
        tmp_path,
        monkeypatch,
) -> None:
    """目录专辑匹配重建曲目时应保留分类事实，并隔离缓存中的可变结果。"""
    file_path = tmp_path / "album" / "01-track.flac"
    classification = ClassificationResult(
        recommended=ClassificationSelection(
            category_id="live",
            category_path=["音乐", "现场"],
            rule_id="music-live",
            source="automatic",
        ),
        effective=ClassificationSelection(
            category_id="favorites",
            category_path=["音乐", "收藏"],
            rule_id="music-live",
            source="subscription",
        ),
        labels=["现场", "高解析"],
        policy_revision=12,
        state="complete",
    )
    matched_info = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Remote title",
        artists=["Remote artist"],
        artist_ids=["artist-1"],
        album="Remote album",
        album_artist="Remote album artist",
        album_id="release-group-1",
        album_type="Album",
        secondary_types=["Live", "Compilation"],
        year=2024,
        release_date="2024-05-01",
        release_status="Official",
        disc_number=1,
        track_number=1,
        total_tracks=10,
        duration=1,
        audio_format="MP3",
        audio_lossless=False,
        bit_depth=16,
        sample_rate=44100,
        bitrate=320000,
        library_category="音乐/收藏",
        metadata_category="Album / Live / Compilation",
        classification=classification,
        genres=["Rock"],
        tags=["concert", "hi-res"],
        artist_country="GB",
        names=["Remote title", "远端曲名"],
        detail_link="https://example.test/recording-1",
        listen_count=42,
        raw_data={"release": {"secondary_types": ["Live"]}},
    )
    local_meta = MetaMusic(
        title="Local title",
        duration=321,
        audio_format="FLAC",
        audio_lossless=True,
        bit_depth=24,
        sample_rate=96000,
        bitrate=2304000,
    )
    file_item = FileItem(
        storage="local",
        path=str(file_path),
        type="file",
        name=file_path.name,
        basename=file_path.stem,
        extension="flac",
    )
    monkeypatch.setattr(
        MediaChain,
        "recognize_music_album_directory",
        lambda _self, _directory: {str(file_path.resolve()): matched_info},
    )

    merged_meta, merged_info = FileFilterMixin._match_music_album_context(
        file_item,
        file_path,
        local_meta,
    )

    assert merged_info is not None
    assert merged_info.classification == classification
    assert merged_info.classification is not classification
    assert merged_info.classification.recommended is not classification.recommended
    assert merged_info.classification.effective is not classification.effective
    assert merged_info.classification.recommended.category_path == ["音乐", "现场"]
    assert merged_info.classification.effective.category_path == ["音乐", "收藏"]
    assert merged_info.classification.labels == ["现场", "高解析"]
    assert merged_info.category == merged_info.library_category == "音乐/收藏"
    assert merged_info.metadata_category == "Album / Live / Compilation"
    assert merged_info.secondary_types == ["Live", "Compilation"]
    assert merged_info.tags == ["concert", "hi-res"]
    assert merged_info.genres == ["Rock"]
    assert merged_info.release_status == "Official"
    assert merged_info.artist_country == "GB"
    assert merged_info.names == ["Remote title", "远端曲名"]

    assert merged_meta.duration == merged_info.duration == 321
    assert merged_meta.audio_format == merged_info.audio_format == "FLAC"
    assert merged_info.audio_lossless is True
    assert merged_info.bit_depth == 24
    assert merged_info.sample_rate == 96000
    assert merged_info.bitrate == 2304000

    merged_info.classification.recommended.category_path.append("追加")
    merged_info.classification.labels.append("新标签")
    merged_info.secondary_types.append("Soundtrack")
    merged_info.tags.append("new-tag")
    merged_info.raw_data["release"]["secondary_types"].append("Remix")

    assert classification.recommended.category_path == ["音乐", "现场"]
    assert classification.labels == ["现场", "高解析"]
    assert matched_info.secondary_types == ["Live", "Compilation"]
    assert matched_info.tags == ["concert", "hi-res"]
    assert matched_info.raw_data == {"release": {"secondary_types": ["Live"]}}
