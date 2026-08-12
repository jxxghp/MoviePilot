from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import benchmark_metainfo_rust as benchmark


def test_build_inputs_separates_video_and_music_domains():
    """影视与音乐输入应独立扩展，并按 repeat 稳定重复。"""
    video_once = benchmark.build_video_inputs(1)
    music_once = benchmark.build_music_inputs(1)

    assert benchmark.build_video_inputs(2) == video_once * 2
    assert benchmark.build_music_inputs(2) == music_once * 2
    assert {kind for kind, _value, _subtitle in music_once} == {
        "music_query",
        "title",
        "path",
    }
    assert all(not value.lower().endswith(tuple(benchmark.metainfo_module.settings.RMT_AUDIOEXT))
               for kind, value, _subtitle in music_once if kind == "music_query")


def test_parse_input_uses_public_production_entries(monkeypatch):
    """输入分发应调用 MetaInfo、MetaInfoPath 和 MetaMusic.parse_query 公开入口。"""
    title_result = object()
    path_result = object()
    music_result = object()
    title_parser = Mock(return_value=title_result)
    path_parser = Mock(return_value=path_result)
    music_parser = Mock(return_value=music_result)
    monkeypatch.setattr(benchmark, "MetaInfo", title_parser)
    monkeypatch.setattr(benchmark, "MetaInfoPath", path_parser)
    monkeypatch.setattr(benchmark.MetaMusic, "parse_query", music_parser)

    assert benchmark.parse_input(("title", "Movie 2026", "subtitle")) is title_result
    assert benchmark.parse_input(("path", "/media/Movie 2026/movie.mkv", None)) is path_result
    assert benchmark.parse_input(("music_query", "Artist - Track", None)) is music_result
    title_parser.assert_called_once_with(
        title="Movie 2026",
        subtitle="subtitle",
        custom_words=["#"],
    )
    path_parser.assert_called_once_with(benchmark.Path("/media/Movie 2026/movie.mkv"))
    music_parser.assert_called_once_with("Artist - Track")


def test_selected_meta_parser_disables_and_restores_all_fast_paths(monkeypatch):
    """Python 对照上下文应屏蔽影视和音乐 Rust 入口，并完整恢复原函数。"""
    rust_accel = benchmark.metainfo_module.rust_accel
    parser_names = (
        "parse_metainfo",
        "parse_metainfo_path",
        "find_metainfo",
        "parse_metamusic",
    )
    originals = {}
    for name in parser_names:
        parser = Mock(name=name)
        monkeypatch.setattr(rust_accel, name, parser, raising=False)
        originals[name] = parser

    with benchmark.selected_meta_parser(use_rust=False):
        for name in parser_names:
            assert getattr(rust_accel, name)("sample") is None

    for name, parser in originals.items():
        assert getattr(rust_accel, name) is parser


def test_measure_switches_once_and_warms_up_outside_samples(monkeypatch):
    """一次测量只应切换一次解析器，并额外执行一轮不计时预热。"""
    context_calls = []
    parse_calls = []

    @contextmanager
    def fake_selected_meta_parser(use_rust: bool):
        """记录测试中的解析器上下文进入次数。"""
        context_calls.append(use_rust)
        yield

    def fake_parse_all(inputs):
        """记录测试中的每轮解析调用。"""
        parse_calls.append(inputs)
        return [object()] * len(inputs)

    monkeypatch.setattr(benchmark, "selected_meta_parser", fake_selected_meta_parser)
    monkeypatch.setattr(benchmark, "parse_all", fake_parse_all)

    elapsed, parsed_count = benchmark.measure(
        [("title", "Movie", None)],
        use_rust=False,
        loops=2,
        repeats=3,
    )

    assert context_calls == [False]
    assert len(parse_calls) == 7
    assert parsed_count == 1
    assert elapsed >= 0


def test_assert_projected_results_equal_reports_first_field_difference():
    """等价校验失败时应报告首个输入及稳定字段差异。"""
    inputs = [("music_query", "Artist - Track", None)]
    rust_result = SimpleNamespace(title="Track", artists=["Artist"])
    python_result = SimpleNamespace(title="Other", artists=["Artist"])

    with pytest.raises(AssertionError) as error:
        benchmark.assert_projected_results_equal(
            inputs,
            [rust_result],
            [python_result],
            lambda result: {
                "title": result.title,
                "artists": list(result.artists),
            },
        )

    message = str(error.value)
    assert "Artist - Track" in message
    assert "title" in message
    assert "Track" in message
    assert "Other" in message


def test_video_projection_ignores_python_parser_internal_state():
    """影视等价投影不应纳入 Python 解析器的临时私有字段。"""
    rust_result = benchmark.MetaInfo("Marty Supreme 2025 2160p WEB-DL")
    python_result = benchmark.MetaInfo("Marty Supreme 2025 2160p WEB-DL")
    rust_result._index = 1
    python_result._index = 99
    rust_result._effect = []
    python_result._effect = ["temporary"]

    assert benchmark.project_video_result(rust_result) == benchmark.project_video_result(
        python_result
    )


def test_validate_rust_runtime_rejects_disabled_and_old_extensions(monkeypatch):
    """运行前检查应拒绝关闭的 Rust 和缺少音乐入口的旧扩展。"""
    rust_accel = benchmark.metainfo_module.rust_accel
    monkeypatch.setattr(rust_accel, "is_enabled", Mock(return_value=False))

    with pytest.raises(RuntimeError, match="未启用"):
        benchmark.validate_rust_runtime()

    monkeypatch.setattr(rust_accel, "is_enabled", Mock(return_value=True))
    monkeypatch.setattr(rust_accel, "parse_metamusic", Mock(return_value={}), raising=False)
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", SimpleNamespace())

    with pytest.raises(RuntimeError, match="版本过旧"):
        benchmark.validate_rust_runtime()


def test_validate_rust_runtime_requires_successful_music_probe(monkeypatch):
    """音乐 Rust 入口存在但实际回退 Python 时也必须拒绝执行基准。"""
    rust_accel = benchmark.metainfo_module.rust_accel
    extension = SimpleNamespace(parse_metamusic_fast=Mock())
    monkeypatch.setattr(rust_accel, "is_enabled", Mock(return_value=True))
    monkeypatch.setattr(rust_accel, "_moviepilot_rust", extension)
    monkeypatch.setattr(rust_accel, "parse_metamusic", Mock(return_value=None), raising=False)

    with pytest.raises(RuntimeError, match="探针"):
        benchmark.validate_rust_runtime()


def test_main_outputs_independent_video_and_music_metrics(monkeypatch, capsys):
    """主程序应分别输出影视和音乐等价状态、耗时及性能提升。"""
    monkeypatch.setattr(benchmark, "validate_rust_runtime", Mock())
    monkeypatch.setattr(benchmark, "build_video_inputs", Mock(return_value=[("title", "V", None)]))
    monkeypatch.setattr(
        benchmark,
        "build_music_inputs",
        Mock(return_value=[("music_query", "M", None), ("title", "M.flac", None)]),
    )
    results = [
        {
            "rust_ms": 1.0,
            "python_ms": 2.0,
            "rust_count": 1,
            "python_count": 1,
            "speedup": 2.0,
        },
        {
            "rust_ms": 2.0,
            "python_ms": 6.0,
            "rust_count": 2,
            "python_count": 2,
            "speedup": 3.0,
        },
    ]
    monkeypatch.setattr(benchmark, "benchmark_suite", Mock(side_effect=results))
    monkeypatch.setattr(
        benchmark.sys,
        "argv",
        ["benchmark_metainfo_rust.py", "--repeat-inputs", "1", "--loops", "1", "--repeats", "1"],
    )

    assert benchmark.main() == 0
    output = capsys.readouterr().out
    assert "video_equivalent=true" in output
    assert "video_speedup=2.00x" in output
    assert "music_equivalent=true" in output
    assert "music_speedup=3.00x" in output
    assert "video_rust_us_per_item=1000.000" in output
    assert "music_rust_us_per_item=1000.000" in output


def test_main_reports_runtime_failure_with_nonzero_exit(monkeypatch, capsys):
    """Rust 未就绪时主程序应明确报错并返回非零状态。"""
    monkeypatch.setattr(
        benchmark,
        "validate_rust_runtime",
        Mock(side_effect=RuntimeError("old extension")),
    )
    monkeypatch.setattr(benchmark.sys, "argv", ["benchmark_metainfo_rust.py"])

    assert benchmark.main() == 2
    assert "benchmark_error=old extension" in capsys.readouterr().err
