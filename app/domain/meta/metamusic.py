import logging
import re
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any, Callable, Optional

from app.domain.meta.metabase import MetaBase
from app.domain.meta.runtime import get_metainfo_accelerator
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType

_AUDIO_FORMAT_PATTERN = re.compile(
    r"(?<![A-Z])(?P<format>DSD(?:64|128|256|512)?|DSF|DFF|SACD|FLAC|ALAC|APE|WAV|WAVE|AIFF?|PCM|"
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
    "SACD": "DSD",
}
_LOSSLESS_AUDIO_FORMATS = frozenset({"DSD", "FLAC", "ALAC", "APE", "WAV", "AIFF", "PCM"})
_LOSSY_AUDIO_FORMATS = frozenset({"MP3", "AAC", "OGG", "OPUS", "WMA"})
logger = logging.getLogger(__name__)


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
    r"DSD(?:64|128|256|512)?|DSF|DFF|SACD|FLAC|ALAC|APE|CUE|WAV|WAVE|AIFF?|PCM|"
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
    rf"(?<![A-Za-z0-9])(?:{_MUSIC_FORMAT_TOKEN_ALT})(?![A-Za-z0-9])|"
    r"\b\d{1,3}\s*-?\s*bits?\b|\b\d{2,4}(?:(?:[.．]|\s)\d)?\s*k(?:hz|bps?)\b|"
    # Single / EP / Album 仅在独立尾段或括号标签中才是发行标记，
    # 不能在这里全局删除，否则 Best Album、Single Ladies 等自然标题会受损。
    r"(?<![A-Za-z0-9])(?:lossless|无损音质|无损|分[轨軌]|整[轨軌]|"
    r"原抓|自抓|自扫|自掃)(?![A-Za-z0-9])|"
    # 合集/精选只有作为独立标签时才删除，不能损伤「楠得精选」「音乐合集」等作品名。
    r"(?<![A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])"
    r"(?:合集|精选)(?![A-Za-z0-9\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af])",
    re.IGNORECASE,
)
# 演唱会/音乐视频种子的视频编码标记：分辨率、编码、容器与声道描述，
# 不是音乐文本信息，不剔除会污染曲名并阻断艺术家/曲名拆分
_MUSIC_VIDEO_TOKEN_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_MUSIC_VIDEO_TOKEN_ALT})(?![A-Za-z0-9])",
    re.IGNORECASE,
)
# 尾部规格段判定：整段仅由格式词、视频标记、位深采样与无损声明词组成，
# 曲名含任何自然语言文本时判定失败，保证「曲名 (注释) - WEB-DL」不被误剥；
# 发布组标签（HHWEB/FHDMv）不是固定词表，由 _strip_spec_segments 的短词规则另行放行
_MUSIC_SPEC_SEGMENT_RE = re.compile(
    rf"^(?:(?<![A-Za-z0-9])(?:{_MUSIC_FORMAT_TOKEN_ALT}|{_MUSIC_VIDEO_TOKEN_ALT}|single|ep|album)"
    r"(?![A-Za-z0-9])"
    r"|\d{1,3}\s*-?\s*bits?|\d{2,4}(?:(?:[.．]|\s)\d)?\s*k(?:hz|bps?)"
    r"|lossless|无损音质|无损|分[轨軌]|整[轨軌]|原抓|自抓|自扫|自掃|合集|精选"
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
# 规格词后的发布组尾巴：FLAC 2.0-ADE、FLAC 分轨-nbarock、FLAC 2.0-LIVE@ADE。
# 必须存在已知规格词才剥离，避免把普通的「标题-艺术家」误当发布组。
_MUSIC_RELEASE_GROUP_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?:{_MUSIC_FORMAT_TOKEN_ALT}|{_MUSIC_VIDEO_TOKEN_ALT})(?![A-Za-z0-9])"
    r"(?:\s*(?:[+/]|\d{1,3}(?:\.\d)?|bits?|k(?:hz|bps?)|lossless|无损|"
    r"分[轨軌]|整[轨軌]|原抓|自抓|自扫|自掃))*"
    r"\s*[-–—−－]+\s*[A-Za-z0-9][A-Za-z0-9@._-]{1,20}\s*$",
    re.IGNORECASE,
)
# 括号内的抓轨说明整体删除，避免逐词清理后留下「(+CUE原抓)」等残片。
_MUSIC_RIP_NOTE_RE = re.compile(
    rf"[\(（][^()（）]{{0,80}}(?:{_MUSIC_FORMAT_TOKEN_ALT})[^()（）]{{0,80}}"
    r"(?:原抓|自抓|自扫|自掃)[^()（）]{0,40}[\)）]",
    re.IGNORECASE,
)
_MUSIC_RIP_SIGNATURE_RE = re.compile(
    rf"(?:{_MUSIC_FORMAT_TOKEN_ALT})[^\r\n]{{0,30}}(?:原抓|自抓|自扫|自掃)",
    re.IGNORECASE,
)
# 中文抓轨方式常与格式词粘连（WAV分轨原抓），不能复用要求 ASCII 边界的
# 格式 token 规则；这些词本身语义明确，单独清理可保留格式词的严格边界。
_MUSIC_RIP_METHOD_RE = re.compile(r"(?:分轨|分軌|整轨|整軌|原抓|自抓|自扫|自掃)", re.IGNORECASE)
# 场景音频尾链：作品名 - 2006-FLAC分轨-OpenCD-发布者。
# 年份属于发行线索，其后的格式、抓轨方式和发布组都不进入音乐名称。
_MUSIC_AUDIO_RELEASE_TAIL_RE = re.compile(
    rf"(?<!\d)(?P<year>(?:19|20)\d{{2}})\s*[-–—−－]\s*"
    rf"(?:{_MUSIC_FORMAT_TOKEN_ALT})(?:\s*(?:分[轨軌]|整[轨軌]|原抓|自抓|自扫|自掃))*"
    r"(?:\s*[-–—−－]\s*[^\s\-–—−－]+){0,4}\s*$",
    re.IGNORECASE,
)
# 圆括号中的联合位深声明（24/48bit）及规格剔除后留下的空括号。
_MUSIC_PAREN_SPEC_RE = re.compile(
    r"[\(（]\s*\d{1,3}\s*/\s*\d{1,3}\s*-?\s*bits?\s*[\)）]",
    re.IGNORECASE,
)
_MUSIC_EMPTY_BRACKET_RE = re.compile(r"[\(（\[]\s*(?:[/+,\-]\s*)*[\)）\]]")
# 尾部花括号通常是唱片目录号或发布标记，仅在末尾剔除，保护正文中的花括号文本。
_MUSIC_TRAILING_CATALOG_RE = re.compile(r"\s*\{[A-Za-z0-9][^{}]{0,40}\}\s*$")
# 年份括号：(2000)（2000）【2000】形式的发行年份，作为候选消歧线索
_MUSIC_YEAR_RE = re.compile(r"[\(\[（【]((?:19|20)\d{2})[\)\]）】]")
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
_MUSIC_TRAILING_CJK_ALIAS_RE = re.compile(
    r"\s+[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]"
    r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af·・.'’\s]{0,60}$"
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
_MUSIC_RECORDING_VERSION_RE = re.compile(
    r"[\[(（【]([^\])）】]*(?:\blive\b|\bremix\b|\binstrumental\b|\bacoustic\b|"
    r"\bunplugged\b|\bdemo\b|\bkaraoke\b|现场|現場|混音|伴奏|不插电|不插電)[^\])）】]*)[\])）】]",
    re.IGNORECASE,
)
_MUSIC_VERSION_LABEL_RE = re.compile(r"^(?:录音版本|錄音版本|版本|version)\s*[:：]\s*(.+)$", re.IGNORECASE)
_MUSIC_SPACES_RE = re.compile(r"\s+")
_MUSIC_COMPACT_RE = re.compile(r"[\W_]+", re.UNICODE)
# 音乐视频/演唱会资源使用影视场景式命名，但标题语义仍属于音乐。
# 这里按 token 分类后再清理，避免用一个跨越整行的大正则吞掉年份后的演出名称。
_MUSIC_SCENE_RESOLUTION_RE = re.compile(
    r"^(?:(?:480|576|720|1080|2160)[pi]|[248]k)$",
    re.IGNORECASE,
)
_MUSIC_SCENE_SOURCE_RE = re.compile(
    r"^(?:uhd|blu[-.]?ray|bdrip|remux|web[-.]?dl|webrip|hdtv|uhdtv|"
    r"hd[-.]?dvd|dvd|dvdrip|2cd\+blu[-.]?ray)$",
    re.IGNORECASE,
)
_MUSIC_SCENE_VIDEO_RE = re.compile(
    r"^(?:x26[45](?:[._-]?(?:8|10|12)bits?)?|h[.]?26[45]|avc|hevc|"
    r"mpeg[-.]?2|vc[-.]?1|prores|av1)$",
    re.IGNORECASE,
)
_MUSIC_SCENE_EFFECT_RE = re.compile(
    r"^(?:sdr|hdr(?:10[+]?)?|hdrvivid|dovi|dv|dolbyvision|3d|repack|hlg|hq)$",
    re.IGNORECASE,
)
_MUSIC_SCENE_AUDIO_RE = re.compile(
    r"^(?:dts(?:-hd)?(?:ma|hra)?|truehd|atmos|ddp|dd[+]?|eac3|ac3|"
    r"lpcm|aac|flac|pcm|opus|vorbis)(?:[257][.]1|2[.]0)?$",
    re.IGNORECASE,
)
_MUSIC_SCENE_AUDIO_AUX_RE = re.compile(r"^(?:ma|hra)(?:[257][.]1|2[.]0)?$", re.IGNORECASE)
_MUSIC_SCENE_CHANNEL_RE = re.compile(r"^(?:1[.]0|2[.]0|[257][.]1)(?:ch(?:annels?)?)?$", re.IGNORECASE)
_MUSIC_SCENE_BIT_RE = re.compile(r"^(?:8|10|12|16|20|24|32)[-_.]?bits?$", re.IGNORECASE)
_MUSIC_SCENE_FPS_RE = re.compile(r"^[0-9]{2,3}fps$", re.IGNORECASE)
_MUSIC_SCENE_AUDIO_COUNT_RE = re.compile(r"^[0-9]{1,2}audios?$", re.IGNORECASE)
_MUSIC_SCENE_YEAR_TOKEN_RE = re.compile(r"^(?:19|20)[0-9]{2}$")
_MUSIC_SCENE_DATE_TOKEN_RE = re.compile(r"^(?P<year>[0-9]{2})(?:0[1-9]|1[0-2])(?:0[1-9]|[12][0-9]|3[01])$")
_MUSIC_SCENE_YEAR_RANGE_TOKEN_RE = re.compile(
    r"^(?P<begin>(?:19|20)[0-9]{2})[-–—~～](?P<end>(?:(?:19|20)[0-9]{2}|[0-9]{2}))$"
)
_MUSIC_SCENE_RELEASE_GROUP_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9@._-]{1,20}$")
_MUSIC_SCENE_PUNCTUATED_TECH_RE = re.compile(
    r"([,;])(?=(?:blu[-.]?ray|web[-.]?dl|hdtv|remux|avc|hevc|x26[45]|h[.]?26[45]))",
    re.IGNORECASE,
)
_MUSIC_SCENE_PLATFORM_TOKENS = frozenset({
    "AMZN", "BAHA", "CR", "FRIDAY", "HMAX", "IQ", "IT", "LINETV",
    "MYTVSUPER", "NF", "OTOTOY",
})
_MUSIC_SCENE_LOCALE_TOKENS = frozenset({"GERMAN", "ITA", "JPN"})
_MUSIC_LATIN_HYPHEN_NON_ARTIST_SUFFIXES = frozenset({"cd", "disc", "part", "type", "vol", "volume"})
# 日文资源标题大量使用全角字符（ＷＯＷＯＷ、５０ｔｈ、全角空格与括号），
# 归一为半角后才能与 MusicBrainz 条目及内置模式匹配
_FULLWIDTH_EXCLAMATION = 0xFF01
_FULLWIDTH_TILDE = 0xFF5E
_HALFWIDTH_OFFSET = 0xFEE0
_FULLWIDTH_MAP = {
    0x3000: " ",   # 全角空格
    0x3010: "[", 0x3011: "]",    # 【】
    # 日文引号「」/『』与《》承载作品名，不属于格式注释，保留原样交给命名模式。
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


@dataclass(frozen=True)
class MusicNameContext:
    """音乐命名公共清理后的解析上下文，供命名模式和解析器共享。"""

    raw: str
    normalized: str
    text: str
    artists: tuple[str, ...]
    year: Optional[int] = None
    comment: Optional[str] = None


@dataclass(frozen=True)
class _MusicSceneTokenResult:
    """音乐视频场景 token 清理结果，仅供 metamusic 内置模式使用。"""

    text: str
    year: Optional[int]
    categories: frozenset[str]


@dataclass(frozen=True)
class MusicNamePattern:
    """可动态注册的音乐命名模式。"""

    name: str
    matcher: Callable[[MusicNameContext], Optional[Any]]
    priority: int = 0


@dataclass(frozen=True)
class MusicNamePatternMatch:
    """第一层命名模式匹配结果。"""

    pattern_name: str
    payload: Any


@dataclass(frozen=True)
class MusicNameParseResult:
    """第二层解析器提取出的音乐命名字段。"""

    title: Optional[str]
    artists: Optional[list[str]] = None
    album: Optional[str] = None
    year: Optional[int] = None
    disc_number: Optional[int] = None


@dataclass(frozen=True)
class MusicNameParser:
    """可动态注册、按命名模式选择的音乐解析器。"""

    name: str
    patterns: tuple[str, ...]
    handler: Callable[
        [MusicNameContext, MusicNamePatternMatch], Optional[MusicNameParseResult]
    ]
    priority: int = 0


class MusicNameRegistry:
    """音乐命名模式与解析器的两层动态注册中心。"""

    _patterns: dict[str, MusicNamePattern] = {}
    _parsers: dict[str, MusicNameParser] = {}
    _default_patterns: dict[str, MusicNamePattern] = {}
    _default_parsers: dict[str, MusicNameParser] = {}
    _lock = RLock()

    @classmethod
    def register_pattern(cls, pattern: MusicNamePattern, replace: bool = False) -> None:
        """注册命名模式，同名模式仅在 ``replace=True`` 时替换。"""
        with cls._lock:
            if pattern.name in cls._patterns and not replace:
                raise ValueError(f"音乐命名模式已存在：{pattern.name}")
            cls._patterns[pattern.name] = pattern

    @classmethod
    def unregister_pattern(cls, name: str) -> bool:
        """按名称注销命名模式，返回是否实际移除。"""
        with cls._lock:
            return cls._patterns.pop(name, None) is not None

    @classmethod
    def register_parser(cls, parser: MusicNameParser, replace: bool = False) -> None:
        """注册解析器，同名解析器仅在 ``replace=True`` 时替换。"""
        with cls._lock:
            if parser.name in cls._parsers and not replace:
                raise ValueError(f"音乐命名解析器已存在：{parser.name}")
            cls._parsers[parser.name] = parser

    @classmethod
    def unregister_parser(cls, name: str) -> bool:
        """按名称注销解析器，返回是否实际移除。"""
        with cls._lock:
            return cls._parsers.pop(name, None) is not None

    @classmethod
    def get_patterns(cls) -> tuple[MusicNamePattern, ...]:
        """按优先级返回当前已注册的命名模式快照。"""
        with cls._lock:
            return tuple(sorted(cls._patterns.values(), key=lambda item: item.priority, reverse=True))

    @classmethod
    def get_parsers(cls) -> tuple[MusicNameParser, ...]:
        """按优先级返回当前已注册的解析器快照。"""
        with cls._lock:
            return tuple(sorted(cls._parsers.values(), key=lambda item: item.priority, reverse=True))

    @classmethod
    def match_pattern(cls, context: MusicNameContext) -> Optional[MusicNamePatternMatch]:
        """执行第一层命名模式匹配，返回首个命中的模式及载荷。"""
        for pattern in cls.get_patterns():
            payload = pattern.matcher(context)
            if payload is not None:
                return MusicNamePatternMatch(pattern_name=pattern.name, payload=payload)
        return None

    @classmethod
    def match_parser(cls, matched: MusicNamePatternMatch) -> Optional[MusicNameParser]:
        """执行第二层解析器匹配，选择支持该模式且优先级最高的解析器。"""
        return next(
            (
                parser
                for parser in cls.get_parsers()
                if matched.pattern_name in parser.patterns or "*" in parser.patterns
            ),
            None,
        )

    @classmethod
    def parse(cls, context: MusicNameContext) -> Optional[MusicNameParseResult]:
        """依次匹配命名模式和解析器，并返回结构化音乐字段。"""
        matched = cls.match_pattern(context)
        if not matched:
            return None
        parser = cls.match_parser(matched)
        if not parser:
            return None
        return parser.handler(context, matched)

    @classmethod
    def _capture_default_components(cls) -> None:
        """保存内置命名组件的对象快照，供 Rust 快路判断兼容性。"""
        with cls._lock:
            cls._default_patterns = dict(cls._patterns)
            cls._default_parsers = dict(cls._parsers)

    @classmethod
    def _uses_default_components(cls) -> bool:
        """判断当前注册表是否仍为未替换的内置命名组件。"""
        with cls._lock:
            if not cls._default_patterns or not cls._default_parsers:
                return False
            if (
                    cls._patterns.keys() != cls._default_patterns.keys()
                    or cls._parsers.keys() != cls._default_parsers.keys()
            ):
                return False
            return all(
                component is cls._default_patterns[name]
                for name, component in cls._patterns.items()
            ) and all(
                component is cls._default_parsers[name]
                for name, component in cls._parsers.items()
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
        media_source: Optional[MediaSource] = None,
        media_id: Optional[str] = None,
        parse_title: bool = False,
    ):
        """初始化音乐标题、标签、音频规格和统一媒体身份。"""
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
        self.media_source, self.media_id = resolve_media_identity(
            media_source=media_source,
            media_id=media_id,
        )
        if parse_title:
            # 种子/文件名字符串场景：解析艺术家、曲名、年份并补充音质参数
            self.apply_title(self.title or org_string or "")

    @classmethod
    def parse_query(cls, query: str) -> "MetaMusic":
        """把用户输入或资源标题解析为音乐元数据。"""
        return cls(org_string=query, title=query, parse_title=True)

    @classmethod
    def parse_resource(cls, title: str, subtitle: Optional[str] = None) -> "MetaMusic":
        """合并资源标题与副标题的独立证据，不使用搜索目标补写作品身份。

        标题解析仍复用 Python/Rust 公共入口；副标题只补缺失字段，保留标题中
        已有的署名。专辑、曲序、曲名的明确多段格式在资源层统一补充。
        """
        meta = cls.parse_query(title)
        if not meta.title:
            # 整个作品名位于中文展示括号内时，旧解析器可能把它误当发布标签删除。
            bracket = re.match(r"^\s*[【《「]([^】》」]+)[】》」]", title)
            if bracket:
                meta.apply_title(bracket.group(1))
                meta.apply_audio_quality(title)
        if meta.artists and meta.title:
            track = re.fullmatch(r"(.+?)\s+-\s+(\d{1,3})\s+-\s+(.+)", meta.title)
            if track:
                meta.album, number, meta.title = track.groups()
                meta.track_number = int(number)
        if subtitle:
            secondary = cls.parse_query(subtitle)
            artist = re.search(
                r"(?:^|[;；\n])\s*(?:艺术家|藝術家|藝人|歌手|演唱|专辑艺人|專輯藝人|artist|performer)"
                r"\s*[:：]\s*([^;；\n]+)", subtitle, re.I,
            )
            if not meta.artists:
                meta.artists = cls._split_artists(artist.group(1)) if artist else list(secondary.artists)
            album = re.search(
                r"(?:^|[;；\n])\s*(?:专辑(?:名|名称)?|專輯(?:名|名稱)?|album)\s*[:：]\s*([^;；\n]+)",
                subtitle, re.I,
            )
            if album and not meta.album:
                meta.album = album.group(1).strip()
            elif not meta.album and secondary.artists and secondary.title != meta.title:
                if {cls.compact_text(item) for item in meta.artists} & {cls.compact_text(item) for item in secondary.artists}:
                    meta.album = secondary.title
            if not meta.year:
                meta.year = secondary.year
            meta.apply_audio_quality(subtitle)
        if not meta.version:
            meta.version = cls._resource_version(title, subtitle)
        return meta

    @staticmethod
    def _resource_version(title: str, subtitle: Optional[str]) -> Optional[str]:
        """优先保留标题版本，副标题仅接受明确版本字段或独立版本标签，避免误读艺名。"""
        version = _MUSIC_RECORDING_VERSION_RE.search(title)
        if version:
            return version.group(1).strip()
        for field in re.split(r"[;；\n]", subtitle or ""):
            labelled = _MUSIC_VERSION_LABEL_RE.fullmatch(field.strip())
            if labelled:
                return labelled.group(1).strip().strip("[]()（）【】").strip() or None
            version = _MUSIC_RECORDING_VERSION_RE.fullmatch(field.strip())
            if version:
                return version.group(1).strip()
        return None

    @classmethod
    def from_music_info(cls, info: Any) -> "MetaMusic":
        """把标准音乐信息转换为下载、整理和站点搜索使用的元数据。"""
        return cls(
            title=info.title,
            artists=list(info.artists),
            album=info.album,
            album_artist=info.album_artist,
            year=info.year,
            disc_number=info.disc_number,
            track_number=info.track_number,
            total_discs=getattr(info, "total_discs", None),
            total_tracks=info.total_tracks,
            version=info.version,
            audio_format=info.audio_format,
            audio_lossless=info.audio_lossless,
            bit_depth=info.bit_depth,
            sample_rate=info.sample_rate,
            bitrate=info.bitrate,
            duration=info.duration,
            isrc=info.isrc,
            media_source=info.media_source,
            media_id=info.media_id,
        )

    @classmethod
    def from_album_context(
            cls,
            directory_name: str,
            tracks: list["MetaMusic"],
    ) -> "MetaMusic":
        """按目录名和多数音轨标签汇总专辑识别条件。"""
        directory = cls.parse_album_dir(directory_name)
        album_votes: dict[str, int] = {}
        artist_votes: dict[str, int] = {}
        for track in tracks:
            if track.album:
                album_votes[track.album] = album_votes.get(track.album, 0) + 1
            artist = track.album_artist or (track.artists[0] if track.artists else None)
            if artist:
                artist_votes[artist] = artist_votes.get(artist, 0) + 1
        majority_album = max(album_votes, key=album_votes.get) if album_votes else None
        majority_artist = max(artist_votes, key=artist_votes.get) if artist_votes else None
        threshold = max(2, len(tracks) // 2)
        album = majority_album if majority_album and album_votes[majority_album] >= threshold else None
        artist = majority_artist if majority_artist and artist_votes[majority_artist] >= threshold else None
        return cls(
            org_string=directory_name,
            title=album or directory.get("album") or directory_name,
            album=album or directory.get("album"),
            artists=[artist or directory.get("artist")]
            if artist or directory.get("artist") else [],
            album_artist=artist or directory.get("artist"),
            year=directory.get("year"),
        )

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

        公共层先完成字符归一、音质与干扰信息剔除；随后由注册中心依次匹配
        命名模式和对应解析器，最后统一回填结构化字段并提取曲序前缀。
        """
        raw = str(value or "")
        accelerator = get_metainfo_accelerator()
        if accelerator and MusicNameRegistry._uses_default_components():
            rust_result = accelerator.parse_metamusic(
                raw,
                artists=list(self.artists) or None,
                year=self.year,
            )
            if rust_result and self._apply_rust_title_result(rust_result):
                return
        self.apply_audio_quality(raw)
        context = self._prepare_name_context(
            raw=raw,
            artists=self.artists,
            year=self.year,
        )
        if not context.normalized:
            return
        parsed = MusicNameRegistry.parse(context)
        if not parsed:
            if not context.text:
                self.title = None
            if self.year is None:
                self.year = context.year
            return
        self._apply_name_result(context, parsed)
        self._apply_track_prefix()

    def _apply_rust_title_result(self, parsed: dict[str, Any]) -> bool:
        """回填 Rust 音乐解析结果，并保留调用方已有的高可信字段。"""
        if "title" not in parsed:
            return False
        parsed_meta = type(self).from_dict(parsed)
        self.title = parsed_meta.title
        for field_name in (
                "artists",
                "album",
                "year",
                "disc_number",
                "track_number",
                "audio_format",
                "audio_lossless",
                "bit_depth",
                "sample_rate",
                "bitrate",
        ):
            current_value = getattr(self, field_name, None)
            parsed_value = getattr(parsed_meta, field_name, None)
            if current_value in (None, "", []) and parsed_value not in (None, "", []):
                setattr(self, field_name, parsed_value)
        return True

    @classmethod
    def _prepare_name_context(
            cls,
            raw: str,
            artists: list[str],
            year: Optional[int],
    ) -> MusicNameContext:
        """统一归一命名文本并剔除音质、视频、日期等干扰信息。"""
        normalized = cls._normalize_text(raw)
        parsed_year = year
        years = _MUSIC_YEAR_RE.findall(normalized)
        if years and parsed_year is None:
            # 多个年份括号时末位通常才是资源的发行年份。
            parsed_year = int(years[-1])
        clean_source, release_year = cls._strip_audio_release_tail(normalized)
        if release_year and parsed_year is None:
            parsed_year = release_year
        cleaned = cls._strip_quality_tokens(cls._strip_spec_segments(clean_source))
        cleaned, range_year = cls._strip_date_prefix(cleaned)
        if range_year and parsed_year is None:
            parsed_year = range_year
        comment = None
        comment_match = _MUSIC_TITLE_COMMENT_RE.search(cleaned)
        if comment_match:
            # 含书名号的版本注释会干扰专辑模式，先移出并在解析后统一拼回。
            comment = comment_match.group("comment").strip()
            cleaned = cleaned[: comment_match.start()].strip()
        return MusicNameContext(
            raw=raw,
            normalized=normalized,
            text=cleaned,
            artists=tuple(artists),
            year=parsed_year,
            comment=comment,
        )

    def _apply_name_result(
            self,
            context: MusicNameContext,
            parsed: MusicNameParseResult,
    ) -> None:
        """把解析器结果回填到当前对象，并保留调用方已有的高可信字段。"""
        self.title = parsed.title
        if context.comment and self.title:
            self.title = f"{self.title} ({context.comment})"
        if parsed.artists is not None:
            self.artists = list(parsed.artists)
        if parsed.album is not None:
            self.album = parsed.album
        if self.year is None:
            self.year = parsed.year or context.year
        if self.disc_number is None:
            self.disc_number = parsed.disc_number

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

    @classmethod
    def _parse_title_year(cls, value: str) -> tuple[str, Optional[int]]:
        """返回剥离尾部独立年份后的曲名及发行年份线索。"""
        title, year = cls._pop_trailing_year(value)
        return title, year

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
    def _scene_token_category(cls, token: str, allow_release_suffix: bool = True) -> Optional[str]:
        """识别音乐视频场景 token 类别，不在这里决定是否删除。"""
        value = str(token or "").strip(" \t[](){};,\"")
        if not value:
            return None
        if _MUSIC_SCENE_YEAR_TOKEN_RE.fullmatch(value):
            return "year"
        if _MUSIC_SCENE_DATE_TOKEN_RE.fullmatch(value):
            return "date"
        if _MUSIC_SCENE_YEAR_RANGE_TOKEN_RE.fullmatch(value):
            return "year_range"
        patterns = (
            ("resolution", _MUSIC_SCENE_RESOLUTION_RE),
            ("source", _MUSIC_SCENE_SOURCE_RE),
            ("video", _MUSIC_SCENE_VIDEO_RE),
            ("effect", _MUSIC_SCENE_EFFECT_RE),
            ("audio", _MUSIC_SCENE_AUDIO_RE),
            ("audio_aux", _MUSIC_SCENE_AUDIO_AUX_RE),
            ("channel", _MUSIC_SCENE_CHANNEL_RE),
            ("bit", _MUSIC_SCENE_BIT_RE),
            ("fps", _MUSIC_SCENE_FPS_RE),
            ("audio_count", _MUSIC_SCENE_AUDIO_COUNT_RE),
        )
        for category, pattern in patterns:
            if pattern.fullmatch(value):
                return category
        upper_value = value.upper()
        if upper_value in _MUSIC_SCENE_PLATFORM_TOKENS or value == "iT":
            return "platform"
        if upper_value in _MUSIC_SCENE_LOCALE_TOKENS:
            return "locale"
        if allow_release_suffix:
            # 技术 token 与发布组常粘连为 H.265-CHORTLE、x264@JJL；
            # 完整 token 未命中时只剥离最后一段，再验证左侧确为技术字段。
            for separator_char in ("-", "@"):
                head, separator, tail = value.rpartition(separator_char)
                if not separator or not _MUSIC_SCENE_RELEASE_GROUP_RE.fullmatch(tail):
                    continue
                head_category = cls._scene_token_category(head, allow_release_suffix=False)
                if head_category in {
                    "resolution", "source", "video", "effect", "audio",
                    "audio_aux", "channel", "bit", "fps", "audio_count",
                }:
                    return head_category
        return None

    @classmethod
    def _parse_music_scene_tokens(cls, value: str) -> Optional[_MusicSceneTokenResult]:
        """按音乐语义清理影视场景 token，强特征不足时不接管标题。"""
        normalized = cls._normalize_text(value)
        # 分类标签有时紧贴规格（Type-A,D,Blu-ray），只在已知技术词前补空格。
        normalized = _MUSIC_SCENE_PUNCTUATED_TECH_RE.sub(r"\1 ", normalized)
        tokens = normalized.split()
        if not tokens:
            return None
        categories = [cls._scene_token_category(token) for token in tokens]

        # 场景点分归一可能把 H.265 拆成 H 265，组合识别后同时标记两个 token。
        for index in range(len(tokens) - 1):
            if tokens[index].upper() == "H" and tokens[index + 1] in {"264", "265"}:
                categories[index] = categories[index + 1] = "video"

        # MA/HRA 与声道数字本身可能是作品名称，只在紧邻音频编码时作为规格清理。
        for index, category in enumerate(categories):
            if category != "audio_aux":
                continue
            neighbors = categories[max(0, index - 1): index] + categories[index + 1: index + 2]
            categories[index] = "audio" if "audio" in neighbors else None
        for index, category in enumerate(categories):
            if category != "channel":
                continue
            nearby = categories[max(0, index - 2): index] + categories[index + 1: index + 3]
            if "audio" not in nearby:
                categories[index] = None

        category_set = {category for category in categories if category}
        primary_count = len(category_set.intersection({"resolution", "source", "video"}))
        strong_signature = primary_count >= 2 or (
            "audio" in category_set
            and bool(category_set.intersection({"resolution", "source"}))
        )
        if not strong_signature:
            return None

        parsed_year = None
        kept_tokens: list[str] = []
        standalone_year_count = categories.count("year")
        removable = {
            "resolution", "source", "video", "effect", "audio", "channel",
            "bit", "fps", "audio_count", "platform", "locale",
        }
        for token, category in zip(tokens, categories):
            if category == "year":
                # 连续双年份常是演出名称的一部分（Live At Montreux 1999 2022），
                # 只有唯一的独立年份才作为发行线索提取。
                if standalone_year_count > 1:
                    kept_tokens.append(token)
                    continue
                parsed_year = int(token.strip("[](){};,\""))
                continue
            if category == "date":
                date_match = _MUSIC_SCENE_DATE_TOKEN_RE.fullmatch(token.strip("[](){};,\""))
                if date_match:
                    short_year = int(date_match.group("year"))
                    parsed_year = 2000 + short_year if short_year < 70 else 1900 + short_year
                continue
            if category == "year_range":
                match = _MUSIC_SCENE_YEAR_RANGE_TOKEN_RE.fullmatch(
                    token.strip("[](){};,\"")
                )
                if match:
                    end_year = match.group("end")
                    parsed_year = int(
                        end_year if len(end_year) == 4 else f"{match.group('begin')[:2]}{end_year}"
                    )
                continue
            if category in removable:
                continue
            kept_tokens.append(token)

        cleaned = cls._clean_tail(" ".join(kept_tokens)).rstrip(" ,;")
        cleaned = re.sub(r"\s+([,;:!?])", r"\1", cleaned)
        cleaned = cls._normalize_text(cleaned)
        if not cleaned:
            return None
        return _MusicSceneTokenResult(
            text=cleaned,
            year=parsed_year,
            categories=frozenset(category_set),
        )

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

    @staticmethod
    def _strip_audio_release_tail(value: str) -> tuple[str, Optional[int]]:
        """剥离年份开头的音频格式发布尾链，并返回发行年份。"""
        text = str(value or "").strip()
        match = _MUSIC_AUDIO_RELEASE_TAIL_RE.search(text)
        if not match:
            return text, None
        return text[:match.start()].rstrip(" \t-–—−－"), int(match.group("year"))

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
        raw = _MUSIC_RIP_NOTE_RE.sub(" ", str(value or ""))
        raw = _MUSIC_PAREN_SPEC_RE.sub(" ", raw)
        raw = _MUSIC_RELEASE_GROUP_RE.sub(" ", raw)
        text = _MUSIC_QUALITY_TOKEN_RE.sub(" ", raw)
        text = _MUSIC_RIP_METHOD_RE.sub(" ", text)
        text = _MUSIC_VIDEO_TOKEN_RE.sub(" ", text)
        text = _MUSIC_TRAILING_CATALOG_RE.sub(" ", text)
        text = _MUSIC_EMPTY_BRACKET_RE.sub(" ", text)
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
        artist_token = suffix.split(" ", 1)[0].strip(" ,，、;；")
        if (
            head
            and artist_token
            and cls._contains_cjk(head)
            and cls._contains_cjk(artist_token)
        ):
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
            if not cls._contains_cjk(artist_text):
                return None, text
            # 艺术家段常见「xx作品全集」合集修饰，剥离后才能与条目署名比对
            artist_text = artist_text.strip(" ,，、;；")
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
        head_suffix_raw = head.rsplit(" ", 1)[-1]
        head_suffix = head_suffix_raw.casefold()
        tail_prefix = tail.split(" ", 1)[0]
        all_caps_compound = (
            1 < len(head_suffix_raw) <= 5
            and 1 < len(tail_prefix) <= 5
            and head_suffix_raw.isalpha()
            and tail_prefix.isalpha()
            and head_suffix_raw.isupper()
            and tail_prefix.isupper()
        )
        if (
            head
            and tail
            and " " in head
            and " " in tail
            and head_suffix not in _MUSIC_LATIN_HYPHEN_NON_ARTIST_SUFFIXES
            and not all_caps_compound
        ):
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

        文件名先走与种子标题相同的动态模式注册中心，再按字段补充音频标签的空缺；
        目录线索优先级最低。标题等于文件主干名时视为读取标签后的文件名兜底，
        允许用完整模式的清理结果替换，真实标签中的非空字段始终保留。
        """
        file_path = Path(path)
        stem = file_path.stem
        title_from_name = not self.title or self.title == stem

        # 曲序/碟号前缀是文件路径的强结构，先于通用艺术家-标题模式剥离，
        # 避免「01 - One More Time」把 01 误判为艺术家。
        track_number, disc_number, filename_title = self.split_track_prefix(stem)
        # 文件名解析使用独立对象，防止 apply_title 覆盖真实音频标签；这里只合并空字段。
        filename_meta = MetaMusic(
            org_string=file_path.name,
            title=filename_title or stem,
            audio_format=file_path.suffix.lstrip(".").upper() or None,
            parse_title=True,
        )
        if filename_meta.track_number is None:
            filename_meta.track_number = track_number
        if filename_meta.disc_number is None:
            filename_meta.disc_number = disc_number
        if title_from_name and filename_meta.title:
            self.title = filename_meta.title
        for field_name in (
            "artists",
            "album",
            "year",
            "disc_number",
            "track_number",
            "total_discs",
            "total_tracks",
            "version",
            "isrc",
            "audio_format",
            "audio_lossless",
            "bit_depth",
            "sample_rate",
            "bitrate",
        ):
            current_value = getattr(self, field_name, None)
            parsed_value = getattr(filename_meta, field_name, None)
            if current_value in (None, "", []) and parsed_value not in (None, "", []):
                setattr(self, field_name, parsed_value)

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
        """返回公共元数据工厂实际应用的识别词，兼容缺少该字段的旧缓存。"""
        return getattr(self, "_apply_words", [])

    @apply_words.setter
    def apply_words(self, value: Optional[list[str]]) -> None:
        """独立保存识别词记录，避免调用方或其它音乐元数据共用可变列表。"""
        self._apply_words = list(value or [])

    def to_dict(self) -> dict[str, Any]:
        """转换为可持久化和传输的字典，字段集与 schemas.MusicMeta 对齐。"""
        return {
            "type": self.type.value,
            "org_string": self.org_string,
            "apply_words": list(self.apply_words),
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
        meta = cls(
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
        meta.apply_words = data.get("apply_words")
        return meta


def _build_name_result(
        context: MusicNameContext,
        value: str,
        artists: Optional[list[str]] = None,
        album: Optional[str] = None,
        year: Optional[int] = None,
        disc_number: Optional[int] = None,
) -> MusicNameParseResult:
    """统一剥离曲名尾部年份并构造解析结果。"""
    title, title_year = MetaMusic._parse_title_year(value)
    return MusicNameParseResult(
        title=title,
        artists=artists,
        album=album,
        year=context.year or year or title_year,
        disc_number=disc_number,
    )


def _match_dangling_artist(context: MusicNameContext) -> Optional[Any]:
    """匹配规格剥离后仅剩艺术家和悬空分隔符的命名。"""
    if context.artists or not context.text:
        return None
    return re.fullmatch(r"(?P<artist>.+?)\s+[\-–—−－]+", context.text)


def _parse_dangling_artist(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析仅包含艺术家的悬空分隔符命名。"""
    return MusicNameParseResult(
        title=None,
        artists=MetaMusic._split_artists(matched.payload.group("artist")),
        year=context.year,
    )


def _match_music_video_scene(context: MusicNameContext) -> Optional[Any]:
    """匹配具有强影视规格组合的音乐视频或演唱会场景命名。"""
    return MetaMusic._parse_music_scene_tokens(context.normalized)


def _parse_music_video_scene(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """清理场景 token 后重新进入音乐模式，避免采用影视媒体类型和标题截断规则。"""
    scene: _MusicSceneTokenResult = matched.payload
    scene_context = MusicNameContext(
        raw=scene.text,
        normalized=scene.text,
        text=scene.text,
        artists=context.artists,
        year=context.year or scene.year,
    )
    parsed = MusicNameRegistry.parse(scene_context)
    if parsed:
        return parsed
    return MusicNameParseResult(
        title=scene.text,
        year=scene_context.year,
    )


def _match_album_marker(context: MusicNameContext) -> Optional[Any]:
    """匹配 CJK 书名号专辑命名。"""
    if context.artists:
        return None
    return _MUSIC_ALBUM_MARKER_RE.match(context.text)


def _parse_album_marker(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析 CJK 书名号命名中的艺术家、专辑、碟号和标题。"""
    marker = matched.payload
    song_hint: Optional[str] = None
    bilingual_prefix = False
    artist_prefix = marker.group("artist")
    artists, head_title = MetaMusic._split_cjk_hyphen(artist_prefix)
    if artists:
        song_hint = MetaMusic._clean_tail(head_title)
    else:
        # 中英双语原声常写成「English Artist - English Album 中文艺人 - 《中文片名》」。
        # 优先保留首个标准 artist-title 结构，并只在英文标题后确有 CJK 别名时剥离别名。
        standard_prefix = _MUSIC_ARTIST_TITLE_RE.match(artist_prefix)
        if standard_prefix:
            candidate_artist = standard_prefix.group("artist")
            candidate_title = MetaMusic._clean_tail(standard_prefix.group("title"))
            alias_match = _MUSIC_TRAILING_CJK_ALIAS_RE.search(candidate_title)
            if (
                alias_match
                and not MetaMusic._contains_cjk(candidate_artist)
                and re.search(r"[A-Za-z]", candidate_title[:alias_match.start()])
            ):
                artists = MetaMusic._split_artists(candidate_artist)
                song_hint = candidate_title[:alias_match.start()].strip()
                bilingual_prefix = True
            else:
                artists = MetaMusic._split_artists(artist_prefix)
        else:
            artists = MetaMusic._split_artists(artist_prefix)
    album = MetaMusic._normalize_text(marker.group("album"))
    disc_number = None
    disc_match = _MUSIC_ALBUM_DISC_RE.search(album)
    if disc_match:
        disc_number = int(disc_match.group(1))
        album = album[:disc_match.start()].strip()
    rest = marker.group("rest").strip(" \t-–—−－_《》.")
    rest, rest_year = MetaMusic._parse_title_year(rest)
    parsed_year = context.year or rest_year
    if bilingual_prefix:
        # 书名号后的「电影原声带」是中文发行类型说明，不应覆盖英文专辑标题。
        rest = ""
    if rest and re.fullmatch(r"(?:19|20)\d{2}", rest):
        parsed_year = parsed_year or int(rest)
        rest = ""
    rest_disc = (
        re.fullmatch(r"(?:cd|disc|disk)\s*(\d{1,2})", rest, re.IGNORECASE)
        if rest
        else None
    )
    if rest_disc:
        disc_number = disc_number or int(rest_disc.group(1))
        rest = ""
    return _build_name_result(
        context=context,
        value=rest or song_hint or album,
        artists=artists,
        album=album,
        year=parsed_year,
        disc_number=disc_number,
    )


def _match_artist_title(context: MusicNameContext) -> Optional[Any]:
    """匹配带空格分隔符的标准艺术家和标题命名。"""
    if context.artists:
        return None
    return _MUSIC_ARTIST_TITLE_RE.match(context.text)


def _parse_artist_title(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析标准艺术家和标题命名。"""
    artists = MetaMusic._split_artists(matched.payload.group("artist"))
    title = MetaMusic._strip_artist_suffix(
        MetaMusic._clean_tail(matched.payload.group("title")),
        artists,
    )
    return _build_name_result(context=context, value=title, artists=artists)


def _match_alias_prefix(context: MusicNameContext) -> Optional[Any]:
    """匹配 VA 等合辑别名的无空格前缀命名。"""
    if context.artists:
        return None
    return _MUSIC_ALIAS_PREFIX_RE.match(context.text)


def _parse_alias_prefix(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析 VA 等合辑别名前缀并归一艺术家名称。"""
    alias = matched.payload.group("alias").casefold()
    artists = [_MUSIC_ARTIST_ALIASES.get(alias, "Various Artists")]
    return _build_name_result(
        context=context,
        value=MetaMusic._clean_tail(matched.payload.group("title")),
        artists=artists,
    )


def _match_cjk_artist_title_rip(context: MusicNameContext) -> Optional[Any]:
    """匹配带抓轨或 SACD 尾标的 CJK「艺术家-标题」命名，避免反拆。"""
    release_signature = _MUSIC_RIP_SIGNATURE_RE.search(context.normalized) or re.search(
        r"SACD\s*$", context.normalized, re.IGNORECASE
    )
    if context.artists or not release_signature:
        return None
    artist, separator, title = context.text.partition("-")
    artist = artist.strip(" \t-–—−－")
    title = title.strip(" \t-–—−－")
    if (
        separator
        and artist
        and title
        and MetaMusic._contains_cjk(artist)
        and MetaMusic._contains_cjk(title)
    ):
        return artist, title
    return None


def _parse_cjk_artist_title_rip(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析带抓轨或 SACD 尾标的 CJK 艺术家和标题。"""
    artist, title = matched.payload
    return _build_name_result(
        context=context,
        value=MetaMusic._clean_tail(title),
        artists=MetaMusic._split_artists(artist),
    )


def _match_cjk_hyphen(context: MusicNameContext) -> Optional[Any]:
    """匹配 CJK 无空格连字符命名。"""
    artists, title = MetaMusic._split_cjk_hyphen(context.text)
    return (artists, title) if artists else None


def _parse_cjk_hyphen(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析 CJK 无空格连字符命名。"""
    artists, title = matched.payload
    return _build_name_result(
        context=context,
        value=MetaMusic._clean_tail(title),
        artists=artists,
    )


def _match_latin_hyphen(context: MusicNameContext) -> Optional[Any]:
    """匹配拉丁多词艺术家和专辑的无空格连字符命名。"""
    artists, title = MetaMusic._split_latin_hyphen(context.text)
    return (artists, title) if artists else None


def _parse_latin_hyphen(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析拉丁无空格连字符命名。"""
    artists, title = matched.payload
    return _build_name_result(
        context=context,
        value=MetaMusic._clean_tail(title),
        artists=artists,
    )


def _match_year_sandwich(context: MusicNameContext) -> Optional[Any]:
    """匹配艺术家、年份、标题三段式命名。"""
    artists, title, year = MetaMusic._split_year_sandwich(context.text)
    return (artists, title, year) if artists else None


def _parse_year_sandwich(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析艺术家、年份、标题三段式命名。"""
    artists, title, year = matched.payload
    return _build_name_result(
        context=context,
        value=MetaMusic._clean_tail(title),
        artists=artists,
        year=year,
    )


def _match_fallback(context: MusicNameContext) -> Optional[Any]:
    """匹配未命中结构化模式的非空音乐标题。"""
    return context.text or None


def _parse_fallback(
        context: MusicNameContext,
        matched: MusicNamePatternMatch,
) -> MusicNameParseResult:
    """解析无结构标题并保留调用方已有艺术家字段。"""
    title = MetaMusic._strip_cjk_artist_suffix(MetaMusic._clean_tail(matched.payload))
    return _build_name_result(context=context, value=title)


def _register_default_name_components() -> None:
    """注册内置命名模式及其解析器，扩展可用更高优先级覆盖选择。"""
    patterns = (
        MusicNamePattern("dangling_artist", _match_dangling_artist, 800),
        MusicNamePattern("music_video_scene", _match_music_video_scene, 750),
        MusicNamePattern("album_marker", _match_album_marker, 700),
        MusicNamePattern("artist_title", _match_artist_title, 600),
        MusicNamePattern("alias_prefix", _match_alias_prefix, 500),
        MusicNamePattern("cjk_artist_title_rip", _match_cjk_artist_title_rip, 450),
        MusicNamePattern("cjk_hyphen", _match_cjk_hyphen, 400),
        MusicNamePattern("latin_hyphen", _match_latin_hyphen, 300),
        MusicNamePattern("year_sandwich", _match_year_sandwich, 200),
        MusicNamePattern("fallback", _match_fallback, -100),
    )
    parsers = (
        MusicNameParser("dangling_artist", ("dangling_artist",), _parse_dangling_artist),
        MusicNameParser("music_video_scene", ("music_video_scene",), _parse_music_video_scene),
        MusicNameParser("album_marker", ("album_marker",), _parse_album_marker),
        MusicNameParser("artist_title", ("artist_title",), _parse_artist_title),
        MusicNameParser("alias_prefix", ("alias_prefix",), _parse_alias_prefix),
        MusicNameParser(
            "cjk_artist_title_rip",
            ("cjk_artist_title_rip",),
            _parse_cjk_artist_title_rip,
        ),
        MusicNameParser("cjk_hyphen", ("cjk_hyphen",), _parse_cjk_hyphen),
        MusicNameParser("latin_hyphen", ("latin_hyphen",), _parse_latin_hyphen),
        MusicNameParser("year_sandwich", ("year_sandwich",), _parse_year_sandwich),
        MusicNameParser("fallback", ("fallback",), _parse_fallback),
    )
    for pattern in patterns:
        MusicNameRegistry.register_pattern(pattern)
    for parser in parsers:
        MusicNameRegistry.register_parser(parser)
    MusicNameRegistry._capture_default_components()


_register_default_name_components()
