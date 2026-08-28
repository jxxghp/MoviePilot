"""已加载原生依赖更新探针的纯逻辑测试。"""

import pytest

from scripts.probe_native_dependency_update import (
    TARGET_BUILD,
    _write_manifest,
    classify_online_attempt,
    online_attempt_succeeded,
    validate_runtime_target,
)


def _attempt(
    *,
    success: bool,
    fresh: int | None,
    loaded: int | None,
    fresh_version: str | None = "2.0.0",
    fresh_hash: str | None = "v2-hash",
) -> dict:
    """构造分类器所需的最小探针结果。"""
    return {
        "before": {"binary_sha256": "v1-hash"},
        "install_success": success,
        "fresh_process": {
            "distribution_version": fresh_version,
            "compiled_version": fresh,
            "binary_sha256": fresh_hash,
        },
        "loaded_after": {"compiled_version": loaded},
    }


def test_classify_online_attempt_distinguishes_failed_update_states():
    """安装失败后必须区分原载荷未变和环境已发生变化。"""
    assert (
        classify_online_attempt(
            _attempt(
                success=False,
                fresh=100,
                loaded=100,
                fresh_version="1.0.0",
                fresh_hash="v1-hash",
            )
        )
        == "install_blocked_unchanged"
    )
    assert (
        classify_online_attempt(_attempt(success=False, fresh=200, loaded=100))
        == "install_failed_with_environment_change"
    )
    assert (
        classify_online_attempt(
            {
                **_attempt(success=False, fresh=None, loaded=100),
                "fresh_process": {"error": "ImportError: broken payload"},
            }
        )
        == "install_failed_with_environment_change"
    )


def test_classify_online_attempt_distinguishes_restart_boundaries():
    """成功回执还要结合磁盘载荷和当前进程状态判定。"""
    assert (
        classify_online_attempt(
            _attempt(
                success=True,
                fresh=100,
                loaded=100,
                fresh_version="1.0.0",
                fresh_hash="v1-hash",
            )
        )
        == "reported_success_without_new_payload"
    )
    assert (
        classify_online_attempt(_attempt(success=True, fresh=TARGET_BUILD, loaded=100))
        == "restart_required_for_activation"
    )
    assert (
        classify_online_attempt(_attempt(success=True, fresh=TARGET_BUILD, loaded=TARGET_BUILD))
        == "online_activation_succeeded"
    )


@pytest.mark.parametrize(
    ("classification", "expected"),
    [
        ("restart_required_for_activation", True),
        ("online_activation_succeeded", True),
        ("install_blocked_unchanged", False),
        ("install_failed_with_environment_change", False),
        ("reported_success_without_new_payload", False),
        ("probe_error", False),
    ],
)
def test_online_attempt_succeeded_requires_target_payload(
    classification: str,
    expected: bool,
):
    """恢复成功不能掩盖在线阶段未写入目标载荷。"""
    assert online_attempt_succeeded({"classification": classification}) is expected


def test_validate_runtime_target_checks_host_and_interpreter_architecture(monkeypatch):
    """宿主架构与解释器 ABI 必须分别进入证据门禁。"""
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.platform.machine",
        lambda: "ARM64",
    )
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.sysconfig.get_platform",
        lambda: "win-amd64",
    )

    assert validate_runtime_target(
        expected_system="Windows",
        expected_machine="ARM64",
        expected_python_platform_suffix="win-amd64",
    ) == {
        "system": "Windows",
        "machine": "ARM64",
        "python_platform": "win-amd64",
    }


@pytest.mark.parametrize(
    ("expected_system", "expected_machine", "expected_suffix", "message"),
    [
        ("Linux", "ARM64", "win-amd64", "宿主系统不匹配"),
        ("Windows", "AMD64", "win-amd64", "宿主架构不匹配"),
        ("Windows", "ARM64", "win-arm64", "解释器目标平台不匹配"),
    ],
)
def test_validate_runtime_target_rejects_mislabeled_evidence(
    monkeypatch,
    expected_system: str,
    expected_machine: str,
    expected_suffix: str,
    message: str,
):
    """平台标签与真实运行目标不一致时不得继续生成成功证据。"""
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.platform.system",
        lambda: "Windows",
    )
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.platform.machine",
        lambda: "ARM64",
    )
    monkeypatch.setattr(
        "scripts.probe_native_dependency_update.sysconfig.get_platform",
        lambda: "win-amd64",
    )

    with pytest.raises(RuntimeError, match=message):
        validate_runtime_target(
            expected_system=expected_system,
            expected_machine=expected_machine,
            expected_python_platform_suffix=expected_suffix,
        )


def test_classify_online_attempt_preserves_probe_failures():
    """探针自身异常不能被误报成系统文件锁或安装结果。"""
    attempt = _attempt(success=False, fresh=None, loaded=100)
    attempt["probe_error"] = "RuntimeError: fixture failed"
    assert classify_online_attempt(attempt) == "probe_error"


def test_write_manifest_covers_legacy_and_modern_plugin_contracts(tmp_path):
    """探针清单必须分别复现 requirements 与 PEP 621 入口。"""
    legacy = _write_manifest(
        tmp_path,
        kind="requirements",
        distribution_version="2.0.0",
    )
    modern = _write_manifest(
        tmp_path,
        kind="pyproject",
        distribution_version="2.0.0",
    )

    assert legacy.name == "requirements.txt"
    assert "moviepilot-native-update-probe==2.0.0" in legacy.read_text(encoding="utf-8")
    assert modern.name == "pyproject.toml"
    assert 'dependencies = ["moviepilot-native-update-probe==2.0.0"]' in (modern.read_text(encoding="utf-8"))


def test_write_manifest_rejects_unknown_contract(tmp_path):
    """未知清单形态不能静默退化成 legacy 行为。"""
    with pytest.raises(ValueError, match="不支持的清单类型"):
        _write_manifest(
            tmp_path,
            kind="unknown",
            distribution_version="2.0.0",
        )
