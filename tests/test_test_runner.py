"""后端单测统一 runner 的分片与 CI 调用合同。"""

from pathlib import Path

import pytest

from tests import run as test_runner


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "test.yml"


def _test_files(count: int) -> list[Path]:
    """构造按字典序排列的测试文件路径。"""
    return [Path(f"test_{index:03d}.py") for index in range(count)]


def test_split_test_files_balances_slow_files_deterministically() -> None:
    """相邻慢文件必须分散，输入顺序变化也不能造成分片漂移或遗漏。"""
    test_files = _test_files(10)
    durations = {test_files[0].name: 12.0, test_files[1].name: 11.0}

    shards = test_runner.split_test_files(test_files, shard_count=4, durations=durations)

    assert shards == test_runner.split_test_files(
        list(reversed(test_files)), shard_count=4, durations=durations,
    )
    assert sorted(test_file for shard in shards for test_file in shard) == test_files
    assert all(shard == sorted(shard) for shard in shards)
    assert not any(test_files[0] in shard and test_files[1] in shard for shard in shards)
    assert max(sum(durations.get(path.name, 1.0) for path in shard) for shard in shards) == 12


@pytest.mark.parametrize("file_count, shard_count", [(0, 4), (2, 4), (10, 4), (10, 1)])
def test_split_test_files_covers_unknown_files_and_empty_shards(
    file_count: int, shard_count: int,
) -> None:
    """新增无历史耗时文件和空分片均须保留，文件不得重复或漏跑。"""
    test_files = _test_files(file_count)

    shards = test_runner.split_test_files(test_files, shard_count, durations={})

    assert len(shards) == shard_count
    assert sorted(path for shard in shards for path in shard) == test_files
    assert max(map(len, shards)) - min(map(len, shards)) <= 1


def test_split_test_files_rejects_nonpositive_shard_count() -> None:
    """分片数非法时须明确拒绝，不能静默丢失测试。"""
    with pytest.raises(ValueError, match="shard_count"):
        test_runner.split_test_files(_test_files(1), shard_count=0)


def test_recorded_durations_balance_the_current_ci_suite() -> None:
    """慢文件权重必须有效，并将当前全集的八分片预计耗时维持均衡。"""
    durations = test_runner.load_test_durations()
    assert all(value > 0 for value in durations.values())
    test_files = test_runner.collect_test_files()
    shards = test_runner.split_test_files(test_files, shard_count=8)
    totals = [sum(durations.get(path.name, 1.0) for path in shard) for shard in shards]

    assert sorted(path for shard in shards for path in shard) == test_files
    assert max(totals) - min(totals) <= max(durations.values())
    slow_files = sorted(durations, key=durations.get, reverse=True)[:3]
    assert len({index for index, shard in enumerate(shards)
                if any(path.name in slow_files for path in shard)}) == 3


def test_main_defaults_to_four_parallel_shards(monkeypatch) -> None:
    """无 runner 参数时应并行执行四个独立 pytest 文件分片。"""
    test_files = _test_files(10)
    captured = {}
    monkeypatch.setattr(test_runner, "collect_test_files", lambda: test_files)

    def fake_run_parallel(shards, pytest_args):
        """记录默认并行入口接收的分片与 pytest 参数。"""
        captured["shards"] = shards
        captured["pytest_args"] = pytest_args
        return 0

    monkeypatch.setattr(test_runner, "run_parallel_shards", fake_run_parallel)

    assert test_runner.main(["-q", "--maxfail=1"]) == 0
    assert [len(shard) for shard in captured["shards"]] == [3, 3, 2, 2]
    assert captured["pytest_args"] == ["-q", "--maxfail=1"]


def test_main_runs_requested_ci_shard_in_current_process(monkeypatch) -> None:
    """CI 指定分片时只运行该分片，并继续透传 pytest 参数。"""
    test_files = _test_files(10)
    captured = {}
    monkeypatch.setattr(test_runner, "collect_test_files", lambda: test_files)

    def fake_run_pytest(paths, pytest_args):
        """记录 pytest 入口接收的文件与透传参数。"""
        captured["paths"] = paths
        captured["pytest_args"] = pytest_args
        return 0

    monkeypatch.setattr(test_runner, "run_pytest", fake_run_pytest)

    assert test_runner.main(["--shard", "2/4", "-q"]) == 0
    assert captured["paths"] == test_files[1::4]
    assert captured["pytest_args"] == ["-q"]


def test_main_serial_preserves_legacy_full_suite_entry(monkeypatch) -> None:
    """串行模式必须保留 tests 根目录加 pytest 参数透传的旧入口。"""
    captured = {}

    def fake_run_pytest(paths, pytest_args):
        """记录 pytest 入口接收的文件与透传参数。"""
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

    assert 'run: uv run --locked --no-sync python tests/run.py --shard' not in workflow
    assert "python -m coverage run --parallel-mode tests/run.py --shard" in workflow
    assert "mapfile" not in workflow
    assert "SHARD_INDEX" not in workflow
