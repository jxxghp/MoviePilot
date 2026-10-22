"""
媒体标题识别的公共字段、季集规则及稳定快照。
"""

import logging
import traceback
from dataclasses import asdict, dataclass
from typing import Any, List, Optional, Self, Union

import cn2an
import regex as re

from app.foundation import text as text_tools
from app.schemas.media import resolve_media_identity
from app.schemas.types import MediaSource, MediaType

logger = logging.getLogger(__name__)


TITLE_EPISODE_RE = re.compile(r"Episode\s+(\d{1,4})", re.IGNORECASE)
SUBTITLE_HAS_SEASON_EPISODE_RE = re.compile(r"[全第季集话話期幕]", re.IGNORECASE)
SUBTITLE_SEASON_RE = re.compile(r"(?<!(?:不包含|不包括|不含)\s*)(?<![全共]\s*)[第\s]+([0-9一二三四五六七八九十S\-]+)\s*季(?!\s*[全共])", re.IGNORECASE)
SUBTITLE_SEASON_ALL_RE = re.compile(r"[全共]\s*([0-9一二三四五六七八九十]+)\s*季", re.IGNORECASE)
SUBTITLE_EPISODE_RE = re.compile(r"(?<!(?:不包含|不包括|不含)\s*)(?<![全共]\s*)[第\s]+([0-9一二三四五六七八九十百零EP]+)\s*[集话話期幕](?!\s*[全共])", re.IGNORECASE)
SUBTITLE_EPISODE_BETWEEN_RE = re.compile(
    r"[第]*\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]?\s*-\s*第*\s*"
    r"([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]",
    re.IGNORECASE,
)
SUBTITLE_EPISODE_ALL_RE = re.compile(
    r"([0-9一二三四五六七八九十百零]+)\s*集\s*全|[全共]\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]",
    re.IGNORECASE,
)
# 结尾分支显式区分有无右方括号，避免可选括号回溯后绕过数字后缀边界
SUBTITLE_EPISODE_RANGE_FIN_RE = re.compile(
    r"(?<!\d)\[?\s*(\d{1,4})\s*-\s*(\d{1,4})\s*"
    r"(?:(?:Fin|End)(?![a-z0-9])|完结(?![\u4e00-\u9fff]))"
    r"(?:\s*\](?!\d)|(?!\s*(?:\]\d|\d))\s*)",
    re.IGNORECASE,
)
VIDEO_BIT_RE = re.compile(
    r"(?<![A-Za-z0-9])(?P<bit>8|10|12|16)[\s._-]*bits?(?![A-Za-z0-9])",
    re.IGNORECASE,
)

_META_OPTIONAL_MERGE_FIELDS = (
    "resource_type",
    "resource_pix",
    "resource_team",
    "customization",
    "resource_effect",
    "web_source",
    "video_encode",
    "video_bit",
    "audio_encode",
    "part",
    "episode_group",
)


@dataclass(frozen=True, slots=True)
class MetaInfoSnapshot:
    """
    MetaInfo 解析器的不可变稳定输出，用于跨实现等价校验和安全缓存。

    :param kind: 解析器类别，区分 anime 动漫与 video 影视
    :param isfile: 输入是否为文件名或文件路径
    :param title: 未经识别词处理的原始标题
    :param org_string: 应用识别词后供解析器使用的标题
    :param subtitle: 用于补充季集和资源信息的副标题；未提供时为 None
    :param type: 识别出的媒体类型
    :param cn_name: 识别出的中文名称
    :param en_name: 识别出的英文名称
    :param original_name: 应用识别词前解析出的名称
    :param year: 展示或匹配使用的年份；未知时为 None
    :param total_season: 识别出的总季数，未识别时为 0
    :param begin_season: 识别范围的起始季号
    :param end_season: 识别范围的结束季号
    :param total_episode: 识别出的总集数，未识别时为 0
    :param begin_episode: 识别范围的起始集号
    :param end_episode: 识别范围的结束集号
    :param part: 分卷或光盘标记，例如 Part、CD、DVD 或 Disc
    :param resource_type: 资源载体或发行类型，例如 BluRay、WEB-DL
    :param resource_effect: 资源画面或音效特征
    :param resource_pix: 资源分辨率标记
    :param resource_team: 发布组或字幕组名称
    :param customization: 自定义识别占位符内容
    :param web_source: 识别出的流媒体平台
    :param video_encode: 视频编码名称
    :param video_bit: 视频位深标记
    :param audio_encode: 音频编码名称
    :param apply_words: 实际应用的识别词记录，保持应用顺序
    :param media_source: 媒体主身份的数据来源，与 media_id 配对使用
    :param media_id: 当前来源的原生媒体标识，与 media_source 配对使用
    :param episode_group: 指定的剧集组标识
    :param fps: 视频每秒帧数；未知时为 None
    """

    kind: str
    isfile: bool
    title: str
    org_string: Optional[str]
    subtitle: Optional[str]
    type: str
    cn_name: Optional[str]
    en_name: Optional[str]
    original_name: Optional[str]
    year: Optional[Union[str, int]]
    total_season: int
    begin_season: Optional[int]
    end_season: Optional[int]
    total_episode: int
    begin_episode: Optional[int]
    end_episode: Optional[int]
    part: Optional[str]
    resource_type: Optional[str]
    resource_effect: Optional[str]
    resource_pix: Optional[str]
    resource_team: Optional[str]
    customization: Optional[str]
    web_source: Optional[str]
    video_encode: Optional[str]
    video_bit: Optional[str]
    audio_encode: Optional[str]
    apply_words: tuple[str, ...]
    media_source: Optional[str]
    media_id: Optional[str]
    episode_group: Optional[str]
    fps: Optional[int]

    @classmethod
    def from_meta(cls, meta: "MetaBase") -> "MetaInfoSnapshot":
        """
        从兼容的可变 MetaBase 对象提取不包含临时状态的完整契约。
        """
        media_type = meta.type.value if meta.type else ""
        media_source = meta.media_source.value if meta.media_source else None
        return cls(
            kind="anime" if type(meta).__name__ == "MetaAnime" else "video",
            isfile=bool(meta.isfile),
            title=meta.title or "",
            org_string=meta.org_string,
            subtitle=meta.subtitle,
            type=media_type,
            cn_name=meta.cn_name,
            en_name=meta.en_name,
            original_name=meta.original_name,
            year=meta.year,
            total_season=meta.total_season,
            begin_season=meta.begin_season,
            end_season=meta.end_season,
            total_episode=meta.total_episode,
            begin_episode=meta.begin_episode,
            end_episode=meta.end_episode,
            part=meta.part,
            resource_type=meta.resource_type,
            resource_effect=meta.resource_effect,
            resource_pix=meta.resource_pix,
            resource_team=meta.resource_team,
            customization=meta.customization,
            web_source=meta.web_source,
            video_encode=meta.video_encode,
            video_bit=meta.video_bit,
            audio_encode=meta.audio_encode,
            apply_words=tuple(meta.apply_words or ()),
            media_source=media_source,
            media_id=meta.media_id,
            episode_group=meta.episode_group,
            fps=meta.fps,
        )

    def to_dict(self) -> dict[str, Any]:
        """
        返回便于差异报告和序列化的独立字典。
        """
        return asdict(self)


@dataclass
class MetaBase(object):
    """
    媒体信息基类。

    :param isfile: 是否处理的文件
    :param title: 原标题字符串（未经过识别词处理）
    :ivar org_string: 识别用字符串（经过识别词处理后）
    :param subtitle: 副标题
    :ivar type: 类型 电影、电视剧
    :ivar cn_name: 识别的中文名
    :ivar en_name: 识别的英文名
    :ivar original_name: 未应用识别词时识别出的名称
    :ivar year: 年份
    :ivar total_season: 总季数
    :ivar begin_season: 识别的开始季 数字
    :ivar end_season: 识别的结束季 数字
    :ivar total_episode: 总集数
    :ivar begin_episode: 识别的开始集
    :ivar end_episode: 识别的结束集
    :ivar part: 分段或碟片标记，例如 Part、CD、DVD、Disk 或 Disc
    :ivar resource_type: 识别的资源类型
    :ivar resource_effect: 识别的效果
    :ivar resource_pix: 识别的分辨率
    :ivar resource_team: 识别的制作组/字幕组
    :ivar customization: 识别的自定义占位符
    :ivar web_source: 识别的流媒体平台
    :ivar video_encode: 视频编码
    :ivar video_bit: 视频位深
    :ivar audio_encode: 音频编码
    :ivar apply_words: 应用的识别词信息
    :ivar media_source: 媒体主身份；来源与ID必须成对使用
    :ivar media_id: 当前来源的原生媒体标识，与 media_source 配对使用
    :ivar episode_group: 指定的剧集组标识
    :ivar fps: 帧率信息（纯数值）
    """
    isfile: bool = False
    title: Optional[str] = ""
    org_string: Optional[str] = None
    subtitle: Optional[str] = None
    type: MediaType = MediaType.UNKNOWN
    cn_name: Optional[str] = None
    en_name: Optional[str] = None
    original_name: Optional[str] = None
    year: Optional[Union[str, int]] = None
    total_season: int = 0
    begin_season: Optional[int] = None
    end_season: Optional[int] = None
    total_episode: int = 0
    begin_episode: Optional[int] = None
    end_episode: Optional[int] = None
    part: Optional[str] = None
    resource_type: Optional[str] = None
    resource_effect: Optional[str] = None
    resource_pix: Optional[str] = None
    resource_team: Optional[str] = None
    customization: Optional[str] = None
    web_source: Optional[str] = None
    video_encode: Optional[str] = None
    video_bit: Optional[str] = None
    audio_encode: Optional[str] = None
    apply_words: Optional[List[str]] = None
    media_source: Optional[MediaSource] = None
    media_id: Optional[str] = None
    episode_group: Optional[str] = None
    fps: Optional[int] = None

    _subtitle_flag = False
    _title_episodel_re = r"Episode\s+(\d{1,4})"
    _subtitle_season_re = r"(?<!(?:不包含|不包括|不含)\s*)(?<![全共]\s*)[第\s]+([0-9一二三四五六七八九十S\-]+)\s*季(?!\s*[全共])"
    _subtitle_season_all_re = r"[全共]\s*([0-9一二三四五六七八九十]+)\s*季"
    _subtitle_episode_re = r"(?<!(?:不包含|不包括|不含)\s*)(?<![全共]\s*)[第\s]+([0-9一二三四五六七八九十百零EP]+)\s*[集话話期幕](?!\s*[全共])"
    _subtitle_episode_between_re = r"[第]*\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]?\s*-\s*第*\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]"
    _subtitle_episode_all_re = r"([0-9一二三四五六七八九十百零]+)\s*集\s*全|[全共]\s*([0-9一二三四五六七八九十百零]+)\s*[集话話期幕]"

    def __init__(self, title: str, subtitle: Optional[str] = None, isfile: bool = False) -> None:
        """
        保存原始标题、辅助描述和文件识别上下文。

        :param title: 原始标题；空标题不写入识别状态
        :param subtitle: 可补充识别信息的副标题，未提供时为 None
        :param isfile: 标题是否来自文件路径
        """
        if not title:
            return
        self.org_string = title.strip() if title else None
        self.subtitle = subtitle.strip() if subtitle else None
        self.isfile = isfile

    @property
    def name(self) -> str:
        """
        返回名称。
        """
        if self.cn_name and text_tools.is_all_chinese(self.cn_name):
            return self.cn_name
        elif self.en_name:
            return self.en_name
        elif self.cn_name:
            return self.cn_name
        return ""

    @name.setter
    def name(self, name: str) -> None:
        """
        设置名称。
        """
        if text_tools.is_all_chinese(name):
            self.cn_name = name
        else:
            self.en_name = name
            self.cn_name = None

    def init_subtitle(self, title_text: Optional[str]) -> None:
        """
        从副标题提取季集与资源信息。

        :param title_text: 待识别的副标题；空值时保持当前状态
        """
        if not title_text:
            return
        title_text = f" {title_text} "
        episode_str = TITLE_EPISODE_RE.search(title_text)
        if episode_str:
            if episode_str:
                try:
                    episode = int(episode_str.group(1))
                except Exception as err:
                    logger.debug(f'识别集失败：{str(err)} - {traceback.format_exc()}')
                    return
                if episode >= 10000:
                    return
                if self.begin_episode is None:
                    self.begin_episode = episode
                    self.total_episode = 1
                self.type = MediaType.TV
                self._subtitle_flag = True
        elif SUBTITLE_HAS_SEASON_EPISODE_RE.search(title_text):
            # 全x季 x季全
            season_all_str = SUBTITLE_SEASON_ALL_RE.search(title_text)
            if season_all_str:
                season_all = season_all_str.group(1)
                if not season_all:
                    season_all = season_all_str.group(2)
                if season_all and self.begin_season is None and self.begin_episode is None:
                    try:
                        self.total_season = int(cn2an.cn2an(season_all.strip(), mode='smart'))
                    except Exception as err:
                        logger.debug(f'识别季失败：{str(err)} - {traceback.format_exc()}')
                        return
                    self.begin_season = 1
                    self.end_season = self.total_season
                    self.type = MediaType.TV
                    self._subtitle_flag = True
                return
            # 第x季
            season_str = SUBTITLE_SEASON_RE.search(title_text)
            if season_str:
                seasons = season_str.group(1)
                if seasons:
                    seasons = seasons.upper().replace("S", "").strip()
                else:
                    return
                try:
                    end_season = None
                    if seasons.find('-') != -1:
                        seasons = seasons.split('-')
                        begin_season = int(cn2an.cn2an(seasons[0].strip(), mode='smart'))
                        if len(seasons) > 1:
                            end_season = int(cn2an.cn2an(seasons[1].strip(), mode='smart'))
                    else:
                        begin_season = int(cn2an.cn2an(seasons, mode='smart'))
                except Exception as err:
                    logger.debug(f'识别季失败：{str(err)} - {traceback.format_exc()}')
                    return
                if begin_season and begin_season > 100:
                    return
                if end_season and end_season > 100:
                    return
                if self.begin_season is None and isinstance(begin_season, int):
                    self.begin_season = begin_season
                    self.total_season = 1
                if self.begin_season is not None \
                        and self.end_season is None \
                        and isinstance(end_season, int) \
                        and end_season != self.begin_season:
                    self.end_season = end_season
                    self.total_season = (self.end_season - self.begin_season) + 1
                self.type = MediaType.TV
                self._subtitle_flag = True
            # 第x-x集 第x集-x集
            episode_between_str = SUBTITLE_EPISODE_BETWEEN_RE.search(title_text)
            if episode_between_str:
                episode_range = episode_between_str.groups()
                if episode_range:
                    begin_episode = episode_range[0]
                    end_episode = episode_range[1]
                else:
                    return
                try:
                    begin_episode = int(cn2an.cn2an(begin_episode.strip(), mode='smart'))
                    end_episode = int(cn2an.cn2an(end_episode.strip(), mode='smart'))
                except Exception as err:
                    logger.debug(f'识别集失败：{str(err)} - {traceback.format_exc()}')
                    return
                if begin_episode and begin_episode >= 10000:
                    return
                if end_episode and end_episode >= 10000:
                    return
                if self.begin_episode is None and isinstance(begin_episode, int):
                    self.begin_episode = begin_episode
                    self.total_episode = 1
                if self.begin_episode is not None \
                        and self.end_episode is None \
                        and isinstance(end_episode, int) \
                        and end_episode != self.begin_episode:
                    self.end_episode = end_episode
                    self.total_episode = (self.end_episode - self.begin_episode) + 1
                self.type = MediaType.TV
                self._subtitle_flag = True
                return
            # 第x集
            episode_str = SUBTITLE_EPISODE_RE.search(title_text)
            if episode_str:
                episodes = episode_str.group(1)
                if episodes:
                    episodes = episodes.upper().replace("E", "").replace("P", "").strip()
                else:
                    return
                try:
                    end_episode = None
                    if episodes.find('-') != -1:
                        episode_parts = episodes.split('-')
                        begin_episode = int(cn2an.cn2an(episode_parts[0].strip(), mode='smart'))
                        if len(episode_parts) > 1:
                            end_episode = int(cn2an.cn2an(episode_parts[1].strip(), mode='smart'))
                    else:
                        begin_episode = int(cn2an.cn2an(episodes, mode='smart'))
                except Exception as err:
                    logger.debug(f'识别集失败：{str(err)} - {traceback.format_exc()}')
                    return
                if begin_episode and begin_episode >= 10000:
                    return
                if end_episode and end_episode >= 10000:
                    return
                if self.begin_episode is None and isinstance(begin_episode, int):
                    self.begin_episode = begin_episode
                    self.total_episode = 1
                if self.begin_episode is not None \
                        and self.end_episode is None \
                        and isinstance(end_episode, int) \
                        and end_episode != self.begin_episode:
                    self.end_episode = end_episode
                    self.total_episode = (self.end_episode - self.begin_episode) + 1
                self.type = MediaType.TV
                self._subtitle_flag = True
                return
            # x集全/全x集
            episode_all_str = SUBTITLE_EPISODE_ALL_RE.search(title_text)
            if episode_all_str:
                episode_all = episode_all_str.group(1)
                if not episode_all:
                    episode_all = episode_all_str.group(2)
                if episode_all and self.begin_episode is None:
                    try:
                        self.total_episode = int(cn2an.cn2an(episode_all.strip(), mode='smart'))
                    except Exception as err:
                        logger.debug(f'识别集失败：{str(err)} - {traceback.format_exc()}')
                        return
                    self.type = MediaType.TV
                    self._subtitle_flag = True
                return
            # 01-26Fin 等数字范围+完结标记
            self.__init_episode_range_fin(title_text)
        else:
            # 副标题无中文季集标记时，仍识别 01-26Fin 等数字范围+完结标记
            self.__init_episode_range_fin(title_text)

    def __init_episode_range_fin(self, title_text: str) -> None:
        """
        识别 01-26Fin / [01-38 END] 等"数字范围+完结标记"格式的集数信息。
        """
        episode_range_str = SUBTITLE_EPISODE_RANGE_FIN_RE.search(title_text)
        if not episode_range_str:
            return
        try:
            begin_episode = int(episode_range_str.group(1))
            end_episode = int(episode_range_str.group(2))
        except Exception as err:
            logger.debug(f'识别集失败：{str(err)} - {traceback.format_exc()}')
            return
        if begin_episode < 1 or begin_episode > end_episode or end_episode >= 10000:
            return
        # 两个数字都落在常见年份区间时视为年份范围而非集数（如 2019-2020完结）
        if begin_episode >= 1900 and end_episode <= 2155:
            return
        if self.begin_episode is None:
            self.begin_episode = begin_episode
            self.end_episode = end_episode
            self.total_episode = end_episode - begin_episode + 1
            self.type = MediaType.TV
            self._subtitle_flag = True

    @property
    def season(self) -> Optional[str]:
        """
        返回开始季、结束季字符串，确定是剧集没有季的返回S01。
        """
        if self.begin_season is not None:
            return "S%s" % str(self.begin_season).rjust(2, "0") \
                if self.end_season is None \
                else "S%s-S%s" % \
                     (str(self.begin_season).rjust(2, "0"),
                      str(self.end_season).rjust(2, "0"))
        else:
            if self.type == MediaType.TV:
                return "S01"
            else:
                return ""

    @property
    def sea(self) -> str:
        """
        返回开始季字符串，确定是剧集没有季的返回空。
        """
        if self.begin_season is not None:
            return self.season or ""
        else:
            return ""

    @property
    def season_seq(self) -> str:
        """
        返回begin_season 的数字，电视剧没有季的返回1。
        """
        if self.begin_season is not None:
            return str(self.begin_season)
        else:
            if self.type == MediaType.TV:
                return "1"
            else:
                return ""

    @property
    def season_list(self) -> List[int]:
        """
        返回季的数组。
        """
        if self.begin_season is None:
            if self.type == MediaType.TV:
                return [1]
            else:
                return []
        elif self.end_season is not None:
            return [season for season in range(self.begin_season, self.end_season + 1)]
        else:
            return [self.begin_season]

    @property
    def episode(self) -> Optional[str]:
        """
        返回开始集、结束集字符串。
        """
        if self.begin_episode is not None:
            return "E%s" % str(self.begin_episode).rjust(2, "0") \
                if self.end_episode is None \
                else "E%s-E%s" % \
                     (
                         str(self.begin_episode).rjust(2, "0"),
                         str(self.end_episode).rjust(2, "0"))
        else:
            return ""

    @property
    def episode_list(self) -> List[int]:
        """
        返回集的数组。
        """
        if self.begin_episode is None:
            return []
        elif self.end_episode is not None:
            return [episode for episode in range(self.begin_episode, self.end_episode + 1)]
        else:
            return [self.begin_episode]

    @property
    def episodes(self) -> str:
        """
        返回集的并列表达方式，用于支持单文件多集。
        """
        return "E%s" % "E".join(str(episode).rjust(2, '0') for episode in self.episode_list)

    @property
    def episode_seqs(self) -> str:
        """
        返回单文件多集的集数表达方式，用于支持单文件多集。
        """
        episodes = self.episode_list
        if episodes:
            # 集 xx
            if len(episodes) == 1:
                return str(episodes[0])
            else:
                return "%s-%s" % (episodes[0], episodes[-1])
        else:
            return ""

    @property
    def episode_seq(self) -> str:
        """
        返回begin_episode 的数字。
        """
        episodes = self.episode_list
        if episodes:
            return str(episodes[0])
        else:
            return ""

    @property
    def season_episode(self) -> str:
        """
        返回季集字符串。
        """
        if self.type == MediaType.TV:
            seaion = self.season
            episode = self.episode
            if seaion and episode:
                return "%s %s" % (seaion, episode)
            elif seaion:
                return "%s" % seaion
            elif episode:
                return "%s" % episode
        else:
            return ""
        return ""

    @property
    def resource_term(self) -> str:
        """
        返回资源类型字符串，含分辨率。
        """
        ret_string = ""
        if self.resource_type:
            ret_string = f"{ret_string} {self.resource_type}"
        if self.resource_effect:
            ret_string = f"{ret_string} {self.resource_effect}"
        if self.resource_pix:
            ret_string = f"{ret_string} {self.resource_pix}"
        return ret_string.strip()

    @property
    def edition(self) -> str:
        """
        返回资源类型字符串，不含分辨率。
        """
        ret_string = ""
        if self.resource_type:
            ret_string = f"{ret_string} {self.resource_type}"
        if self.resource_effect:
            ret_string = f"{ret_string} {self.resource_effect}"
        return ret_string.strip()

    @property
    def release_group(self) -> str:
        """
        返回发布组/字幕组字符串。
        """
        if self.resource_team:
            return self.resource_team
        else:
            return ""

    @property
    def video_term(self) -> str:
        """
        返回视频编码。
        """
        return self.video_encode or ""

    @property
    def audio_term(self) -> str:
        """
        返回音频编码。
        """
        return self.audio_encode or ""

    @property
    def frame_rate(self) -> Optional[int]:
        """
        返回帧率信息，未识别时为 None。
        """
        return self.fps or None

    @staticmethod
    def extract_video_bit(value: Optional[str]) -> Optional[str]:
        """
        从标题或编码文本中提取视频位深标签。
        """
        if not value:
            return None
        bit_match = VIDEO_BIT_RE.search(value)
        if not bit_match:
            return None
        return f"{bit_match.group('bit')}bit"

    def is_in_season(self, season: Union[list[Union[int, str]], int, str]) -> bool:
        """
        判断当前识别结果是否覆盖指定季。

        :param season: 单季或季列表；未标季数的资源按第 1 季处理
        :return: 全部指定季均在识别范围内时为 True
        """
        begin_season = self.begin_season if self.begin_season is not None else 1
        if isinstance(season, list):
            if self.end_season is not None:
                meta_season = list(range(begin_season, self.end_season + 1))
            else:
                meta_season = [begin_season]

            return set(meta_season).issuperset(set(season))
        else:
            if self.end_season is not None:
                return begin_season <= int(season) <= self.end_season
            return int(season) == begin_season

    def is_in_episode(self, episode: Union[list[Union[int, str]], int, str]) -> bool:
        """
        判断当前识别结果是否覆盖指定集。

        :param episode: 单集或集列表
        :return: 全部指定集均在识别范围内时为 True
        """
        begin_episode = self.begin_episode
        if begin_episode is None:
            return isinstance(episode, list) and not episode
        if isinstance(episode, list):
            if self.end_episode is not None:
                meta_episode = list(range(begin_episode, self.end_episode + 1))
            else:
                meta_episode = [begin_episode]
            return set(meta_episode).issuperset(set(episode))
        else:
            if self.end_episode is not None:
                return begin_episode <= int(episode) <= self.end_episode
            return int(episode) == begin_episode

    def set_season(self, sea: Union[list[Union[int, str]], int, str]) -> None:
        """
        从数字文本、季号或季列表更新季范围。

        :param sea: 待设置的季信息；空值或非数字内容不更新
        """
        if not sea:
            return
        if isinstance(sea, list):
            if len(sea) == 1 and str(sea[0]).isdigit():
                self.begin_season = int(sea[0])
                self.end_season = None
            elif len(sea) > 1 and str(sea[0]).isdigit() and str(sea[-1]).isdigit():
                self.begin_season = int(sea[0])
                self.end_season = int(sea[-1])
        elif str(sea).isdigit():
            self.begin_season = int(sea)
            self.end_season = None

    def set_episode(self, ep: Union[list[Union[int, str]], int, str]) -> None:
        """
        从数字文本、集号或集列表更新集范围。

        :param ep: 待设置的集信息；空值或非数字内容不更新
        """
        if not ep:
            return
        if isinstance(ep, list):
            if len(ep) == 1 and str(ep[0]).isdigit():
                self.begin_episode = int(ep[0])
                self.end_episode = None
            elif len(ep) > 1 and str(ep[0]).isdigit() and str(ep[-1]).isdigit():
                self.begin_episode = int(ep[0])
                self.end_episode = int(ep[-1])
                self.total_episode = (self.end_episode - self.begin_episode) + 1
        elif str(ep).isdigit():
            self.begin_episode = int(ep)
            self.end_episode = None

    def set_episodes(self, begin: int, end: int) -> None:
        """
        设置集数起止范围，并在两端均有效时更新总集数。

        :param begin: 起始集数；零不覆盖当前值
        :param end: 结束集数；零不覆盖当前值
        """
        if begin:
            self.begin_episode = begin
        if end:
            self.end_episode = end
        if self.begin_episode and self.end_episode:
            self.total_episode = (self.end_episode - self.begin_episode) + 1

    def merge(self, meta: Self) -> None:
        """
        用另一份识别结果补齐当前缺失字段，保留已有媒体身份组合。

        :param meta: 用于补齐当前识别结果的父级元信息
        """
        if self.type == MediaType.UNKNOWN \
                and meta.type != MediaType.UNKNOWN:
            self.type = meta.type
        if not self.name:
            self.cn_name = meta.cn_name
            self.en_name = meta.en_name
        # 未应用识别词时识别出的名称
        if not self.original_name:
            self.original_name = meta.original_name
        if not self.year:
            self.year = meta.year
        if (self.type == MediaType.TV
                and self.begin_season is None):
            self.begin_season = meta.begin_season
            self.end_season = meta.end_season
            self.total_season = meta.total_season
        if (self.type == MediaType.TV
                and self.begin_episode is None):
            self.begin_episode = meta.begin_episode
            self.end_episode = meta.end_episode
            self.total_episode = meta.total_episode
        # 普通可选字段统一遵循文件优先、父目录补空，新增字段只维护一份策略。
        for field_name in _META_OPTIONAL_MERGE_FIELDS:
            if not getattr(self, field_name):
                setattr(self, field_name, getattr(meta, field_name))
        if not self.fps:
            self.fps = meta.fps
        # 媒体身份必须原子合并，不能将不同目录层级的来源和ID拼成一对
        current_source, current_id = resolve_media_identity(media=self)
        if current_source and current_id:
            self.media_source, self.media_id = current_source, current_id
        else:
            self.media_source, self.media_id = resolve_media_identity(media=meta)

    def to_dict(self) -> dict[str, object]:
        """
        转为包含季集、名称和版本派生字段的字典。

        :return: 当前识别状态及派生字段的浅拷贝
        """
        dicts = vars(self).copy()
        dicts["type"] = self.type.value if self.type else None
        dicts["season_episode"] = self.season_episode
        dicts["edition"] = self.edition
        dicts["name"] = self.name
        dicts["episode_list"] = self.episode_list
        return dicts
