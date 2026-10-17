"""
验证飞牛影视媒体库的权限查询和同步范围映射。
"""

from types import SimpleNamespace
from unittest.mock import Mock

from app.modules.trimemedia.api import Category, MediaDb, Type
from app.modules.trimemedia.trimemedia import TrimeMedia
from app.schemas.types import MediaType


def test_admin_libraries_hide_unsynced_libraries_and_map_movie_details() -> None:
    """
    管理员查询保留同步范围内的电影库，并映射封面、数量与外网链接。
    """
    client = TrimeMedia.__new__(TrimeMedia)
    client._host = "https://trimemedia.example/v"
    client._username = "admin"
    client._password = "secret"
    client._userinfo = SimpleNamespace(is_admin=1)
    client._sync_libraries = ["movie-library"]
    client._playhost = "https://play.example/v"
    client._libraries = {}
    client._api = Mock()
    client._api.token = "session-token"
    client._api.host = "https://trimemedia.example/v"
    client._api.mdb_list.return_value = [
        MediaDb(
            guid="movie-library",
            category=Category.MOVIE,
            name="电影",
            posters=["/poster.jpg"],
            dir_list=["/media/movies"],
        ),
        MediaDb(guid="tv-library", category=Category.TV, name="剧集"),
        MediaDb(guid="other-library", category=Category.OTHERS, name="其他"),
    ]
    client._api.item_count.return_value = 12

    libraries = client.get_librarys(hidden=True)

    assert libraries is not None
    assert len(libraries) == 1
    movie = libraries[0]
    assert movie.id == "movie-library"
    assert movie.name == "电影"
    assert movie.type == MediaType.MOVIE.value
    assert movie.path == ["/media/movies"]
    assert movie.item_count == 12
    assert movie.image_list == ["https://trimemedia.example/v/poster.jpg?w=256"]
    assert movie.link == "https://play.example/v/library/movie-library"
    assert movie.use_cookies is True
    assert set(client._libraries) == {"movie-library", "tv-library", "other-library"}
    client._api.mdb_list.assert_called_once_with()
    client._api.mediadb_list.assert_not_called()
    client._api.item_count.assert_called_once_with(
        guid="movie-library", types=[Type.MOVIE, Type.TV]
    )
