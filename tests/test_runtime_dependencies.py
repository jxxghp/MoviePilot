import tomllib
from pathlib import Path

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


def test_runtime_profiles_select_gil_safe_crcmod_distribution():
    project_file = Path(__file__).resolve().parents[1] / "pyproject.toml"
    with project_file.open("rb") as file:
        document = tomllib.load(file)

    groups = document["dependency-groups"]
    assert "crcmod==1.7" in groups["runtime-standard"]
    assert "crcmod-plus==2.3.1" in groups["runtime-free-threaded"]
    assert {
        "package": {"name": "oss2"},
        "dependencies": ["crcmod"],
    } in document["tool"]["uv"]["exclude-dependencies"]
