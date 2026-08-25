from pathlib import Path

from app.chain.system import SystemChain
from app.runtime import version as runtime_version


def test_installed_frontend_version_prefers_deployed_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    frontend_path = tmp_path / "public"
    frontend_path.mkdir()
    (frontend_path / "version.txt").write_text("v3.2.1\n", encoding="utf-8")
    monkeypatch.setattr(runtime_version, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_version, "is_windows", lambda: False)
    monkeypatch.setattr(
        runtime_version,
        "get_runtime_setting",
        lambda key: frontend_path if key == "FRONTEND_PATH" else tmp_path / "config",
    )

    assert runtime_version.get_frontend_version() == "v3.2.1"
    assert SystemChain.get_frontend_version() == "v3.2.1"


def test_installed_frontend_version_falls_back_to_release_declaration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(runtime_version, "is_frozen", lambda: False)
    monkeypatch.setattr(runtime_version, "is_windows", lambda: False)
    monkeypatch.setattr(runtime_version, "_FRONTEND_VERSION", "v3.0.0")
    monkeypatch.setattr(
        runtime_version,
        "get_runtime_setting",
        lambda key: tmp_path / key.lower(),
    )

    assert runtime_version.get_frontend_version() == "v3.0.0"
    assert (
        runtime_version.get_frontend_version(fallback_to_declared=False) is None
    )
