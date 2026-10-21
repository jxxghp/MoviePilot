"""
验证类型例外不能增加、转移或通过配置和本地类型桩掩盖。
"""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.architecture import mypy_policy


@pytest.fixture
def policy_root(tmp_path: Path) -> Path:
    """
    隔离宿主源码和配置，门禁测试不读取或修改运行时数据。
    """
    (tmp_path / "app").mkdir()
    (tmp_path / "mypy.ini").write_text("[mypy]\nstrict = True\nfiles = app\n", encoding="utf-8")
    return tmp_path


def _source(root: Path, source: str, name: str = "example.py") -> None:
    """
    写入待扫描的独立宿主文件。
    """
    (root / "app" / name).write_text(source, encoding="utf-8")


def _policy(root: Path) -> dict:
    """
    为测试中的已知例外建立有原因的初始登记。
    """
    ignores, stubs = mypy_policy.collect(root)
    return {
        "configuration": mypy_policy.configuration(root),
        "reasons": {"reviewed": "测试中的既有第三方兼容接口"},
        "ignores": [{**entry, "reason": "reviewed"} for entry in ignores],
        "stubs": [{"path": path, "sha256": digest, "reason": "reviewed"} for path, digest in stubs.items()],
    }


def test_policy_accepts_only_exact_reviewed_ignore(policy_root: Path) -> None:
    """
    空白和注释调整不消耗新的例外额度。
    """
    source = "def run(value: str) -> str:  # type: ignore[override]\n    return value\n"
    _source(policy_root, source)
    policy = _policy(policy_root)
    _source(policy_root, "\n# 已核对的接口。\n" + source)
    assert mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root)) == ([], [])


@pytest.mark.parametrize("replacement", [
    "def other(value: str) -> str:  # type: ignore[override]\n    return value\n",
    "def run(value: int) -> str:  # type: ignore[override]\n    return str(value)\n",
    "def run(value: str) -> str:  # type: ignore[return-value]\n    return value\n",
])
def test_policy_rejects_reusing_ignore_for_another_contract(policy_root: Path, replacement: str) -> None:
    """
    方法、参数合同或错误码变化不能复用相同数量的旧例外。
    """
    _source(policy_root, "def run(value: str) -> str:  # type: ignore[override]\n    return value\n")
    policy = _policy(policy_root)
    _source(policy_root, replacement)
    regressions, _ = mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root))
    assert any("新增或变化的类型忽略" in message for message in regressions)


def test_new_file_and_duplicate_ignore_cannot_reuse_allowance(policy_root: Path) -> None:
    """
    新文件和同一函数内重复的忽略均独立阻断，不能借用既有条目。
    """
    source = "def run() -> str:\n    return 1  # type: ignore[return-value]\n"
    _source(policy_root, source)
    policy = _policy(policy_root)
    _source(policy_root, source, "new.py")
    _source(policy_root, source.replace("    return 1", "    value = 2  # type: ignore[return-value]\n    return 1"))
    regressions, _ = mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root))
    assert len(regressions) == 2


@pytest.mark.parametrize("source", [
    "def run() -> str:\n    return 1  # type: ignore\n",
    "# mypy: ignore-errors\ndef run():\n    return 1\n",
    "from typing import no_type_check\n@no_type_check\ndef run():\n    return 1\n",
    "from typing import no_type_check as unchecked\n@unchecked\ndef run():\n    return 1\n",
    "import typing as t\n@t.no_type_check\ndef run():\n    return 1\n",
    "# type: ignore[override]\ndef run():\n    return 1\n",
])
def test_policy_rejects_broad_suppression(policy_root: Path, source: str) -> None:
    """
    文件级忽略、无错误码忽略和 no_type_check 的常见写法均不能进入宿主。
    """
    _source(policy_root, source)
    with pytest.raises(ValueError):
        mypy_policy.collect(policy_root)


@pytest.mark.parametrize("config", [
    "[mypy]\nstrict = False\nfiles = app\n",
    "[mypy]\nstrict = True\nfiles = app/old.py\n",
    "[mypy]\nstrict = True\nfiles = app\nexclude = app/new.py\n",
    "[mypy]\nstrict = True\nfiles = app\n[mypy-app.new]\nignore_errors = True\n",
])
def test_policy_locks_full_configuration(policy_root: Path, config: str) -> None:
    """
    降低严格程度、缩小文件范围和新增模块豁免必须被门禁拒绝。
    """
    policy = _policy(policy_root)
    (policy_root / "mypy.ini").write_text(config, encoding="utf-8")
    regressions, _ = mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root))
    assert regressions == ["mypy 配置发生变化：检查范围和依赖类型边界需要复核"]


def test_new_or_changed_local_stubs_are_rejected(policy_root: Path) -> None:
    """
    类型桩不可以悄悄覆盖供应方真实签名，仓库外层的新类型桩也必须扫描。
    """
    stub = policy_root / "app" / "site.pyi"
    stub.write_text("def run() -> str: ...\n", encoding="utf-8")
    policy = _policy(policy_root)
    stub.write_text("def run() -> int: ...\n", encoding="utf-8")
    (policy_root / "vendor.pyi").write_text("run: object\n", encoding="utf-8")
    regressions, _ = mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root))
    assert len(regressions) == 2
    assert all("本地类型桩" in message for message in regressions)


def test_policy_reduction_preserves_reasons_and_cannot_add_entries(policy_root: Path) -> None:
    """
    债务消除必须固化低水位，写入器只保留原本已登记且仍存在的条目。
    """
    _source(policy_root, "def run() -> str:\n    return 1  # type: ignore[return-value]\n")
    policy = _policy(policy_root)
    _source(policy_root, "def run() -> str:\n    return 'ok'\n")
    current = mypy_policy.collect(policy_root)
    regressions, stale = mypy_policy.compare(policy_root, policy, *current)
    assert not regressions and len(stale) == 1
    reduced = mypy_policy.reduce_policy(policy, *current)
    assert reduced["ignores"] == []
    assert reduced["reasons"] == {}
    assert policy["ignores"]
    assert mypy_policy.compare(policy_root, reduced, *current) == ([], [])


def test_write_refuses_growth_and_preserves_registry(policy_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """
    --write 不得把新增忽略写进登记表，失败时原文件内容保持完整。
    """
    policy = _policy(policy_root)
    path = policy_root / "policy.json"
    content = json.dumps(policy)
    path.write_text(content, encoding="utf-8")
    _source(policy_root, "def run() -> str:\n    return 1  # type: ignore[return-value]\n")
    monkeypatch.setattr(mypy_policy, "PROJECT_ROOT", policy_root)
    monkeypatch.setattr(sys, "argv", ["mypy_policy.py", "--write", "--policy", str(path)])
    assert mypy_policy.main() == 1
    assert path.read_text(encoding="utf-8") == content


def test_missing_reason_is_rejected(policy_root: Path) -> None:
    """
    即使指纹未变化，没有说明兼容原因的例外仍然无效。
    """
    _source(policy_root, "def run() -> str:\n    return 1  # type: ignore[return-value]\n")
    policy = copy.deepcopy(_policy(policy_root))
    policy["reasons"].clear()
    regressions, _ = mypy_policy.compare(policy_root, policy, *mypy_policy.collect(policy_root))
    assert len(regressions) == 1 and "缺少登记原因" in regressions[0]


def test_default_mypy_scope_rejects_new_host_file(policy_root: Path) -> None:
    """
    实际执行 mypy，确认新文件自动纳入检查且返回值类型错误会阻断。
    """
    _source(policy_root, "def run() -> str:\n    return 1\n", "new.py")
    result = subprocess.run([sys.executable, "-m", "mypy", "--no-incremental", "--no-pretty"],
                            cwd=policy_root, capture_output=True, text=True, check=False)
    assert result.returncode == 1
    assert "app/new.py:2: error:" in result.stdout
    assert "[return-value]" in result.stdout


def test_host_diagnostic_budget_remains_zero() -> None:
    """
    已清零的宿主不得重新添加错误数量预算，兼容问题只能经过精确登记。
    """
    root = Path(__file__).resolve().parents[1]
    assert json.loads((root / "tests/fixtures/architecture/mypy-baseline.json").read_text()) == {}
    workflow = (root / ".github/workflows/test.yml").read_text()
    assert "python scripts/architecture/mypy_policy.py" in workflow
