"""媒体标题候选判断和搜索关键字解析规则。"""

import re
from typing import Optional, Tuple

import cn2an

from app.foundation.text import count_words
from app.schemas.types import MediaType


_MAX_TITLE_WORDS = 10
_MIN_TITLE_LENGTH = 2
_NON_TITLE_PATTERN = re.compile(r"^#|^请[问帮你]|[?？]$|^继续$")
_CHAT_INTENT_PATTERN = re.compile(r"帮我|请问|怎么|如何|为什么|可以|能否|推荐|介绍|谢谢|想看|找一下|搜一下")
_MEDIA_FEATURE_PATTERN = re.compile(
    r"第\s*[0-9一二三四五六七八九十百零]+\s*[季集]|S\d{1,2}(?:E\d{1,4})?|E\d{1,4}|(?:19|20)\d{2}",
    re.IGNORECASE,
)
_MEDIA_SEPARATOR_PATTERN = re.compile(r"[\s\-_.:：·'\"()\[\]【】]+")
_SENTENCE_PUNCTUATION_PATTERN = re.compile(r"[，。！？!?,；;]")
_TITLE_CHARACTER_PATTERN = re.compile(r"[\u4e00-\u9fffA-Za-z]")


def is_media_title_like(value: str) -> bool:
    """判断短文本是否具备影视标题特征而不是对话或链接。"""
    if not value:
        return False
    normalized = re.sub(r"\s+", " ", value).strip()
    if not normalized:
        return False
    if _NON_TITLE_PATTERN.search(normalized) or count_words(normalized) > _MAX_TITLE_WORDS:
        return False
    if "://" in normalized or normalized.startswith("magnet:?"):
        return False
    if _CHAT_INTENT_PATTERN.search(normalized):
        return False
    if _SENTENCE_PUNCTUATION_PATTERN.search(normalized):
        return False

    candidate = _MEDIA_FEATURE_PATTERN.sub("", normalized)
    candidate = _MEDIA_SEPARATOR_PATTERN.sub("", candidate)
    return (
        len(candidate) >= _MIN_TITLE_LENGTH
        and _TITLE_CHARACTER_PATTERN.search(candidate) is not None
    )


def parse_search_keyword(
    content: str,
) -> Tuple[Optional[MediaType], Optional[str], Optional[int], Optional[int], Optional[str], Optional[str]]:
    """从搜索文本中提取媒体类型、标题、季、集和年份。"""
    if not content:
        return None, None, None, None, None, None

    media_type = MediaType.TV if re.search(r"^(电视剧|动漫|\s+电视剧|\s+动漫)", content) else None
    content = re.sub(r"^(电影|电视剧|动漫|\s+电影|\s+电视剧|\s+动漫)", "", content).strip()

    season = None
    episode = None
    season_match = re.search(r"第\s*([0-9一二三四五六七八九十]+)\s*季", content, re.IGNORECASE)
    if season_match:
        media_type = MediaType.TV
        season = int(cn2an.cn2an(season_match.group(1), mode="smart"))

    episode_match = re.search(
        r"第\s*([0-9一二三四五六七八九十百零]+)\s*集",
        content,
        re.IGNORECASE,
    )
    if episode_match:
        media_type = MediaType.TV
        episode = int(cn2an.cn2an(episode_match.group(1), mode="smart"))
        if episode and not season:
            season = 1

    year_match = re.search(r"[\s(]+(\d{4})[\s)]*", content)
    year = year_match.group(1) if year_match else None
    keyword = re.sub(
        r"第\s*[0-9一二三四五六七八九十]+\s*季|"
        r"第\s*[0-9一二三四五六七八九十百零]+\s*集|"
        r"[\s(]+(\d{4})[\s)]*",
        "",
        content,
        flags=re.IGNORECASE,
    ).strip()
    keyword = re.sub(r"\s+", " ", keyword) if keyword else year
    return media_type, keyword, season, episode, year, content
