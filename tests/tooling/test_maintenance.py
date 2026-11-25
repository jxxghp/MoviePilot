"""
用临时 Git 仓库验证测试维护检查拒绝平铺和耦合，文件历史不构成例外。
"""

import subprocess

from scripts.testing.maintenance import compare_base, inventory, layout_violations
from scripts.testing.maintenance import test_imports as collect_test_imports


def test_import_forms_resolve_the_same_test_dependency():
    """
    绝对、相对、包导入均识别为测试依赖，普通共享设施不会被误报。
    """
    source = """
from tests.plugin.test_state import helper
from .test_state import helper
from . import test_state
import tests.plugin.test_state
from tests.support.plugin import factory
from scripts.testing.maintenance import test_imports
"""
    modules = collect_test_imports(source, "tests/plugin/test_other.py")
    assert "tests.plugin.test_state" in modules
    assert not any("support" in module for module in modules)
    assert not any(module.startswith("scripts.") for module in modules)


def test_maintenance_compares_real_git_history_without_freezing_old_debt(tmp_path):
    """
    真实基准中的旧导入也必须清理，不能借旧路径例外重新引入平铺与耦合。
    """
    tests = tmp_path / "tests"
    tests.mkdir()
    legacy = tests / "test_legacy.py"
    legacy.write_text("from tests.test_other import helper\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
         "commit", "-qm", "baseline"], cwd=tmp_path, check=True,
    )
    assert len(layout_violations(tmp_path, inventory(tmp_path))) == 2
    assert compare_base(tmp_path, "HEAD")["added_files"] == []

    owned = tests / "application" / "test_state.py"
    owned.parent.mkdir()
    owned.write_text("from tests.test_other import helper\n", encoding="utf-8")
    added = tests / "test_added.py"
    added.write_text("def test_new():\n    assert True\n", encoding="utf-8")
    violations = layout_violations(tmp_path, inventory(tmp_path))
    assert len(violations) == 4
    assert any("tests/test_added.py" in violation for violation in violations)
    assert any("tests/application/test_state.py" in violation for violation in violations)

    owned.write_text("from tests.support.plugin import factory\n", encoding="utf-8")
    added.unlink()
    legacy.unlink()
    support = tests / "application" / "support.py"
    support.write_text("from tests.test_other import helper\n", encoding="utf-8")
    violations = layout_violations(tmp_path, inventory(tmp_path))
    assert len(violations) == 1
    assert "tests/application/support.py" in violations[0]
    support.write_text("def helper():\n    return 1\n", encoding="utf-8")
    assert layout_violations(tmp_path, inventory(tmp_path)) == []
    changes = compare_base(tmp_path, "HEAD")
    assert changes["removed_files"] == ["tests/test_legacy.py"]
    assert changes["added_files"] == ["tests/application/test_state.py"]


def test_identical_bodies_are_candidates_not_automatic_deletion(tmp_path):
    """
    同体候选仍保留各自文件与函数，不因重复报告阻止不同前置环境的测试。
    """
    tests = tmp_path / "tests" / "domain"
    tests.mkdir(parents=True)
    for name in ("first", "second"):
        (tests / f"test_{name}.py").write_text(
            f"def test_{name}(context):\n    assert context.ready\n", encoding="utf-8",
        )
    report = inventory(tmp_path)
    assert report["files"] == 2
    assert report["definitions"] == 2
    assert report["owners"] == {"domain": 2}
    assert len(report["duplicate_body_candidates"]) == 1
    assert len(report["duplicate_body_candidates"][0]) == 2
