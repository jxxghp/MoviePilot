import re
from pathlib import Path
from typing import Any, Optional

from app.core.meta import MetaMusic


class MusicNameParser:
    """解析音频文件名和目录名，在音频标签缺失时补充音乐识别线索。

    WAV 等容器不带标签、FLAC/MP3 标签不全时，唯一线索来自文件名和目录结构。
    这里只负责从文本中提取结构化信息（曲序、碟号、歌手、专辑、年份），
    不访问文件系统以外的任何资源，解析结果按"标签 > 文件名 > 目录名"优先级回填。
    """

    # 碟号-曲序前缀：1-02、CD1.03、Disc2-05 等，后面可跟分隔符和曲名
    _disc_track_prefix_pattern = re.compile(
        r"^\s*(?:(?:cd|disc|disk)\s*)?(?P<disc>\d{1,2})\s*[-._]\s*(?P<num>\d{1,3})"
        r"\s*[-–—.。、) ]*\s*(?P<rest>.*\S)?\s*$",
        re.IGNORECASE,
    )
    # 曲序前缀：01.、01 -、01)、01 晴天、Track 01 - 等
    _track_prefix_pattern = re.compile(
        r"^\s*(?:track\s*)?(?P<num>\d{1,3})\s*[-–—.。、) ]+\s*(?P<rest>.*\S)\s*$",
        re.IGNORECASE,
    )
    # 纯数字文件名：01.wav、Track 12.flac，只能得到曲序没有曲名
    _number_only_pattern = re.compile(
        r"^\s*(?:(?:track|cd|disc|disk)\s*)?(?P<num>\d{1,3})\s*$",
        re.IGNORECASE,
    )
    # 碟片目录名：CD1、Disc 2、Disk01
    _disc_dir_pattern = re.compile(
        r"^\s*(?:cd|disc|disk)\s*(?P<num>\d{1,2})\s*$",
        re.IGNORECASE,
    )
    # 目录名中的年份：(2004)、[2004]
    _year_pattern = re.compile(r"[(\[]\s*(?P<year>(?:19|20)\d{2})\s*[)\]]")
    # 目录名中的括号补充说明（格式、音质、厂牌等），如 [FLAC 24bit-96kHz]
    _bracket_pattern = re.compile(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)")
    # 歌手与标题/专辑的分隔符
    _artist_title_pattern = re.compile(
        r"^\s*(?P<artist>.+?)\s+[-–—]\s+(?P<title>.+?)\s*$"
    )
    _spaces_pattern = re.compile(r"\s+")

    @classmethod
    def strip_track_prefix(cls, stem: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
        """剥离文件名中的曲序和碟号前缀。

        :param stem: 不含扩展名的文件名
        :return: (曲序, 碟号, 剥离前缀后的曲名)，无法剥离的字段返回 None；
                 曲名为 None 表示文件名没有携带曲名信息
        """
        text = str(stem or "").strip()
        if not text:
            return None, None, None
        match = cls._disc_track_prefix_pattern.match(text)
        if match:
            return (
                int(match.group("num")),
                int(match.group("disc")),
                cls._clean(match.group("rest")),
            )
        match = cls._track_prefix_pattern.match(text)
        if match:
            return int(match.group("num")), None, cls._clean(match.group("rest"))
        match = cls._number_only_pattern.match(text)
        if match:
            # 纯数字文件名保留原始文本作为兜底标题，只提取曲序
            return int(match.group("num")), None, None
        return None, None, None

    @classmethod
    def split_artist_title(cls, text: str) -> tuple[Optional[str], str]:
        """拆分 `歌手 - 标题` 结构，未命中时原文作为标题返回。"""
        match = cls._artist_title_pattern.match(str(text or "").strip())
        if match:
            return cls._clean(match.group("artist")), cls._clean(match.group("title"))
        return None, cls._clean(text)

    @classmethod
    def parse_disc_dir(cls, name: str) -> Optional[int]:
        """识别 CD1、Disc 2 这类碟片子目录并返回碟号。"""
        match = cls._disc_dir_pattern.match(str(name or "").strip())
        return int(match.group("num")) if match else None

    @classmethod
    def parse_album_dir(cls, name: str) -> dict[str, Any]:
        """解析专辑目录名，提取歌手、专辑名、年份和音质描述。

        支持 `歌手 - 专辑 (2004) [FLAC 24bit-96kHz]` 等常见命名。
        """
        text = cls._clean(name)
        if not text:
            return {}
        year = None
        year_match = cls._year_pattern.search(text)
        if year_match:
            year = int(year_match.group("year"))
            text = cls._year_pattern.sub(" ", text)
        # 括号内的格式/音质描述先剥离出专辑名，但仍可用于音质解析
        brackets = " ".join(
            fragment
            for fragment in cls._bracket_pattern.findall(text)
        )
        album_text = cls._clean(cls._bracket_pattern.sub(" ", text))
        if not album_text:
            return {}
        artist, album = cls.split_artist_title(album_text)
        return {
            "artist": artist,
            "album": album,
            "year": year,
            "quality_text": cls._clean(f"{album_text} {brackets}"),
        }

    @classmethod
    def apply_path_context(cls, meta: MetaMusic, path: Path) -> MetaMusic:
        """用文件名和目录线索回填音乐元数据中缺失的字段。

        仅补充空字段，音频标签中已读取到的内容不会被目录猜测覆盖；
        标题来自文件名兜底（等于文件主干名）时视为缺失，允许用解析结果替换。
        """
        file_path = Path(path)
        stem = file_path.stem
        title_from_name = not meta.title or meta.title == stem

        # 文件名前缀：曲序、碟号、曲名
        track_number, disc_number, parsed_title = cls.strip_track_prefix(stem)
        if meta.track_number is None and track_number is not None:
            meta.track_number = track_number
        if meta.disc_number is None and disc_number is not None:
            meta.disc_number = disc_number
        if title_from_name:
            base_title = parsed_title or stem
            if not meta.artists:
                # `歌手 - 曲名` 文件名在无艺术家标签时继续拆分
                artist, title = cls.split_artist_title(base_title)
                if artist:
                    meta.artists = [artist]
                    base_title = title
            meta.title = base_title

        # 目录结构：父目录可能是碟片目录，专辑目录再往上一级
        parent = file_path.parent
        album_dir = parent
        parent_disc = cls.parse_disc_dir(parent.name)
        if parent_disc is not None:
            if meta.disc_number is None:
                meta.disc_number = parent_disc
            album_dir = parent.parent
        dir_info = cls.parse_album_dir(album_dir.name)
        if dir_info:
            # 目录名同时带歌手或年份才视为有意的专辑命名，避免把监控根目录误当专辑
            if dir_info.get("artist") or dir_info.get("year"):
                if not meta.album and dir_info.get("album"):
                    meta.album = dir_info["album"]
                if not meta.artists and dir_info.get("artist"):
                    meta.artists = [dir_info["artist"]]
                if not meta.album_artist and dir_info.get("artist"):
                    meta.album_artist = dir_info["artist"]
            if meta.year is None and dir_info.get("year"):
                meta.year = dir_info["year"]
            # 目录名里的格式、位深、采样率可补齐本地标签未声明的音质参数
            if dir_info.get("quality_text"):
                meta.apply_audio_quality(dir_info["quality_text"])
        return meta

    @classmethod
    def _clean(cls, value: Optional[str]) -> str:
        """压缩多余空白，返回可用于匹配和展示的文本。"""
        return cls._spaces_pattern.sub(" ", str(value or "")).strip()
