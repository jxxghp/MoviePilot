"""种子搜索工具辅助函数"""

import re
from typing import List, Optional

from app.core.context import Context
from app.utils.crypto import HashUtils
from app.utils.string import StringUtils

SEARCH_RESULT_CACHE_FILE = "__search_result__"
TORRENT_RESULT_LIMIT = 200


def build_torrent_ref(context: Optional[Context]) -> str:
    """生成用于下载校验的短引用"""
    if not context or not context.torrent_info:
        return ""
    return HashUtils.sha1(context.torrent_info.enclosure or "")[:7]


def sort_season_options(options: List[str]) -> List[str]:
    """按前端逻辑排序季集选项"""
    if len(options) <= 1:
        return options

    parsed_options = []
    for index, option in enumerate(options):
        match = re.match(r"^S(\d+)(?:-S(\d+))?\s*(?:E(\d+)(?:-E(\d+))?)?$", option or "")
        if not match:
            parsed_options.append({
                "original": option,
                "season_num": 0,
                "episode_num": 0,
                "max_episode_num": 0,
                "is_whole_season": False,
                "index": index,
            })
            continue

        episode_num = int(match.group(3)) if match.group(3) else 0
        max_episode_num = int(match.group(4)) if match.group(4) else episode_num
        parsed_options.append({
            "original": option,
            "season_num": int(match.group(1)),
            "episode_num": episode_num,
            "max_episode_num": max_episode_num,
            "is_whole_season": not match.group(3),
            "index": index,
        })

    whole_seasons = [item for item in parsed_options if item["is_whole_season"]]
    episodes = [item for item in parsed_options if not item["is_whole_season"]]

    whole_seasons.sort(key=lambda item: (-item["season_num"], item["index"]))
    episodes.sort(
        key=lambda item: (
            -item["season_num"],
            -(item["max_episode_num"] or item["episode_num"]),
            -item["episode_num"],
            item["index"],
        )
    )
    return [item["original"] for item in whole_seasons + episodes]


def append_option(options: List[str], value: Optional[str]) -> None:
    """按前端逻辑收集去重后的筛选项"""
    if value and value not in options:
        options.append(value)


def build_filter_options(items: List[Context]) -> dict:
    """从搜索结果中构建筛选项汇总"""
    filter_options = {
        "site": [],
        "season": [],
        "freeState": [],
        "edition": [],
        "resolution": [],
        "videoCode": [],
        "releaseGroup": [],
    }

    for item in items:
        torrent_info = item.torrent_info
        meta_info = item.meta_info
        append_option(filter_options["site"], getattr(torrent_info, "site_name", None))
        append_option(filter_options["season"], getattr(meta_info, "season_episode", None))
        append_option(filter_options["freeState"], getattr(torrent_info, "volume_factor", None))
        append_option(filter_options["edition"], getattr(meta_info, "edition", None))
        append_option(filter_options["resolution"], getattr(meta_info, "resource_pix", None))
        append_option(filter_options["videoCode"], getattr(meta_info, "video_encode", None))
        append_option(filter_options["releaseGroup"], getattr(meta_info, "resource_team", None))

    filter_options["season"] = sort_season_options(filter_options["season"])
    return filter_options


def match_filter(filter_values: Optional[List[str]], value: Optional[str]) -> bool:
    """匹配前端同款多选筛选规则"""
    return not filter_values or bool(value and value in filter_values)


def filter_contexts(items: List[Context],
                    site: Optional[List[str]] = None,
                    season: Optional[List[str]] = None,
                    free_state: Optional[List[str]] = None,
                    video_code: Optional[List[str]] = None,
                    edition: Optional[List[str]] = None,
                    resolution: Optional[List[str]] = None,
                    release_group: Optional[List[str]] = None) -> List[Context]:
    """按前端同款维度筛选结果"""
    filtered_items = []
    for item in items:
        torrent_info = item.torrent_info
        meta_info = item.meta_info
        if (
            match_filter(site, getattr(torrent_info, "site_name", None))
            and match_filter(free_state, getattr(torrent_info, "volume_factor", None))
            and match_filter(season, getattr(meta_info, "season_episode", None))
            and match_filter(release_group, getattr(meta_info, "resource_team", None))
            and match_filter(video_code, getattr(meta_info, "video_encode", None))
            and match_filter(resolution, getattr(meta_info, "resource_pix", None))
            and match_filter(edition, getattr(meta_info, "edition", None))
        ):
            filtered_items.append(item)
    return filtered_items


def simplify_search_result(context: Context, index: int) -> dict:
    """精简单条搜索结果"""
    simplified = {}
    torrent_info = context.torrent_info
    meta_info = context.meta_info
    media_info = context.media_info

    if torrent_info:
        simplified["torrent_info"] = {
            "title": torrent_info.title,
            "size": StringUtils.format_size(torrent_info.size),
            "seeders": torrent_info.seeders,
            "peers": torrent_info.peers,
            "site_name": torrent_info.site_name,
            "torrent_url": f"{build_torrent_ref(context)}:{index}",
            "page_url": torrent_info.page_url,
            "volume_factor": torrent_info.volume_factor,
            "freedate_diff": torrent_info.freedate_diff,
            "pubdate": torrent_info.pubdate,
        }

    if media_info:
        simplified["media_info"] = {
            "title": media_info.title,
            "en_title": media_info.en_title,
            "year": media_info.year,
            "type": media_info.type.value if media_info.type else None,
            "season": media_info.season,
            "tmdb_id": media_info.tmdb_id,
        }

    if meta_info:
        simplified["meta_info"] = {
            "name": meta_info.name,
            "cn_name": meta_info.cn_name,
            "en_name": meta_info.en_name,
            "year": meta_info.year,
            "type": meta_info.type.value if meta_info.type else None,
            "begin_season": meta_info.begin_season,
            "season_episode": meta_info.season_episode,
            "resource_team": meta_info.resource_team,
            "video_encode": meta_info.video_encode,
            "edition": meta_info.edition,
            "resource_pix": meta_info.resource_pix,
        }

    return simplified
