import re
from copy import deepcopy
from functools import lru_cache
from typing import List, Tuple, Union, Dict, Optional

from app.domain.context import TorrentInfo, MediaInfo
from app.domain.filterrule import get_builtin_rule_set, parse_rule_group
from app.domain.metainfo import MetaInfo, clear_rust_parse_options_cache, _rust_parse_options
from app.runtime.log import logger
from app.modules import _ModuleBase
from app.runtime.extensions.registry.filter_rule import plugin_filter_rule_registry
from app.runtime.hostports.filterrules import filter_rule_group_port
from app.runtime.hostports.torrentanalysis import torrent_analysis_port
from app.schemas.filter import TorrentVerdict
from app.schemas.types import SystemConfigKey
from app.adapters.system import rust as rust_accel
from app.foundation import size as size_tools


_SIZE_UNIT = 1024 * 1024


@lru_cache(maxsize=1024)
def _compile_ignorecase(pattern: str) -> re.Pattern:
    """
    编译过滤规则正则。
    过滤规则在搜索/订阅中会被大量种子重复匹配，缓存编译结果能减少热路径开销；
    这里仍保留原有的 IGNORECASE 语义，非法正则也会像原来一样在匹配时抛出异常。
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
    解析大小范围，单位为 MB。
    返回值中的操作符只供本模块内部使用，避免每个种子重复拆分同一个规则。
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
    解析发布时间规则，避免同一规则对大量种子反复转换 float。
    """
    return tuple(float(t) for t in publish_time.split("-"))


class FilterModule(_ModuleBase):
    """
    过滤器模块，以内置和自定义规则实现候选种子分析契约，
    并按各分析器的合取判定筛选种子资源。
    """

    CONFIG_WATCH = {
        SystemConfigKey.CustomFilterRules.value,
        SystemConfigKey.CustomIdentifiers.value,
        SystemConfigKey.CustomReleaseGroups.value,
        SystemConfigKey.Customization.value,
    }

    # 内置规则引擎分析器的标识，写入判定结果供追溯是谁给出的结论。
    ANALYZER_ID = "filter"

    # 内置规则的只读快照，由 init_module 写入，方便查询工具区分内置规则与自定义规则。
    builtin_rule_set: Dict[str, dict] = {}
    # 运行期规则集 = 内置规则 + 插件规则覆盖 + 用户自定义规则覆盖。
    rule_set: Dict[str, dict] = {}

    def __init__(self) -> None:
        """
        初始化过滤器模块依赖的规则仓库。
        """
        super().__init__()
        self.rulehelper = filter_rule_group_port.resolve()
        # 用户自定义规则快照，由 init_module 从系统配置读入，插件登记变化时无需重读
        self._custom_rule_set: Dict[str, dict] = {}
        # 组装当前规则集时插件规则注册表的版本号，用于判断规则集是否过期
        self._plugin_rule_revision = plugin_filter_rule_registry.revision

    def init_module(self) -> None:
        """
        初始化过滤规则集，按内置 < 插件 < 用户的次序合并三层规则。
        """
        # 每次重载都先恢复为纯内置规则，避免旧的插件规则或自定义规则残留在内存里。
        self.builtin_rule_set = deepcopy(get_builtin_rule_set())
        self._custom_rule_set = self.__load_custom_rules()
        self.__compose_rule_set()

    def __load_custom_rules(self) -> Dict[str, dict]:
        """
        读取用户自定义规则。
        """
        custom_rule_set: Dict[str, dict] = {}
        for rule in self.rulehelper.get_custom_rules():
            logger.info(f"加载自定义规则 {rule.id} - {rule.name}")
            custom_rule_set[rule.id] = rule.model_dump()
        return custom_rule_set

    def __compose_rule_set(self) -> None:
        """
        按内置 < 插件 < 用户的次序组装运行期规则集。

        用户自定义永远赢：用户手改过的规则不能被装了个插件之后悄悄改掉。插件规则
        每次都从注册表实时取，因此插件停用后其规则在下一次组装时即消失。
        """
        rule_set = deepcopy(self.builtin_rule_set)
        revision = plugin_filter_rule_registry.revision
        rule_set.update(deepcopy(plugin_filter_rule_registry.rule_definitions()))
        rule_set.update(self._custom_rule_set)
        self.rule_set = rule_set
        self._plugin_rule_revision = revision

    def __refresh_plugin_rules(self) -> None:
        """
        插件规则登记发生变化时重新组装运行期规则集。

        插件的启停不经过模块重载路径，规则集因此需要一个自己的过期判据；版本号
        未变时不做任何事，判定只是一次整数比较。
        """
        if plugin_filter_rule_registry.revision != self._plugin_rule_revision:
            self.__compose_rule_set()

    @staticmethod
    def get_name() -> str:
        """
        获取模块名称。
        """
        return "过滤器"

    @staticmethod
    def get_priority() -> int:
        """
        获取模块优先级，数字越小优先级越高，只有同一接口下优先级才生效
        """
        return 4

    def stop(self) -> None:
        """停止模块"""
        clear_rust_parse_options_cache()

    def test(self) -> None:
        """
        测试过滤器模块状态。
        """
        pass

    def init_setting(self) -> Tuple[str, Union[str, bool]]:
        """
        返回过滤器模块启用配置。
        """
        pass

    def filter_torrents(self, rule_groups: List[str],
                        torrent_list: List[TorrentInfo],
                        mediainfo: MediaInfo = None) -> List[TorrentInfo]:
        """
        过滤种子资源

        判定由各分析器经 ``analyze_torrent_candidates`` 给出，按合取组合：
        候选必须被全部分析器判为通过才保留。排序权重取分发顺序中首个给出
        ``order`` 的分析器的取值（插件分析器先于内置分析器），全部未给出时
        候选原有的排序权重保持不变。

        插件实现 ``filter_torrents`` 时整体接管过滤，内置规则引擎不再参与；
        插件实现 ``analyze_torrent_candidates`` 时与内置规则引擎的判定合取组合。

        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  媒体信息
        :return: 过滤后的资源列表，添加资源优先级
        """
        verdict_groups = self.__collect_verdicts(rule_groups, torrent_list, mediainfo)
        if not verdict_groups:
            # 没有任何分析器给出判定，候选整体放行且不改动排序权重
            return torrent_list
        return self.__apply_verdicts(torrent_list, verdict_groups)

    def analyze_torrent_candidates(self, rule_groups: List[str],
                                   torrent_list: List[TorrentInfo],
                                   mediainfo: MediaInfo = None) -> Optional[List[TorrentVerdict]]:
        """
        按过滤规则组判定候选种子

        分析器契约的内置实现：输入与 ``filter_torrents`` 一致，输出为严格的判定列表，
        不改动候选自身。插件实现同名方法即成为并列的分析器，判定与本实现合取组合。

        :param rule_groups:  过滤规则组名称列表
        :param torrent_list:  资源列表
        :param mediainfo:  媒体信息
        :return: 与资源列表等长、按下标一一对应的判定列表；没有可用规则组时返回 None 表示不参与判定
        """
        if not rule_groups or not torrent_list:
            return None
        # 插件的启停不经过模块重载路径，判定前先确认规则集没有过期
        self.__refresh_plugin_rules()
        # 查询规则表详情
        groups = self.rulehelper.get_rule_group_by_media(media=mediainfo, group_names=rule_groups)
        if not groups:
            return None
        group_defs = [group.model_dump() if hasattr(group, "model_dump") else vars(group) for group in groups]
        orders = self.__match_orders(group_defs, torrent_list, mediainfo)
        group_names = "、".join(
            str(group.get("name") or group.get("rule_string") or "") for group in group_defs
        )
        return [
            TorrentVerdict(
                analyzer=self.ANALYZER_ID,
                passed=index in orders,
                reason=(
                    f"匹配过滤规则组 {group_names}"
                    if index in orders
                    else f"不匹配过滤规则组 {group_names}"
                ),
                order=orders.get(index),
            )
            for index in range(len(torrent_list))
        ]

    def __collect_verdicts(self, rule_groups: List[str],
                           torrent_list: List[TorrentInfo],
                           mediainfo: MediaInfo) -> List[List[TorrentVerdict]]:
        """
        收集各分析器对候选种子的判定。
        分析能力端口已注入时按多播收集全部分析器的判定，未注入时只运行内置分析器；
        长度与候选列表不一致的判定会被丢弃，避免判定与候选错位。
        """
        if torrent_analysis_port.registered:
            verdict_groups = torrent_analysis_port.resolve().analyze_torrent_candidates(
                rule_groups=rule_groups,
                torrent_list=torrent_list,
                mediainfo=mediainfo,
            )
        else:
            verdict_groups = [
                self.analyze_torrent_candidates(
                    rule_groups=rule_groups,
                    torrent_list=torrent_list,
                    mediainfo=mediainfo,
                )
            ]
        aligned = []
        for verdicts in verdict_groups or []:
            if not verdicts:
                continue
            if len(verdicts) != len(torrent_list):
                logger.warn(f"分析器 {verdicts[0].analyzer} 返回 {len(verdicts)} 条判定，"
                            f"与 {len(torrent_list)} 个候选不一致，已忽略该分析器")
                continue
            aligned.append(verdicts)
        return aligned

    @staticmethod
    def __apply_verdicts(torrent_list: List[TorrentInfo],
                         verdict_groups: List[List[TorrentVerdict]]) -> List[TorrentInfo]:
        """
        按合取组合各分析器的判定，保留全部通过的候选并写入排序权重。
        """
        ret_torrents = []
        for index, torrent in enumerate(torrent_list):
            verdicts = [verdict_group[index] for verdict_group in verdict_groups]
            rejected = [verdict for verdict in verdicts if not verdict.passed]
            if rejected:
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} {torrent.description or ''} 被过滤："
                             + "；".join(f"{verdict.analyzer} {verdict.reason}" for verdict in rejected))
                continue
            order = next((verdict.order for verdict in verdicts if verdict.order is not None), None)
            if order is not None:
                torrent.pri_order = order
            ret_torrents.append(torrent)
        return ret_torrents

    def __match_orders(self, groups: List[dict],
                       torrent_list: List[TorrentInfo],
                       mediainfo: MediaInfo = None) -> Dict[int, Optional[int]]:
        """
        计算命中全部规则组的候选下标及其排序权重，规则组未给出层级时权重为 None。
        """
        matched_orders = rust_accel.filter_torrents(
            groups=groups,
            torrent_list=torrent_list,
            rule_set=self.rule_set,
            mediainfo=mediainfo,
            metainfo_options=_rust_parse_options() if self.__needs_metainfo_options(groups) else None,
        )
        if matched_orders is None:
            return self.__match_orders_with_python(
                groups=groups,
                torrent_list=torrent_list,
                mediainfo=mediainfo
            )
        matched_orders, traces = self.__parse_rust_filter_result(matched_orders)
        self.__log_rust_filter_traces(traces)
        return dict(matched_orders)

    @staticmethod
    def __parse_rust_filter_result(result) -> Tuple[list, list]:
        """
        兼容新旧 Rust 过滤返回值，统一拆出匹配结果和调试日志。
        """
        if (
                isinstance(result, tuple)
                and len(result) == 2
                and isinstance(result[1], list)
        ):
            return result
        return result, []

    @staticmethod
    def __log_rust_filter_traces(traces: list) -> None:
        """
        输出 Rust 过滤路径返回的规则级调试日志。
        """
        for trace in traces:
            logger.debug(trace)

    def __match_orders_with_python(self, groups: List[dict],
                                   torrent_list: List[TorrentInfo],
                                   mediainfo: MediaInfo = None) -> Dict[int, Optional[int]]:
        """
        使用 Python 规则匹配计算排序权重，供 Rust 加速关闭或不可用时兜底。
        规则组按顺序逐轮收窄候选，排序权重以最后一个命中的规则组为准。
        """
        matched_indexes = list(range(len(torrent_list)))
        orders: Dict[int, int] = {}
        parsed_rule_cache = {}
        for group in groups:
            rule_string = group.get("rule_string")
            if not rule_string:
                continue
            group_orders = self.__match_group_orders(
                rule_string=rule_string,
                rule_name=group.get("name") or rule_string,
                torrent_list=torrent_list,
                candidate_indexes=matched_indexes,
                mediainfo=mediainfo,
                parsed_rule_cache=parsed_rule_cache,
            )
            orders.update(group_orders)
            matched_indexes = list(group_orders)
            if not matched_indexes:
                break
        return {index: orders.get(index) for index in matched_indexes}

    def __needs_metainfo_options(self, groups: List[dict]) -> bool:
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

    def __match_group_orders(self, rule_string: str, rule_name: str,
                             torrent_list: List[TorrentInfo],
                             candidate_indexes: List[int],
                             mediainfo: MediaInfo,
                             parsed_rule_cache: Dict[str, Union[list, str]]) -> Dict[int, int]:
        """
        按一个规则组匹配候选，返回命中的候选下标及其排序权重
        """
        # 只拆分一次规则层级；具体层级仍延迟到真正需要匹配时解析。
        rule_levels = [rule_level.strip() for rule_level in rule_string.split('>')]
        matched_orders: Dict[int, int] = {}
        for index in candidate_indexes:
            torrent = torrent_list[index]
            # 能命中优先级的才保留
            order = self.__get_order(torrent, rule_levels, mediainfo, parsed_rule_cache)
            if order is None:
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} {torrent.description or ''} "
                             f"不匹配 {rule_name} 过滤规则")
                continue
            matched_orders[index] = order

        return matched_orders

    def __get_order(self, torrent: TorrentInfo, rule_levels: List[str],
                    mediainfo: MediaInfo,
                    parsed_rule_cache: Dict[str, Union[list, str]]) -> Optional[int]:
        """
        获取种子匹配的规则优先级，值越大越优先，未匹配时返回None
        """
        # 优先级
        res_order = 100

        for rule_level in rule_levels:
            # 解析规则组
            parsed_group = self.__parse_rule_group(rule_level, parsed_rule_cache)
            if self.__match_group(torrent, parsed_group, mediainfo):
                # 出现匹配时中断
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 优先级为 {100 - res_order + 1}")
                return res_order
            # 优先级降低，继续匹配
            res_order -= 1

        return None

    @staticmethod
    def __parse_rule_group(rule_group: str,
                           parsed_rule_cache: Dict[str, Union[list, str]]) -> Union[list, str]:
        """
        解析单个优先级层级。
        缓存粒度放在层级表达式上，兼容多个规则组复用相同表达式的情况。
        """
        if rule_group not in parsed_rule_cache:
            parsed_rule_cache[rule_group] = parse_rule_group(rule_group)
        return parsed_rule_cache[rule_group]

    def __match_group(self, torrent: TorrentInfo, rule_group: Union[list, str],
                      mediainfo: MediaInfo) -> Optional[bool]:
        """
        判断种子是否匹配规则组
        """
        if not isinstance(rule_group, list):
            # 不是列表，说明是规则名称
            return self.__match_rule(torrent, rule_group, mediainfo)
        elif isinstance(rule_group, list) and len(rule_group) == 1:
            # 只有一个规则项
            return self.__match_group(torrent, rule_group[0], mediainfo)
        elif rule_group[0] == "not":
            # 非操作
            return not self.__match_group(torrent, rule_group[1:], mediainfo)
        elif rule_group[1] == "and":
            # 与操作
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                and self.__match_group(torrent, rule_group[2:], mediainfo)
        elif rule_group[1] == "or":
            # 或操作
            return self.__match_group(torrent, rule_group[0], mediainfo) \
                or self.__match_group(torrent, rule_group[2:], mediainfo)

    def __match_rule(self, torrent: TorrentInfo, rule_name: str,
                     mediainfo: MediaInfo) -> bool:
        """
        判断种子是否匹配规则项
        """
        rule = self.rule_set.get(rule_name)
        if not rule:
            # 规则不存在
            logger.debug(f"规则 {rule_name} 不存在")
            return False
        # TMDB规则
        tmdb = rule.get("tmdb")
        # 符合TMDB规则的直接返回True，即不过滤
        if tmdb and self.__match_tmdb(tmdb, mediainfo):
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 符合 {rule_name} 的TMDB规则，匹配成功")
            return True
        # 匹配项：标题、副标题、标签
        content = f"{torrent.title} {torrent.description} {' '.join(torrent.labels or [])}"
        # 只匹配指定关键字
        match_content = []
        matchs = rule.get("match") or []
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
        includes = rule.get("include") or []
        if not isinstance(includes, list):
            includes = [includes]
        # 排除规则项
        excludes = rule.get("exclude") or []
        if not isinstance(excludes, list):
            excludes = [excludes]
        # 大小范围规则项
        size_range = rule.get("size_range")
        # 做种人数规则项
        seeders = rule.get("seeders")
        # FREE规则
        downloadvolumefactor = rule.get("downloadvolumefactor")
        # 发布时间规则
        pubdate: str = rule.get("publish_time")
        if includes and not any(_regex_search(include, content) for include in includes):
            # 未发现任何包含项
            logger.debug(f"种子 {torrent.site_name} - {torrent.title} 不包含任何项 {includes}")
            return False
        for exclude in excludes:
            if _regex_search(exclude, content):
                # 发现排除项
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 包含 {exclude}")
                return False
        if size_range:
            if not self.__match_size(torrent, size_range):
                # 大小范围不匹配
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 大小 "
                             f"{size_tools.format_compact_size(torrent.size)} 不在范围 {size_range}MB")
                return False
        if seeders:
            if torrent.seeders < int(seeders):
                # 做种人数不匹配
                logger.debug(f"种子 {torrent.site_name} - {torrent.title} 做种人数 {torrent.seeders} 小于 {seeders}")
                return False
        if downloadvolumefactor is not None:
            if torrent.downloadvolumefactor != downloadvolumefactor:
                # FREE规则不匹配
                logger.debug(
                    f"种子 {torrent.site_name} - {torrent.title} FREE值 {torrent.downloadvolumefactor} 不是 {downloadvolumefactor}")
                return False
        if pubdate:
            # 种子发布时间
            pub_minutes = torrent.pub_minutes()
            # 发布时间规则
            pub_times = _parse_publish_time(pubdate)
            if len(pub_times) == 1:
                # 发布时间小于规则
                if pub_minutes < pub_times[0]:
                    logger.debug(
                        f"种子 {torrent.site_name} - {torrent.title} 发布时间 {pub_minutes} 小于 {pub_times[0]}")
                    return False
            else:
                # 区间
                if not (pub_times[0] <= pub_minutes <= pub_times[1]):
                    logger.debug(
                        f"种子 {torrent.site_name} - {torrent.title} 发布时间 {pub_minutes} 不在 {pub_times[0]}-{pub_times[1]} 时间区间")
                    return False

        return True

    @staticmethod
    def __match_tmdb(tmdb: dict, mediainfo: MediaInfo) -> bool:
        """
        判断种子是否匹配TMDB规则
        """

        def __get_media_value(key: str):
            try:
                return getattr(mediainfo, key)
            except ValueError:
                return ""

        if not mediainfo:
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

    @staticmethod
    def __match_size(torrent: TorrentInfo, size_range: str) -> bool:
        """
        判断种子是否匹配大小范围（MB），剧集拆分为每集大小
        """
        if not size_range:
            return True
        # 集数
        meta = MetaInfo(title=torrent.title, subtitle=torrent.description)
        episode_count = meta.total_episode or 1
        # 每集大小
        torrent_size = torrent.size / episode_count
        # 大小范围
        size_rule, size_min, size_max = _parse_size_range(size_range)
        if size_rule == "between":
            # 区间
            if size_min <= torrent_size <= size_max:
                return True
        elif size_rule == "gte":
            # 大于
            if torrent_size >= size_min:
                return True
        elif size_rule == "lte":
            # 小于
            if torrent_size <= size_max:
                return True
        return False
