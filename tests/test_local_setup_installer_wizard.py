"""Cover installer wizard behavior that must work before MoviePilot starts."""

from __future__ import annotations

import builtins
import importlib.util
import sys
import types
import uuid
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """Load an isolated installer module for focused wizard tests."""
    module_name = f"moviepilot_local_setup_wizard_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_llm_provider_module_loads_when_repository_root_is_not_on_import_path(
    monkeypatch,
):
    """The standalone installer must expose app imports to the LLM provider."""
    module = load_local_setup_module()
    repository_root = str(module.ROOT)
    monkeypatch.setattr(
        module.sys,
        "path",
        [entry for entry in sys.path if entry != repository_root],
    )

    provider_module = module._load_llm_provider_module()

    assert module.sys.path[0] == repository_root
    assert hasattr(provider_module, "LLMProviderManager")
    monkeypatch.delitem(module.sys.modules, provider_module.__name__)


def test_startup_python_resolution_preserves_virtualenv_symlink(
    monkeypatch, tmp_path: Path
):
    """Autostart must invoke the venv launcher instead of its base interpreter."""
    module = load_local_setup_module()
    venv_dir = tmp_path / "venv"
    python_launcher = module.get_venv_python(venv_dir)
    python_launcher.parent.mkdir(parents=True)
    python_launcher.symlink_to(Path(sys.executable))
    expected_path = python_launcher.absolute()
    monkeypatch.setattr(
        module,
        "_can_run_moviepilot_cli",
        lambda candidate: candidate == expected_path,
    )

    runtime_python = module._resolve_runtime_python_for_startup(
        python_launcher, venv_dir
    )

    assert runtime_python == expected_path
    assert runtime_python.is_symlink()


def test_startup_python_resolution_prefers_project_runtime_launcher(
    monkeypatch, tmp_path: Path
):
    """Autostart must prefer the project runtime launcher over an external interpreter."""
    module = load_local_setup_module()
    venv_dir = tmp_path / "venv"
    runtime_launcher = module.get_venv_bin_dir(venv_dir) / "moviepilot-python"
    runtime_launcher.parent.mkdir(parents=True)
    runtime_launcher.symlink_to(Path(sys.executable))
    expected_path = runtime_launcher.absolute()
    external_python = tmp_path / "external-python"
    monkeypatch.setattr(
        module,
        "_can_run_moviepilot_cli",
        lambda candidate: candidate == expected_path,
    )

    runtime_python = module._resolve_runtime_python_for_startup(
        external_python, venv_dir
    )

    assert runtime_python == expected_path


def test_directory_config_keeps_download_path_as_downloader_path(monkeypatch):
    """The wizard must preserve downloader paths without resolving them locally."""
    module = load_local_setup_module()
    text_prompts: list[tuple[str, str]] = []
    path_prompts: list[str] = []
    monkeypatch.setattr(module, "print_step", lambda _message: None)

    def prompt_text(label: str, default: str) -> str:
        """Record downloader-path prompts and supply the container path."""
        text_prompts.append((label, default))
        return "/downloads"

    def prompt_path(label: str, default: Path) -> str:
        """Supply the host-side media library path."""
        assert isinstance(default, Path)
        path_prompts.append(label)
        return "/video"

    monkeypatch.setattr(module, "_prompt_text", prompt_text)
    monkeypatch.setattr(module, "_prompt_path", prompt_path)
    monkeypatch.setattr(module, "_prompt_choice", lambda *_args, **_kwargs: "copy")

    directory_config = module._collect_directory_config()

    assert directory_config["download_path"] == "/downloads"
    assert directory_config["library_path"] == "/video"
    assert text_prompts[0][0] == "下载器中的下载目录根路径"
    assert path_prompts == ["媒体库目录"]


def test_auth_site_definitions_load_without_database_transaction_runners(
    monkeypatch,
):
    """Static authentication fields must load before setup initializes the DB."""
    from app.db import uow

    module = load_local_setup_module()
    monkeypatch.setattr(uow, "_sync_transaction_runner", None)
    monkeypatch.setattr(uow, "_async_transaction_runner", None)

    class AuthSitesHelper:
        """Return a static auth schema while exercising the temporary UOW."""

        def get_authsites(self) -> dict[str, object]:
            """Read the active-site query through the temporary empty runner."""
            assert uow.run_sync_transaction(lambda _session: "queried") == []
            return {
                "example": {
                    "name": "示例站点",
                    "params": {
                        "cookie": {
                            "name": "Cookie",
                            "type": "text",
                            "placeholder": "填写 Cookie",
                            "tooltip": "用于站点认证",
                            "convert": "upper",
                        }
                    },
                }
            }

    sites_module = types.ModuleType("app.application.site.sites")
    sites_module.SitesHelper = AuthSitesHelper
    monkeypatch.setitem(sys.modules, "app.application.site.sites", sites_module)

    definitions = module._load_auth_site_definitions_inner()

    assert definitions == {
        "example": {
            "name": "示例站点",
            "params": {
                "cookie": {
                    "name": "Cookie",
                    "type": "text",
                    "placeholder": "填写 Cookie",
                    "tooltip": "用于站点认证",
                    "convert": "upper",
                }
            },
        }
    }
    assert uow._sync_transaction_runner is None
    assert uow._async_transaction_runner is None


def test_apply_config_does_not_create_downloader_internal_path(monkeypatch):
    """Applying installer config must create only host-side media directories."""
    module = load_local_setup_module()
    created_directories: list[str] = []
    original_import = builtins.__import__

    def fake_path(value: str):
        """Record directory creation without touching the host filesystem."""

        def mkdir(*, parents: bool, exist_ok: bool) -> None:
            """Record the requested directory creation."""
            assert parents and exist_ok
            created_directories.append(value)

        return types.SimpleNamespace(mkdir=mkdir)

    def fail_before_runtime_initialization(name: str, *args, **kwargs):
        """Stop before database setup after the path-creation phase."""
        if name == "app.application.classification.reference":
            raise ModuleNotFoundError("stop after path setup")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(module, "Path", fake_path)
    monkeypatch.setattr(builtins, "__import__", fail_before_runtime_initialization)

    with pytest.raises(RuntimeError, match="尚未安装 MoviePilot 运行依赖"):
        module._apply_local_system_config_inner(
            {
                "directories": [
                    {"download_path": "/downloads", "library_path": "/video"}
                ]
            }
        )

    assert created_directories == ["/video"]
