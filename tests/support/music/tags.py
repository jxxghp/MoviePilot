"""
音乐标签证据的真实音频工厂；不注册测试用例。
"""

from pathlib import Path

from mutagen import File
from mutagen.flac import FLAC
from mutagen.id3 import TALB, TDOR, TDRC, TIT2, TPE1, TPE2, TPOS, TRCK, TXXX, UFID
from mutagen.mp4 import MP4

RECORDING = "11111111-1111-4111-8111-111111111111"


RELEASE = "22222222-2222-4222-8222-222222222222"


GROUP = "33333333-3333-4333-8333-333333333333"


TRACK = "44444444-4444-4444-8444-444444444444"


def _tag_audio(path: Path) -> None:
    """
    按容器的实际规范写入不同层级 ID，不调用产品标签写入实现。
    """
    if path.suffix == ".flac":
        audio = FLAC(path)
        audio.update({
            "title": ["Song"], "artist": ["Artist"], "albumartist": ["Album Artist"], "album": ["Record"],
            "date": ["2020-06-01"], "originaldate": ["2003-01-01"], "tracknumber": ["3/12"], "discnumber": ["2/2"],
            "musicbrainz_trackid": [RECORDING], "musicbrainz_albumid": [RELEASE],
            "musicbrainz_releasegroupid": [GROUP], "musicbrainz_releasetrackid": [TRACK],
        })
    elif path.suffix == ".m4a":
        audio = MP4(path)
        audio.update({"\xa9nam": ["Song"], "\xa9ART": ["Artist"], "\xa9alb": ["Record"],
                      "aART": ["Album Artist"], "\xa9day": ["2020-06-01"], "trkn": [(3, 12)], "disk": [(2, 2)]})
        for name, value in (("MusicBrainz Track Id", RECORDING), ("MusicBrainz Album Id", RELEASE),
                            ("MusicBrainz Release Group Id", GROUP), ("MusicBrainz Release Track Id", TRACK),
                            ("ORIGINALDATE", "2003-01-01")):
            audio[f"----:com.apple.iTunes:{name}"] = [value.encode("utf-8")]
    else:
        audio = File(path)
        if audio.tags is None:
            audio.add_tags()
        for frame in (
            TIT2(encoding=3, text=["Song"]), TPE1(encoding=3, text=["Artist"]),
            TALB(encoding=3, text=["Record"]), TPE2(encoding=3, text=["Album Artist"]),
            TDRC(encoding=3, text=["2020-06-01"]), TDOR(encoding=3, text=["2003-01-01"]),
            TRCK(encoding=3, text=["3/12"]), TPOS(encoding=3, text=["2/2"]),
            UFID(owner="http://musicbrainz.org", data=RECORDING.encode("ascii")),
            TXXX(encoding=3, desc="MusicBrainz Album Id", text=[RELEASE]),
            TXXX(encoding=3, desc="MusicBrainz Release Group Id", text=[GROUP]),
            TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=[TRACK]),
        ):
            audio.tags.add(frame)
    audio.save()
