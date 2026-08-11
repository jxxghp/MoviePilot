import re
from pathlib import Path
from typing import Any, Optional

from app.core.meta.metabase import MetaBase
from app.schemas.types import MediaType


_AUDIO_FORMAT_PATTERN = re.compile(
    r"(?<![A-Z])(?P<format>DSD(?:64|128|256|512)?|DSF|DFF|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|"
    r"MP3|AAC|M4A|OGG|VORBIS|OPUS|WMA)(?![A-Z])",
    re.IGNORECASE,
)
_BIT_DEPTH_PATTERN = re.compile(r"(?<!\d)(?P<value>16|20|24|32)\s*(?:-?bit|bits?|位)(?!\w)", re.IGNORECASE)
_SAMPLE_RATE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>44(?:\.1)?|48|88(?:\.2)?|96|176(?:\.4)?|192|352(?:\.8)?|384|705(?:\.6)?|768)"
    r"\s*k(?:hz)?(?!\w)",
    re.IGNORECASE,
)
_BITRATE_PATTERN = re.compile(
    r"(?<!\d)(?P<value>\d{2,4})\s*k(?:bps?|b(?:it)?/?s?)?(?![a-z])",
    re.IGNORECASE,
)
_LOSSLESS_PATTERN = re.compile(r"(?<![A-Za-z0-9])(?:lossless|无损音质|无损)(?![A-Za-z0-9])", re.IGNORECASE)
_HIRES_PATTERN = re.compile(r"(?<!\w)(?:hi[ ._-]?res(?:olution)?|高解析|高分辨率音频)(?!\w)", re.IGNORECASE)

_AUDIO_FORMAT_ALIASES = {
    "WAVE": "WAV",
    "AIF": "AIFF",
    "VORBIS": "OGG",
    "M4A": "AAC",
    "DSF": "DSD",
    "DFF": "DSD",
}
_LOSSLESS_AUDIO_FORMATS = frozenset({"DSD", "FLAC", "ALAC", "APE", "WAV", "AIFF", "PCM"})
_LOSSY_AUDIO_FORMATS = frozenset({"MP3", "AAC", "OGG", "OPUS", "WMA"})


def normalize_audio_format(value: Any) -> Optional[str]:
    """将音频格式名称归一为订阅筛选和展示使用的规范值。"""
    text = str(value or "").strip().upper()
    if not text:
        return None
    match = _AUDIO_FORMAT_PATTERN.search(text)
    if not match:
        return text
    normalized = match.group("format").upper()
    if normalized.startswith("DSD"):
        return "DSD"
    return _AUDIO_FORMAT_ALIASES.get(normalized, normalized)


def infer_audio_lossless(audio_format: Any, explicit: Optional[bool] = None) -> Optional[bool]:
    """根据格式推断是否无损；显式识别结果优先于格式推断。"""
    if explicit is not None:
        return bool(explicit)
    normalized = normalize_audio_format(audio_format)
    if normalized in _LOSSLESS_AUDIO_FORMATS:
        return True
    if normalized in _LOSSY_AUDIO_FORMATS:
        return False
    return None


def parse_audio_quality(value: Any) -> dict[str, Any]:
    """从资源标题或描述中提取声明的格式、位深、采样率和码率。"""
    text = str(value or "")
    format_match = _AUDIO_FORMAT_PATTERN.search(text)
    bit_depth_match = _BIT_DEPTH_PATTERN.search(text)
    sample_rate_match = _SAMPLE_RATE_PATTERN.search(text)
    bitrate_match = _BITRATE_PATTERN.search(text)
    audio_format = normalize_audio_format(format_match.group("format")) if format_match else None
    bit_depth = int(bit_depth_match.group("value")) if bit_depth_match else None
    sample_rate = (
        int(float(sample_rate_match.group("value")) * 1000)
        if sample_rate_match
        else None
    )
    bitrate = int(bitrate_match.group("value")) * 1000 if bitrate_match else None
    explicit_lossless = True if (_LOSSLESS_PATTERN.search(text) or _HIRES_PATTERN.search(text)) else None
    return {
        "audio_format": audio_format,
        "audio_lossless": infer_audio_lossless(audio_format, explicit_lossless),
        "bit_depth": bit_depth,
        "sample_rate": sample_rate,
        "bitrate": bitrate,
    }


def audio_quality_tier(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> Optional[str]:
    """返回 hires、lossless 或 lossy 音质等级，未知参数返回 None。"""
    normalized = normalize_audio_format(audio_format)
    lossless = infer_audio_lossless(normalized, audio_lossless)
    if normalized == "DSD" or (lossless and ((bit_depth or 0) >= 24 or (sample_rate or 0) >= 88200)):
        return "hires"
    if lossless:
        return "lossless"
    if lossless is False or normalized or bitrate:
        return "lossy"
    return None


def audio_quality_score(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> int:
    """将音乐音质换算为 0 至 100 的稳定洗版优先级。"""
    normalized = normalize_audio_format(audio_format)
    lossless = infer_audio_lossless(normalized, audio_lossless)
    if normalized == "DSD" or (lossless and (bit_depth or 0) >= 24 and (sample_rate or 0) >= 192000):
        return 100
    if lossless:
        score = 86
        if (bit_depth or 0) >= 24:
            score += 5
        elif (bit_depth or 0) >= 16:
            score += 2
        if (sample_rate or 0) >= 176400:
            score += 7
        elif (sample_rate or 0) >= 88200:
            score += 5
        elif sample_rate:
            score += 2
        return min(score, 99)
    if bitrate:
        kbps = bitrate // 1000
        if kbps >= 320:
            return 80
        if kbps >= 256:
            return 70
        if kbps >= 192:
            return 60
        if kbps >= 128:
            return 50
        return 40
    return 35 if normalized in _LOSSY_AUDIO_FORMATS else 0


def format_audio_quality(
        audio_format: Any,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
) -> Optional[str]:
    """将音频技术参数格式化为适合识别结果和通知展示的紧凑文本。"""
    parts: list[str] = []
    normalized = normalize_audio_format(audio_format)
    if normalized:
        parts.append(normalized)
    if bit_depth:
        parts.append(f"{bit_depth}-bit")
    if sample_rate:
        rate = sample_rate / 1000
        parts.append(f"{rate:g} kHz")
    if bitrate:
        parts.append(f"{round(bitrate / 1000):,} kbps")
    if not parts:
        tier = audio_quality_tier(audio_format, audio_lossless, bit_depth, sample_rate, bitrate)
        if tier:
            parts.append({"hires": "Hi-Res", "lossless": "Lossless", "lossy": "Lossy"}[tier])
    return " · ".join(parts) or None


def _optional_int(value: Any) -> Optional[int]:
    """将音频技术参数安全转换为整数，空值与非数字返回 None。"""
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# 资源标题中的音质规格与发行标记（格式、位深采样、年份括号、发行实体标记），
# 拆分艺术家/曲名前需先剔除，否则「曲名 - FLAC [16B-44.1kHz]」会被误拆成艺术家与曲名
_MUSIC_FORMAT_TOKEN_ALT = (
    r"DSD(?:64|128|256|512)?|DSF|DFF|FLAC|ALAC|APE|CUE|WAV|WAVE|AIFF?|PCM|"
    r"MP3|AAC|M4A|OGG|VORBIS|OPUS|WMA|WEB-?DL|WEBRip|WEB"
)
_MUSIC_VIDEO_TOKEN_ALT = (
    r"1080[pi]|720p|2160[pi]|480[pi]|4k|8k|uhd|"
    r"bluray|blu-ray|bdrip|uhd\s*bd|hddvd|hdvd|hdtv|webrip|remux|"
    r"avc|hevc|x26[45]|h\.?26[45]|mpeg-?2|vc-?1|prores|av1|"
    r"dts(?:-hd\s*(?:ma|hra)?)?(?:\s*[257]\.1)?|"
    r"truehd|atmos|ddp?(?:\+[\w.]*)?|eac3|ac3|lpcm|flac\s*[257]\.1|"
    r"[257]\.1(?:\s*ch(?:annels?)?)?|stereo|mono|"
    r"hdr10\+?|dovi|dolby\s*vision|sub(?:title)?s?|chs&cht"
)
_MUSIC_QUALITY_TOKEN_RE = re.compile(
    r"\[[^\]]*\]|\((?:19|20)\d{2}\)|"
    rf"\b(?:{_MUSIC_FORMAT_TOKEN_ALT})\b|"
    r"\b\d{1,3}\s*-?\s*bits?\b|\b\d{2,4}(?:(?:[.．]|\s)\d)?\s*k(?:hz|bps?)\b|"
    # 无损声明词、 ripping 方式与流媒体发行实体标记（- Single / - EP），不是曲名的一部分；
    # 合集/精选是发行形态标记，CJK 词用非字母数字定界（\b 对中文不可靠）
    r"\b(?:single|ep|album)\b|(?<![A-Za-z0-9])(?:lossless|无损音质|无损|分轨|整轨|合集|精选)(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 演唱会/音乐视频种子的视频编码标记：分辨率、编码、容器与声道描述，
# 不是音乐文本信息，不剔除会污染曲名并阻断艺术家/曲名拆分
_MUSIC_VIDEO_TOKEN_RE = re.compile(rf"\b(?:{_MUSIC_VIDEO_TOKEN_ALT})\b", re.IGNORECASE)
# 尾部规格段判定：整段仅由格式词、视频标记、位深采样与无损声明词组成，
# 曲名含任何自然语言文本时判定失败，保证「曲名 (注释) - WEB-DL」不被误剥；
# 发布组标签（HHWEB/FHDMv）不是固定词表，由 _strip_spec_segments 的短词规则另行放行
_MUSIC_SPEC_SEGMENT_RE = re.compile(
    rf"^(?:\b(?:{_MUSIC_FORMAT_TOKEN_ALT}|{_MUSIC_VIDEO_TOKEN_ALT}|single|ep|album)\b"
    r"|\d{1,3}\s*-?\s*bits?|\d{2,4}(?:(?:[.．]|\s)\d)?\s*k(?:hz|bps?)"
    r"|lossless|无损音质|无损|分轨|整轨|合集|精选"
    r"|[\s\-–—−－/+]"
    r")+$",
    re.IGNORECASE,
)
# 尾部规格段定位：最后一个「空白+连字符」或「格式词连字符」分隔的片段；
# 片段内不允许连字符，保证匹配落在最尾部片段而不是从左吞掉整个尾巴
_MUSIC_TRAILING_SEGMENT_RE = re.compile(
    r"(?:\s+[\-–—−－]+\s*|(?P<prefix>[A-Za-z0-9]+)[\-–—−－]+)"
    r"(?P<segment>[^\s\-–—−－]+(?:\s+[^\s\-–—−－]+)*)\s*$"
)
# 年份括号：(2000)（2000）【2000】形式的发行年份，作为候选消歧线索
_MUSIC_YEAR_RE = re.compile(r"[\(（【]((?:19|20)\d{2})[\)）】]")
# 标题尾部独立年份：「xxx音乐会 2018」「Funky Jazz Saxophone 2024」「系列-2007」，
# 提取为发行年份线索并从曲名剥离，避免年份文本进入检索式造成零命中
_MUSIC_TRAILING_YEAR_RE = re.compile(r"(?<!\d)[\s\-–—]+((?:19|20)\d{2})\s*$")
# 无括号年份区间：全集/精选标题尾部的 1967-1995、2015-16，取结束年作为发行年份线索；
# CJK 字符属于 \w，不能用 \b 定界，改用数字负向断言；
# 短年右侧禁止再跟数字，避免把 2024-01-27 这类日期的 2024-01 误当区间；
# 区间位于标题末尾（后随空白/括号/行尾）才整段剔除（全集1967-1995），
# 后随「年」等内容文字时区间是标题的一部分（1995-2000年光华真纪录），仅提取年份保留原文
_MUSIC_YEAR_RANGE_STRIP_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*[-–—~～]\s*(?:(?:19|20)(\d{2})|(\d{2}))(?=\s|[\(（]|$)"
)
_MUSIC_YEAR_RANGE_DETECT_RE = re.compile(
    r"(?<!\d)(?:19|20)\d{2}\s*[-–—~～]\s*(?:(?:19|20)(\d{2})|(\d{2})(?!\d))"
)
# 标题开头的广播/发行日期前缀：2018.01.10、2024-01-27_20-00 等电视录制命名，
# 非音乐文本信息，需剔除后才能正确拆分艺术家与曲名
_MUSIC_DATE_PREFIX_RE = re.compile(
    r"^\s*(?:19|20)\d{2}\s*[.\-/年]\s*\d{1,2}\s*[.\-/月]\s*\d{1,2}\s*日?"
    r"(?:\s*[_\-–—\s]\s*\d{1,2}\s*[-:.]\s*\d{2})?"
)
# 多艺术家分隔符：& , ， / 、
_MUSIC_ARTIST_SEPARATOR_RE = re.compile(r"\s*(?:&|,|，|、|/)\s*")
# 合辑资源的 Various Artists 署名别名：VA 是场景命名常用缩写，
# 归一为 MusicBrainz 规范署名 Various Artists 才能命中合辑条目
_MUSIC_ARTIST_ALIASES = {"va": "Various Artists", "various artists": "Various Artists"}
# VA-Title / Various Artists-Title 无空格连字符前缀写法（场景命名），主拆分不适用需单独处理
_MUSIC_ALIAS_PREFIX_RE = re.compile(
    r"^\s*(?P<alias>VA|Various\.?\s*Artists)\s*[-–—−－]\s*(?P<title>.+\S)\s*$",
    re.IGNORECASE,
)
# 艺术家与曲名的主分隔：半角/全角空格包裹的连字符（- – — − －）
_MUSIC_ARTIST_TITLE_RE = re.compile(
    r"^\s*(?P<artist>.+?)\s+[\-–—−－]+\s+(?P<title>.+?)\s*$"
)
# 曲名后含书名号的括号注释（影视原声说明等）：「等得到 (电影《如影随心》主题曲 独唱版)」，
# 注释内的《》会抢先触发专辑书名号判定，需在结构解析前提取；
# 单层括号注释（电影版/Live）是 MusicBrainz 条目的消歧后缀，不提取
_MUSIC_TITLE_COMMENT_RE = re.compile(r"\s*[\(（](?P<comment>[^)）]*[《》][^)）]*)[\)）]\s*$")
# CJK「歌手《专辑名》」书名号命名：提取艺术家与专辑，专辑内尾部 -CD2 为碟号
_MUSIC_ALBUM_MARKER_RE = re.compile(
    r"^\s*(?P<artist>[^《》]+?)\s*《(?P<album>[^《》]+)》\s*(?P<rest>.*)$"
)
_MUSIC_ALBUM_DISC_RE = re.compile(r"[\s\-–—−－]*(?:cd|disc)\s*(\d{1,2})$", re.IGNORECASE)
# 曲名尾部重复的艺术家署名（如「名人名曲-毛阿敏」「xxx - 许茹芸」）
_MUSIC_ARTIST_SUFFIX_RE = re.compile(r"[\-–—−－]\s*(?P<suffix>[^\-–—−－]+?)\s*$")
# 艺术家段尾部的合集修饰词：「邓丽君作品全集」需剥离为「邓丽君」才能命中条目署名
_MUSIC_COLLECTION_SUFFIX_RE = re.compile(r"(?:的)?(?:作品)?(?:全集|精选集?|合集|精选辑)$")
# 曲序/碟号前缀：碟号-曲序（1-02、CD1.03、Disc2-05）与单曲序（01.、01 -、01 晴天、Track 01）
_MUSIC_DISC_TRACK_PREFIX_RE = re.compile(
    r"^\s*(?:(?:cd|disc|disk)\s*)?(?P<disc>\d{1,2})\s*[-._]\s*(?P<num>\d{1,3})"
    r"\s*[-–—.。、) ]*\s*(?P<rest>.*\S)?\s*$",
    re.IGNORECASE,
)
_MUSIC_TRACK_PREFIX_RE = re.compile(
    r"^\s*(?:track\s*)?(?P<num>\d{1,3})\s*[-–—.。、) ]+\s*(?P<rest>.*\S)\s*$",
    re.IGNORECASE,
)
# 纯数字文件名：01.wav、Track 12.flac，只能得到曲序没有曲名
_MUSIC_NUMBER_ONLY_RE = re.compile(
    r"^\s*(?:(?:track|cd|disc|disk)\s*)?(?P<num>\d{1,3})\s*$",
    re.IGNORECASE,
)
# 碟片目录名：CD1、Disc 2、Disk01
_MUSIC_DISC_DIR_RE = re.compile(
    r"^\s*(?:cd|disc|disk)\s*(?P<num>\d{1,2})\s*$",
    re.IGNORECASE,
)
# 目录名中的年份：(2004)、[2004]
_MUSIC_DIR_YEAR_RE = re.compile(r"[(\[]\s*(?P<year>(?:19|20)\d{2})\s*[)\]]")
# 目录名中的括号补充说明（格式、音质、厂牌等），如 [FLAC 24bit-96kHz]
_MUSIC_BRACKET_RE = re.compile(r"\[[^\]]*\]|【[^】]*】|\([^)]*\)")
_MUSIC_SPACES_RE = re.compile(r"\s+")
_MUSIC_COMPACT_RE = re.compile(r"[\W_]+", re.UNICODE)
# 日文资源标题大量使用全角字符（ＷＯＷＯＷ、５０ｔｈ、全角空格与括号），
# 归一为半角后才能与 MusicBrainz 条目及内置模式匹配
_FULLWIDTH_EXCLAMATION = 0xFF01
_FULLWIDTH_TILDE = 0xFF5E
_HALFWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_MAP = {
    0x3000: " ",   # 全角空格
    0x300C: "[", 0x300D: "]",    # 「」
    0x300E: "[", 0x300F: "]",    # 『』
    0x3010: "[", 0x3011: "]",    # 【】
    # 《》多为专辑书名号（歌手《专辑名》），不属于格式注释，保留原样由专辑结构规则处理
    0xFF08: "(", 0xFF09: ")",    # （）
    0xFF3B: "[", 0xFF3D: "]",    # ［］
    0xFF5B: "{", 0xFF5D: "}",    # ｛｝
}


def _string_list(value: Any) -> list[str]:
    """将标签原始值归一为非空字符串列表，兼容单值、列表与逗号分隔。"""
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value if str(item)]
    return [str(value)]


def _to_halfwidth(value: str) -> str:
    """全角字符归一为半角：FF01-FF5E 按偏移换算，全角空格与括号类字符查表替换。"""

    def _translate(char: str) -> str:
        code = ord(char)
        if _FULLWIDTH_EXCLAMATION <= code <= _FULLWIDTH_TILDE:
            return chr(code - _HALFWIDTH_OFFSET)
        return _FULLWIDTH_MAP.get(code, char)

    return "".join(_translate(char) for char in str(value or ""))


# 场景点分命名的可替换点号：字母/数字两侧的点分单词（Shan.Ge.Liao.Zai.2023）、
# 四位年份间的点分（1999.2022）与环绕符号的点（Purple.&.Orchestra、-.Live）；
# 不含单位数字间的小数点，保护 5.1 声道与 44.1kHz 这类规格写法
_SCENE_DOT_RE = re.compile(
    r"(?<=[A-Za-z])\.(?=[A-Za-z])"
    r"|(?<=[A-Za-z])\.(?=\d)"
    r"|(?<=\d)\.(?=[A-Za-z])"
    r"|(?<=\d{4})\.(?=\d)"
    r"|(?<=[A-Za-z0-9])\.(?=[\-–—&+])"
    r"|(?<=[\-–—&+])\.(?=[A-Za-z0-9])"
)


def _normalize_scene_dots(value: str) -> str:
    """场景命名的点分单词（Shan.Ge.Liao.Zai.2023）归一为空格。

    点分隔少于 3 处时视为普通缩写（如 E.S.Posthumus）不处理，
    避免破坏艺术家名中的合法点号。
    """
    if len(_SCENE_DOT_RE.findall(value or "")) < 3:
        return value
    return _SCENE_DOT_RE.sub(" ", value)


# 连续单字母空格序列是缩写点号被全角归一/场景点分压平的结果（Ｓ.Ｈ.Ｅ -> S H E），
# 还原为点号缩写（S.H.E）才能与 MusicBrainz 条目署名比对
_LETTER_RUN_RE = re.compile(r"(?<![A-Za-z])((?:[A-Za-z] ){2,}[A-Za-z])(?![A-Za-z])")


def _restore_letter_abbrev(value: str) -> str:
    """把连续单字母空格序列还原为点号缩写（S H E -> S.H.E）。"""
    return _LETTER_RUN_RE.sub(
        lambda m: ".".join(m.group(1).split(" ")), str(value or "")
    )


class MetaMusic(MetaBase):
    """音乐文件名及音频标签解析结果，作为 MetaBase 的音乐分支实现。"""

    def __init__(
        self,
        org_string: Optional[str] = None,
        title: Optional[str] = None,
        artists: Optional[list[str]] = None,
        album: Optional[str] = None,
        album_artist: Optional[str] = None,
        year: Optional[int] = None,
        disc_number: Optional[int] = None,
        track_number: Optional[int] = None,
        total_discs: Optional[int] = None,
        total_tracks: Optional[int] = None,
        version: Optional[str] = None,
        audio_format: Optional[str] = None,
        audio_lossless: Optional[bool] = None,
        bit_depth: Optional[int] = None,
        sample_rate: Optional[int] = None,
        bitrate: Optional[int] = None,
        duration: Optional[int] = None,
        isrc: Optional[str] = None,
        media_source: Optional[str] = None,
        media_id: Optional[str] = None,
        parse_title: bool = False,
    ):
        # 音乐无季集概念，仅复用 MetaBase 的基础字段初始化，不触发副标题季集识别
        super().__init__(title or org_string or "")
        self.type = MediaType.MUSIC
        self.org_string = org_string
        self.title = title
        self.artists = list(artists) if artists else []
        self.album = album
        self.album_artist = album_artist
        self.year = year
        self.disc_number = disc_number
        self.track_number = track_number
        self.total_discs = total_discs
        self.total_tracks = total_tracks
        self.version = version
        self.audio_format = normalize_audio_format(audio_format)
        self.audio_lossless = infer_audio_lossless(self.audio_format, audio_lossless)
        self.bit_depth = bit_depth
        self.sample_rate = sample_rate
        self.bitrate = bitrate
        self.duration = duration
        self.isrc = isrc
        self.media_source = media_source
        self.media_id = media_id
        if parse_title:
            # 种子/文件名字符串场景：解析艺术家、曲名、年份并补充音质参数
            self.apply_title(self.title or org_string or "")

    @property
    def name(self) -> str:
        """返回搜索和展示使用的音乐名称，优先专辑名其次标题。"""
        return self.album or self.title or ""

    @name.setter
    def name(self, value: Optional[str]) -> None:
        """辅助识别链回写标题时落到 title 字段，保持音乐名称可写。"""
        self.title = value or None

    @property
    def original_name(self) -> str:
        """返回未经过通用识别词处理的原始名称，兼容影视识别链的公共访问。"""
        return self.org_string or self.title or self.album or ""

    @property
    def artist(self) -> str:
        """返回兼容现有展示组件的艺术家文本。"""
        return " / ".join(self.artists)

    @property
    def audio_quality(self) -> Optional[str]:
        """返回 hires、lossless 或 lossy 音质等级。"""
        return audio_quality_tier(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_quality_score(self) -> int:
        """返回订阅洗版使用的音质优先级。"""
        return audio_quality_score(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    @property
    def audio_specs(self) -> Optional[str]:
        """返回识别结果和通知使用的格式化音频参数。"""
        return format_audio_quality(
            self.audio_format, self.audio_lossless, self.bit_depth, self.sample_rate, self.bitrate
        )

    def apply_audio_quality(self, value: Any, overwrite: bool = False) -> None:
        """从资源文本补充音质参数，默认保留文件标签读取到的实际值。"""
        parsed = parse_audio_quality(value)
        for key, parsed_value in parsed.items():
            if parsed_value is not None and (overwrite or getattr(self, key, None) is None):
                setattr(self, key, parsed_value)
        self.audio_format = normalize_audio_format(self.audio_format)
        self.audio_lossless = infer_audio_lossless(self.audio_format, self.audio_lossless)

    def apply_title(self, value: Any) -> None:
        """解析种子/文件名标题字符串，提取艺术家、曲名、年份并补充音质参数。

        与影视 MetaVideo 在构造时解析标题一致，这里是音乐分支的识别核心：
        先剔除音质规格与发行标记，再拆分艺术家与曲名，最后提取曲序前缀。
        """
        raw = str(value or "")
        self.apply_audio_quality(raw)
        normalized = self._normalize_text(raw)
        if not normalized:
            return
        # 年份括号在规格剔除前提取，作为发行年份线索参与候选消歧
        years = _MUSIC_YEAR_RE.findall(normalized)
        if years and not self.year:
            # 多个年份括号时取最后一个，资源标题中年份通常位于末位
            self.year = int(years[-1])
        # 音质标记先剔除再拆分，避免规格文本独占曲名位置；
        # 尾部纯规格段（含发布组标签）先整段剔除，保护「曲名 (注释)」不被拆散
        cleaned = self._strip_spec_segments(normalized)
        cleaned = self._strip_quality_tokens(cleaned)
        # 广播/发行日期前缀与年份区间在规格剔除后处理，避免误伤曲名中的短数字序列
        cleaned, range_year = self._strip_date_prefix(cleaned)
        if range_year and not self.year:
            self.year = range_year
        # 曲名尾部括号注释先提取：注释内嵌套《》会干扰专辑书名号判定；
        # 注释是曲名的版本说明，最终拼回曲名供展示与弱匹配
        comment = None
        comment_match = _MUSIC_TITLE_COMMENT_RE.search(cleaned)
        if comment_match:
            comment = comment_match.group("comment").strip()
            cleaned = cleaned[: comment_match.start()].strip()
        # 规格剔除后曲名侧无剩余文本时，尾部悬空分隔符仍是艺术家署名结构
        # （「周杰伦 - 合集 2000-2022 - FLAC 16bit 44 1khz」剔除后仅剩「周杰伦 -」）
        dangling = re.fullmatch(r"(?P<artist>.+?)\s+[\-–—−－]+", cleaned) if cleaned else None
        if dangling and not self.artists:
            self.artists = self._split_artists(dangling.group("artist"))
            self.title = None
            self._apply_track_prefix()
            return
        # CJK「歌手《专辑名》」书名号命名优先于连字符拆分，提取艺术家与专辑实体
        marker = None if self.artists else _MUSIC_ALBUM_MARKER_RE.match(cleaned)
        if marker:
            # 书名号前的艺术家段可能是「曲名-歌手」无空格连字符写法（为你盛开-许巍），
            # 反向拆分后首段是曲名线索，比专辑名更接近单曲检索目标
            song_hint: Optional[str] = None
            hyphen_artists, head_title = self._split_cjk_hyphen(marker.group("artist"))
            if hyphen_artists:
                self.artists = hyphen_artists
                song_hint = self._clean_tail(head_title)
            else:
                self.artists = self._split_artists(marker.group("artist"))
            album = self._normalize_text(marker.group("album"))
            disc_match = _MUSIC_ALBUM_DISC_RE.search(album)
            if disc_match:
                # 专辑名尾部 -CD2 是碟号线索不是专辑名内容
                self.disc_number = self.disc_number or int(disc_match.group(1))
                album = album[: disc_match.start()].strip()
            self.album = album
            rest = marker.group("rest").strip(" \t-–—−－_《》.")
            # 书名号后仅剩年份时作为发行年份线索
            if rest and re.fullmatch(r"(19|20)\d{2}", rest):
                if not self.year:
                    self.year = int(rest)
                rest = ""
            # 书名号后仅剩碟号（CD2/Disc1）时提取为碟号线索
            rest_disc = re.fullmatch(r"(?:cd|disc|disk)\s*(\d{1,2})", rest, re.IGNORECASE) if rest else None
            if rest_disc:
                self.disc_number = self.disc_number or int(rest_disc.group(1))
                rest = ""
            # 曲名回退依次用剩余文本、曲名线索与专辑名供检索
            self._finalize_title(rest or song_hint or album)
            if comment:
                # 书名号结构下注释属于专辑后的补充说明，拼回曲名
                self.title = f"{self.title} ({comment})" if self.title else comment
            self._apply_track_prefix()
            return
        match = _MUSIC_ARTIST_TITLE_RE.match(cleaned)
        if match and not self.artists:
            self.artists = self._split_artists(match.group("artist"))
            self._finalize_title(
                self._strip_artist_suffix(
                    self._clean_tail(match.group("title")), self.artists))
        elif cleaned:
            # 场景命名的 VA-Title 无空格连字符写法，主拆分不适用，按别名前缀单独拆分
            alias_match = None if self.artists else _MUSIC_ALIAS_PREFIX_RE.match(cleaned)
            if alias_match:
                # 别名前缀均为合辑署名写法，未收录变体统一归一为 Various Artists
                self.artists = [_MUSIC_ARTIST_ALIASES.get(
                    alias_match.group("alias").casefold(), "Various Artists")]
                self._finalize_title(self._clean_tail(alias_match.group("title")))
            else:
                # CJK 标题常见「专辑名-歌手」无空格连字符写法，主拆分未命中时兜底反向拆分
                artists, title = self._split_cjk_hyphen(cleaned)
                if not artists:
                    # 拉丁「艺术家-专辑」无空格连字符写法（Gene Clark-White Light）
                    artists, title = self._split_latin_hyphen(cleaned)
                if artists:
                    self.artists = artists
                    self._finalize_title(self._clean_tail(title))
                else:
                    # 「艺术家 年份 专辑」三明治结构（Leehom Wang 2010 The 18 Martial Arts），
                    # 其他拆分均无艺术家线索时按中部年份拆分，年份提取为发行线索
                    artists, title, inner_year = self._split_year_sandwich(cleaned)
                    if artists:
                        self.artists = artists
                        if inner_year and not self.year:
                            self.year = inner_year
                        self._finalize_title(self._clean_tail(title))
                    else:
                        title = self._clean_tail(cleaned)
                        # 无艺术家线索时剥离 CJK 标题尾部的「曲名-歌手」署名，候选比对阶段负责验证身份
                        self._finalize_title(self._strip_cjk_artist_suffix(title))
        else:
            self.title = None
        # 括号注释属于曲名的版本/出处说明，剥离的规格文本之后拼回展示
        if comment and self.title:
            self.title = f"{self.title} ({comment})"
        self._apply_track_prefix()

    @classmethod
    def _pop_trailing_year(cls, value: str) -> tuple[str, Optional[int]]:
        """剥离曲名尾部独立年份并返回 (曲名, 年份)。"""
        match = _MUSIC_TRAILING_YEAR_RE.search(str(value or ""))
        if not match:
            return value, None
        head = value[:match.start()]
        # 「Live At Montreux 1999 2022」连续双年份属于标题内容，不剥离
        if re.search(r"(?:19|20)\d{2}\s*$", head):
            return value, None
        return head.strip(), int(match.group(1))

    def _finalize_title(self, value: str) -> None:
        """设置曲名并提取尾部独立年份作为发行年份线索。"""
        title, year = self._pop_trailing_year(value)
        if year and not self.year:
            self.year = year
        self.title = title

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """全角归一为半角后清理多余空白，文件名消毒下划线统一转空格。"""
        text = _to_halfwidth(str(value or "")).replace("_", " ")
        # 场景命名用点号分隔单词（Shan.Ge.Liao.Zai.2023），归一为空格便于拆分检索
        text = _normalize_scene_dots(text)
        # 缩写点号被全角归一/场景点分压平成单字母空格序列，在点分之后还原（S H E -> S.H.E）
        text = _restore_letter_abbrev(text)
        return _MUSIC_SPACES_RE.sub(" ", text).strip()

    @classmethod
    def _split_artists(cls, value: str) -> list[str]:
        """拆分多艺术家字段（如 章子怡 & 周深），保留顺序供检索与候选比对使用。"""
        return [
            cls._canonical_artist(artist.strip())
            for artist in _MUSIC_ARTIST_SEPARATOR_RE.split(value)
            if artist.strip()
        ]

    @staticmethod
    def _canonical_artist(value: str) -> str:
        """归一 VA 等合辑艺术家别名为 MusicBrainz 规范署名。"""
        return _MUSIC_ARTIST_ALIASES.get(value.casefold(), value)

    @staticmethod
    def _clean_tail(value: str) -> str:
        """修剪曲名尾部残留：流媒体文件名消毒产生的下划线与悬空分隔符。"""
        return re.sub(r"[\s_\-–—−－/.+]+$", "", str(value or "")).strip()

    @classmethod
    def _strip_spec_segments(cls, value: str) -> str:
        """从尾部反复剥离纯规格段（- WEB-DL - 16bit ALAC-HHWEB）。

        规格段在艺术家/曲名拆分前整段剔除，避免规格后缀把主拆分位置推到
        「艺术家 - 曲名」的连字符上；曲名含自然语言文本的段永远不会被误剥。
        """
        text = str(value or "").strip()
        while True:
            segment_match = _MUSIC_TRAILING_SEGMENT_RE.search(text)
            if not segment_match:
                return text
            prefix = segment_match.group("prefix")
            segment = segment_match.group("segment")
            # 无空格连字符段（FLAC-HHWEB）只有紧跟格式词时才是发布组标签，
            # 否则是「曲名-歌手」类连字符命名，交由后续拆分规则处理
            if prefix and not re.fullmatch(rf"{_MUSIC_FORMAT_TOKEN_ALT}", prefix, re.IGNORECASE):
                return text
            # 年份括号是发行线索不是规格词，占位后再判定，避免「曲名 (2000)」被误当规格段
            probe = re.sub(r"\((?:19|20)\d{2}\)", " ", segment)
            has_spec_token = bool(
                _MUSIC_QUALITY_TOKEN_RE.search(probe) or _MUSIC_VIDEO_TOKEN_RE.search(probe)
            )
            # 加号是 APE+CUE 类格式联合写法的分隔符，占位为空格后再判定
            probe = _MUSIC_VIDEO_TOKEN_RE.sub(" ", _MUSIC_QUALITY_TOKEN_RE.sub(" ", probe))
            residue = probe.replace("+", " ").strip()
            if residue and not cls._is_spec_residue(residue, bool(prefix)):
                return text
            # 不含任何规格词的纯标签段，仅在紧跟格式词的无空格形态下才是发布组标签；
            # 「艺术家 - 单词曲名」的曲名段不含规格词，必须保留
            if not has_spec_token and not prefix:
                return text
            text = text[: segment_match.start()].rstrip()

    @classmethod
    def _is_spec_residue(cls, residue: str, has_prefix: bool) -> bool:
        """判定规格词替换后的残留是否为发布组标签而非自然语言曲名。

        发布组标签只出现在格式词无空格连字符形态（FLAC-HHWEB、AAC-FHDMv），
        此时短字母数字组合可放行；空白连字符分隔的段（艺术家 - 曲名 FLAC）
        残留可能是自然语言曲名，仅严格规格词表可过。
        """
        if _MUSIC_SPEC_SEGMENT_RE.fullmatch(residue):
            return True
        tokens = residue.split()
        return has_prefix and bool(tokens) and all(
            len(token) <= 8 and re.fullmatch(r"[A-Za-z0-9]+", token) for token in tokens
        )

    @classmethod
    def _strip_quality_tokens(cls, value: str) -> str:
        """剥离音频格式、视频编码、规格参数与年份括号，保留有效的艺术家与曲名文本。"""
        raw = str(value or "")
        text = _MUSIC_QUALITY_TOKEN_RE.sub(" ", raw)
        text = _MUSIC_VIDEO_TOKEN_RE.sub(" ", text)
        # 规格剥离后可能残留悬空分隔符（含 APE+CUE 类格式联合写法残留的加号），统一修剪
        return cls._normalize_text(re.sub(r"^[\s\-–—−－/+]+|[\s\-–—−－/+]+$", "", text))

    @classmethod
    def _strip_artist_suffix(cls, value: str, artists: list[str]) -> str:
        """剥离曲名尾部重复的艺术家署名，资源标题常见「曲名 - 艺术家」写法。"""
        if not artists or not value:
            return value
        match = _MUSIC_ARTIST_SUFFIX_RE.search(value)
        if not match:
            return value
        suffix = cls.compact_text(match.group("suffix"))
        if not suffix:
            return value
        if any(suffix == cls.compact_text(artist) for artist in artists):
            return value[:match.start()].strip()
        return value

    @classmethod
    def _strip_cjk_artist_suffix(cls, value: str) -> str:
        """无艺术家线索时剥离 CJK 曲名尾部的歌手署名（「因为有你-毛阿敏」）。

        仅限曲名与署名都含 CJK 才剥离，避免误伤英文曲名的连字符组成部分
        （如 Live-in-XXX）；剥离后的署名身份由候选比对阶段验证。
        """
        if not value:
            return value
        match = _MUSIC_ARTIST_SUFFIX_RE.search(value)
        if not match:
            return value
        head = value[:match.start()].strip()
        suffix = match.group("suffix").strip()
        if head and suffix and cls._contains_cjk(head) and cls._contains_cjk(suffix):
            return head
        return value

    @classmethod
    def _strip_date_prefix(cls, value: str) -> tuple[str, Optional[int]]:
        """剔除标题开头的广播/发行日期前缀，并提取尾部年份区间结束年。

        :return: (处理后的文本, 年份区间结束年)，无区间时为 None
        """
        text = _MUSIC_DATE_PREFIX_RE.sub("", str(value or "")).strip(" -–—_\t")
        year: Optional[int] = None
        # 年份区间在日期前缀剔除后提取；位于标题末尾的整段剔除，其余仅提取年份线索
        range_match = _MUSIC_YEAR_RANGE_STRIP_RE.search(text)
        strip_span = True
        if not range_match:
            range_match = _MUSIC_YEAR_RANGE_DETECT_RE.search(text)
            strip_span = False
        if range_match:
            end_year = int(range_match.group(1) or range_match.group(2))
            # 两位结束年补世纪：50 以上视为 19xx，否则 20xx
            year = end_year + (1900 if end_year >= 50 else 2000)
            if strip_span:
                text = text[:range_match.start()] + text[range_match.end():]
        return cls._normalize_text(text), year

    @staticmethod
    def _contains_cjk(value: str) -> bool:
        """判断文本是否包含中日韩字符，用于限定裸连字符拆分适用范围。"""
        return any(
            0x3040 <= code <= 0x30FF or 0x3400 <= code <= 0x9FFF or 0xAC00 <= code <= 0xD7AF
            for code in map(ord, value or "")
        )

    @classmethod
    def _split_cjk_hyphen(cls, value: str) -> tuple[Optional[list[str]], str]:
        """CJK 文本按无空格连字符拆分「专辑/曲名-艺术家」，两侧均需含 CJK 才采信。

        英文标题连字符多为曲名组成部分（如 Alchemy-Live），不适用此拆分；
        多个连字符时取最后一段为艺术家，兼容「作品全集-系列名-艺术家」。
        """
        text = str(value or "").strip()
        if not cls._contains_cjk(text):
            return None, text
        # 依次尝试双破折号（为你盛开——许巍）、半角连字符、全角连字符（已归一）与单破折号
        head, sep, tail = "", "", ""
        for separator in ("——", "-", "－", "—"):
            head, sep, tail = text.rpartition(separator)
            if sep:
                break
        if not sep:
            return None, text
        head = head.strip(" \t-–—−－")
        tail = tail.strip(" \t-–—−－")
        if head and tail and cls._contains_cjk(head) and cls._contains_cjk(tail):
            # 分隔符后跟随多个词时只取首词为艺术家（「为你盛开——许巍 巡回演唱会」）
            artist_text = tail.split(" ", 1)[0]
            # 艺术家段常见「xx作品全集」合集修饰，剥离后才能与条目署名比对
            artist = _MUSIC_COLLECTION_SUFFIX_RE.sub("", artist_text).strip() or artist_text
            return [artist], head
        return None, text

    @classmethod
    def _split_latin_hyphen(cls, value: str) -> tuple[Optional[list[str]], str]:
        """拉丁「艺术家-专辑」无空格连字符命名拆分（Gene Clark-White Light）。

        首个连字符左侧为艺术家；两侧均需含空格（至少两个词）才采信：
        左侧保护 Jay-Z 类连字符艺术家名，右侧排除 -ProfessorP 类发布组标签；
        艺术家身份由候选比对验证。
        """
        text = str(value or "").strip()
        if cls._contains_cjk(text):
            return None, text
        head, sep, tail = text.partition("-")
        if not sep:
            return None, text
        head = head.strip(" \t-–—−－")
        tail = tail.strip(" \t-–—−－")
        if head and tail and " " in head and " " in tail:
            return cls._split_artists(head), tail
        return None, text

    # 「艺术家 年份 专辑」三明治结构：艺术家段为不超过 4 个词的拉丁词组；
    # 贪婪匹配艺术家段，避免懒惰量词把首个词当艺术家、剩余词混入专辑名
    _YEAR_SANDWICH_RE = re.compile(
        r"^(?P<artist>[A-Za-z][A-Za-z0-9&+.'’\- ]*)\s+(?P<year>(?:19|20)\d{2})\s+(?P<rest>\S.*)$"
    )

    @classmethod
    def _split_year_sandwich(cls, value: str) -> tuple[Optional[list[str]], str, Optional[int]]:
        """按中部独立年份拆分「艺术家 年份 专辑」（Jacky Cheung 1987 Jacky）。

        :return: (艺术家列表, 专辑名, 年份)，不适用时 (None, 原文, None)
        """
        match = cls._YEAR_SANDWICH_RE.match(str(value or "").strip())
        if not match:
            return None, str(value or ""), None
        artist_text = match.group("artist").strip()
        raw_rest = match.group("rest").strip()
        rest = raw_rest.strip(" \t-–—−－")
        # 艺术家段限 4 个词以内；年份后紧跟另一个年份时是年份区间不是三明治结构；
        # 剩余段原文需以字母开头：「… 2014 2.0 -MINIBEL」数字开头、
        # 「… 2013 -PTer」发布组标签连字符开头都不是专辑名，拒绝拆分
        if (
            len(artist_text.split()) <= 4
            and rest
            and not re.match(r"(?:19|20)\d{2}\b", raw_rest)
            and raw_rest[0].isalpha()
            and any(char.isalpha() for char in rest)
        ):
            return cls._split_artists(artist_text), rest, int(match.group("year"))
        return None, str(value or ""), None

    @staticmethod
    def compact_text(value: Any) -> str:
        """移除大小写、空白与标点，生成比对使用的紧凑文本。"""
        return _MUSIC_COMPACT_RE.sub("", str(value or "").casefold())

    @staticmethod
    def _clean_text(value: Any) -> str:
        """压缩多余空白，返回可用于匹配和展示的文本。"""
        return _MUSIC_SPACES_RE.sub(" ", str(value or "")).strip()

    def _apply_track_prefix(self) -> None:
        """提取曲名开头的曲序/碟号前缀（01. 曲名、1-02 曲名、CD1.03 曲名）。"""
        if not self.title:
            return
        track_number, disc_number, remainder = self.split_track_prefix(self.title)
        if track_number is None and disc_number is None:
            return
        if self.track_number is None:
            self.track_number = track_number
        if self.disc_number is None:
            self.disc_number = disc_number
        if remainder:
            self.title = remainder

    @classmethod
    def split_track_prefix(cls, stem: str) -> tuple[Optional[int], Optional[int], Optional[str]]:
        """剥离文件名/曲名中的曲序和碟号前缀。

        :param stem: 不含扩展名的文件名或曲名文本
        :return: (曲序, 碟号, 剥离前缀后的曲名)，无法剥离的字段返回 None；
                 曲名为 None 表示文本没有携带曲名信息
        """
        text = str(stem or "").strip()
        if not text:
            return None, None, None
        match = _MUSIC_DISC_TRACK_PREFIX_RE.match(text)
        if match:
            return (
                int(match.group("num")),
                int(match.group("disc")),
                cls._clean_text(match.group("rest")),
            )
        match = _MUSIC_TRACK_PREFIX_RE.match(text)
        if match:
            return int(match.group("num")), None, cls._clean_text(match.group("rest"))
        match = _MUSIC_NUMBER_ONLY_RE.match(text)
        if match:
            # 纯数字文件名保留原始文本作为兜底标题，只提取曲序
            return int(match.group("num")), None, None
        return None, None, None

    @classmethod
    def split_artist_title(cls, text: str) -> tuple[Optional[str], str]:
        """拆分 `歌手 - 标题` 结构，未命中时原文作为标题返回。"""
        cleaned = cls._clean_text(text)
        if not cleaned:
            return None, ""
        match = _MUSIC_ARTIST_TITLE_RE.match(cleaned)
        if match:
            return cls._clean_text(match.group("artist")), cls._clean_text(match.group("title"))
        return None, cleaned

    @classmethod
    def parse_disc_dir(cls, name: str) -> Optional[int]:
        """识别 CD1、Disc 2 这类碟片子目录并返回碟号。"""
        match = _MUSIC_DISC_DIR_RE.match(str(name or "").strip())
        return int(match.group("num")) if match else None

    @classmethod
    def parse_album_dir(cls, name: str) -> dict[str, Any]:
        """解析专辑目录名，提取歌手、专辑名、年份和音质描述。

        支持 `歌手 - 专辑 (2004) [FLAC 24bit-96kHz]` 等常见命名。
        """
        text = cls._clean_text(name)
        if not text:
            return {}
        year = None
        year_match = _MUSIC_DIR_YEAR_RE.search(text)
        if year_match:
            year = int(year_match.group("year"))
            text = _MUSIC_DIR_YEAR_RE.sub(" ", text)
        # 括号内的格式/音质描述先剥离出专辑名，但仍可用于音质解析
        brackets = " ".join(fragment for fragment in _MUSIC_BRACKET_RE.findall(text))
        album_text = cls._clean_text(_MUSIC_BRACKET_RE.sub(" ", text))
        if not album_text:
            return {}
        artist, album = cls.split_artist_title(album_text)
        return {
            "artist": artist,
            "album": album,
            "year": year,
            "quality_text": cls._clean_text(f"{album_text} {brackets}"),
        }

    def apply_path_context(self, path: "str | Path") -> "MetaMusic":
        """用文件名和目录线索回填音乐元数据中缺失的字段。

        仅补充空字段，音频标签中已读取到的内容不会被目录猜测覆盖；
        标题来自文件名兜底（等于文件主干名）时视为缺失，允许用解析结果替换。
        """
        file_path = Path(path)
        stem = file_path.stem
        title_from_name = not self.title or self.title == stem

        # 文件名前缀：曲序、碟号、曲名
        track_number, disc_number, parsed_title = self.split_track_prefix(stem)
        if self.track_number is None and track_number is not None:
            self.track_number = track_number
        if self.disc_number is None and disc_number is not None:
            self.disc_number = disc_number
        if title_from_name:
            base_title = parsed_title or stem
            if not self.artists:
                # `歌手 - 曲名` 文件名在无艺术家标签时继续拆分
                artist, title = self.split_artist_title(base_title)
                if artist:
                    self.artists = [artist]
                    base_title = title
            self.title = base_title

        # 目录结构：父目录可能是碟片目录，专辑目录再往上一级
        parent = file_path.parent
        album_dir = parent
        parent_disc = self.parse_disc_dir(parent.name)
        if parent_disc is not None:
            if self.disc_number is None:
                self.disc_number = parent_disc
            album_dir = parent.parent
        dir_info = self.parse_album_dir(album_dir.name)
        if dir_info:
            # 目录名同时带歌手或年份才视为有意的专辑命名，避免把监控根目录误当专辑
            if dir_info.get("artist") or dir_info.get("year"):
                if not self.album and dir_info.get("album"):
                    self.album = dir_info["album"]
                if not self.artists and dir_info.get("artist"):
                    self.artists = [dir_info["artist"]]
                if not self.album_artist and dir_info.get("artist"):
                    self.album_artist = dir_info["artist"]
            if self.year is None and dir_info.get("year"):
                self.year = dir_info["year"]
            # 目录名里的格式、位深、采样率可补齐本地标签未声明的音质参数
            if dir_info.get("quality_text"):
                self.apply_audio_quality(dir_info["quality_text"])
        return self

    @property
    def season(self) -> None:
        """音乐没有季信息，兼容下载与事件链的通用访问。"""
        return None

    @property
    def episode(self) -> None:
        """音乐没有集信息，兼容下载与历史记录的通用访问。"""
        return None

    @property
    def apply_words(self) -> list[str]:
        """音乐当前不应用影视自定义识别词。"""
        return []

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化和传输的字典，字段集与 schemas.MusicMeta 对齐。"""
        return {
            "type": self.type.value,
            "org_string": self.org_string,
            "title": self.title,
            "artists": list(self.artists),
            "artist": self.artist,
            "album": self.album,
            "album_artist": self.album_artist,
            "year": self.year,
            "disc_number": self.disc_number,
            "track_number": self.track_number,
            "total_discs": self.total_discs,
            "total_tracks": self.total_tracks,
            "version": self.version,
            "audio_format": self.audio_format,
            "audio_lossless": self.audio_lossless,
            "audio_quality": self.audio_quality,
            "audio_quality_score": self.audio_quality_score,
            "audio_specs": self.audio_specs,
            "bit_depth": self.bit_depth,
            "sample_rate": self.sample_rate,
            "bitrate": self.bitrate,
            "duration": self.duration,
            "isrc": self.isrc,
            "media_source": self.media_source,
            "media_id": self.media_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MetaMusic":
        """从字典恢复音乐解析结果，兼容 artists/artist 两种键。"""
        raw_type = data.get("type")
        if raw_type not in (None, MediaType.MUSIC, MediaType.MUSIC.value, "music"):
            raise ValueError(f"不支持的音乐媒体类型：{raw_type}")
        return cls(
            org_string=data.get("org_string"),
            title=data.get("title"),
            artists=_string_list(data.get("artists") or data.get("artist")),
            album=data.get("album"),
            album_artist=data.get("album_artist"),
            year=_optional_int(data.get("year")),
            disc_number=_optional_int(data.get("disc_number")),
            track_number=_optional_int(data.get("track_number")),
            total_discs=_optional_int(data.get("total_discs")),
            total_tracks=_optional_int(data.get("total_tracks")),
            version=data.get("version"),
            audio_format=data.get("audio_format"),
            audio_lossless=data.get("audio_lossless"),
            bit_depth=_optional_int(data.get("bit_depth")),
            sample_rate=_optional_int(data.get("sample_rate")),
            bitrate=_optional_int(data.get("bitrate")),
            duration=_optional_int(data.get("duration")),
            isrc=data.get("isrc"),
            media_source=data.get("media_source"),
            media_id=data.get("media_id"),
        )
