"""标准媒体对象到分类事实的纯投影测试。"""

import pytest

from app.domain.classification.facts import build_classification_facts
from app.domain.context import MediaInfo, MusicAlbumInfo, MusicInfo
from app.schemas.types import MediaType


def test_video_facts_preserve_non_tmdb_identity_and_project_standard_fields() -> None:
    """豆瓣身份不得因分类事实构造而切换到 TMDB。"""
    media = MediaInfo(
        media_source="douban",
        media_id="1295644",
        type=MediaType.MOVIE,
        title="这个杀手不太冷",
        year="1994",
        original_language="fr",
        origin_country=["FR"],
        genres=[{"id": "剧情", "name": "剧情"}],
        adult=False,
        runtime=133,
        content_rating="PG-13",
        production_companies=[{"name": "Gaumont"}],
    )
    media.classification_genre_keys = ["drama"]

    facts = build_classification_facts(media)

    assert facts.identity.media_source == "douban"
    assert facts.identity.media_id == "1295644"
    assert facts.media.type == "电影"
    assert facts.media.year == 1994
    assert facts.media.countries == ["FR"]
    assert facts.media.genre_keys == ["drama"]
    assert facts.media.genre_names == ["剧情"]
    assert facts.media.companies == ["Gaumont"]
    assert facts.music is None


def test_music_facts_use_metadata_fields_without_promoting_library_category() -> None:
    """音乐来源分类只形成事实，不能自动成为媒体库目录分类。"""
    music = MusicInfo(
        media_source="musicbrainz",
        media_id="recording-1",
        title="Example Live",
        artists=["Example Artist"],
        music_type="recording",
        album_type="Album",
        secondary_types=["Live"],
        genres=["Rock"],
        tags=["j-rock"],
        artist_country="JP",
        release_status="Official",
        metadata_category="Album / Live",
        library_category="现场专辑",
        category="现场专辑",
        year=2024,
    )

    facts = build_classification_facts(music)

    assert facts.identity.media_source == "musicbrainz"
    assert facts.media.type == "音乐"
    assert facts.media.countries == ["JP"]
    assert facts.music is not None
    assert facts.music.album_type == "Album"
    assert facts.music.secondary_types == ["Live"]
    assert facts.music.genres == ["Rock"]
    assert facts.music.tags == ["j-rock"]
    assert facts.model_dump_json().find("现场专辑") == -1
    assert facts.model_dump_json().find("Album / Live") == -1


def test_album_facts_include_album_specific_fields() -> None:
    """专辑对象应完整投影副类型、标签和发行状态。"""
    album = MusicAlbumInfo(
        media_source="theaudiodb",
        media_id="album-1",
        title="Live Album",
        artists=["Artist"],
        album_type="Album",
        secondary_types=["Live"],
        genres=["Rock"],
        tags=["Concert"],
        artist_country="GB",
        release_status="Official",
    )

    facts = build_classification_facts(album)

    assert facts.music is not None
    assert facts.music.entity_type == "album"
    assert facts.music.secondary_types == ["Live"]
    assert facts.music.artist_country == "GB"
    assert facts.music.release_status == "Official"


def test_missing_optional_fields_remain_missing_instead_of_empty_lists() -> None:
    """来源未提供的字段应保留为 None，避免排除型规则误命中。"""
    media = MediaInfo(
        media_source="anilist",
        media_id="1",
        type=MediaType.TV,
        title="Example",
    )

    facts = build_classification_facts(media)

    assert facts.media.countries is None
    assert facts.media.genre_keys is None
    assert facts.media.genre_names is None
    assert facts.media.companies is None
    assert facts.media.networks is None


def test_extension_facts_are_copied_without_changing_identity() -> None:
    """受控扩展事实必须保持来源命名空间和主身份。"""
    media = MediaInfo(
        media_source="plugin.example",
        media_id="native-1",
        type=MediaType.MOVIE,
        title="Example",
    )

    facts = build_classification_facts(
        media,
        extensions={"plugin.example": {"region_group": "east-asia"}},
    )

    assert facts.identity.media_source == "plugin.example"
    assert facts.identity.media_id == "native-1"
    assert facts.extensions == {"plugin.example": {"region_group": "east-asia"}}


@pytest.mark.parametrize(  # type: ignore[misc]
    ("media_source", "media_id"),
    [(None, "1"), ("douban", None), ("", "1"), ("douban", "")],
)
def test_incomplete_identity_is_rejected(
    media_source: str | None,
    media_id: str | None,
) -> None:
    """分类事实必须绑定完整稳定身份。"""
    media = MediaInfo(
        media_source=media_source,
        media_id=media_id,
        type=MediaType.MOVIE,
        title="Example",
    )

    with pytest.raises(ValueError, match="media_source"):
        build_classification_facts(media)


def test_local_music_facts_require_explicit_opt_in_and_keep_empty_identity() -> None:
    """完整本地音乐可在离线整理中分类，但不伪造在线身份或改变默认校验。"""
    media = MusicInfo(title="晴天", artists=["周杰伦"], album="叶惠美")

    with pytest.raises(ValueError, match="media_source"):
        build_classification_facts(media)
    facts = build_classification_facts(media, allow_local_music=True)

    assert facts.identity.media_source == facts.identity.media_id == ""
    assert facts.media.type == "音乐"
    assert facts.media.title == "晴天"
    assert facts.music.artists == ["周杰伦"]


def test_local_music_opt_in_does_not_accept_incomplete_or_video_evidence() -> None:
    """离线许可不能让残缺音乐标签或缺身份影视进入已完成分类。"""
    for media in (MusicInfo(title="Track 01"), MediaInfo(title="Example", type=MediaType.MOVIE)):
        with pytest.raises(ValueError, match="media_source"):
            build_classification_facts(media, allow_local_music=True)
