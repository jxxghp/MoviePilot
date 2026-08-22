"""字符串模块方法协议的可检查契约清单。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModuleResultAggregation(StrEnum):
    """描述多模块结果沿调用链的聚合方式。"""

    LEGACY = "legacy"
    PIPELINE = "pipeline"


@dataclass(frozen=True, slots=True)
class ModuleMethodContract:
    """记录一个模块方法族的调用模式和结果规则。"""

    family: str
    aggregation: ModuleResultAggregation = ModuleResultAggregation.LEGACY
    supports_sync: bool = True
    supports_async: bool = True
    plugin_short_circuit: bool = True


@dataclass(frozen=True, slots=True)
class MultiSourceCapabilityContract:
    """记录一个多来源能力的应答来源、让出方式、收窄开关与结果取用规则。"""

    method: str
    sources: tuple[str, ...]
    abstain: str
    narrowing: tuple[tuple[str, str], ...]
    arbitration: str


_DEFAULT_CONTRACT = ModuleMethodContract(family="legacy")

# 由多类来源共同应答的能力，其应答协议不体现在方法签名上，登记于此供实现方与调用方共同遵循。
_MULTI_SOURCE_CONTRACTS = {
    "media_exists": MultiSourceCapabilityContract(
        method="media_exists",
        sources=(
            "媒体服务器：Emby、Jellyfin、Plex、TrimeMedia、Ugreen、ZSpace、Navidrome，按各自库中的条目应答",
            "文件系统：medialibrary 按标准媒体库结构扫描已入库文件应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖库中没有该媒体，也涵盖被收窄开关排除在外；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("server", "媒体服务器专有：仅同名媒体服务器应答，其余媒体服务器与文件系统来源全部让出"),
            ("itemid", "媒体服务器专有：媒体服务器条目 ID，文件系统来源收到后直接忽略"),
            ("LOCAL_EXISTS_SEARCH", "文件系统专有：关闭时文件系统来源一律让出，媒体服务器来源不受影响"),
        ),
        arbitration=(
            "电视剧收齐全部来源答案后按季号取已存在集的并集，媒体库标识沿用最高优先级来源；"
            "电影与音乐取首个非空答案；"
            "同一模块下的多台同类型服务器由模块自行仲裁，对外只出一个答案"
        ),
    ),
    "match_media": MultiSourceCapabilityContract(
        method="match_media",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答",
            "豆瓣：DoubanModule 按 source=Douban 应答",
            "插件：模块自带 match_media 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖本来源未匹配到媒体信息；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "person_detail": MultiSourceCapabilityContract(
        method="person_detail",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答",
            "豆瓣：DoubanModule 按 source=Douban 应答",
            "Bangumi：BangumiModule 按 source=Bangumi 应答",
            "AniList：AniListModule 按 source=AniList 应答",
            "插件：模块自带 person_detail 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖收窄开关排除在外；"
            "返回空列表会被视为已认领而短路，因此非本来源必须返回 None；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "person_credits": MultiSourceCapabilityContract(
        method="person_credits",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答",
            "豆瓣：DoubanModule 按 source=Douban 应答",
            "Bangumi：BangumiModule 按 source=Bangumi 应答",
            "AniList：AniListModule 按 source=AniList 应答",
            "插件：模块自带 person_credits 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖收窄开关排除在外；"
            "返回空列表会被视为已认领而短路，因此非本来源必须返回 None；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "media_credits": MultiSourceCapabilityContract(
        method="media_credits",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答",
            "豆瓣：DoubanModule 按 source=Douban 应答",
            "Bangumi：BangumiModule 按 source=Bangumi 应答",
            "AniList：AniListModule 按 source=AniList 应答",
            "插件：模块自带 media_credits 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖 media_id 为空或无法转换为"
            "本来源要求的ID类型；返回空列表会被视为已认领而短路，因此非本来源必须返回 None；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "media_recommend": MultiSourceCapabilityContract(
        method="media_recommend",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答",
            "豆瓣：DoubanModule 按 source=Douban 应答",
            "Bangumi：BangumiModule 按 source=Bangumi 应答",
            "AniList：AniListModule 按 source=AniList 应答",
            "插件：模块自带 media_recommend 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖 media_id 为空或无法转换为"
            "本来源要求的ID类型；返回空列表会被视为已认领而短路，因此非本来源必须返回 None；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "media_similar": MultiSourceCapabilityContract(
        method="media_similar",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答，是当前唯一内建实现来源",
            "插件：模块自带 media_similar 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖 media_id 为空或无法转换为"
            "本来源要求的ID类型；返回空列表会被视为已认领而短路，因此非本来源必须返回 None；"
            "调度据此继续询问下一来源；豆瓣、Bangumi、AniList 均未实现本方法，不会进入能力索引"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "media_detail": MultiSourceCapabilityContract(
        method="media_detail",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答，接受 mtype 与 season",
            "豆瓣：DoubanModule 按 source=Douban 应答，接受 mtype，不支持 season",
            "Bangumi：BangumiModule 按 source=Bangumi 应答，不支持 mtype、season",
            "AniList：AniListModule 按 source=AniList 应答，不支持 mtype、season",
            "TVDB：TheTvDbModule 按 source=TVDB 应答，不支持 mtype、season；本模块只有同步原生"
            "实现，async_media_detail 经 run_in_threadpool 包装同步方法",
            "插件：模块自带 media_detail 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖 media_id 为空或无法转换为"
            "本来源要求的ID类型；调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出，其余不支持的参数各来源就地丢弃"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "discover": MultiSourceCapabilityContract(
        method="discover",
        sources=(
            "TMDB：TheMovieDbModule 按 source=TMDB 应答，筛选条件原样转发给 tmdb_discover",
            "豆瓣：DoubanModule 按 source=Douban 应答，筛选条件原样转发给 douban_discover",
            "Bangumi：BangumiModule 按 source=Bangumi 应答，筛选条件原样转发给 bangumi_discover",
            "AniList：AniListModule 按 source=AniList 应答，筛选条件原样转发给 anilist_discover",
            "插件：模块自带 discover 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，仅涵盖 source 非本来源；"
            "筛选条件（criteria）按各来源原方法签名原样转发，本契约不为任何条件补默认值，"
            "本来源必填条件缺失时由被委托的原方法自身抛出异常，而非静默返回 None 或默认结果；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "唯一收窄键：非本来源一律让出，其余条件原样转发不做归一"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
    "discover_board": MultiSourceCapabilityContract(
        method="discover_board",
        sources=(
            "豆瓣：DoubanModule 按 source=Douban 应答，支持 movie_showing/movie_hot/movie_top250/"
            "tv_hot/tv_animation/tv_weekly_chinese/tv_weekly_global 共7个榜单，接受 page 与 count",
            "TMDB：TheMovieDbModule 按 source=TMDB 应答，仅支持 trending 榜单，只接受 page",
            "Bangumi：BangumiModule 按 source=Bangumi 应答，仅支持 calendar 榜单，不接受分页参数",
            "AniList：AniListModule 按 source=AniList 应答，支持 trending/popular_this_season 两个"
            "榜单，接受 page 与 count",
            "插件：模块自带 discover_board 实现按 source 自认领应答",
        ),
        abstain=(
            "返回 None 表示本来源不认领，既涵盖 source 非本来源，也涵盖 board 未命中本来源榜单白名单；"
            "白名单校验先于方法查找完成，未登记标识不会触发任意方法调用；"
            "返回空列表会被视为已认领而短路，因此非本来源与未登记榜单都必须返回 None；"
            "调度据此继续询问下一来源"
        ),
        narrowing=(
            ("source", "收窄键之一：非本来源一律让出"),
            ("board", "收窄键之一：须命中本来源榜单白名单，否则让出；"
                      "各来源只下传自己认得的分页参数，其余就地丢弃"),
        ),
        arbitration="首个非空答案即为最终答案；插件提供者先于内建模块被询问",
    ),
}

# 首批登记高频能力族。方法名仍保持开放字符串，以兼容第三方插件自定义模块能力；
# 未命中项继续使用冻结的 legacy 规则，并由架构快照记录新增调用位置。
_METHOD_CONTRACTS = {
    "recognize_media": ModuleMethodContract(family="media-recognition"),
    "search_medias": ModuleMethodContract(family="media-recognition"),
    "obtain_images": ModuleMethodContract(
        family="media-recognition", aggregation=ModuleResultAggregation.PIPELINE
    ),
    "async_obtain_images": ModuleMethodContract(
        family="media-recognition", aggregation=ModuleResultAggregation.PIPELINE
    ),
    "media_category": ModuleMethodContract(family="media-recognition"),
    "media_exists": ModuleMethodContract(family="media-library"),
    "match_media": ModuleMethodContract(family="media-metadata"),
    "async_match_media": ModuleMethodContract(family="media-metadata"),
    "person_detail": ModuleMethodContract(family="media-metadata"),
    "async_person_detail": ModuleMethodContract(family="media-metadata"),
    "person_credits": ModuleMethodContract(family="media-metadata"),
    "async_person_credits": ModuleMethodContract(family="media-metadata"),
    "media_credits": ModuleMethodContract(family="media-metadata"),
    "async_media_credits": ModuleMethodContract(family="media-metadata"),
    "media_recommend": ModuleMethodContract(family="media-metadata"),
    "async_media_recommend": ModuleMethodContract(family="media-metadata"),
    "media_similar": ModuleMethodContract(family="media-metadata"),
    "async_media_similar": ModuleMethodContract(family="media-metadata"),
    "media_detail": ModuleMethodContract(family="media-metadata"),
    "async_media_detail": ModuleMethodContract(family="media-metadata"),
    "discover": ModuleMethodContract(family="media-discovery"),
    "async_discover": ModuleMethodContract(family="media-discovery"),
    "discover_board": ModuleMethodContract(family="media-discovery"),
    "async_discover_board": ModuleMethodContract(family="media-discovery"),
    "media_files": ModuleMethodContract(family="media-library"),
    "mediaserver_items": ModuleMethodContract(family="media-server"),
    "mediaserver_iteminfo": ModuleMethodContract(family="media-server"),
    "mediaserver_play_url": ModuleMethodContract(family="media-server"),
    "mediaserver_tv_episodes": ModuleMethodContract(family="media-server"),
    "download_file": ModuleMethodContract(family="storage"),
    "upload_file": ModuleMethodContract(family="storage"),
    "list_files": ModuleMethodContract(family="storage"),
    "get_file_item": ModuleMethodContract(family="storage"),
    "get_folder": ModuleMethodContract(family="storage"),
    "get_parent_item": ModuleMethodContract(family="storage"),
    "rename_file": ModuleMethodContract(family="storage"),
    "storage_manage": ModuleMethodContract(family="storage"),
    "snapshot_storage": ModuleMethodContract(family="storage"),
    "send_message": ModuleMethodContract(family="messaging"),
    "finalize_message": ModuleMethodContract(family="messaging"),
    "register_commands": ModuleMethodContract(family="messaging"),
    "scheduler_job": ModuleMethodContract(family="scheduling"),
    "webhook_parser": ModuleMethodContract(family="integration"),
}

_PREFIX_CONTRACTS = (
    ("music_", ModuleMethodContract(family="music")),
    ("torrent_", ModuleMethodContract(family="downloader")),
)


def get_module_method_contract(method: str) -> ModuleMethodContract:
    """返回方法的显式能力族契约，未知方法保持既有 legacy 协议。"""
    if contract := _METHOD_CONTRACTS.get(method):
        return contract
    for prefix, contract in _PREFIX_CONTRACTS:
        if method.startswith(prefix):
            return contract
    return _DEFAULT_CONTRACT


def get_multi_source_contract(method: str) -> MultiSourceCapabilityContract | None:
    """返回方法的多来源应答契约，单一来源能力返回 ``None``。"""
    return _MULTI_SOURCE_CONTRACTS.get(method)


def is_explicit_module_method(method: str) -> bool:
    """判断方法是否已进入首批显式能力族清单。"""
    return get_module_method_contract(method) is not _DEFAULT_CONTRACT


def list_explicit_module_contracts() -> dict[str, ModuleMethodContract]:
    """返回逐方法登记的显式契约副本。

    只含 ``_METHOD_CONTRACTS`` 里逐个点名的方法，按前缀兜底命中的不在其中——
    前缀规则覆盖的方法集合随模块增删而变，收进快照会让基线在与契约无关的改动上抖动。

    :return: 方法名到显式契约的映射副本
    """
    return dict(_METHOD_CONTRACTS)
