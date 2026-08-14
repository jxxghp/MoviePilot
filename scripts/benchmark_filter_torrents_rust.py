import argparse
import statistics
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domain.context import MediaInfo, TorrentInfo
from app.modules.filter import FilterModule
from app.modules.filter.RuleParser import RuleParser


class StaticRuleHelper:
    """
    为 benchmark 提供固定规则组，避免系统配置和数据库访问影响过滤链路测量。
    """

    def __init__(self, groups):
        self._groups = groups

    def get_rule_group_by_media(self, media=None, group_names=None):  # noqa: ARG002
        """
        按规则名返回固定规则组。
        """
        if not group_names:
            return self._groups
        return [group for group in self._groups if group.name in group_names]


def build_module() -> FilterModule:
    """
    构造覆盖正则、标签、站点字段、促销、做种数、发布时间和 size_range 的过滤模块。
    """
    module = FilterModule()
    module.rulehelper = StaticRuleHelper(
        [
            SimpleNamespace(name="quality", rule_string="(HDR | DV) & !BLU > WEBDL & FREE"),
            SimpleNamespace(name="site", rule_string="LABEL & SITE & SIZE & SEED & PUB & TMDB"),
        ]
    )
    module.rule_set = {
        "HDR": {"include": r"[\s.]+HDR[\s.]+|HDR10|HDR10\+"},
        "DV": {"include": r"DOVI|Dolby[\s.]+Vision"},
        "BLU": {
            "include": [r"\bBlu-?Ray\b"],
            "exclude": [r"(?<!WEB-|HDTV)RIP"],
        },
        "WEBDL": {"include": r"WEB-?DL|WEB-?RIP"},
        "FREE": {"downloadvolumefactor": 0},
        "LABEL": {"include": "官方", "match": ["labels"]},
        "SITE": {"include": "Alpha|Beta", "match": ["site_name"]},
        "SIZE": {"size_range": "100-500"},
        "SEED": {"seeders": "5"},
        "PUB": {"publish_time": "0-2880"},
        "TMDB": {"tmdb": {"original_language": "zh,cn"}},
    }
    return module


def build_torrents(count: int) -> list[TorrentInfo]:
    """
    构造稳定的种子列表，让匹配和不匹配路径都进入完整过滤逻辑。
    """
    pubdate = (datetime.now() - timedelta(minutes=90)).strftime("%Y-%m-%d %H:%M:%S")
    torrents = []
    for index in range(count):
        matched = index % 3 != 0
        title = f"Example Show S01E{index % 12 + 1:02d}-E{index % 12 + 2:02d} 1080p WEB-DL HDR10"
        if not matched:
            title = f"Example Show S01E{index % 12 + 1:02d} 1080p BluRay HDR"
        torrents.append(
            TorrentInfo(
                site_name="Alpha" if index % 2 else "Beta",
                title=title,
                description="简繁中字",
                labels=["官方"] if matched else ["转载"],
                size=(700 if matched else 2400) * 1024 * 1024,
                seeders=20 if matched else 1,
                downloadvolumefactor=0 if matched else 1,
                pubdate=pubdate,
            )
        )
    return torrents


def build_mediainfo() -> MediaInfo:
    """
    构造命中 TMDB 规则的媒体信息。
    """
    mediainfo = MediaInfo()
    mediainfo.original_language = "zh"
    return mediainfo


def reset_priority(torrents: list[TorrentInfo]) -> None:
    """
    清理上一轮过滤写入的优先级，避免重复测量间互相影响。
    """
    for torrent in torrents:
        torrent.pri_order = 0


def python_filter(module: FilterModule, rule_groups: list[str], torrent_list: list[TorrentInfo], mediainfo: MediaInfo):
    """
    调用旧 Python 私有实现，作为同一入口语义下的基准对照。
    """
    parser = RuleParser()
    parsed_rule_cache = {}
    groups = module.rulehelper.get_rule_group_by_media(media=mediainfo, group_names=rule_groups)
    for group in groups:
        torrent_list = module._FilterModule__filter_torrents(
            rule_string=group.rule_string,
            rule_name=group.name,
            torrent_list=torrent_list,
            mediainfo=mediainfo,
            parser=parser,
            parsed_rule_cache=parsed_rule_cache,
        )
    return torrent_list


def rust_filter(module: FilterModule, rule_groups: list[str], torrent_list: list[TorrentInfo], mediainfo: MediaInfo):
    """
    调用当前生产入口，测量 Rust 完整过滤链路。
    """
    return module.filter_torrents(rule_groups=rule_groups, torrent_list=torrent_list, mediainfo=mediainfo)


def measure(func, module: FilterModule, torrents: list[TorrentInfo], mediainfo: MediaInfo, loops: int, repeats: int):
    """
    多轮测量过滤耗时并返回中位数。
    """
    samples = []
    filtered_count = 0
    rule_groups = ["quality", "site"]
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(loops):
            reset_priority(torrents)
            filtered_count = len(func(module, rule_groups, torrents, mediainfo))
        samples.append((time.perf_counter() - start) * 1000 / loops)
    return statistics.median(samples), filtered_count


def parse_args():
    """
    解析命令行参数。
    """
    parser = argparse.ArgumentParser(description="Benchmark FilterModule.filter_torrents Rust entry")
    parser.add_argument("--items", type=int, default=2000, help="Torrent count per loop")
    parser.add_argument("--loops", type=int, default=20, help="Loops per repeat")
    parser.add_argument("--repeats", type=int, default=5, help="Repeat count")
    return parser.parse_args()


def main() -> int:
    """
    运行 Filter Rust 与旧 Python 过滤链路基准测试。
    """
    args = parse_args()
    module = build_module()
    torrents = build_torrents(args.items)
    mediainfo = build_mediainfo()

    rust_ms, rust_count = measure(rust_filter, module, torrents, mediainfo, args.loops, args.repeats)
    python_ms, python_count = measure(python_filter, module, torrents, mediainfo, args.loops, args.repeats)
    speedup = python_ms / rust_ms if rust_ms else 0

    print(f"items_per_loop={len(torrents)} loops={args.loops} repeats={args.repeats}")
    print(f"rust_items={rust_count} python_items={python_count}")
    print(f"rust_chain_ms_per_loop={rust_ms:.3f}")
    print(f"python_chain_ms_per_loop={python_ms:.3f}")
    print(f"speedup={speedup:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
