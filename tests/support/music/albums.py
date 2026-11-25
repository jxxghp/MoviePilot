"""
音乐专辑匹配的发行回放与本地曲目工厂；不注册测试用例。
"""

from app.domain.meta.metamusic import MetaMusic


def _release_detail(release_id: str, title: str, artist: str, tracks: list[tuple[str, int]]):
    """
    构造 MusicBrainz Release 详情响应，tracks 为 (曲名, 时长秒) 列表。
    """
    return {
        "id": release_id,
        "title": title,
        "date": "2004-08-03",
        "artist-credit": [{"artist": {"id": "artist-1", "name": artist}}],
        "release-group": {"id": "rg-1", "primary-type": "Album", "secondary-types": []},
        "media": [
            {
                "position": 1,
                "track-count": len(tracks),
                "tracks": [
                    {
                        "position": index + 1,
                        "length": length * 1000,
                        "title": name,
                        "recording": {"id": f"rec-{index + 1}", "title": name, "length": length * 1000},
                    }
                    for index, (name, length) in enumerate(tracks)
                ],
            }
        ],
    }


ALBUM_TRACKS = [("我的地盘", 215), ("七里香", 299), ("借口", 265)]


def _local_tracks():
    """
    构造无标签整专目录读取得到的本地曲目元数据。
    """
    return [
        MetaMusic(title=name, track_number=index + 1, duration=length, audio_format="WAV")
        for index, (name, length) in enumerate(ALBUM_TRACKS)
    ]
