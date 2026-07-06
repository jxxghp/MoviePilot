import argparse
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.helper import rss as rss_module
from app.helper.rss import RssHelper
from app.utils import rust_accel
from app.utils.http import RequestUtils


class FakeRequestUtils:
    """
    基准测试用 RequestUtils，固定返回内存中的 RSS 文本。
    """

    xml_text = ""
    get_decoded_xml_content = staticmethod(RequestUtils.get_decoded_xml_content)

    def __init__(self, **_kwargs):
        """
        保持与真实 RequestUtils 构造签名兼容。
        """

    def get_res(self, _url):
        """
        返回 RssHelper.parse 所需的最小响应对象。
        """
        return SimpleNamespace(
            status_code=200,
            content=self.xml_text.encode("utf-8"),
            text=self.xml_text,
            apparent_encoding="utf-8",
            encoding="utf-8",
        )


def build_rss_xml(items: int) -> str:
    """
    构造覆盖标题、描述、链接、enclosure、日期和 creator 的 RSS 文本。
    """
    rows = []
    for index in range(items):
        rows.append(f"""
        <item>
          <title>MoviePilot Benchmark {index}</title>
          <description><![CDATA[Benchmark description {index} <b>tag</b>]]></description>
          <link>https://example.com/details/{index}</link>
          <enclosure url="https://example.com/download/{index}.torrent" length="{1024 + index}" />
          <pubDate>Tue, 19 May 2026 08:30:00 GMT</pubDate>
          <dc:creator>bench-user-{index}</dc:creator>
        </item>
        """)
    return f"""
    <rss xmlns:dc="http://purl.org/dc/elements/1.1/">
      <channel>
        {''.join(rows)}
      </channel>
    </rss>
    """


@contextmanager
def patched_request_utils(xml_text: str):
    """
    临时替换 RSS 请求层，让基准覆盖 RssHelper.parse 的实际解析链路。
    """
    original_request_utils = rss_module.RequestUtils
    FakeRequestUtils.xml_text = xml_text
    rss_module.RequestUtils = FakeRequestUtils
    try:
        yield
    finally:
        rss_module.RequestUtils = original_request_utils


def disabled_rust_parse(_xml_text: str, _max_items: int = 1000):
    """
    关闭 Rust 快路径，用同一条 RssHelper.parse 链路测量 Python lxml 兜底性能。
    """
    return None


@contextmanager
def selected_rss_parser(use_rust: bool):
    """
    在 Rust 快路径和 Python lxml 解析之间切换，保持请求与编码成本一致。
    """
    original_parse = rss_module.rust_accel.parse_rss_items
    if not use_rust:
        rss_module.rust_accel.parse_rss_items = disabled_rust_parse
    try:
        yield
    finally:
        rss_module.rust_accel.parse_rss_items = original_parse


def parse_chain(xml_text: str, use_rust: bool):
    """
    执行一次 RssHelper.parse，返回解析到的 RSS 条目。
    """
    with patched_request_utils(xml_text), selected_rss_parser(use_rust):
        return RssHelper().parse("https://example.com/rss")


def measure_chain(xml_text: str, use_rust: bool, loops: int, repeats: int):
    """
    多轮测量 RssHelper.parse 平均耗时，并校验每轮解析数量稳定。
    """
    samples = []
    parsed_count = 0
    for _ in range(repeats):
        start = time.perf_counter()
        for _ in range(loops):
            parsed = parse_chain(xml_text, use_rust)
            parsed_count = len(parsed)
        samples.append((time.perf_counter() - start) * 1000 / loops)
    return statistics.median(samples), parsed_count


def parse_args():
    """
    解析命令行参数。
    """
    parser = argparse.ArgumentParser(description="Benchmark RSS parsing through RssHelper.parse")
    parser.add_argument("--items", type=int, default=200, help="RSS item count")
    parser.add_argument("--loops", type=int, default=50, help="Loops per repeat")
    parser.add_argument("--repeats", type=int, default=5, help="Repeat count")
    return parser.parse_args()


def main() -> int:
    """
    运行 Rust 与 Python RSS 解析链路基准测试。
    """
    args = parse_args()
    xml_text = build_rss_xml(args.items)
    rust_ms, rust_count = measure_chain(xml_text, use_rust=True, loops=args.loops, repeats=args.repeats)
    python_ms, python_count = measure_chain(xml_text, use_rust=False, loops=args.loops, repeats=args.repeats)
    speedup = python_ms / rust_ms if rust_ms else 0

    print(f"rust_available={rust_accel.is_available()}")
    print(f"items={args.items} loops={args.loops} repeats={args.repeats}")
    print(f"rust_items={rust_count} python_items={python_count}")
    print(f"rust_chain_ms_per_loop={rust_ms:.3f}")
    print(f"python_chain_ms_per_loop={python_ms:.3f}")
    print(f"speedup={speedup:.2f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
