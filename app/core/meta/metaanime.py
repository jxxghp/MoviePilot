import re
import traceback

import zhconv
import anitopy
from app.core.meta.customization import CustomizationMatcher
from app.core.meta.metabase import MetaBase
from app.core.meta.releasegroup import ReleaseGroupsMatcher
from app.log import logger
from app.utils.string import StringUtils
from app.schemas.types import MediaType


class MetaAnime(MetaBase):
    """
    识别动漫
    """
    _anime_no_words = ['CHS&CHT', 'MP4', 'GB MP4', 'WEB-DL']
    _name_nostring_re = r"S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}|\s+GB"

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
                    name_match = re.search(r'\[(.+?)]', title)
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
                        self.cn_name = re.sub(r'%s' % self._name_nostring_re, '', self.cn_name, flags=re.IGNORECASE).strip()
                        self.cn_name = zhconv.convert(self.cn_name, "zh-hans")
                if self.en_name:
                    self.en_name = re.sub(r'%s' % self._name_nostring_re, '', self.en_name, flags=re.IGNORECASE).strip().title()
                    self._name = StringUtils.str_title(self.en_name)
                # 年份
                year = anitopy_info.get("anime_year")
                if str(year).isdigit():
                    self.year = str(year)
                # 季号
                anime_season = anitopy_info.get("anime_season")
                if isinstance(anime_season, list):
                    if len(anime_season) == 1:
                        begin_season = anime_season[0]
                        end_season = None
                    else:
                        begin_season = anime_season[0]
                        end_season = anime_season[-1]
                elif anime_season:
                    begin_season = anime_season
                    end_season = None
                else:
                    begin_season = None
                    end_season = None
                if begin_season:
                    self.begin_season = int(begin_season)
                    if end_season and int(end_season) != self.begin_season:
                        self.end_season = int(end_season)
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
                    if re.search(r'x', self.resource_pix, re.IGNORECASE):
                        self.resource_pix = re.split(r'[Xx]', self.resource_pix)[-1] + "p"
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
                # 音频编码
                self.audio_encode = anitopy_info.get("audio_term")
                if isinstance(self.audio_encode, list):
                    self.audio_encode = self.audio_encode[0]
                # 解析副标题，只要季和集
                self.init_subtitle(self.org_string)
                if not self._subtitle_flag and self.subtitle:
                    self.init_subtitle(self.subtitle)
            if not self.type:
                self.type = MediaType.TV
        except Exception as e:
            logger.error(f"解析动漫信息失败：{str(e)} - {traceback.format_exc()}")

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
        match = re.search(r"新番|月?番|[日美国][漫剧]", title)
        if match and match.span()[1] < len(title) - 1:
            title = re.sub(".*番.|.*[日美国][漫剧].", "", title)
        elif match:
            title = title[:title.rfind('[')]
        # 截掉分类
        first_item = title.split(']')[0]
        if first_item and re.search(r"[动漫画纪录片电影视连续剧集日美韩中港台海外亚洲华语大陆综艺原盘高清]{2,}|TV|Animation|Movie|Documentar|Anime",
                                    zhconv.convert(first_item, "zh-hans"),
                                    re.IGNORECASE):
            title = re.sub(r"^[^]]*]", "", title).strip()
        # 去掉大小
        title = re.sub(r'[0-9.]+\s*[MGT]i?B(?![A-Z]+)', "", title, flags=re.IGNORECASE)
        # 将TVxx改为xx
        title = re.sub(r"\[TV\s+(\d{1,4})", r"[\1", title, flags=re.IGNORECASE)
        # 将4K转为2160p
        title = re.sub(r'\[4k]', '2160p', title, flags=re.IGNORECASE)
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
                        if not re.search(r"\[\d+", name, re.IGNORECASE):
                            name = re.sub(r'[\d|#:：\-()（）\u4e00-\u9fff]', '', name).strip()
                        if not name or name.strip().isdigit():
                            continue
                    if name == '[':
                        titles.append("")
                    else:
                        titles.append("%s%s" % (left_char, name.strip()))
            return "]".join(titles)
        return title
