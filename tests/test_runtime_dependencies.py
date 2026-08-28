import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.doctor import dependencies as dependency_doctor
from app.foundation import environment
from app.runtime import dependencies


def test_free_threaded_runtime_tracks_interpreter_build(monkeypatch):
    monkeypatch.setattr(
        environment.sysconfig,
        "get_config_var",
        lambda name: 1 if name == "Py_GIL_DISABLED" else None,
    )

    assert environment.is_free_threaded_runtime() is True


def test_gil_status_tracks_current_interpreter_state(monkeypatch):
    monkeypatch.setattr(environment.sys, "_is_gil_enabled", lambda: False)

    assert environment.is_gil_enabled() is False


def test_windows_dll_directory_registration_retains_handle(tmp_path, monkeypatch):
    """Windows DLL 搜索路径句柄必须在进程生命周期内保留。"""
    handle = object()
    registered = []
    monkeypatch.setattr(environment, "is_windows", lambda: True)
    monkeypatch.setattr(
        environment.os,
        "add_dll_directory",
        lambda path: registered.append(path) or handle,
        raising=False,
    )
    monkeypatch.setattr(environment, "_windows_dll_directory_handles", [])

    assert environment.register_windows_dll_directory(str(tmp_path)) is True
    assert registered == [str(tmp_path)]
    assert environment._windows_dll_directory_handles == [handle]


def test_windows_dll_directory_registration_ignores_other_platforms(tmp_path, monkeypatch):
    """非 Windows 运行时不得调用平台专属 DLL 注册 API。"""
    monkeypatch.setattr(environment, "is_windows", lambda: False)
    monkeypatch.setattr(
        environment.os,
        "add_dll_directory",
        lambda _path: pytest.fail("must not register a DLL directory"),
        raising=False,
    )

    assert environment.register_windows_dll_directory(str(tmp_path)) is False


def test_runtime_dependency_group_tracks_interpreter_abi(monkeypatch):
    monkeypatch.setattr(dependencies, "is_free_threaded_runtime", lambda: False)
    assert dependencies.runtime_dependency_group() == "runtime-standard"

    monkeypatch.setattr(dependencies, "is_free_threaded_runtime", lambda: True)
    assert dependencies.runtime_dependency_group() == "runtime-free-threaded"


def test_runtime_requirements_include_project_and_active_group(tmp_path: Path, monkeypatch):
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text(
        """
[project]
dependencies = ["shared==1"]

[dependency-groups]
runtime-standard = ["standard==2"]
runtime-free-threaded = ["free-threaded==3"]
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        dependencies,
        "runtime_dependency_group",
        lambda: "runtime-free-threaded",
    )

    assert list(dependencies.iter_runtime_requirement_strings(project_file)) == [
        "shared==1",
        "free-threaded==3",
    ]
    assert list(dependencies.iter_runtime_profile_requirement_strings(project_file)) == [
        "free-threaded==3",
    ]


def test_runtime_profiles_share_gil_safe_crcmod_distribution():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as file:
        document = tomllib.load(file)

    assert "crcmod-plus==2.3.1" in document["project"]["dependencies"]
    groups = document["dependency-groups"]
    assert all(
        not requirement.lower().startswith("crcmod")
        for group in ("runtime-standard", "runtime-free-threaded")
        for requirement in groups[group]
    )
    assert {
        "package": {"name": "oss2"},
        "dependencies": ["crcmod"],
    } in document["tool"]["uv"]["exclude-dependencies"]


def test_windows_runtime_profiles_isolate_pywin32_dependency():
    """标准 Windows 保留 named-pipe 依赖，free-threaded profile 不解析 pywin32。"""
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as file:
        document = tomllib.load(file)

    dependencies = document["project"]["dependencies"]
    groups = document["dependency-groups"]
    assert not any(requirement.lower().startswith("pympler") for requirement in dependencies)
    assert "pywin32==312 ; sys_platform == 'win32'" in groups["runtime-standard"]
    assert not any(
        requirement.lower().startswith("pywin32")
        for requirement in groups["runtime-free-threaded"]
    )
    assert {
        "package": {"name": "docker"},
        "dependencies": ["pywin32"],
    } in document["tool"]["uv"]["exclude-dependencies"]


def test_standard_runtime_uses_shared_rust_text_capabilities(monkeypatch):
    """标准镜像的文本能力不得重新引入 profile 专属实现。"""
    imported = []
    moviepilot_rust = SimpleNamespace(
        is_available=lambda: True,
        jieba_cut=lambda _value: ["中文", "分词"],
        zhconv_fast=lambda value, _target: value,
    )

    def import_module(name):
        imported.append(name)
        return moviepilot_rust if name == "moviepilot_rust" else SimpleNamespace()

    monkeypatch.setattr(dependency_doctor, "import_module", import_module)
    monkeypatch.setattr(dependency_doctor.sysconfig, "get_config_var", lambda _name: None)

    dependency_doctor.main()

    assert "zhconv_rs" not in imported


def test_runtime_excluded_dependency_pairs_reads_uv_policy(tmp_path: Path):
    """运行时诊断应复用 uv 排除配置，不维护第二份包名特判。"""
    project_file = tmp_path / "pyproject.toml"
    project_file.write_text(
        """
[tool.uv]
exclude-dependencies = [
    { package = { name = "Demo_Package" }, dependencies = ["Legacy-Dep>=1"] },
]
""",
        encoding="utf-8",
    )

    assert dependencies.runtime_excluded_dependency_pairs(project_file) == {
        ("demo-package", "legacy-dep")
    }


def test_full_dependency_probe_rejects_psycopg_python_fallback(monkeypatch):
    """V3t 构建不得把 psycopg 纯 Python 实现误认为可发布能力。"""
    modules = {
        "moviepilot_rust": SimpleNamespace(
            is_available=lambda: True,
            jieba_cut=lambda _value: ["中文", "分词"],
            zhconv_fast=lambda value, _target: value,
        ),
        "crcmod.crcmod": SimpleNamespace(_usingExtension=True),
        "psycopg": SimpleNamespace(pq=SimpleNamespace(__impl__="python")),
    }
    monkeypatch.setattr(
        dependency_doctor,
        "import_module",
        lambda name: modules.get(name, SimpleNamespace()),
    )
    monkeypatch.setattr(
        dependency_doctor.sysconfig,
        "get_config_var",
        lambda name: 1 if name == "Py_GIL_DISABLED" else None,
    )
    monkeypatch.setattr(dependency_doctor.sys, "_is_gil_enabled", lambda: False)

    with pytest.raises(RuntimeError, match="psycopg C 实现不可用"):
        dependency_doctor.main(full=True)
