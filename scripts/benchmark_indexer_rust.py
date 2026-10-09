#!/usr/bin/env python3
"""
Benchmark SiteSpider indexer parsing with real MoviePilot-Build site configs.
"""

import argparse
import contextlib
import copy
import io
import statistics
import sys
import time
from pathlib import Path
from typing import Callable, Dict, List

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.modules.indexer import spider as spider_module
from app.modules.indexer.spider import SiteSpider


def _load_site_config(sites_dir: Path, site_id: str) -> dict:
    """
    从 MoviePilot-Build sites 目录读取指定站点配置。
    """
    candidates = list(sites_dir.rglob(f"{site_id}.yml"))
    if not candidates:
        raise FileNotFoundError(f"找不到站点配置：{site_id}")
    with candidates[0].open("r", encoding="utf-8") as file:
        return yaml.safe_load(file)


def _nexus_row(
        torrent_id: int,
        title: str,
        category: str = "402",
        date_text: str = "2025-05-01 12:13:14",
        free_class: str = "pro_free",
) -> str:
    """
    生成 NexusPhp 类站点配置可解析的单行 HTML。
    """
    return f"""
    <tr>
      <td><a href="?cat={category}">cat</a></td>
      <td>
        <table class="torrentname">
          <tr>
            <td class="embedded">
              <a href="details.php?id={torrent_id}" title="{title}">{title}.fallback</a>
              <a href="download.php?id={torrent_id}">download</a>
              <img class="{free_class}" />
              <img class="hitandrun" />
              <font class="subtitle">Desc {torrent_id} <span>remove</span></font>
              <span class="tags">标签{torrent_id}</span>
            </td>
          </tr>
        </table>
      </td>
      <td></td>
      <td><span title="{date_text}">{date_text}</span></td>
      <td>1.5 GB</td>
      <td>{torrent_id + 10}</td>
      <td>{torrent_id % 7}</td>
      <td>{torrent_id % 11}</td>
    </tr>
    """


def _build_nexus_html(rows: int) -> str:
    """
    生成多行 NexusPhp 表格 HTML，用于驱动真实 SiteSpider 解析链路。
    """
    body = "\n".join(
        _nexus_row(1000 + index, f"Benchmark.Title.{index}")
        for index in range(rows)
    )
    return f"<table class=\"torrents\">{body}</table>"


def _build_ipt_html(rows: int) -> str:
    """
    生成 IPT 配置使用的表格 HTML，覆盖 lstrip 和英文相对时间过滤器。
    """
    body = "\n".join(
        f"""
        <tr>
          <td><a href="/t/{1000 + index}">Benchmark.IPT.{index}</a>
              <a href="/download.php/{1000 + index}">download</a><span class="free">Free</span></td>
          <td><div>Uploaded | {index % 5 + 1} hours ago</div></td>
          <td></td><td></td><td></td>
          <td>{1 + index % 9}.2 GB</td>
          <td>{index % 13}</td>
          <td>{index + 20}</td>
          <td>{index % 7}</td>
        </tr>
        """
        for index in range(rows)
    )
    return f"<table id=\"torrents\"><tbody>{body}</tbody></table>"


def _prepare_cases(sites_dir: Path, rows: int) -> List[Dict[str, object]]:
    """
    构造使用真实站点配置的 benchmark 用例。
    """
    cases = []
    for site_id in ["agsvpt", "pttime", "chdbits"]:
        config = _load_site_config(sites_dir, site_id)
        cases.append({
            "site": site_id,
            "indexer": config,
            "html": _build_nexus_html(rows),
        })
    cases.append({
        "site": "iptorrents",
        "indexer": _load_site_config(sites_dir, "iptorrents"),
        "html": _build_ipt_html(rows),
    })
    return cases


def _parse_with_rust(indexer: dict, html: str) -> List[dict]:
    """
    使用 SiteSpider 默认入口解析，Rust 扩展可用时会命中 Rust fast path。
    """
    return SiteSpider(copy.deepcopy(indexer)).parse(html)


def _parse_with_python(indexer: dict, html: str) -> List[dict]:
    """
    临时禁用 Rust fast path，复用同一个 SiteSpider.parse 入口测 PyQuery 路径。
    """
    original = spider_module.rust_accel.parse_indexer_torrents
    spider_module.rust_accel.parse_indexer_torrents = lambda *args, **kwargs: None
    try:
        return SiteSpider(copy.deepcopy(indexer)).parse(html)
    finally:
        spider_module.rust_accel.parse_indexer_torrents = original


def _time_parser(parser: Callable[[dict, str], List[dict]], indexer: dict, html: str, loops: int) -> float:
    """
    多次执行解析函数并返回总耗时。
    """
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
        started = time.perf_counter()
        for _ in range(loops):
            parser(indexer, html)
    return time.perf_counter() - started


def _median_time(parser: Callable[[dict, str], List[dict]], indexer: dict, html: str, loops: int, repeats: int) -> float:
    """
    重复测量并取中位数，降低单次抖动对结论的影响。
    """
    samples = [_time_parser(parser, indexer, html, loops) for _ in range(repeats)]
    return statistics.median(samples)


def run_benchmark(sites_dir: Path, rows: int, loops: int, repeats: int) -> None:
    """
    执行真实 SiteSpider.parse 链路 benchmark 并打印 Rust/PyQuery 对比。
    """
    cases = _prepare_cases(sites_dir, rows)
    print(f"sites_dir={sites_dir}")
    print(f"rows={rows} loops={loops} repeats={repeats}")
    print("site, rows, rust_ms, python_ms, speedup")
    for case in cases:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer), contextlib.redirect_stderr(buffer):
            rust_result = _parse_with_rust(case["indexer"], case["html"])
            python_result = _parse_with_python(case["indexer"], case["html"])
        if not rust_result or not python_result:
            raise RuntimeError(f"{case['site']} benchmark fixture did not parse any rows")

        rust_time = _median_time(_parse_with_rust, case["indexer"], case["html"], loops, repeats)
        python_time = _median_time(_parse_with_python, case["indexer"], case["html"], loops, repeats)
        speedup = python_time / rust_time if rust_time else 0
        print(
            f"{case['site']}, {len(rust_result)}, "
            f"{rust_time * 1000:.2f}, {python_time * 1000:.2f}, {speedup:.2f}x"
        )


def parse_args() -> argparse.Namespace:
    """
    解析命令行参数。
    """
    parser = argparse.ArgumentParser(description="Benchmark MoviePilot Rust indexer parser")
    parser.add_argument(
        "--sites-dir",
        type=Path,
        default=Path("/Users/jxxghp/PycharmProjects/MoviePilot-Build/sites"),
        help="MoviePilot-Build sites directory",
    )
    parser.add_argument("--rows", type=int, default=80, help="Rows per fixture page")
    parser.add_argument("--loops", type=int, default=50, help="Loops per timing sample")
    parser.add_argument("--repeats", type=int, default=5, help="Timing samples per parser")
    return parser.parse_args()


def main() -> None:
    """
    入口函数。
    """
    args = parse_args()
    run_benchmark(args.sites_dir, args.rows, args.loops, args.repeats)


if __name__ == "__main__":
    main()
