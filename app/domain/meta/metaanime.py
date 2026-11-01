"""
解析动漫标题中的名称、季集、字幕组和资源规格。
"""

import logging
import re
import traceback
from typing import Any, Optional

import anitopy

from app.domain import title as title_rules
from app.domain.meta.customization import CustomizationMatcher
from app.domain.meta.metabase import MetaBase
from app.domain.meta.releasegroup import ReleaseGroupsMatcher
from app.foundation import text as text_tools
from app.foundation.text import convert as zhconv_convert
from app.schemas.types import MediaType

logger = logging.getLogger(__name__)


BRACKET_TITLE_RE = re.compile(r'\[(.+?)]')
RESOURCE_PIX_X_RE = re.compile(r'x', re.IGNORECASE)
RESOURCE_PIX_SPLIT_RE = re.compile(r'[Xx]')
ANIME_MARK_RE = re.compile(r"新番|月?番|[日美国][漫剧]")
ANIME_PREFIX_RE = re.compile(r".*番.|.*[日美国][漫剧].")
CATEGORY_TAG_RE = re.compile(
    r"[动漫画纪录片电影视连续剧集日美韩中港台海外亚洲华语大陆综艺原盘高清]{2,}|TV|Animation|Movie|Documentar|Anime",
    re.IGNORECASE,
)
LEADING_BRACKET_BLOCK_RE = re.compile(r"^[^]]*]")
FILE_SIZE_RE = re.compile(r'[0-9.]+\s*[MGT]i?B(?![A-Z]+)', re.IGNORECASE)
TV_EPISODE_BRACKET_RE = re.compile(r"\[TV\s+(\d{1,4})", re.IGNORECASE)
FOUR_K_BRACKET_RE = re.compile(r'\[4k]', re.IGNORECASE)
NUMERIC_BRACKET_RE = re.compile(r"\[\d+", re.IGNORECASE)
MIXED_CHINESE_TOKEN_RE = re.compile(r'[\d|#:：\-()（）\u4e00-\u9fff]')
# 分类词仅在占据整个标签时排除，片名内部的 Movie、TV 和动画仍属于名称。
ANIME_CATEGORY_LABEL_RE = re.compile(
    r"^[★☆\s]*(?:(?:\d{2,4}年)?(?:\d{1,2}月|月)?新?番|(?:\d{2,4}年)?(?:日剧|美剧)|"
    r"动漫|动画(?:片)?|Animations?(?:\(动画片\))?|Anime|Movie|"
    r"TV(?:\s+(?:Series|\d{1,4}(?:-\d{1,4})?(?:\s+Fin)?))?)[★☆\s]*$",
    re.IGNORECASE,
)
SUBTITLE_LABEL_RE = re.compile(
    r"字幕|双语|雙語|简体|繁体|简繁|繁日|中日|国语|国配|招募|片源|翻译|翻譯|校对|校對|内封|外挂|中字"
)
LATIN_TITLE_RE = re.compile(r"[A-Za-z]")
# 多词英文片名可能被 anitopy 误归为发布组，只在预处理后的标题位置允许恢复。
RELEASE_GROUP_TITLE_RE = re.compile(r"[A-Za-z0-9]+(?:\s+[A-Za-z0-9]+)+")


class MetaAnime(MetaBase):
    """
    识别动漫。
    """
    _anime_no_words = ['CHS&CHT', 'MP4', 'GB MP4', 'WEB-DL']
    _name_nostring_re = r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}|\s+GB"
    _fps_re = r"(\d{2,3})(?=FPS)"
    _name_nostring_pattern = re.compile(_name_nostring_re, re.IGNORECASE)
    _fps_pattern = re.compile(r"(%s)" % _fps_re, re.IGNORECASE)

    @staticmethod
    def _parse_season_number(value: object) -> Optional[int]:
        """
        解析第三方动漫季号，仅接受整数或纯数字字符串并保留数值 0。

        :param value: anitopy 返回的季号字段
        :return: 可解析的季号；字段缺失或格式不符时返回 None
        """
        if value is None:
            return None
        text = str(value).strip()
        return int(text) if text.isdigit() else None

    def __init__(self, title: str, subtitle: Optional[str] = None, isfile: bool = False):
        """
        解析动漫标题并补充季集、字幕组和媒体规格。
        """
        super().__init__(title, subtitle or "", isfile)
        if not title:
            return
        try:
            original_title = title
            # 字幕组信息会被预处理掉
            anitopy_info_origin = anitopy.parse(title)
            title = self.__prepare_title(title)
            anitopy_info = anitopy.parse(title)
            if anitopy_info:
                name = self._resolve_anime_name(title, original_title, anitopy_info, anitopy_info_origin)
                if name:
                    _split_flag = True
                    if name.find("/") != -1:
                        names = name.split("/")
                        if text_tools.contains_chinese(names[0]):
                            self.cn_name = names[0]
                            if len(names) > 1:
                                self.en_name = names[1]
                            _split_flag = False
                        elif text_tools.contains_chinese(names[-1]):
                            self.cn_name = names[-1]
                            if len(names) > 1:
                                self.en_name = names[0]
                            _split_flag = False
                        else:
                            name = names[-1]
                    if _split_flag:
                        lastword_type = ""
                        for word in name.split():
                            if not word:
                                continue
                            if word.endswith(']'):
                                word = word[:-1]
                            if word.isdigit():
                                if lastword_type == "cn":
                                    self.cn_name = "%s %s" % (self.cn_name or "", word)
                                elif lastword_type == "en":
                                    self.en_name = "%s %s" % (self.en_name or "", word)
                            elif text_tools.contains_chinese(word):
                                self.cn_name = "%s %s" % (self.cn_name or "", word)
                                lastword_type = "cn"
                            else:
                                self.en_name = "%s %s" % (self.en_name or "", word)
                                lastword_type = "en"
                if self.cn_name:
                    _, self.cn_name, _, _, _, _ = title_rules.parse_search_keyword(self.cn_name)
                    if self.cn_name:
                        self.cn_name = self._name_nostring_pattern.sub('', self.cn_name).strip()
                if self.en_name:
                    self.en_name = self._name_nostring_pattern.sub('', self.en_name).strip().title()
                    self._name = text_tools.title_case(self.en_name)
                year = anitopy_info.get("anime_year")
                if str(year).isdigit():
                    self.year = str(year)
                anime_season = anitopy_info.get("anime_season")
                if isinstance(anime_season, list):
                    seasons = [
                        season for item in anime_season
                        if (season := self._parse_season_number(item)) is not None
                    ]
                    begin_season = seasons[0] if seasons else None
                    end_season = seasons[-1] if len(seasons) > 1 else None
                else:
                    begin_season = self._parse_season_number(anime_season)
                    end_season = None
                if begin_season is not None:
                    self.begin_season = begin_season
                    if end_season is not None and end_season != self.begin_season:
                        self.end_season = end_season
                        self.total_season = (self.end_season - self.begin_season) + 1
                    else:
                        self.total_season = 1
                    self.type = MediaType.TV
                episode_number = anitopy_info.get("episode_number")
                if isinstance(episode_number, list):
                    if len(episode_number) == 1:
                        begin_episode = episode_number[0]
                        end_episode = None
                    else:
                        begin_episode = episode_number[0]
                        end_episode = episode_number[-1]
                elif episode_number:
                    begin_episode = episode_number
                    end_episode = None
                else:
                    begin_episode = None
                    end_episode = None
                if begin_episode:
                    try:
                        self.begin_episode = int(begin_episode)
                        if end_episode and int(end_episode) != self.begin_episode:
                            self.end_episode = int(end_episode)
                            self.total_episode = (self.end_episode - self.begin_episode) + 1
                        else:
                            self.total_episode = 1
                    except Exception as err:
                        logger.debug(f"解析集数失败：{str(err)} - {traceback.format_exc()}")
                        self.begin_episode = None
                        self.end_episode = None
                    self.type = MediaType.TV
                if not self.type:
                    anime_type = anitopy_info.get('anime_type')
                    if isinstance(anime_type, list):
                        anime_type = anime_type[0]
                    if anime_type and anime_type.upper() == "TV":
                        self.type = MediaType.TV
                    else:
                        self.type = MediaType.MOVIE
                self.resource_pix = anitopy_info.get("video_resolution")
                if isinstance(self.resource_pix, list):
                    self.resource_pix = self.resource_pix[0]
                if self.resource_pix:
                    if RESOURCE_PIX_X_RE.search(self.resource_pix):
                        self.resource_pix = RESOURCE_PIX_SPLIT_RE.split(self.resource_pix)[-1] + "p"
                    else:
                        self.resource_pix = self.resource_pix.lower()
                    if str(self.resource_pix).isdigit():
                        self.resource_pix = str(self.resource_pix) + "p"
                self.resource_team = \
                    ReleaseGroupsMatcher().match(title=original_title) or \
                    anitopy_info_origin.get("release_group") or None
                self.customization = CustomizationMatcher().match(title=original_title) or None
                self.video_encode = anitopy_info.get("video_term")
                if isinstance(self.video_encode, list):
                    self.video_encode = self.video_encode[0]
                self.video_bit = self.extract_video_bit(original_title) or self.extract_video_bit(self.video_encode)
                self.audio_encode = anitopy_info.get("audio_term")
                if isinstance(self.audio_encode, list):
                    self.audio_encode = self.audio_encode[0]
                self.__init_anime_fps(anitopy_info, original_title)
                # 解析副标题，只要季和集
                self.init_subtitle(self.org_string or "")
                if not self._subtitle_flag and self.subtitle:
                    self.init_subtitle(self.subtitle)
            if not self.type:
                self.type = MediaType.TV
        except Exception as e:
            logger.error(f"解析动漫信息失败：{str(e)} - {traceback.format_exc()}")

    @classmethod
    def _invalid_anime_name(cls, name: Optional[str], release_groups: tuple[str, ...]) -> bool:
        """
        排除字幕、完整分类标签和已识别的发布组，保留片名内部的分类词。

        :param name: 第三方解析器或方括号提供的候选标题
        :param release_groups: 当前发布名中已识别的制作组名称
        :return: 候选项是否不能作为真实片名
        """
        return (
            not name
            or name in cls._anime_no_words
            or (len(name) < 5 and not text_tools.contains_chinese(name))
            or bool(SUBTITLE_LABEL_RE.search(name))
            or bool(ANIME_CATEGORY_LABEL_RE.fullmatch(name))
            or name.casefold() in release_groups
        )

    @classmethod
    def _resolve_anime_name(
        cls, prepared: str, original: str, parsed: dict[str, Any], parsed_origin: dict[str, Any],
    ) -> Optional[str]:
        """
        从原始解析和方括号恢复真实标题，只替换名称而不改写资源规格与季集字段。

        :param prepared: 预处理后交给 anitopy 的发布名
        :param original: 保留双语片名和发布组的原始发布名
        :param parsed: 预处理后的第三方解析结果
        :param parsed_origin: 原始发布名的第三方解析结果
        :return: 有效标题；只有分类或规格标签时返回 None
        """
        matched_group = ReleaseGroupsMatcher().match(title=original)
        release_groups = tuple(
            group.casefold() for group in (matched_group, parsed_origin.get("release_group")) if group
        )
        origin_name = parsed_origin.get("anime_title")
        for name in (parsed.get("anime_title"), cls._recovered_anime_name(origin_name)):
            if not cls._invalid_anime_name(name, release_groups):
                return name
        retry_name: Optional[str] = anitopy.parse("[ANIME]" + prepared).get("anime_title")
        if not cls._invalid_anime_name(retry_name, release_groups):
            return retry_name
        # 只允许预处理后第一个方括号中的多词英文名突破第三方发布组误判。
        first_bracket = BRACKET_TITLE_RE.search(prepared)
        configured_groups = (matched_group.casefold(),) if matched_group else ()
        if first_bracket:
            name = first_bracket.group(1).strip()
            if RELEASE_GROUP_TITLE_RE.fullmatch(name) and not cls._invalid_anime_name(name, configured_groups):
                return name
        return cls._bracket_anime_name(original, release_groups)

    @staticmethod
    def _recovered_anime_name(name: Optional[str]) -> Optional[str]:
        """
        双语方括号名称恢复时优先保留英文片名，与既有预处理的结果一致。

        :param name: 原始解析器或方括号给出的候选名称
        :return: 可用英文名称，或不含英文时的右侧名称；空值保持不变
        """
        if not name or "/" not in name:
            return name
        parts = [part.strip() for part in name.split("/")]
        return next((part for part in parts if LATIN_TITLE_RE.search(part)), parts[-1])

    @classmethod
    def _bracket_anime_name(cls, original: str, release_groups: tuple[str, ...]) -> Optional[str]:
        """
        第三方解析失败时，从原始方括号中跳过发布组、分类和资源规格后恢复标题。

        :param original: 未丢失中文或双语名称的原始发布名
        :param release_groups: 已识别发布组的大小写折叠值
        :return: 第一个有效候选名称，没有真实片名时返回 None
        """
        for match in BRACKET_TITLE_RE.finditer(original.replace("【", "[").replace("】", "]")):
            name = cls._recovered_anime_name(match.group(1).strip("[] "))
            if not name or cls._invalid_anime_name(name, release_groups) or name.isdigit():
                continue
            metadata = anitopy.parse(f"[{name}]")
            if any(field in metadata for field in (
                "video_resolution", "video_term", "audio_term", "source", "file_extension", "file_checksum",
                "anime_year", "episode_number",
            )):
                continue
            return name
        return None

    def __init_anime_fps(self, anitopy_info: dict[str, object], original_title: str) -> None:
        """
        从原始标题中提取帧率信息，与MetaVideo保持完全一致的实现。
        """
        re_res = self._fps_pattern.search(original_title)
        if re_res:
            fps_value = None
            # FPS格式
            if re_res.group(1):
                fps_value = re_res.group(1)

            if fps_value and fps_value.isdigit():
                self.fps = int(fps_value)

    @staticmethod
    def __prepare_title(title: str) -> str:
        """
        对命名进行预处理。
        """
        if not title:
            return title
        title = title.replace("【", "[").replace("】", "]").strip()
        match = ANIME_MARK_RE.search(title)
        if match and match.span()[1] < len(title) - 1:
            title = ANIME_PREFIX_RE.sub("", title)
        elif match:
            title = title[:title.rfind('[')]
        first_item = title.split(']')[0]
        if first_item and CATEGORY_TAG_RE.search(zhconv_convert(first_item, "zh-hans")):
            title = LEADING_BRACKET_BLOCK_RE.sub("", title).strip()
        title = FILE_SIZE_RE.sub("", title)
        title = TV_EPISODE_BRACKET_RE.sub(r"[\1", title)
        title = FOUR_K_BRACKET_RE.sub('2160p', title)
        # 处理/分隔的中英文标题
        names = title.split("]")
        if len(names) > 1 and title.find("- ") == -1:
            titles = []
            for name in names:
                if not name:
                    continue
                left_char = ''
                if name.startswith('['):
                    left_char = '['
                    name = name[1:]
                if name and name.find("/") != -1:
                    if name.split("/")[-1].strip():
                        titles.append("%s%s" % (left_char, name.split("/")[-1].strip()))
                    else:
                        titles.append("%s%s" % (left_char, name.split("/")[0].strip()))
                elif name:
                    if text_tools.contains_chinese(name) and not text_tools.is_all_chinese(name):
                        if not NUMERIC_BRACKET_RE.search(name):
                            name = MIXED_CHINESE_TOKEN_RE.sub('', name).strip()
                        if not name or name.strip().isdigit():
                            continue
                    if name == '[':
                        titles.append("")
                    else:
                        titles.append("%s%s" % (left_char, name.strip()))
            return "]".join(titles)
        return title
