import re
from typing import List, Tuple, Union, Dict, Optional

from app.core.context import TorrentInfo, MediaInfo
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules import _ModuleBase
from app.modules.filter.RuleParser import RuleParser


class FilterModule(_ModuleBase):
    # 规则解析器
    parser: RuleParser = None
    # 媒体信息
    media: MediaInfo = None

    # 内置规则集
    rule_set: Dict[str, dict] = {
        # 蓝光原盘
        "BLU": {
            "include": [r'Blu-?Ray.+VC-?1|Blu-?Ray.+AVC|UHD.+blu-?ray.+HEVC|MiniBD'],
            "exclude": [r'[Hx].?264|[Hx].?265|WEB-?DL|WEB-?RIP|REMUX']
        },
        # 4K
        "4K": {
            "include": [r'4k|2160p|x2160'],
            "exclude": []
        },
        # 1080P
        "1080P": {
            "include": [r'1080[pi]|x1080'],
            "exclude": []
        },
        # 720P
        "720P": {
            "include": [r'720[pi]|x720'],
            "exclude": []
        },
        # 中字
        "CNSUB": {
            "include": [
                r'[中国國繁简](/|\s|\\|\|)?[繁简英粤]|[英简繁](/|\s|\\|\|)?[中繁简]'
                r'|繁體|简体|[中国國][字配]|国语|國語|中文|中字|简日|繁日|简繁|繁体'
                r'|([\s,.-\[])(CHT|CHS|cht|chs)(|[\s,.-\]])'],
            "exclude": [],
            "tmdb": {
                "original_language": "zh,cn"
            }
        },
        # 官种
        "GZ": {
            "include": [r'官方', r'官种'],
            "match": ["labels"]
        },
        # 特效字幕
        "SPECSUB": {
            "include": [r'特效'],
            "exclude": []
        },
        # BluRay
        "BLURAY": {
            "include": [r'Blu-?Ray'],
            "exclude": []
        },
        # UHD
        "UHD": {
            "include": [r'UHD|UltraHD'],
            "exclude": []
        },
        # H265
        "H265": {
            "include": [r'[Hx].?265|HEVC'],
            "exclude": []
        },
        # H264
        "H264": {
            "include": [r'[Hx].?264|AVC'],
            "exclude": []
        },
        # 杜比视界
        "DOLBY": {
            "include": [r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+|杜比视界"],
            "exclude": []
        },
        # 杜比全景声
        "ATMOS": {
            "include": [r"Dolby[\s.+]+Atmos|Atmos|杜比全景[声聲]"],
            "exclude": []
        },
        # HDR
        "HDR": {
            "include": [r"[\s.]+HDR[\s.]+|HDR10|HDR10\+"],
            "exclude": []
        },
        # SDR
        "SDR": {
            "include": [r"[\s.]+SDR[\s.]+"],
            "exclude": []
        },
        # 重编码
        "REMUX": {
            "include": [r'REMUX'],
            "exclude": []
        },
        # WEB-DL
        "WEBDL": {
            "include": [r'WEB-?DL|WEB-?RIP'],
            "exclude": []
        },
        # 免费
        "FREE": {
            "downloadvolumefactor": 0
        },
        # 国语配音
        "CNVOI": {
            "include": [r'[国國][语語]配音|[国國]配|[国國][语語]'],
            "exclude": []
        },
        # 粤语配音
        "HKVOI": {
            "include": [r'粤语配音|粤语'],
            "exclude": []
        },
        # 60FPS
        "60FPS": {
            "include": [r'60fps'],
            "exclude": []
        },
        # 3D
        "3D": {
            "include": [r'3D'],
            "exclude": []
        },
    }

    def init_module(self) -> None:
        self.parser = RuleParser()

    @staticmethod
    def get_name() -> str:
        return "过滤器"

    def stop(self):
        pass

    def test(self):
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        pass

    def filter_torrents(self, rule_string: str,
                        torrent_list: List[TorrentInfo],
                        season_episodes: Dict[int, list] = None,
                        mediainfo: MediaInfo = None) -> List[TorrentInfo]:
        """
        过滤种子资源
        :param rule_string:  过滤规则
        :param torrent_list:  资源列表
        :param season_episodes:  季集数过滤 {season:[episodes]}
        :param mediainfo:  媒体信息
        :return: 过滤后的资源列表，添加资源优先级
        """
        if not rule_string:
            return torrent_list
        self.media = mediainfo
        # 返回种子列表
        ret_torrents = []
        for torrent in torrent_list:
            # 季集数过滤
            if season_episodes \
                    and not self.__match_season_episodes(torrent, season_episodes):
                continue
            # 能命中优先级的才返回
            if not self.__get_order(torrent, rule_string):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} {torrent.description} 不匹配优先级规则")
                continue
            ret_torrents.append(torrent)

        return ret_torrents

    @staticmethod
    def __match_season_episodes(torrent: TorrentInfo, season_episodes: Dict[int, list]):
        """
        判断种子是否匹配季集数
        """
        # 匹配季
        seasons = season_episodes.keys()
        meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
        # 种子季
        torrent_seasons = meta.season_list
        if not torrent_seasons:
            # 按第一季处理
            torrent_seasons = [1]
        # 种子集
        torrent_episodes = meta.episode_list
        if not set(torrent_seasons).issubset(set(seasons)):
            # 种子季不在过滤季中
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 包含季 {torrent_seasons} 不是需要的季 {list(seasons)}")
            return False
        if not torrent_episodes:
            # 整季按匹配处理
            return True
        if len(torrent_seasons) == 1:
            need_episodes = season_episodes.get(torrent_seasons[0])
            if need_episodes \
                    and not set(torrent_episodes).intersection(set(need_episodes)):
                # 单季集没有交集的不要
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} "
                            f"集 {torrent_episodes} 没有需要的集：{need_episodes}")
                return False
        return True

    def __get_order(self, torrent: TorrentInfo, rule_str: str) -> Optional[TorrentInfo]:
        """
        获取种子匹配的规则优先级，值越大越优先，未匹配时返回None
        """
        # 多级规则
        rule_groups = rule_str.split('>')
        # 优先级
        res_order = 100
        # 是否匹配
        matched = False

        for rule_group in rule_groups:
            # 解析规则组
            parsed_group = self.parser.parse(rule_group.strip())
            if self.__match_group(torrent, parsed_group.as_list()[0]):
                # 出现匹配时中断
                matched = True
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 优先级为 {100 - res_order + 1}")
                torrent.pri_order = res_order
                break
            # 优先级降低，继续匹配
            res_order -= 1

        return None if not matched else torrent

    def __match_group(self, torrent: TorrentInfo, rule_group: Union[list, str]) -> bool:
        """
        判断种子是否匹配规则组
        """
        if not isinstance(rule_group, list):
            # 不是列表，说明是规则名称
            return self.__match_rule(torrent, rule_group)
        elif isinstance(rule_group, list) and len(rule_group) == 1:
            # 只有一个规则项
            return self.__match_group(torrent, rule_group[0])
        elif rule_group[0] == "not":
            # 非操作
            return not self.__match_group(torrent, rule_group[1:])
        elif rule_group[1] == "and":
            # 与操作
            return self.__match_group(torrent, rule_group[0]) and self.__match_group(torrent, rule_group[2:])
        elif rule_group[1] == "or":
            # 或操作
            return self.__match_group(torrent, rule_group[0]) or self.__match_group(torrent, rule_group[2:])

    def __match_rule(self, torrent: TorrentInfo, rule_name: str) -> bool:
        """
        判断种子是否匹配规则项
        """
        if not self.rule_set.get(rule_name):
            # 规则不存在
            return False
        # TMDB规则
        tmdb = self.rule_set[rule_name].get("tmdb")
        # 符合TMDB规则的直接返回True，即不过滤
        if tmdb and self.__match_tmdb(tmdb):
            return True
        # 匹配项：标题、副标题、标签
        content = f"{torrent.title} {torrent.description} {' '.join(torrent.labels or [])}"
        # 只匹配指定关键字
        match_content = []
        matchs = self.rule_set[rule_name].get("match") or []
        if matchs:
            for match in matchs:
                if not hasattr(torrent, match):
                    continue
                match_value = getattr(torrent, match)
                if not match_value:
                    continue
                if isinstance(match_value, list):
                    match_content.extend(match_value)
                else:
                    match_content.append(match_value)
        if match_content:
            content = " ".join(match_content)
        # 包含规则项
        includes = self.rule_set[rule_name].get("include") or []
        # 排除规则项
        excludes = self.rule_set[rule_name].get("exclude") or []
        # FREE规则
        downloadvolumefactor = self.rule_set[rule_name].get("downloadvolumefactor")
        for include in includes:
            if not re.search(r"%s" % include, content, re.IGNORECASE):
                # 未发现包含项
                return False
        for exclude in excludes:
            if re.search(r"%s" % exclude, content, re.IGNORECASE):
                # 发现排除项
                return False
        if downloadvolumefactor is not None:
            if torrent.downloadvolumefactor != downloadvolumefactor:
                # FREE规则不匹配
                return False
        return True

    def __match_tmdb(self, tmdb: dict) -> bool:
        """
        判断种子是否匹配TMDB规则
        """
        def __get_media_value(key: str):
            try:
                return getattr(self.media, key)
            except ValueError:
                return ""

        if not self.media:
            return False

        for attr, value in tmdb.items():
            if not value:
                continue
            # 获取media信息的值
            info_value = __get_media_value(attr)
            if not info_value:
                # 没有该值，不匹配
                return False
            elif attr == "production_countries":
                # 国家信息
                info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
            else:
                # media信息转化为数组
                if isinstance(info_value, list):
                    info_values = [str(val).upper() for val in info_value]
                else:
                    info_values = [str(info_value).upper()]
            # 过滤值转化为数组
            if value.find(",") != -1:
                values = [str(val).upper() for val in value.split(",") if val]
            else:
                values = [str(value).upper()]
            # 没有交集为不匹配
            if not set(values).intersection(set(info_values)):
                return False

        return True
