"""音乐刮削结果中 album_type 等字段不应包含两端空白（Issue #6327）。"""

from app.modules.listenbrainz import ListenBrainzModule
from app.modules.musicbrainz import MusicBrainzModule
from app.schemas.music import MusicAlbumInfo as SchemaMusicAlbumInfo
from app.schemas.music import MusicInfo as SchemaMusicInfo


class TestMusicBrainzAlbumTypeStripped:
    """MusicBrainz 模块应将 album_type 和 secondary_types 两端空白去除。"""

    def test_release_to_album_album_type_stripped(self):
        """专辑详情中 primary-type 带尾部空格时应被清理。"""
        detail = {
            "id": "release-1",
            "title": "Test Album",
            "artist-credit": [{"name": "Artist", "artist": {"id": "a1"}}],
            "release-group": {
                "id": "rg-1",
                "primary-type": "Album ",
                "secondary-types": ["Compilation ", " Soundtrack"],
            },
            "date": "2024-01-01",
            "media": [],
        }

        album = MusicBrainzModule._release_to_album(detail)

        assert album is not None
        assert album.album_type == "Album"
        assert album.secondary_types == ["Compilation", "Soundtrack"]

    def test_recording_to_info_album_type_stripped(self):
        """歌曲识别结果中 album_type 和 category 不应包含空白。"""
        recording = {
            "id": "rec-1",
            "title": "Test Song",
            "artist-credit": [{"name": "Artist", "artist": {"id": "a1"}}],
            "length": 300000,
            "isrcs": ["USRC10000001"],
            "genres": [],
            "releases": [
                {
                    "id": "rel-1",
                    "title": "Test Single",
                    "artist-credit": [{"name": "Artist", "artist": {"id": "a1"}}],
                    "release-group": {
                        "id": "rg-1",
                        "primary-type": "Single ",
                        "secondary-types": [" Remix "],
                    },
                },
            ],
        }

        info = MusicBrainzModule._recording_to_info(recording)

        assert info is not None
        assert info.album_type == "Single"
        assert info.metadata_category == "Single / Remix"
        assert info.category == ""

    def test_release_group_to_album_strips_types(self):
        """Release Group 浏览结果中 album_type 和 secondary_types 应被清理。"""
        release_group = {
            "id": "rg-1",
            "title": "Test EP",
            "artist-credit": [{"name": "Artist", "artist": {"id": "a1"}}],
            "primary-type": "EP ",
            "secondary-types": ["Live ", " Compilation "],
            "first-release-date": "2024",
            "rating": {"value": "4", "votes-count": "10"},
        }

        album = MusicBrainzModule._release_group_to_album(release_group)

        assert album is not None
        assert album.album_type == "EP"
        assert album.secondary_types == ["Live", "Compilation"]


class TestListenBrainzAlbumTypeStripped:
    """ListenBrainz 模块应将 album_type 和 category 两端空白去除。"""

    def test_fresh_release_to_info_strips_album_type(self):
        """新发行条目中 release_group_primary_type 带空格时应被清理。"""
        release = {
            "release_group_mbid": "rg-1",
            "release_name": "Test Album",
            "artist_credit_name": "Artist",
            "release_group_primary_type": "Album ",
            "release_group_secondary_type": " Compilation ",
            "release_date": "2024-01-01",
        }

        info = ListenBrainzModule._fresh_release_to_info(release)

        assert info is not None
        assert info.album_type == "Album"
        assert info.metadata_category == "Album / Compilation"
        assert info.category == ""

    def test_fresh_release_to_info_handles_none_type(self):
        """空白的 release_group_primary_type 应返回 None。"""
        release = {
            "release_group_mbid": "rg-1",
            "release_name": "Test Album",
            "artist_credit_name": "Artist",
            "release_group_primary_type": "  ",
            "release_date": "2024-01-01",
        }

        info = ListenBrainzModule._fresh_release_to_info(release)

        assert info is not None
        assert info.album_type is None


class TestSchemaAlbumTypeValidator:
    """Pydantic schema 应作为兜底清理 album_type 两端空白。"""

    def test_music_info_schema_strips_album_type(self):
        """MusicInfo schema 构造时 album_type 尾部空格应被去除。"""
        info = SchemaMusicInfo(
            album_type="Broadcast ",
            title="Test",
        )
        assert info.album_type == "Broadcast"

    def test_music_album_info_schema_strips_album_type(self):
        """MusicAlbumInfo schema 构造时 album_type 尾部空格应被去除。"""
        album = SchemaMusicAlbumInfo(
            album_type="EP ",
            title="Test EP",
        )
        assert album.album_type == "EP"

    def test_schema_blank_album_type_becomes_none(self):
        """纯空白的 album_type 应被归一为 None。"""
        info = SchemaMusicInfo(
            album_type="  ",
            title="Test",
        )
        assert info.album_type is None

    def test_schema_none_album_type_stays_none(self):
        """None 值的 album_type 应保持不变。"""
        info = SchemaMusicInfo(
            album_type=None,
            title="Test",
        )
        assert info.album_type is None
