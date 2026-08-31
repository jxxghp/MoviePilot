"""后端单测统一 runner 的分片与 CI 调用合同。"""

from pathlib import Path

import pytest

from tests import run as test_runner


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _test_files(count: int) -> list[Path]:
    """构造按字典序排列的测试文件路径。"""
    return [Path(f"test_{index:03d}.py") for index in range(count)]


def test_split_test_files_uses_stable_contiguous_chunks() -> None:
    """文件分片必须稳定覆盖全集，且与既有 CI 的连续均分语义一致。"""
    test_files = _test_files(10)

    shards = test_runner.split_test_files(test_files, shard_count=4)

    assert [len(shard) for shard in shards] == [3, 3, 3, 1]
    assert [test_file for shard in shards for test_file in shard] == test_files


def test_main_defaults_to_four_parallel_shards(monkeypatch) -> None:
    """无 runner 参数时应并行执行四个独立 pytest 文件分片。"""
    test_files = _test_files(10)
    captured = {}
    monkeypatch.setattr(test_runner, "collect_test_files", lambda: test_files)

    def fake_run_parallel(shards, pytest_args):
        captured["shards"] = shards
        captured["pytest_args"] = pytest_args
        return 0

    monkeypatch.setattr(test_runner, "run_parallel_shards", fake_run_parallel)

    assert test_runner.main(["-q", "--maxfail=1"]) == 0
    assert [len(shard) for shard in captured["shards"]] == [3, 3, 3, 1]
    assert captured["pytest_args"] == ["-q", "--maxfail=1"]


def test_main_runs_requested_ci_shard_in_current_process(monkeypatch) -> None:
    """CI 指定分片时只运行该分片，并继续透传 pytest 参数。"""
    test_files = _test_files(10)
    captured = {}
    monkeypatch.setattr(test_runner, "collect_test_files", lambda: test_files)

    def fake_run_pytest(paths, pytest_args):
        captured["paths"] = paths
        captured["pytest_args"] = pytest_args
        return 0

    monkeypatch.setattr(test_runner, "run_pytest", fake_run_pytest)

    assert test_runner.main(["--shard", "2/4", "-q"]) == 0
    assert captured["paths"] == test_files[3:6]
    assert captured["pytest_args"] == ["-q"]


def test_main_serial_preserves_legacy_full_suite_entry(monkeypatch) -> None:
    """串行模式必须保留 tests 根目录加 pytest 参数透传的旧入口。"""
    captured = {}

    def fake_run_pytest(paths, pytest_args):
        captured["paths"] = paths
        captured["pytest_args"] = pytest_args
        return 0

    monkeypatch.setattr(test_runner, "run_pytest", fake_run_pytest)

    assert test_runner.main(["--serial", "-q", "--maxfail=1"]) == 0
    assert captured["paths"] == [test_runner.TESTS_DIR]
    assert captured["pytest_args"] == ["-q", "--maxfail=1"]


@pytest.mark.parametrize("value", ["0/4", "5/4", "1/0", "invalid"])
def test_invalid_shard_values_are_rejected(value: str) -> None:
    """分片参数必须使用有效的一基 N/TOTAL 范围。"""
    with pytest.raises(SystemExit, match="2"):
        test_runner.parse_args(["--shard", value])


def test_workflow_uses_the_shared_runner_contract() -> None:
    """CI 不得另行维护 shell 分片算法，Coverage 必须复用同一分片入口。"""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert 'python tests/run.py --shard "${{ matrix.shard }}"' in workflow
    assert "python -m coverage run --parallel-mode tests/run.py --shard" in workflow
    assert "mapfile" not in workflow
    assert "SHARD_INDEX" not in workflow
