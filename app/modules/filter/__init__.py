"""
按媒体条件和优先级规则筛选种子，协调 Rust 加速与 Python 回退。
"""

import re
from copy import deepcopy
from functools import lru_cache
from typing import Any, Dict, List, Optional, Tuple, Union, cast

from app.adapters.system import rust as rust_accel
from app.application.rules import BUILTIN_RULE_SET, RuleHelper, RuleParser
from app.domain.context import MediaInfo, TorrentInfo
from app.domain.metainfo import MetaInfo, _rust_parse_options, clear_rust_parse_options_cache
from app.foundation import size as size_tools
from app.modules import _ModuleBase
from app.runtime.log import logger
from app.schemas.types import ModuleType, OtherModulesType, SystemConfigKey

# 大小规则使用 MiB，匹配时换算为种子大小的字节单位。
_SIZE_UNIT = 1024 * 1024


@lru_cache(maxsize=1024)
def _compile_ignorecase(pattern: str) -> re.Pattern[str]:
    """
    缓存过滤规则的忽略大小写正则，减少重复匹配的编译开销。

    :param pattern: 规则中的正则表达式
    :return: 编译后的正则对象
    :raises re.error: 正则表达式不合法
    """
    return re.compile(r"%s" % pattern, re.IGNORECASE)


def _regex_search(pattern: Union[str, int, float], content: str) -> bool:
    """
    按原有字符串插值语义执行正则匹配，同时复用已编译表达式。
    """
    return bool(_compile_ignorecase(str(pattern)).search(content))


@lru_cache(maxsize=256)
def _parse_size_range(size_range: str) -> Tuple[str, float, Optional[float]]:
    """
    将以 MiB 表示的大小规则解析为字节边界。

    :param size_range: a-b 表示闭区间，>a 和 <a 分别表示含边界的下限和上限
    :return: 操作符、下限字节数和可选上限字节数；未知格式的操作符为 unknown
    :raises ValueError: 数值或区间格式不合法
    """
    size_range = size_range.strip()
    if size_range.find("-") != -1:
        size_min, size_max = size_range.split("-")
        return "between", float(size_min.strip()) * _SIZE_UNIT, float(size_max.strip()) * _SIZE_UNIT
    if size_range.startswith(">"):
        return "gte", float(size_range[1:].strip()) * _SIZE_UNIT, None
    if size_range.startswith("<"):
        return "lte", 0, float(size_range[1:].strip()) * _SIZE_UNIT
    return "unknown", 0, None


@lru_cache(maxsize=256)
def _parse_publish_time(publish_time: str) -> Tuple[float, ...]:
    """
    缓存发布时间规则的分钟数边界。

    :param publish_time: 单个数值表示最短发布时长，a-b 表示发布时长闭区间
    :return: 解析后的分钟数元组
    :raises ValueError: 边界不是有效数值
    """
    return tuple(float(t) for t in publish_time.split("-"))


class FilterModule(_ModuleBase):
    """
    过滤器模块，负责按内置和自定义规则筛选种子资源。
    """

    CONFIG_WATCH = {
        SystemConfigKey.CustomFilterRules.value,
        SystemConfigKey.CustomIdentifiers.value,
        SystemConfigKey.CustomReleaseGroups.value,
        SystemConfigKey.Customization.value,
    }

    # 保留一份只读内置规则定义，方便查询工具准确区分“内置规则”和“自定义规则”。
    builtin_rule_set: Dict[str, dict[str, Any]] = deepcopy(BUILTIN_RULE_SET)
    # 运行期规则集 = 内置规则 + 自定义规则覆盖。
    rule_set: Dict[str, dict[str, Any]] = {}

    def __init__(self) -> None:
        """
        初始化过滤器模块依赖的规则仓库。
        """
        super().__init__()
        self.rulehelper = RuleHelper()

    def init_module(self) -> None:
        """
        初始化过滤规则集，合并内置规则和用户自定义规则。
        """
        # 每次重载都先恢复为纯内置规则，避免旧的自定义规则残留在内存里。
        self.rule_set = deepcopy(self.builtin_rule_set)
        self.__init_custom_rules()

    def __init_custom_rules(self) -> None:
        """
        加载用户自定义规则，如跟内置规则冲突，以用户自定义规则为准。
        """
        custom_rules = self.rulehelper.get_custom_rules()
        for rule in custom_rules:
            logger.info(f"加载自定义规则 {rule.id} - {rule.name}")
            if rule.id:
                self.rule_set[rule.id] = rule.model_dump()

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "过滤器"

    @staticmethod
    def get_type() -> ModuleType:
        """
        获取模块类型。
        """
        return ModuleType.Other

    @staticmethod
    def get_subtype() -> OtherModulesType:
        """
        获取模块子类型。
        """
        return OtherModulesType.Filter

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效。
        """
        return 4

    def stop(self) -> None:
        """
        清理 Rust 元信息解析选项缓存，供下次初始化重新读取。
        """
        clear_rust_parse_options_cache()

    def test(self) -> None:
        """
        保留模块自检入口；过滤器不依赖外部服务，无需执行探测。
        """
        return None

    def init_setting(self) -> Optional[Tuple[str, Union[str, bool]]]:
        """
        保留模块配置入口；过滤器没有独立启用配置，返回 None。
        """
        pass

    def filter_torrents(self, rule_groups: List[str],
                        torrent_list: List[TorrentInfo],
                        mediainfo: Optional[MediaInfo] = None) -> List[TorrentInfo]:
        """
        按规则组依次筛选种子，并在原种子对象上写入优先级。

        优先调用 Rust 加速；加速不可用时回退到 Python。每组只接收上一组留下的种子，
        后续组可覆盖优先级。规则表达式或正则无效时，解析异常向调用方传播。

        :param rule_groups: 按执行顺序排列的规则组名称
        :param torrent_list: 待过滤种子；命中时原对象的 pri_order 会被更新
        :param mediainfo: 用于选择规则组和匹配 TMDB 条件的媒体信息，可为空
        :return: 保留的原种子对象列表；未指定或未找到规则组时返回原列表
        """
        if not rule_groups:
            return torrent_list
        groups = self.rulehelper.get_rule_group_by_media(media=mediainfo, group_names=rule_groups)
        if groups:
            group_defs: list[dict[str, Any]] = [
                group.model_dump() if hasattr(group, "model_dump") else vars(group)
                for group in groups
            ]
            matched_orders = rust_accel.filter_torrents(
                groups=group_defs,
                torrent_list=torrent_list,
                rule_set=self.rule_set,
                mediainfo=mediainfo,
                metainfo_options=_rust_parse_options() if self.__needs_metainfo_options(group_defs) else None,
            )
            if matched_orders is None:
                return self.__filter_torrents_with_python(
                    groups=group_defs,
                    torrent_list=torrent_list,
                    mediainfo=mediainfo
                )
            parsed_orders, traces = self.__parse_rust_filter_result(matched_orders)
            self.__log_rust_filter_traces(traces)
            ret_torrents: list[TorrentInfo] = []
            for index, pri_order in parsed_orders:
                torrent = torrent_list[index]
                torrent.pri_order = pri_order
                ret_torrents.append(torrent)
            return ret_torrents
        return torrent_list

    @staticmethod
    def __parse_rust_filter_result(
            result: object,
    ) -> Tuple[list[tuple[int, int]], list[Any]]:
        """
        兼容新旧 Rust 过滤返回值，统一拆出匹配结果和调试日志。
        """
        if (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[1], list)
        ):
            orders, traces = result
            parsed_orders = [
                (int(item[0]), int(item[1]))
                for item in orders
                if isinstance(item, (tuple, list)) and len(item) == 2
            ]
            parsed_traces = list(traces)
            return parsed_orders, parsed_traces
        if isinstance(result, list):
            parsed_orders = [
                (int(item[0]), int(item[1]))
                for item in result
                if isinstance(item, (tuple, list)) and len(item) == 2
            ]
            return parsed_orders, []
        return [], []

    @staticmethod
    def __log_rust_filter_traces(traces: list[Any]) -> None:
        """
        输出 Rust 过滤路径返回的规则级调试日志。
        """
        for trace in traces:
            logger.debug(trace)

    def __filter_torrents_with_python(self, groups: List[dict[str, Any]],
                                      torrent_list: List[TorrentInfo],
                                      mediainfo: Optional[MediaInfo] = None) -> List[TorrentInfo]:
        """
        依次执行规则组，复用本次调用的表达式解析缓存。

        每组只筛选上一组结果；一旦结果为空便停止。命中规则会修改原种子的优先级。
        """
        ret_torrents = torrent_list
        parser = RuleParser()
        parsed_rule_cache: Dict[str, Union[list[object], str]] = {}
        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            ret_torrents = self.__filter_torrents(
                rule_string=rule_string,
                rule_name=group.get("name") or rule_string,
                torrent_list=ret_torrents,
                mediainfo=mediainfo,
                parser=parser,
                parsed_rule_cache=parsed_rule_cache,
            )
            if not ret_torrents:
                break
        return ret_torrents

    def __needs_metainfo_options(self, groups: List[dict[str, Any]]) -> bool:
        """
        判断当前规则链是否会触发 size_range，避免无大小规则时读取 MetaInfo 运行配置。
        """
        rule_ids = set()
        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            rule_ids.update(re.findall(r"[A-Za-z][A-Za-z0-9]*|[0-9]+[A-Za-z][A-Za-z0-9]*", rule_string))
        return any(self.rule_set.get(rule_id, {}).get("size_range") for rule_id in rule_ids)

    def __filter_torrents(self, rule_string: str, rule_name: str,
                          torrent_list: List[TorrentInfo],
                          mediainfo: Optional[MediaInfo],
                          parser: RuleParser,
                          parsed_rule_cache: Dict[str, Any]) -> List[TorrentInfo]:
        """
        筛选命中任一优先级层级的种子，并保留输入顺序。

        每个种子的优先级由第一个命中的层级决定；解析结果复用调用方提供的缓存。
        """
        if not torrent_list:
            return []
        # 只拆分一次规则层级；具体层级仍延迟到真正需要匹配时解析。
        rule_groups = [rule_group.strip() for rule_group in rule_string.split('>')]
        ret_torrents = []
        for torrent in torrent_list:
            if not self.__get_order(torrent, rule_groups, mediainfo, parser, parsed_rule_cache):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} {torrent.description or ''} "
                             f"不匹配 {rule_name} 过滤规则")
                continue
            ret_torrents.append(torrent)

        return ret_torrents

    def __get_order(self, torrent: TorrentInfo, rule_groups: List[str],
                    mediainfo: Optional[MediaInfo], parser: RuleParser,
                    parsed_rule_cache: Dict[str, Any]) -> Optional[TorrentInfo]:
        """
        按层级顺序匹配种子，在原对象上记录首个命中的优先级。

        首层优先级为 100，后续每层减 1；数值越大越优先。

        :return: 命中的原种子对象；没有命中时返回 None
        """
        res_order = 100
        matched = False

        for rule_group in rule_groups:
            parsed_group = self.__parse_rule_group(rule_group, parser, parsed_rule_cache)
            if self.__match_group(torrent, parsed_group, mediainfo):
                matched = True
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 优先级为 {100 - res_order + 1}")
                torrent.pri_order = res_order
                break
            res_order -= 1

        return None if not matched else torrent

    @staticmethod
    def __parse_rule_group(rule_group: str, parser: RuleParser,
                           parsed_rule_cache: Dict[str, Any]) -> Any:
        """
        解析单个优先级层级，并将结果写入调用方缓存。

        以层级表达式为缓存键，使同一次筛选中的不同规则组可复用解析结果。
        """
        if rule_group not in parsed_rule_cache:
            parsed_rule_cache[rule_group] = parser.parse(rule_group).as_list()[0]
        return parsed_rule_cache[rule_group]

    def __match_group(self, torrent: TorrentInfo, rule_group: Any,
                      mediainfo: Optional[MediaInfo]) -> Optional[bool]:
        """
        递归计算规则项以及 not、and、or 组成的布尔表达式。

        :return: 合法表达式的匹配结果；未识别的表达式结构可能返回 None
        """
        if not isinstance(rule_group, list):
            return self.__match_rule(torrent, rule_group, mediainfo)
        if not rule_group:
            return False
        if len(rule_group) == 1:
            return self.__match_group(torrent, rule_group[0], mediainfo)
        elif rule_group[0] == "not":
            return not self.__match_group(torrent, rule_group[1:], mediainfo)
        elif rule_group[1] == "and":
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                and self.__match_group(torrent, rule_group[2:], mediainfo)
        elif rule_group[1] == "or":
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                or self.__match_group(torrent, rule_group[2:], mediainfo)
        return None

    def __match_rule(self, torrent: TorrentInfo, rule_name: str,
                     mediainfo: Optional[MediaInfo]) -> bool:
        """
        匹配单条规则的媒体条件、文本、大小、做种人数和发布时间。

        TMDB 条件命中时直接通过，不再检查该规则的其他条件。否则逐项过滤：
        包含项至少命中一个，排除项不能命中；下载流量倍率要求完全相等。
        默认匹配标题、副标题和标签；指定字段没有可用值时仍回退到默认匹配内容。
        """
        rule = self.rule_set.get(rule_name)
        if not rule:
            logger.debug(f"规则 {rule_name} 不存在")
            return False
        tmdb = rule.get("tmdb")
        if tmdb and self.__match_tmdb(tmdb, mediainfo):
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 符合 {rule_name} 的TMDB规则，匹配成功")
            return True
        content = f"{torrent.title} {torrent.description} {' '.join(torrent.labels or [])}"
        match_content: list[str] = []
        matchs = rule.get("match") or []
        if matchs:
            for match in matchs:
                if not hasattr(torrent, match):
                    continue
                match_value = getattr(torrent, match)
                if not match_value:
                    continue
                if isinstance(match_value, list):
                    match_content.extend(str(value) for value in match_value)
                else:
                    match_content.append(str(match_value))
        if match_content:
            content = " ".join(match_content)
        includes = rule.get("include") or []
        if not isinstance(includes, list):
            includes = [includes]
        excludes = rule.get("exclude") or []
        if not isinstance(excludes, list):
            excludes = [excludes]
        size_range = rule.get("size_range")
        seeders = rule.get("seeders")
        downloadvolumefactor = rule.get("downloadvolumefactor")
        pubdate = cast(str, rule.get("publish_time") or "")
        if includes and not any(_regex_search(include, content) for include in includes):
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 不包含任何项 {includes}")
            return False
        for exclude in excludes:
            if _regex_search(exclude, content):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 包含 {exclude}")
                return False
        if size_range:
            if not self.__match_size(torrent, size_range):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 大小 "
                             f"{size_tools.format_compact_size(torrent.size)} 不在范围 {size_range}MB")
                return False
        if seeders:
            if torrent.seeders < int(seeders):
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 做种人数 {torrent.seeders} 小于 {seeders}")
                return False
        if downloadvolumefactor is not None:
            if torrent.downloadvolumefactor != downloadvolumefactor:
                logger.debug(
                    f"种子 {torrent.site_name} - {torrent.title} FREE值 {torrent.downloadvolumefactor} 不是 {downloadvolumefactor}")
                return False
        if pubdate:
            pub_minutes = torrent.pub_minutes()
            pub_times = _parse_publish_time(pubdate)
            if len(pub_times) == 1:
                if pub_minutes < pub_times[0]:
                    logger.debug(
                        f"种子 {torrent.site_name} - {torrent.title} 发布时间 {pub_minutes} 小于 {pub_times[0]}")
                    return False
            else:
                if not (pub_times[0] <= pub_minutes <= pub_times[1]):
                    logger.debug(
                        f"种子 {torrent.site_name} - {torrent.title} 发布时间 {pub_minutes} 不在 {pub_times[0]}-{pub_times[1]} 时间区间")
                    return False

        return True

    @staticmethod
    def __match_tmdb(tmdb: dict[str, Any], mediainfo: Optional[MediaInfo]) -> bool:
        """
        检查媒体属性是否满足规则中的全部非空 TMDB 条件。

        忽略大小写，逗号分隔条件的候选值；制作国家读取 iso_3166_1 字段。

        :return: 每个条件与对应媒体属性至少有一个值相交时返回 True；没有媒体信息时返回 False
        """

        def __get_media_value(key: str) -> Any:
            """
            读取过滤表达式需要的媒体属性，并沿用无效属性的回退行为。
            """
            try:
                return getattr(mediainfo, key)
            except ValueError:
                return ""

        if not mediainfo:
            return False

        for attr, value in tmdb.items():
            if not value:
                continue
            info_value = __get_media_value(attr)
            if not info_value:
                return False
            elif attr == "production_countries":
                info_values = [str(val.get("iso_3166_1")).upper() for val in info_value]
            else:
                if isinstance(info_value, list):
                    info_values = [str(val).upper() for val in info_value]
                else:
                    info_values = [str(info_value).upper()]
            if value.find(",") != -1:
                values = [str(val).upper() for val in value.split(",") if val]
            else:
                values = [str(value).upper()]
            if not set(values).intersection(set(info_values)):
                return False

        return True

    @staticmethod
    def __match_size(torrent: TorrentInfo, size_range: str) -> bool:
        """
        按单集平均字节数匹配以 MiB 配置的大小边界。

        :param torrent: 待判断种子；根据标题和副标题解析集数，无法识别时按 1 集计算
        :param size_range: a-b、>a 或 <a；全部包含边界，空规则不限制大小
        :return: 大小符合规则时返回 True，未知规则格式返回 False
        """
        if not size_range:
            return True
        meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description)
        episode_count = meta.total_episode or 1
        torrent_size = torrent.size / episode_count
        size_rule, size_min, size_max = _parse_size_range(size_range)
        if size_rule == "between":
            if size_max is not None and size_min <= torrent_size <= size_max:
                return True
        elif size_rule == "gte":
            if torrent_size >= size_min:
                return True
        elif size_rule == "lte":
            if size_max is not None and torrent_size <= size_max:
                return True
        return False
