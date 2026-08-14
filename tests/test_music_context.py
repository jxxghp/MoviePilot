from app.domain.context import Context, MediaInfo
from app.domain.meta.metamusic import MetaMusic
from app.domain.context import MusicInfo
from app.schemas.context import Context as ContextSchema
from app.schemas.context import MediaInfo as MediaInfoSchema
from app.schemas.music import MusicInfo as MusicInfoSchema
from app.schemas.music import MusicMeta as MusicMetaSchema
from app.schemas.types import MediaSource, MediaType, media_type_to_agent


def test_media_type_supports_music_agent_conversion():
    """音乐媒体类型应支持 Agent 标识双向转换。"""
    assert MediaType.from_agent("music") == MediaType.MUSIC
    assert MediaType.MUSIC.to_agent() == "music"
    assert media_type_to_agent(MediaType.MUSIC) == "music"
    assert media_type_to_agent("music") == "music"
    assert media_type_to_agent(MediaType.MUSIC.value) == "music"
    assert media_type_to_agent(MediaType.MOVIE.value) == "movie"
    assert media_type_to_agent(MediaType.TV.value) == "tv"


def test_music_meta_round_trip_preserves_list_isolation():
    """MetaMusic 字典往返后应保留字段且不共享可变列表。"""
    meta = MetaMusic(
        org_string="Jay Chou - Common Jasmin Orange",
        title="七里香",
        artists=["周杰伦"],
        album="七里香",
        year=2004,
        audio_format="FLAC",
    )

    payload = meta.to_dict()
    restored = MetaMusic.from_dict(payload)
    restored.artists.append("Jay Chou")

    assert payload["type"] == "音乐"
    assert restored.title == "七里香"
    assert restored.album == "七里香"
    assert restored.year == 2004
    assert meta.artists == ["周杰伦"]


def test_music_info_serializes_shared_media_display_fields():
    """MusicInfo 应输出现有媒体卡片可复用的展示字段。"""
    info = MusicInfo(
        media_source="musicbrainz",
        media_id="release-1",
        title="七里香",
        artists=["周杰伦"],
        album="七里香",
        year=2004,
        cover_url="https://example.invalid/cover.jpg",
    )

    payload = info.to_dict()

    assert payload["type"] == "音乐"
    assert payload["artist"] == "周杰伦"
    assert payload["title_year"] == "七里香 (2004)"
    assert payload["poster_path"] == "https://example.invalid/cover.jpg"
    assert payload["media_source"] == "musicbrainz"
    assert payload["media_id"] == "release-1"


def test_core_context_serializes_music_models_without_video_fields():
    """核心 Context 应使用既有外层结构序列化音乐对象。"""
    context = Context(
        meta_info=MetaMusic(title="七里香", artists=["周杰伦"]),
        media_info=MusicInfo(
            media_source="musicbrainz",
            media_id="release-1",
            title="七里香",
            artists=["周杰伦"],
        ),
    )

    payload = context.to_dict()

    assert payload["meta_info"]["type"] == "音乐"
    assert payload["media_info"]["type"] == "音乐"
    assert payload["media_info"]["artists"] == ["周杰伦"]
    assert "tmdb_id" not in payload["media_info"]


def test_schema_context_uses_music_models_for_music_payload():
    """API Context 应将音乐负载解析为音乐专属 Schema。"""
    context = ContextSchema.model_validate(
        {
            "meta_info": {
                "type": "音乐",
                "title": "七里香",
                "artists": ["周杰伦"],
            },
            "media_info": {
                "type": "音乐",
                "media_source": "musicbrainz",
                "media_id": "release-1",
                "title": "七里香",
                "artists": ["周杰伦"],
                "album": "七里香",
                "year": 2004,
            },
        }
    )

    assert isinstance(context.meta_info, MusicMetaSchema)
    assert isinstance(context.media_info, MusicInfoSchema)
    assert context.media_info.artists == ["周杰伦"]


def test_schema_context_keeps_video_payload_on_existing_model():
    """电影负载应继续使用现有 MediaInfo Schema。"""
    context = ContextSchema.model_validate(
        {
            "media_info": {
                "type": "电影",
                "media_source": "themoviedb",
                "media_id": "157336",
                "title": "Interstellar",
                "tmdb_id": 157336,
            }
        }
    )

    assert isinstance(context.media_info, MediaInfoSchema)
    assert context.media_info.tmdb_id == 157336


def test_core_video_context_remains_compatible():
    """新增音乐模型后现有电影 Context 序列化应保持兼容。"""
    context = Context(
        media_info=MediaInfo(
            media_source=MediaSource.TMDB,
            media_id="157336",
            type=MediaType.MOVIE,
            title="Interstellar",
            year="2014",
            tmdb_id=157336,
        )
    )

    payload = context.to_dict()

    assert payload["media_info"]["type"] == "电影"
    assert payload["media_info"]["tmdb_id"] == 157336
