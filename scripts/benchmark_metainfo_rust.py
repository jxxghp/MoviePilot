import argparse
import statistics
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.domain import metainfo as metainfo_module
from app.domain.meta.metaanime import MetaAnime
from app.domain.meta.metamusic import MetaMusic
from app.domain.meta.runtime import get_audio_extensions
from app.domain.metainfo import MetaInfo, MetaInfoPath
from app.adapters.system import rust as rust_accel
from tests.cases.meta import meta_cases


BenchmarkInput = tuple[str, str, Optional[str]]
ResultProjector = Callable[[Any], dict[str, Any]]

_MUSIC_CASES: tuple[BenchmarkInput, ...] = (
    ("music_query", "毛阿敏 - 永遠是朋友(2000) - ALAC [16B-44.1kHz]", None),
    (
        "music_query",
        "VA-Once.Upon.a.Time.in.Hollywood.Original.Motion.Picture.Soundtrack."
        "2019.FLAC.24bit.96kHz",
        None,
    ),
    ("music_query", "李宗盛《理性与感性作品音乐会-CD2》2006-FLAC-分轨", None),
    ("music_query", "天国的情人-邓丽君作品全集1967-1995", None),
    (
        "music_query",
        "S H E - S H E十七音乐会 2018 WEB-DL 1080P AVC AAC-FHDMv",
        None,
    ),
    ("title", "周杰伦 - 晴天.flac", None),
    ("title", "01.我的地盘.wav", None),
    (
        "path",
        "/benchmark/music/周杰伦 - 七里香 (2004) [FLAC 24bit-96kHz]/01.我的地盘.flac",
        None,
    ),
    (
        "path",
        "/benchmark/music/Daft Punk - Discovery (2001)/CD1/01 - One More Time.flac",
        None,
    ),
    (
        "path",
        "/benchmark/music/喜多郎 - 古事记 (1990) [SACD]/1-02 古事记.dsf",
        None,
    ),
)


def build_video_inputs(repeat: int) -> list[BenchmarkInput]:
    """构造覆盖影视 MetaInfo 和 MetaInfoPath 生产入口的基准输入。"""
    inputs: list[BenchmarkInput] = []
    for _ in range(repeat):
        for item in meta_cases:
            if item.get("path"):
                inputs.append(("path", item["path"], item.get("subtitle")))
            else:
                inputs.append(("title", item["title"], item.get("subtitle")))
    return inputs


def build_music_inputs(repeat: int) -> list[BenchmarkInput]:
    """构造覆盖音乐查询、音频文件名和目录路径生产入口的基准输入。"""
    return list(_MUSIC_CASES) * repeat


def disabled_rust_parse(*_args, **_kwargs):
    """关闭一个 Rust 快路径，使生产入口自然回退到 Python 实现。"""
    return None


@contextmanager
def selected_meta_parser(use_rust: bool):
    """在 Rust 入口和 Python 回退链路之间切换，并在退出时恢复适配器。"""
    parser_names = (
        "parse_metainfo",
        "parse_metainfo_path",
        "find_metainfo",
        "parse_metamusic",
    )
    original_parsers = {
        name: getattr(rust_accel, name)
        for name in parser_names
    }
    if not use_rust:
        for name in parser_names:
            setattr(rust_accel, name, disabled_rust_parse)
    try:
        yield
    finally:
        for name, parser in original_parsers.items():
            setattr(rust_accel, name, parser)


def parse_input(item: BenchmarkInput):
    """按输入类型调用应用实际使用的公开识别入口。"""
    kind, value, subtitle = item
    if kind == "path":
        return MetaInfoPath(Path(value))
    if kind == "music_query":
        return MetaMusic.parse_query(value)
    if kind == "title":
        return MetaInfo(title=value, subtitle=subtitle, custom_words=["#"])
    raise ValueError(f"未知基准输入类型：{kind}")


def parse_all(inputs: list[BenchmarkInput]) -> list[Any]:
    """通过生产入口解析一轮完整输入。"""
    return [parse_input(item) for item in inputs]


def _enum_value(value: Any) -> Any:
    """把枚举值归一为稳定的可比较值。"""
    return getattr(value, "value", value)


def project_video_result(meta: Any) -> dict[str, Any]:
    """提取影视识别对外契约字段，排除 Python 解析器的临时内部状态。"""
    return {
        "kind": "anime" if isinstance(meta, MetaAnime) else "video",
        "type": _enum_value(meta.type),
        "cn_name": meta.cn_name or "",
        "en_name": meta.en_name or "",
        "year": meta.year or "",
        "part": meta.part or "",
        "season": meta.season,
        "episode": meta.episode,
        "resource_type": meta.edition,
        "resource_pix": meta.resource_pix or "",
        "video_encode": meta.video_encode or "",
        "audio_encode": meta.audio_encode or "",
        "fps": meta.fps or None,
        "media_source": _enum_value(meta.media_source),
        "media_id": meta.media_id,
    }


def project_music_result(meta: Any) -> dict[str, Any]:
    """提取音乐识别持久字段和派生音质字段，用于 Rust/Python 等价校验。"""
    return {
        "type": _enum_value(meta.type),
        "org_string": meta.org_string,
        "title": meta.title,
        "artists": list(meta.artists),
        "album": meta.album,
        "album_artist": meta.album_artist,
        "year": meta.year,
        "disc_number": meta.disc_number,
        "track_number": meta.track_number,
        "total_discs": meta.total_discs,
        "total_tracks": meta.total_tracks,
        "version": meta.version,
        "audio_format": meta.audio_format,
        "audio_lossless": meta.audio_lossless,
        "bit_depth": meta.bit_depth,
        "sample_rate": meta.sample_rate,
        "bitrate": meta.bitrate,
        "duration": meta.duration,
        "isrc": meta.isrc,
        "media_source": _enum_value(meta.media_source),
        "media_id": meta.media_id,
        "audio_quality": meta.audio_quality,
        "audio_quality_score": meta.audio_quality_score,
        "audio_specs": meta.audio_specs,
    }


def assert_projected_results_equal(
        inputs: list[BenchmarkInput],
        rust_results: list[Any],
        python_results: list[Any],
        projector: ResultProjector,
) -> None:
    """逐项校验 Rust/Python 稳定输出，首个差异携带输入和字段明细。"""
    if len(rust_results) != len(python_results):
        raise AssertionError(
            f"Rust/Python 结果数量不一致：{len(rust_results)} != {len(python_results)}"
        )
    for index, (rust_result, python_result) in enumerate(zip(rust_results, python_results)):
        rust_projection = projector(rust_result)
        python_projection = projector(python_result)
        if rust_projection == python_projection:
            continue
        differences = {
            key: (rust_projection.get(key), python_projection.get(key))
            for key in sorted(set(rust_projection) | set(python_projection))
            if rust_projection.get(key) != python_projection.get(key)
        }
        raise AssertionError(
            f"Rust/Python 输出不等价：index={index} input={inputs[index]!r} "
            f"differences={differences!r}"
        )


def validate_equivalent_results(
        inputs: list[BenchmarkInput],
        projector: ResultProjector,
) -> None:
    """分别通过 Rust/Python 生产链路解析并校验稳定输出等价。"""
    with selected_meta_parser(use_rust=True):
        rust_results = parse_all(inputs)
    with selected_meta_parser(use_rust=False):
        python_results = parse_all(inputs)
    assert_projected_results_equal(inputs, rust_results, python_results, projector)


def measure(
        inputs: list[BenchmarkInput],
        use_rust: bool,
        loops: int,
        repeats: int,
) -> tuple[float, int]:
    """在一次解析器切换上下文中预热并多轮测量生产入口耗时。"""
    samples = []
    parsed_count = 0
    with selected_meta_parser(use_rust):
        parse_all(inputs)
        for _ in range(repeats):
            start = time.perf_counter()
            for _ in range(loops):
                parsed_count = len(parse_all(inputs))
            samples.append((time.perf_counter() - start) * 1000 / loops)
    return statistics.median(samples), parsed_count


def benchmark_suite(
        inputs: list[BenchmarkInput],
        projector: ResultProjector,
        loops: int,
        repeats: int,
) -> dict[str, float | int]:
    """先校验一个媒体域的结果等价，再返回 Rust/Python 独立性能指标。"""
    validate_equivalent_results(inputs, projector)
    rust_ms, rust_count = measure(inputs, use_rust=True, loops=loops, repeats=repeats)
    python_ms, python_count = measure(inputs, use_rust=False, loops=loops, repeats=repeats)
    return {
        "rust_ms": rust_ms,
        "python_ms": python_ms,
        "rust_count": rust_count,
        "python_count": python_count,
        "speedup": python_ms / rust_ms if rust_ms else 0,
    }


def validate_rust_runtime() -> None:
    """确认 Rust 总开关和音乐扩展入口可用，拒绝静默回退形成伪基准。"""
    if not rust_accel.is_enabled():
        raise RuntimeError("Rust 加速未启用或 moviepilot-rust 扩展不可用")
    if not callable(getattr(rust_accel, "parse_metamusic", None)):
        raise RuntimeError("MoviePilot 后端缺少 rust_accel.parse_metamusic 适配器")
    extension = getattr(rust_accel, "_moviepilot_rust", None)
    if not callable(getattr(extension, "parse_metamusic_fast", None)):
        raise RuntimeError("moviepilot-rust 版本过旧，缺少 parse_metamusic_fast")
    probe = rust_accel.parse_metamusic("Daft Punk - Get Lucky 2013 FLAC")
    if not isinstance(probe, dict):
        raise RuntimeError("Rust 音乐解析探针未返回有效结果，拒绝测量 Python 回退")


def positive_int(value: str) -> int:
    """解析命令行正整数，拒绝空循环和空样本配置。"""
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须为正整数")
    return parsed


def parse_args():
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="Benchmark video and music metadata through public entries"
    )
    parser.add_argument(
        "--repeat-inputs",
        type=positive_int,
        default=20,
        help="Repeat video and music cases per loop",
    )
    parser.add_argument("--loops", type=positive_int, default=10, help="Loops per repeat")
    parser.add_argument("--repeats", type=positive_int, default=5, help="Repeat count")
    return parser.parse_args()


def print_suite_result(
        name: str,
        inputs: list[BenchmarkInput],
        result: dict[str, float | int],
        loops: int,
        repeats: int,
) -> None:
    """按媒体域输出等价状态、耗时、单项耗时和性能提升倍数。"""
    rust_ms = float(result["rust_ms"])
    python_ms = float(result["python_ms"])
    print(f"{name}_items_per_loop={len(inputs)} loops={loops} repeats={repeats}")
    print(
        f"{name}_rust_items={result['rust_count']} "
        f"{name}_python_items={result['python_count']}"
    )
    print(f"{name}_equivalent=true")
    print(f"{name}_rust_ms_per_loop={rust_ms:.3f}")
    print(f"{name}_python_ms_per_loop={python_ms:.3f}")
    print(f"{name}_rust_us_per_item={rust_ms * 1000 / len(inputs):.3f}")
    print(f"{name}_python_us_per_item={python_ms * 1000 / len(inputs):.3f}")
    print(f"{name}_speedup={float(result['speedup']):.2f}x")


def main() -> int:
    """运行影视与音乐 Rust/Python 生产入口基准测试。"""
    args = parse_args()
    try:
        validate_rust_runtime()
        video_inputs = build_video_inputs(args.repeat_inputs)
        music_inputs = build_music_inputs(args.repeat_inputs)
        video_result = benchmark_suite(
            video_inputs,
            project_video_result,
            loops=args.loops,
            repeats=args.repeats,
        )
        music_result = benchmark_suite(
            music_inputs,
            project_music_result,
            loops=args.loops,
            repeats=args.repeats,
        )
    except (AssertionError, RuntimeError) as err:
        print(f"benchmark_error={err}", file=sys.stderr)
        return 2

    print_suite_result("video", video_inputs, video_result, args.loops, args.repeats)
    print_suite_result("music", music_inputs, music_result, args.loops, args.repeats)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
