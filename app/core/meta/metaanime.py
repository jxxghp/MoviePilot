import re
import traceback

import anitopy
from app.core.meta.customization import CustomizationMatcher
from app.core.meta.metabase import MetaBase
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.log import logger
from app.utils.string import StringUtils
from app.utils.zhconv import convert as zhconv_convert
from app.schemas.types import MediaType


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


class MetaAnime(MetaBase):
    """
    识别动漫
    """
    _anime_no_words = ['CHS&CHT', 'MP4', 'GB MP4', 'WEB-DL']
    _name_nostring_re = r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}|\s+GB"
    _fps_re = r"(\d{2,3})(?=FPS)"
    _name_nostring_pattern = re.compile(_name_nostring_re, re.IGNORECASE)
    _fps_pattern = re.compile(r"(%s)" % _fps_re, re.IGNORECASE)

    @staticmethod
    def _parse_season_number(value):
        """解析第三方动漫季号，仅接受整数或纯数字字符串并保留数值 0。"""
        if value is None:
            return None
        text = str(value).strip()
        return int(text) if text.isdigit() else None

    def __init__(self, title: str, subtitle: str = None, isfile: bool = False):
        super().__init__(title, subtitle, isfile)
        if not title:
            return
        # 调用第三方模块识别动漫
        try:
            original_title = title
            # 字幕组信息会被预处理掉
            anitopy_info_origin = anitopy.parse(title)
            title = self.__prepare_title(title)
            anitopy_info = anitopy.parse(title)
            if anitopy_info:
                # 名称
                name = anitopy_info.get("anime_title")
                if not name or name in self._anime_no_words or (len(name) < 5 and not StringUtils.is_chinese(name)):
                    anitopy_info = anitopy.parse("[ANIME]" + title)
                    if anitopy_info:
                        name = anitopy_info.get("anime_title")
                if not name or name in self._anime_no_words or (len(name) < 5 and not StringUtils.is_chinese(name)):
                    name_match = BRACKET_TITLE_RE.search(title)
                    if name_match and name_match.group(1):
                        name = name_match.group(1).strip()
                # 拆份中英文名称
                if name:
                    _split_flag = True
                    # 按/拆分中英文
                    if name.find("/") != -1:
                        names = name.split("/")
                        if StringUtils.is_chinese(names[0]):
                            self.cn_name = names[0]
                            if len(names) > 1:
                                self.en_name = names[1]
                            _split_flag = False
                        elif StringUtils.is_chinese(names[-1]):
                            self.cn_name = names[-1]
                            if len(names) > 1:
                                self.en_name = names[0]
                            _split_flag = False
                        else:
                            name = names[-1]
                    # 拆分中英文
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
                            elif StringUtils.is_chinese(word):
                                self.cn_name = "%s %s" % (self.cn_name or "", word)
                                lastword_type = "cn"
                            else:
                                self.en_name = "%s %s" % (self.en_name or "", word)
                                lastword_type = "en"
                if self.cn_name:
                    _, self.cn_name, _, _, _, _ = StringUtils.get_keyword(self.cn_name)
                    if self.cn_name:
                        self.cn_name = self._name_nostring_pattern.sub('', self.cn_name).strip()
                if self.en_name:
                    self.en_name = self._name_nostring_pattern.sub('', self.en_name).strip().title()
                    self._name = StringUtils.str_title(self.en_name)
                # 年份
                year = anitopy_info.get("anime_year")
                if str(year).isdigit():
                    self.year = str(year)
                # 季号
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
                # 集号
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
                # 类型
                if not self.type:
                    anime_type = anitopy_info.get('anime_type')
                    if isinstance(anime_type, list):
                        anime_type = anime_type[0]
                    if anime_type and anime_type.upper() == "TV":
                        self.type = MediaType.TV
                    else:
                        self.type = MediaType.MOVIE
                # 分辨率
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
                # 制作组/字幕组
                self.resource_team = \
                    ReleaseGroupsMatcher().match(title=original_title) or \
                    anitopy_info_origin.get("release_group") or None
                # 自定义占位符
                self.customization = CustomizationMatcher().match(title=original_title) or None
                # 视频编码
                self.video_encode = anitopy_info.get("video_term")
                if isinstance(self.video_encode, list):
                    self.video_encode = self.video_encode[0]
                # 视频位深
                self.video_bit = self.extract_video_bit(original_title) or self.extract_video_bit(self.video_encode)
                # 音频编码
                self.audio_encode = anitopy_info.get("audio_term")
                if isinstance(self.audio_encode, list):
                    self.audio_encode = self.audio_encode[0]
                # 帧率信息
                self.__init_anime_fps(anitopy_info, original_title)
                # 解析副标题，只要季和集
                self.init_subtitle(self.org_string)
                if not self._subtitle_flag and self.subtitle:
                    self.init_subtitle(self.subtitle)
            if not self.type:
                self.type = MediaType.TV
        except Exception as e:
            logger.error(f"解析动漫信息失败：{str(e)} - {traceback.format_exc()}")

    def __init_anime_fps(self, anitopy_info: dict, original_title: str):
        """
        从原始标题中提取帧率信息，与MetaVideo保持完全一致的实现
        """
        re_res = self._fps_pattern.search(original_title)
        if re_res:
            fps_value = None
            if re_res.group(1):  # FPS格式
                fps_value = re_res.group(1)
                    
            if fps_value and fps_value.isdigit():
                # 只存储纯数值
                self.fps = int(fps_value)

    @staticmethod
    def __prepare_title(title: str):
        """
        对命名进行预处理
        """
        if not title:
            return title
        # 所有【】换成[]
        title = title.replace("【", "[").replace("】", "]").strip()
        # 截掉xx番剧漫
        match = ANIME_MARK_RE.search(title)
        if match and match.span()[1] < len(title) - 1:
            title = ANIME_PREFIX_RE.sub("", title)
        elif match:
            title = title[:title.rfind('[')]
        # 截掉分类
        first_item = title.split(']')[0]
        if first_item and CATEGORY_TAG_RE.search(zhconv_convert(first_item, "zh-hans")):
            title = LEADING_BRACKET_BLOCK_RE.sub("", title).strip()
        # 去掉大小
        title = FILE_SIZE_RE.sub("", title)
        # 将TVxx改为xx
        title = TV_EPISODE_BRACKET_RE.sub(r"[\1", title)
        # 将4K转为2160p
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
                    if StringUtils.is_chinese(name) and not StringUtils.is_all_chinese(name):
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
