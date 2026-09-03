"""媒体库分类与来源描述分类的领域语义测试。"""

from app.domain.context import (
    MediaInfo,
    MusicAlbumInfo,
    MusicArtistInfo,
    MusicInfo,
)
from app.schemas.category import ClassificationResult
from app.schemas.types import MediaType


def _classification(path: list[str] | None = None) -> ClassificationResult:
    """构造测试使用的最小分类结果。"""
    category_path = path or ["现场专辑"]
    return ClassificationResult(
        recommended={
            "category_id": "music.live",
            "category_path": category_path,
            "rule_id": "rule.music.live",
            "source": "automatic",
        },
        effective={
            "category_id": "music.live",
            "category_path": category_path,
            "rule_id": "rule.music.live",
            "source": "automatic",
        },
        labels=["现场"],
        policy_revision=7,
        state="complete",
    )


def test_media_info_legacy_category_normalizes_to_library_category() -> None:
    """旧影视 category 构造参数和载荷应无损迁入媒体库分类。"""
    media = MediaInfo(
        type=MediaType.MOVIE,
        category="动画电影",
        classification=_classification().model_dump(mode="json"),
    )

    assert media.category == "动画电影"
    assert media.library_category == "动画电影"
    assert media.metadata_category == ""
    assert isinstance(media.classification, ClassificationResult)

    payload = media.to_dict()
    restored = MediaInfo()
    restored.from_dict(payload)

    assert payload["category"] == payload["library_category"] == "动画电影"
    assert isinstance(payload["classification"], dict)
    assert restored.library_category == restored.category == "动画电影"
    assert restored.classification == media.classification


def test_media_info_new_library_category_wins_and_legacy_writes_stay_synced() -> None:
    """显式新字段应优先，旧字段和两个写入方法仍保持双写。"""
    media = MediaInfo(category="旧分类", library_category="新分类")

    assert media.category == media.library_category == "新分类"

    media.category = "插件覆盖"
    assert media.category == media.library_category == "插件覆盖"

    media.set_category("旧入口覆盖")
    assert media.category == media.library_category == "旧入口覆盖"

    media.set_library_category("统一入口覆盖")
    assert media.category == media.library_category == "统一入口覆盖"


def test_music_info_metadata_category_never_becomes_library_category() -> None:
    """显式来源描述分类不得自动成为目录分类或兼容 category。"""
    music = MusicInfo(
        title="Track",
        metadata_category="Rock / Alternative",
        genres=["Rock", "Alternative"],
    )

    assert music.metadata_category == "Rock / Alternative"
    assert music.library_category == ""
    assert music.category == ""

    music.set_library_category("摇滚精选")
    assert music.library_category == music.category == "摇滚精选"
    assert music.metadata_category == "Rock / Alternative"


def test_music_info_legacy_source_description_uses_explainable_evidence() -> None:
    """旧音乐 category 与类型或流派吻合时应迁为来源描述。"""
    release = MusicInfo.from_dict(
        {
            "type": "音乐",
            "category": "Album / Live",
            "album_type": "Album",
            "secondary_types": ["Live"],
        }
    )
    genre = MusicInfo(
        category="華語流行",
        genres=["華語"],
    )

    assert release.metadata_category == "Album / Live"
    assert release.library_category == release.category == ""
    assert genre.metadata_category == "華語流行"
    assert genre.library_category == genre.category == ""


def test_music_info_legacy_library_category_is_preserved() -> None:
    """无法由来源事实解释或由分类结果确认的旧值应保留为库分类。"""
    unexplained = MusicInfo.from_dict(
        {
            "type": "音乐",
            "category": "私人收藏",
            "album_type": "Album",
        }
    )
    classified = MusicInfo.from_dict(
        {
            "type": "音乐",
            "category": "Live",
            "album_type": "Album",
            "classification": _classification(["Live"]).model_dump(mode="json"),
        }
    )

    assert unexplained.library_category == unexplained.category == "私人收藏"
    assert unexplained.metadata_category == ""
    assert classified.library_category == classified.category == "Live"
    assert classified.metadata_category == ""


def test_music_info_new_payload_uses_category_only_as_library_compatibility() -> None:
    """只要载荷包含新字段，category 就只能作为 library_category 兼容值。"""
    restored = MusicInfo.from_dict(
        {
            "type": "音乐",
            "category": "现场专辑",
            "metadata_category": "Album / Live",
            "secondary_types": ["Live"],
            "tags": ["concert"],
            "artist_country": "CN",
            "release_status": "Official",
        }
    )

    assert restored.category == restored.library_category == "现场专辑"
    assert restored.metadata_category == "Album / Live"
    assert restored.secondary_types == ["Live"]
    assert restored.tags == ["concert"]
    assert restored.artist_country == "CN"
    assert restored.release_status == "Official"

    explicitly_empty = MusicInfo.from_dict(
        {
            "type": "音乐",
            "category": "陈旧兼容值",
            "library_category": "",
            "metadata_category": "Album",
        }
    )
    assert explicitly_empty.category == explicitly_empty.library_category == ""
    assert explicitly_empty.metadata_category == "Album"


def test_music_album_round_trip_separates_metadata_and_library_categories() -> None:
    """专辑摘要、序列化和 MusicInfo 投影应使用拆分后的分类语义。"""
    album = MusicAlbumInfo(
        media_source="musicbrainz",
        media_id="album-1",
        title="Live Album",
        artists=["Artist"],
        album_type="Album",
        secondary_types=["Live"],
        library_category="现场专辑",
        classification=_classification(),
        genres=["Rock"],
        tags=["concert"],
        artist_country="GB",
        release_status="Official",
    )

    assert album.metadata_category == "Album / Live"
    assert album.category == album.library_category == "现场专辑"
    assert "Album / Live" in album.overview
    assert "现场专辑" not in album.overview

    payload = album.to_dict()
    restored = MusicAlbumInfo.from_dict(payload)
    projected = restored.to_music_info()

    assert isinstance(payload["classification"], dict)
    assert restored.classification == album.classification
    assert restored.metadata_category == "Album / Live"
    assert restored.category == restored.library_category == "现场专辑"
    assert projected.metadata_category == "Album / Live"
    assert projected.category == projected.library_category == "现场专辑"
    assert projected.secondary_types == ["Live"]
    assert projected.tags == ["concert"]
    assert projected.artist_country == "GB"
    assert projected.release_status == "Official"
    assert projected.classification == album.classification


def test_music_album_legacy_category_and_artist_projection_use_metadata() -> None:
    """旧专辑描述和艺术家类型应进入 metadata_category，而非库分类。"""
    album = MusicAlbumInfo.from_dict(
        {
            "type": "音乐",
            "category": "Album / Compilation",
            "album_type": "Album",
            "secondary_types": ["Compilation"],
        }
    )
    artist = MusicArtistInfo(
        media_source="musicbrainz",
        media_id="artist-1",
        name="Band",
        artist_type="Group",
        country="GB",
        tags=["rock"],
    ).to_music_info()

    assert album.metadata_category == "Album / Compilation"
    assert album.category == album.library_category == ""
    assert artist.metadata_category == "Group"
    assert artist.category == artist.library_category == ""
    assert artist.artist_country == "GB"
    assert artist.tags == ["rock"]
