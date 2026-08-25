import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional

from Pinyin2Hanzi import is_pinyin

from app.domain.meta.customization import CustomizationMatcher
from app.domain.meta.metabase import MetaBase
from app.domain.meta.releasegroup import ReleaseGroupsMatcher
from app.schemas.types import MediaType
from app.foundation import text as text_tools
from app.domain.tokens import Tokens
from app.domain.meta.streamingplatform import StreamingPlatforms
from app.domain.meta.runtime import get_media_extensions


SEASON_FULL_RE = re.compile(r"^(?:Season\s+|S)(\d{1,3})$", re.IGNORECASE)
FIRST_BRACKET_RE = re.compile(r'^[\[【](.+?)[\]】]')
BRACKET_DOT_TITLE_RE = re.compile(r'[A-Za-z]+\..+(?:19|20)\d{2}')
BRACKET_RESOURCE_RE = re.compile(
    r'(?:2160|1080|720|480)[PIpi]|4K|UHD|Blu[\-.]?ray|REMUX|WEB[\-.]?DL|HDTV',
    re.IGNORECASE,
)
YEAR_RANGE_RE = re.compile(r'([\s.]+)(\d{4})-(\d{4})')
FILE_SIZE_RE = re.compile(r'[0-9.]+\s*[MGT]i?B(?![A-Z]+)', re.IGNORECASE)
DATE_RE = re.compile(r'\d{4}[\s._-]\d{1,2}[\s._-]\d{1,2}')
DIY_RE = re.compile(r'DIY', re.IGNORECASE)
DIY_TITLE_RE = re.compile(r'-DIY@', re.IGNORECASE)
DESCRIPTION_SPLIT_RE = re.compile(r'[\s/|]+')
SPACE_RE = re.compile(r'\s+')
SEASON_SUFFIX_RE = re.compile(r"SEASON$", re.IGNORECASE)

SOURCE_RE = (
    r"^BLURAY$|^HDTV$|^UHDTV$|^HDDVD$|^WEBRIP$|^DVDRIP$|^BDRIP$|"
    r"^BLU$|^WEB$|^BD$|^HDRip$|^REMUX$|^UHD$"
)
SOURCE_PATTERN = re.compile(r"(%s)" % SOURCE_RE, re.IGNORECASE)
SOURCE_NAMES = {
    "BLURAY": "BluRay",
    "HDTV": "HDTV",
    "UHDTV": "UHDTV",
    "HDDVD": "HDDVD",
    "WEBRIP": "WEBRip",
    "DVDRIP": "DVDRip",
    "BDRIP": "BDRIP",
    "BLU": "BLU",
    "WEB": "WEB",
    "BD": "BD",
    "HDRIP": "HDRip",
    "REMUX": "REMUX",
    "UHD": "UHD",
}


class _VideoTokenKind(Enum):
    """记录会影响后续词元判断的识别类型。"""

    ENGLISH_NAME = auto()
    CHINESE_NAME = auto()
    NAME_SEASON_WORD = auto()
    PART = auto()
    YEAR = auto()
    PIX = auto()
    SEASON = auto()
    SEASON_MARKER = auto()
    EPISODE = auto()
    EPISODE_MARKER = auto()
    SOURCE = auto()
    EFFECT = auto()
    VIDEO_ENCODE = auto()
    VIDEO_BIT = auto()
    AUDIO_ENCODE = auto()
    FPS = auto()


@dataclass(slots=True)
class _VideoParseState:
    """保存一次视频标题解析中跨词元延续的状态。"""

    token_index: int = 0
    last_token: str = ""
    last_kind: Optional[_VideoTokenKind] = None
    stop_name: bool = False
    stop_cn_name: bool = False
    pending_name: str = ""
    sources: list[str] = field(default_factory=list)
    effects: list[str] = field(default_factory=list)

    def advance(self) -> None:
        """进入下一个由解析管线处理的词元。"""
        self.token_index += 1

    def remember(self, kind: _VideoTokenKind, token: Optional[str] = None) -> None:
        """原子更新后续规则依赖的上一词元类型和值。"""
        self.last_kind = kind
        if token is not None:
            self.last_token = token

    def append_source(self, source: str) -> None:
        """按出现顺序追加规范化资源类型并忽略重复项。"""
        source_name = SOURCE_NAMES.get(source.upper(), source)
        if source_name.casefold() not in {
            item.casefold() for item in self.sources
        }:
            self.sources.append(source_name)

    def replace_last_source(self, source: str, replacement: str) -> None:
        """将拆分的资源类型前缀替换为完整规范名称。"""
        if self.sources and self.sources[-1].casefold() == source.casefold():
            self.sources.pop()
        self.append_source(replacement)


class MetaVideo(MetaBase):
    """
    识别电影、电视剧
    """
    # 正则式区
    _season_re = r"S(\d{3})|^S(\d{1,3})$|S(\d{1,3})E"
    _episode_re = r"EP?(\d{2,4})$|^EP?(\d{1,4})$|^S\d{1,2}EP?(\d{1,4})$|S\d{2}EP?(\d{2,4})"
    _part_re = r"(^PART[0-9ABI]{0,2}$|^CD[0-9]{0,2}$|^DVD[0-9]{0,2}$|^DISK[0-9]{0,2}$|^DISC[0-9]{0,2}$)"
    _roman_numerals = r"^(?=[MDCLXVI])M*(C[MD]|D?C{0,3})(X[CL]|L?X{0,3})(I[XV]|V?I{0,3})$"
    _source_re = SOURCE_RE
    _effect_re = r"^SDR$|^HDR\d*$|^HDRVIVID$|^DOLBY$|^DOVI$|^DV$|^3D$|^REPACK$|^HLG$|^HDR10(\+|Plus)$|^HDR10P$|^VIVID$|^EDR$|^HQ$"
    _resources_type_re = r"%s|%s" % (_source_re, _effect_re)
    _name_no_begin_re = r"^[\[【].+?[\]】]"
    _name_no_chinese_re = r".*版|.*字幕"
    _name_se_words = ['共', '第', '季', '集', '话', '話', '期']
    _name_movie_words = ['剧场版', '劇場版', '电影版', '電影版']
    _name_nostring_re = r"^PTS|^JADE|^AOD|^CHC|^[A-Z]{1,4}TV[\-0-9UVHDK]*" \
                        r"|\d{1,2}th|\d{1,2}bit|IMAX|^3D|\s+3D|\s+DC$" \
                        r"|[第\s共]+[0-9一二三四五六七八九十\-\s]+季" \
                        r"|[第\s共]+[0-9一二三四五六七八九十百零\-\s]+[集话話]" \
                        r"|连载|日剧|美剧|电视剧|动画片|动漫|欧美|西德|日韩|超高清|高清|无水印|下载|蓝光|翡翠台|梦幻天堂·龙网|★?\d*月?新番" \
                        r"|最终季|合集|[多中国英葡法俄日韩德意西印泰台港粤双文语简繁体特效内封官译外挂]+字幕|版本|出品|台版|港版|\w+字幕组|\w+字幕社" \
                        r"|未删减版|UNCUT$|UNRATE$|WITH EXTRAS$|RERIP$|SUBBED$|PROPER$|REPACK$|SEASON$|EPISODE$|Complete$|Extended$|Extended Version$" \
                        r"|S\d{2}\s*-\s*S\d{2}|S\d{2}|\s+S\d{1,2}|EP?\d{2,4}\s*-\s*EP?\d{2,4}|EP?\d{2,4}|\s+EP?\d{1,4}" \
                        r"|CD[\s.]*[1-9]|DVD[\s.]*[1-9]|DISK[\s.]*[1-9]|DISC[\s.]*[1-9]" \
                        r"|[248]K|\d{3,4}[PIX]+" \
                        r"|CD[\s.]*[1-9]|DVD[\s.]*[1-9]|DISK[\s.]*[1-9]|DISC[\s.]*[1-9]|\s+GB"
    _resources_pix_re = r"^[SBUHD]*(\d{3,4}[PI]+)|\d{3,4}X(\d{3,4})"
    _resources_pix_re2 = r"(^[248]+K)"
    _video_encode_re = r"^(H26[45])$|^(x26[45])$|^AVC$|^HEVC$|^VC\d?$|^MPEG\d?$|^Xvid$|^DivX$|^AV1$|^HDR\d*$|^AVS(\+|[23])$"
    _audio_encode_re = r"^DTS\d?$|^DTSHD$|^DTSHDMA$|^Atmos$|^TrueHD\d?$|^AC3$|^EAC3\d?$|^\dAudios?$|^DDP\d?$|^DD\+\d?$|^DD\d?$|^LPCM\d?$|^AAC\d?$|^FLAC\d?$|^HD\d?$|^MA\d?$|^HR\d?$|^Opus\d?$|^Vorbis\d?$|^AV[3S]A$"
    _fps_re = r"(\d{2,3})(?=FPS)"
    _season_pattern = re.compile(_season_re, re.IGNORECASE)
    _episode_pattern = re.compile(_episode_re, re.IGNORECASE)
    _part_pattern = re.compile(_part_re, re.IGNORECASE)
    _roman_numerals_pattern = re.compile(_roman_numerals)
    _source_pattern = SOURCE_PATTERN
    _effect_pattern = re.compile(r"(%s)" % _effect_re, re.IGNORECASE)
    _resources_type_pattern = re.compile(r"(%s)" % _resources_type_re, re.IGNORECASE)
    _name_no_chinese_pattern = re.compile(_name_no_chinese_re, re.IGNORECASE)
    _name_movie_words_pattern = re.compile("|".join(_name_movie_words), re.IGNORECASE)
    _name_nostring_pattern = re.compile(_name_nostring_re, re.IGNORECASE)
    _resources_pix_pattern = re.compile(_resources_pix_re, re.IGNORECASE)
    _resources_pix_pattern2 = re.compile(_resources_pix_re2, re.IGNORECASE)
    _video_encode_pattern = re.compile(r"(%s)" % _video_encode_re, re.IGNORECASE)
    _audio_encode_pattern = re.compile(r"(%s)" % _audio_encode_re, re.IGNORECASE)
    _fps_pattern = re.compile(r"(%s)" % _fps_re, re.IGNORECASE)

    def __init__(self, title: str, subtitle: str = None, isfile: bool = False):
        """
        初始化
        :param title: 标题，文件为去掉了后缀
        :param subtitle: 副标题
        :param isfile: 是否是文件名
        """
        super().__init__(title, subtitle, isfile)
        if not title:
            return
        original_title = title
        state = _VideoParseState()
        # 判断是否纯数字命名
        if isfile \
                and title.isdigit() \
                and len(title) < 5:
            self.begin_episode = int(title)
            self.total_episode = 1
            self.type = MediaType.TV
            return
        # 全名为Season xx 及 Sxx 直接返回
        season_full_res = SEASON_FULL_RE.search(title)
        if season_full_res:
            self.type = MediaType.TV
            season = season_full_res.group(1)
            if season:
                self.begin_season = int(season)
                self.total_season = 1
            return
        # 去掉名称中第1个[]的内容
        _first_bracket = FIRST_BRACKET_RE.match(title)
        if _first_bracket:
            _bracket_content = _first_bracket.group(1)
            # 如果第一个括号内为点分隔的英文发布名格式（含年份+资源类型），保留内容去掉括号
            if BRACKET_DOT_TITLE_RE.search(_bracket_content) \
                    and BRACKET_RESOURCE_RE.search(_bracket_content):
                title = _bracket_content + title[_first_bracket.end():]
            else:
                title = title[_first_bracket.end():]
        # 把xxxx-xxxx年份换成前一个年份，常出现在季集上
        title = YEAR_RANGE_RE.sub(r'\1\2', title)
        # 把大小去掉
        title = FILE_SIZE_RE.sub("", title)
        # 把年月日去掉
        title = DATE_RE.sub("", title)
        media_exts = get_media_extensions()
        # 拆分tokens
        tokens = Tokens(title)
        # 实例化StreamingPlatforms对象
        streaming_platforms = StreamingPlatforms()
        # 解析名称、年份、季、集、资源类型、分辨率等
        token = tokens.get_next()
        while token:
            state.advance()
            self.__parse_token(token, tokens, streaming_platforms, media_exts, state)
            token = tokens.get_next()
        # 合成质量
        if state.effects:
            self.resource_effect = " ".join(reversed(state.effects))
        if state.sources:
            self.resource_type = " ".join(state.sources)
        # 提取原盘DIY
        if self.resource_type and "BluRay" in self.resource_type:
            if (self.subtitle and DIY_RE.search(self.subtitle)) \
                    or DIY_TITLE_RE.search(original_title):
                self.resource_type = f"{self.resource_type} DIY"
        # 解析副标题，只要季和集
        self.init_subtitle(self.org_string)
        if not self._subtitle_flag and self.subtitle:
            self.init_subtitle(self.subtitle)
        # 去掉名字中不需要的干扰字符，过短的纯数字不要
        self.cn_name = self.__fix_name(self.cn_name)
        self.en_name = text_tools.title_case(self.__fix_name(self.en_name))
        # 处理part
        if self.part and self.part.upper() == "PART":
            self.part = None
        # 没有中文标题时，尝试中描述中获取中文名
        if not self.cn_name and self.en_name and self.subtitle:
            if self.__is_pinyin(self.en_name):
                # 英文名是拼音
                cn_name = self.__get_title_from_description(self.subtitle)
                if cn_name and len(cn_name) == len(self.en_name.split()):
                    # 中文名和拼音单词数相同，认为是中文名
                    self.cn_name = cn_name
        # 制作组/字幕组
        self.resource_team = ReleaseGroupsMatcher().match(title=original_title) or None
        # 自定义占位符
        self.customization = CustomizationMatcher().match(title=original_title) or None
        if not self.video_bit:
            self.video_bit = self.extract_video_bit(self.video_encode)

    def __parse_token(
        self,
        token: str,
        tokens: Tokens,
        streaming_platforms: StreamingPlatforms,
        media_exts: list,
        state: _VideoParseState,
    ) -> None:
        """按固定优先级处理单个词元，首个命中的阶段终止后续识别。"""
        if self.__init_part(token, tokens, state):
            return
        if self.__init_name(token, media_exts, state):
            return
        if self.__init_year(token, state):
            return
        if self.__init_resource_pix(token, state):
            return
        if self.__init_season(token, state):
            return
        if self.__init_episode(token, state):
            return
        if self.__init_resource_type(token, state):
            return
        if self.__init_web_source(token, tokens, streaming_platforms, state):
            return
        if self.__init_video_encode(token, state):
            return
        if self.__init_video_bit(token, state):
            return
        if self.__init_audio_encode(token, state):
            return
        self.__init_fps(token, state)

    @staticmethod
    def __get_title_from_description(description: str) -> Optional[str]:
        """
        从描述中提取标题
        """
        if not description:
            return None
        titles = DESCRIPTION_SPLIT_RE.split(description)
        if text_tools.contains_chinese(titles[0]):
            return titles[0]
        return None

    @staticmethod
    def __is_pinyin(name_str: Optional[str]) -> bool:
        """
        判断是否拼音
        """
        if not name_str:
            return False
        for n in name_str.lower().split():
            if not is_pinyin(n):
                return False
        return True

    def __fix_name(self, name: Optional[str]):
        """
        去掉名字中不需要的干扰字符
        """
        if not name:
            return name
        name = self._name_nostring_pattern.sub('', name).strip()
        name = SPACE_RE.sub(' ', name)
        if name.isdecimal() \
                and int(name) < 1800 \
                and not self.year \
                and self.begin_season is None \
                and not self.resource_pix \
                and not self.resource_type \
                and not self.audio_encode \
                and not self.video_encode:
            if self.begin_episode is None:
                self.begin_episode = int(name)
                name = None
            elif self.is_in_episode(int(name)) and self.begin_season is None:
                name = None
        return name

    def __init_name(
        self,
        token: Optional[str],
        media_exts: list,
        state: _VideoParseState,
    ) -> bool:
        """
        识别名称
        """
        if not token:
            return False
        # 回收标题
        if state.pending_name:
            if not self.cn_name:
                if not self.en_name:
                    self.en_name = state.pending_name
                elif state.pending_name != self.year:
                    self.en_name = "%s %s" % (self.en_name, state.pending_name)
                state.remember(_VideoTokenKind.ENGLISH_NAME)
            state.pending_name = ""
        if state.stop_name:
            return False
        if token.upper() == "AKA":
            state.stop_name = True
            return True
        if token in self._name_se_words:
            state.remember(_VideoTokenKind.NAME_SEASON_WORD)
            return False
        if text_tools.contains_chinese(token):
            # 含有中文，直接做为标题（连着的数字或者英文会保留），且不再取用后面出现的中文
            state.remember(_VideoTokenKind.CHINESE_NAME)
            if not self.cn_name:
                self.cn_name = token
            elif not state.stop_cn_name:
                if self._name_movie_words_pattern.search(token) \
                        or (not self._name_no_chinese_pattern.search(token)
                            and not any(w in token for w in self._name_se_words)):
                    self.cn_name = "%s %s" % (self.cn_name, token)
                state.stop_cn_name = True
        else:
            is_roman_digit = self._roman_numerals_pattern.search(token)
            # 阿拉伯数字或者罗马数字
            if token.isdigit() or is_roman_digit:
                # 第季集后面的不要
                if state.last_kind == _VideoTokenKind.NAME_SEASON_WORD:
                    return False
                if self.name:
                    # 名字后面以 0 开头的不要，极有可能是集
                    if token.startswith('0'):
                        return False
                    # 检查是否真正的数字
                    if token.isdigit():
                        try:
                            int(token)
                        except ValueError:
                            return False
                    # 中文名后面跟的数字不是年份的极有可能是集
                    if not is_roman_digit \
                            and state.last_kind == _VideoTokenKind.CHINESE_NAME \
                            and int(token) < 1900:
                        return False
                    if (token.isdigit() and len(token) < 4) or is_roman_digit:
                        # 4位以下的数字或者罗马数字，拼装到已有标题中
                        if state.last_kind == _VideoTokenKind.CHINESE_NAME:
                            self.cn_name = "%s %s" % (self.cn_name, token)
                        elif state.last_kind == _VideoTokenKind.ENGLISH_NAME:
                            self.en_name = "%s %s" % (self.en_name, token)
                        return True
                    elif token.isdigit() and len(token) == 4:
                        # 4位数字，可能是年份，也可能真的是标题的一部分，也有可能是集
                        if not state.pending_name:
                            state.pending_name = token
                else:
                    # 名字未出现前的第一个数字，记下来
                    if not state.pending_name:
                        state.pending_name = token
            elif self._season_pattern.search(token):
                # 季的处理
                if self.en_name and SEASON_SUFFIX_RE.search(self.en_name):
                    # 如果匹配到季，英文名结尾为Season，说明Season属于标题，不应在后续作为干扰词去除
                    self.en_name += ' '
                state.stop_name = True
                return False
            elif self._episode_pattern.search(token) \
                    or self._resources_type_pattern.search(token) \
                    or self._resources_pix_pattern.search(token):
                # 集、来源、版本等不要
                state.stop_name = True
                return False
            else:
                # 后缀名不要
                if ".%s".lower() % token in media_exts:
                    return False
                # 英文或者英文+数字，拼装起来
                if self.en_name:
                    self.en_name = "%s %s" % (self.en_name, token)
                else:
                    self.en_name = token
                state.remember(_VideoTokenKind.ENGLISH_NAME)
        return False

    def __init_part(self, token: str, tokens: Tokens, state: _VideoParseState) -> bool:
        """
        识别Part
        """
        if not self.name:
            return False
        if not self.year \
                and self.begin_season is None \
                and not self.begin_episode \
                and not self.resource_pix \
                and not self.resource_type:
            return False
        re_res = self._part_pattern.search(token)
        if re_res:
            if not self.part:
                self.part = re_res.group(1)
            nextv = tokens.cur()
            if nextv \
                    and ((nextv.isdigit() and (len(nextv) == 1 or len(nextv) == 2 and nextv.startswith('0')))
                         or nextv.upper() in ['A', 'B', 'C', 'I', 'II', 'III']):
                self.part = "%s%s" % (self.part, nextv)
                tokens.get_next()
            state.remember(_VideoTokenKind.PART)
            return True
        return False

    def __init_year(self, token: str, state: _VideoParseState) -> bool:
        """
        识别年份
        """
        if not self.name:
            return False
        if not token.isdigit():
            return False
        if len(token) != 4:
            return False
        if not 1900 < int(token) < 2050:
            return False
        if self.year:
            if self.en_name:
                self.en_name = "%s %s" % (self.en_name.strip(), self.year)
            elif self.cn_name:
                self.cn_name = "%s %s" % (self.cn_name, self.year)
        elif self.en_name and SEASON_SUFFIX_RE.search(self.en_name):
            # 如果匹配到年，且英文名结尾为Season，说明Season属于标题，不应在后续作为干扰词去除
            self.en_name += ' '
        self.year = token
        state.remember(_VideoTokenKind.YEAR)
        state.stop_name = True
        return True

    def __init_resource_pix(self, token: str, state: _VideoParseState) -> bool:
        """
        识别分辨率
        """
        if not self.name:
            return False
        re_res = self._resources_pix_pattern.findall(token)
        if re_res:
            state.remember(_VideoTokenKind.PIX)
            state.stop_name = True
            resource_pix = None
            for pixs in re_res:
                if isinstance(pixs, tuple):
                    pix_t = None
                    for pix_i in pixs:
                        if pix_i:
                            pix_t = pix_i
                            break
                    if pix_t:
                        resource_pix = pix_t
                else:
                    resource_pix = pixs
                if resource_pix and not self.resource_pix:
                    self.resource_pix = resource_pix.lower()
                    break
            if self.resource_pix \
                    and self.resource_pix.isdigit() \
                    and self.resource_pix[-1] not in 'kpi':
                self.resource_pix = "%sp" % self.resource_pix
            return True
        re_res = self._resources_pix_pattern2.search(token)
        if re_res:
            state.remember(_VideoTokenKind.PIX)
            state.stop_name = True
            if not self.resource_pix:
                self.resource_pix = re_res.group(1).lower()
            return True
        return False

    def __init_season(self, token: str, state: _VideoParseState) -> bool:
        """
        识别季
        """
        re_res = self._season_pattern.findall(token)
        if re_res:
            state.remember(_VideoTokenKind.SEASON)
            self.type = MediaType.TV
            state.stop_name = True
            for se in re_res:
                if isinstance(se, tuple):
                    se_t = None
                    for se_i in se:
                        if se_i and str(se_i).isdigit():
                            se_t = se_i
                            break
                    if se_t:
                        se = int(se_t)
                    else:
                        break
                else:
                    se = int(se)
                if self.begin_season is None:
                    self.begin_season = se
                    self.total_season = 1
                else:
                    if se > self.begin_season:
                        self.end_season = se
                        self.total_season = (self.end_season - self.begin_season) + 1
                        if self.isfile and self.total_season > 1:
                            self.end_season = None
                            self.total_season = 1
            return False
        elif token.isdigit():
            try:
                int(token)
            except ValueError:
                return False
            if state.last_kind == _VideoTokenKind.SEASON_MARKER \
                    and self.begin_season is None \
                    and len(token) < 3:
                self.begin_season = int(token)
                self.total_season = 1
                state.remember(_VideoTokenKind.SEASON)
                state.stop_name = True
                self.type = MediaType.TV
                return True
        elif token.upper() == "SEASON" and self.begin_season is None:
            state.remember(_VideoTokenKind.SEASON_MARKER)
        elif self.type == MediaType.TV and self.begin_season is None:
            self.begin_season = 1
        return False

    def __init_episode(self, token: str, state: _VideoParseState) -> bool:
        """
        识别集
        """
        re_res = self._episode_pattern.findall(token)
        if re_res:
            state.remember(_VideoTokenKind.EPISODE)
            state.stop_name = True
            self.type = MediaType.TV
            for se in re_res:
                if isinstance(se, tuple):
                    se_t = None
                    for se_i in se:
                        if se_i and str(se_i).isdigit():
                            se_t = se_i
                            break
                    if se_t:
                        se = int(se_t)
                    else:
                        break
                else:
                    se = int(se)
                if self.begin_episode is None:
                    self.begin_episode = se
                    self.total_episode = 1
                else:
                    if se > self.begin_episode:
                        self.end_episode = se
                        self.total_episode = (self.end_episode - self.begin_episode) + 1
                        if self.isfile and self.total_episode > 2:
                            self.end_episode = None
                            self.total_episode = 1
            return True
        elif token.isdigit():
            try:
                int(token)
            except ValueError:
                return False
            if self.begin_episode is not None \
                    and self.end_episode is None \
                    and len(token) < 5 \
                    and int(token) > self.begin_episode \
                    and state.last_kind == _VideoTokenKind.EPISODE:
                self.end_episode = int(token)
                self.total_episode = (self.end_episode - self.begin_episode) + 1
                if self.isfile and self.total_episode > 2:
                    self.end_episode = None
                    self.total_episode = 1
                self.type = MediaType.TV
                return True
            elif self.begin_episode is None \
                    and 1 < len(token) < 4 \
                    and state.last_kind != _VideoTokenKind.YEAR \
                    and state.last_kind != _VideoTokenKind.VIDEO_ENCODE \
                    and token != state.pending_name:
                self.begin_episode = int(token)
                self.total_episode = 1
                state.remember(_VideoTokenKind.EPISODE)
                state.stop_name = True
                self.type = MediaType.TV
                return True
            elif state.last_kind == _VideoTokenKind.EPISODE_MARKER \
                    and self.begin_episode is None \
                    and len(token) < 5:
                self.begin_episode = int(token)
                self.total_episode = 1
                state.remember(_VideoTokenKind.EPISODE)
                state.stop_name = True
                self.type = MediaType.TV
                return True
        elif token.upper() == "EPISODE":
            state.remember(_VideoTokenKind.EPISODE_MARKER)
        return False

    def __init_resource_type(self, token: str, state: _VideoParseState) -> bool:
        """
        识别资源类型
        """
        if not self.name:
            return False
        if token.upper() == "DL" \
                and state.last_kind == _VideoTokenKind.SOURCE \
                and state.last_token == "WEB":
            state.replace_last_source("WEB", "WEB-DL")
            return True
        elif token.upper() == "RAY" \
                and state.last_kind == _VideoTokenKind.SOURCE \
                and state.last_token == "BLU":
            state.replace_last_source("BLU", "BluRay")
            return True
        elif token.upper() == "WEBDL":
            state.append_source("WEB-DL")
            return True
        source_res = self._source_pattern.search(token)
        if source_res:
            state.stop_name = True
            source = source_res.group(1)
            state.append_source(source)
            state.remember(_VideoTokenKind.SOURCE, source.upper())
            return True
        effect_res = self._effect_pattern.search(token)
        if effect_res:
            state.stop_name = True
            effect = effect_res.group(1)
            if effect not in state.effects:
                state.effects.append(effect)
            state.remember(_VideoTokenKind.EFFECT, effect.upper())
            return True
        return False

    def __init_web_source(
        self,
        token: str,
        tokens: Tokens,
        streaming_platforms: StreamingPlatforms,
        state: _VideoParseState,
    ) -> bool:
        """
        识别流媒体平台
        """
        if not self.name:
            return False

        platform_name = None
        query_range = 1

        prev_token = None
        prev_idx = state.token_index - 2
        if 0 <= prev_idx < len(tokens.tokens):
            prev_token = tokens.tokens[prev_idx]

        next_token = tokens.peek()

        if streaming_platforms.is_streaming_platform(token):
            platform_name = streaming_platforms.get_streaming_platform_name(token)
        else:
            for adjacent_token, is_next in [(prev_token, False), (next_token, True)]:
                if not adjacent_token or platform_name:
                    continue

                for separator in [" ", "-"]:
                    if is_next:
                        combined_token = f"{token}{separator}{adjacent_token}"
                    else:
                        combined_token = f"{adjacent_token}{separator}{token}"

                    if streaming_platforms.is_streaming_platform(combined_token):
                        platform_name = streaming_platforms.get_streaming_platform_name(combined_token)
                        query_range = 2
                        if is_next:
                            tokens.get_next()
                        break

        if not platform_name:
            return False

        web_tokens = ["WEB", "DL", "WEBDL", "WEBRIP"]
        match_start_idx = state.token_index - query_range
        match_end_idx = state.token_index - 1
        start_index = max(0, match_start_idx - query_range)
        end_index = min(len(tokens.tokens), match_end_idx + 1 + query_range)
        tokens_to_check = tokens.tokens[start_index:end_index]

        if any(tok and tok.upper() in web_tokens for tok in tokens_to_check):
            self.web_source = platform_name
            return True
        return False

    def __init_video_encode(self, token: str, state: _VideoParseState) -> bool:
        """
        识别视频编码
        """
        if not self.name:
            return False
        if not self.year \
                and not self.resource_pix \
                and not self.resource_type \
                and self.begin_season is None \
                and not self.begin_episode:
            return False
        re_res = self._video_encode_pattern.search(token)
        if re_res:
            state.stop_name = True
            if not self.video_encode:
                if re_res.group(2):
                    self.video_encode = re_res.group(2).upper()
                elif re_res.group(3):
                    self.video_encode = re_res.group(3).lower()
                else:
                    self.video_encode = re_res.group(1).upper()
                state.remember(_VideoTokenKind.VIDEO_ENCODE, self.video_encode)
            elif self.video_encode == "10bit":
                self.video_encode = f"{re_res.group(1).upper()} 10bit"
                state.remember(_VideoTokenKind.VIDEO_ENCODE, re_res.group(1).upper())
            else:
                state.remember(_VideoTokenKind.VIDEO_ENCODE)
            return True
        elif token.upper() in ['H', 'X']:
            state.stop_name = True
            last_token = token.upper() if token.upper() == "H" else token.lower()
            state.remember(_VideoTokenKind.VIDEO_ENCODE, last_token)
            return True
        elif token in ["264", "265"] \
                and state.last_kind == _VideoTokenKind.VIDEO_ENCODE \
                and state.last_token in ['H', 'X']:
            self.video_encode = "%s%s" % (state.last_token, token)
        elif token.isdigit() \
                and state.last_kind == _VideoTokenKind.VIDEO_ENCODE \
                and state.last_token in ['VC', 'MPEG']:
            self.video_encode = "%s%s" % (state.last_token, token)
        elif token.upper() == "10BIT":
            state.remember(_VideoTokenKind.VIDEO_ENCODE)
            if not self.video_encode:
                self.video_encode = "10bit"
            else:
                self.video_encode = f"{self.video_encode} 10bit"
        return False

    def __init_video_bit(self, token: str, state: _VideoParseState) -> bool:
        """
        识别视频位深。
        """
        if not self.name:
            return False
        if not self.year \
                and not self.resource_pix \
                and not self.resource_type \
                and self.begin_season is None \
                and not self.begin_episode:
            return False
        video_bit = self.extract_video_bit(token)
        if not video_bit:
            return False
        state.stop_name = True
        state.remember(_VideoTokenKind.VIDEO_BIT)
        if not self.video_bit:
            self.video_bit = video_bit
        return True

    def __init_audio_encode(self, token: str, state: _VideoParseState) -> bool:
        """
        识别音频编码
        """
        if not self.name:
            return False
        if not self.year \
                and not self.resource_pix \
                and not self.resource_type \
                and self.begin_season is None \
                and not self.begin_episode:
            return False
        re_res = self._audio_encode_pattern.search(token)
        if re_res:
            state.stop_name = True
            state.remember(_VideoTokenKind.AUDIO_ENCODE, re_res.group(1).upper())
            if not self.audio_encode:
                self.audio_encode = re_res.group(1)
            else:
                if self.audio_encode.upper() == "DTS":
                    self.audio_encode = "%s-%s" % (self.audio_encode, re_res.group(1))
                else:
                    self.audio_encode = "%s %s" % (self.audio_encode, re_res.group(1))
            return True
        elif token.isdigit() \
                and state.last_kind == _VideoTokenKind.AUDIO_ENCODE:
            if self.audio_encode:
                if state.last_token.isdigit():
                    self.audio_encode = "%s.%s" % (self.audio_encode, token)
                elif self.audio_encode[-1].isdigit() and self.audio_encode.upper() not in {"AC3", "EAC3"}:
                    self.audio_encode = "%s %s.%s" % (self.audio_encode[:-1], self.audio_encode[-1], token)
                else:
                    self.audio_encode = "%s %s" % (self.audio_encode, token)
            state.last_token = token
        return False

    def __init_fps(self, token: str, state: _VideoParseState) -> bool:
        """
        识别帧率
        """
        if not self.name:
            return False

        re_res = self._fps_pattern.search(token)
        if re_res:
            state.stop_name = True
            state.remember(_VideoTokenKind.FPS)
            # 提取帧率数值
            fps_value = None
            if re_res.group(1):  # FPS格式
                fps_value = re_res.group(1)
            
            if fps_value and fps_value.isdigit():
                # 只存储纯数值
                self.fps = int(fps_value)
                state.last_token = f"{self.fps}FPS"
            return True
        return False
