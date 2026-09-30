from __future__ import annotations

import importlib.util
import tempfile
import pytest
import uuid
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """隔离加载本地安装脚本，避免跨用例修改模块状态。"""
    module_name = f"moviepilot_local_setup_frontend_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_repo_frontend_version_reads_version_file():
    """前端固定版本取自当前源码声明。"""
    module = load_local_setup_module()

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        (root / "version.py").write_text(
            "APP_VERSION = 'v0.0.1'\nFRONTEND_VERSION = 'v9.9.9'\n",
            encoding="utf-8",
        )

        with patch.object(module, "ROOT", root):
            assert module._repo_frontend_version() == "v9.9.9"

def test_resolve_frontend_release_uses_repo_frontend_version_by_default():
    """普通模式请求源码声明的前端 Release。"""
    module = load_local_setup_module()
    release = {
        "tag_name": "v9.9.9",
        "assets": [
            {
                "name": "dist.zip",
                "browser_download_url": "https://example.com/dist.zip",
            }
        ],
    }

    with patch.object(module, "_repo_frontend_version", return_value="v9.9.9"), patch.object(
        module, "fetch_json", return_value=release
    ) as fetch_mock:
        tag_name, download_url = module._resolve_frontend_release(None)

    fetch_mock.assert_called_once_with(
        module.FRONTEND_TAG_API.format(tag="v9.9.9")
    )
    assert tag_name == "v9.9.9"
    assert download_url == "https://example.com/dist.zip"

def test_parser_leaves_frontend_version_empty_until_runtime_resolution():
    """未显式指定版本时延迟到运行阶段读取最新源码。"""
    module = load_local_setup_module()
    parser = module.build_parser()

    install_args = parser.parse_args(["install-frontend"])
    setup_args = parser.parse_args(["setup"])
    update_args = parser.parse_args(["update", "frontend"])

    assert install_args.version is None
    assert setup_args.frontend_version is None
    assert update_args.frontend_version is None


@pytest.mark.parametrize("enabled", ["true", "1", "yes", "false", "0"])
@pytest.mark.parametrize("target", ["backend", "frontend", "all"])
def test_update_uses_configured_dev_mode(monkeypatch, enabled, target):
    """DEV 配置作用于所有手动更新目标，前端从最新 Release 解析。"""
    module = load_local_setup_module()
    monkeypatch.delenv("MOVIEPILOT_UPDATE_DEV", raising=False)
    monkeypatch.setattr(module, "read_env_value", lambda _key: enabled)
    monkeypatch.setattr(module, "_git_output", lambda *_args: "v3")
    args = module.build_parser().parse_args(["update", target])
    assert module._resolve_update_versions(args) == (
        "latest", "latest" if enabled in {"true", "1", "yes"} else None
    )


@pytest.mark.parametrize("options, expected", [
    (["--dev"], ("v3", "latest")),
    (["--no-dev"], ("latest", None)),
    (["--dev", "--ref", "v3.0.0", "--frontend-version", "v3.0.0"], ("v3.0.0", "v3.0.0")),
    (["--offline-backend", "--ref", "v3.0.0", "--frontend-version", "v3.0.0"], ("v3.0.0", "v3.0.0")),
])
def test_explicit_update_options_and_detached_head(monkeypatch, options, expected):
    """显式选项覆盖 DEV 默认值，离线安装保持下载清单的版本。"""
    module = load_local_setup_module()
    monkeypatch.setenv("MOVIEPILOT_UPDATE_DEV", "true")
    monkeypatch.setattr(module, "_git_output", lambda *_args: "HEAD")
    args = module.build_parser().parse_args(["update", *options])
    assert args.target == "all"
    assert module._resolve_update_versions(args) == expected


def test_environment_overrides_saved_dev_config(monkeypatch):
    """进程环境配置优先于 app.env 中保存的 DEV 偏好。"""
    module = load_local_setup_module()
    monkeypatch.setenv("MOVIEPILOT_UPDATE_DEV", "false")
    monkeypatch.setattr(module, "read_env_value", lambda _key: "true")
    assert module._resolve_update_versions(module.build_parser().parse_args(["update"])) == ("latest", None)


def test_dev_frontend_downloads_latest_release():
    """latest 必须访问最新 Release API，不读取后端固定前端版本。"""
    module = load_local_setup_module()
    release = {"tag_name": "v3.0.11", "assets": [{"name": "dist.zip", "browser_download_url": "https://example.com/dist.zip"}]}
    with patch.object(module, "fetch_json", return_value=release) as fetch, patch.object(
        module, "_repo_frontend_version", side_effect=AssertionError("不应读取固定版本")
    ):
        assert module._resolve_frontend_release("latest")[0] == "v3.0.11"
    fetch.assert_called_once_with(module.FRONTEND_LATEST_API)


@pytest.mark.parametrize("target", ["backend", "frontend", "all"])
def test_update_command_passes_dev_selection_to_installers(monkeypatch, tmp_path, target):
    """命令入口把 DEV 选择传给实际安装边界，并保持后端先于前端执行。"""
    module = load_local_setup_module()
    monkeypatch.setenv("MOVIEPILOT_UPDATE_DEV", "true")
    monkeypatch.setattr(module.sys, "argv", ["local_setup.py", "update", target, "--skip-resources"])
    monkeypatch.setattr(module, "configure_config_dir", lambda **_: tmp_path)
    monkeypatch.setattr(module, "_git_output", lambda *_: "v3")
    calls = []
    with patch.object(module, "ensure_services_stopped"), patch.object(
        module, "update_backend", side_effect=lambda **kwargs: calls.append(("backend", kwargs["ref"]))
    ), patch.object(module, "install_frontend", side_effect=lambda **kwargs: calls.append(
        ("frontend", kwargs["frontend_version"])
    ) or {"version": "v3.0.11"}):
        assert module.main() == 0
    assert calls == [(name, "latest") for name in ("backend", "frontend") if target in {name, "all"}]


@pytest.mark.parametrize("fetch, branch", [(True, "v3"), (False, "v3"), (True, "HEAD")])
def test_switching_to_dev_branch_fetches_latest_tip(monkeypatch, tmp_path, fetch, branch):
    """切回开发分支后快进远端提交；离线安装和标签检出不拉取分支。"""
    module = load_local_setup_module()
    (tmp_path / ".git").touch()
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "_ensure_git_clean", lambda: None)
    monkeypatch.setattr(module, "_git_output", lambda *_: branch)
    with patch.object(module, "run") as run:
        assert module._update_backend_ref("v3", fetch=fetch) == "v3"
    pull_calls = [call.args[0] for call in run.call_args_list if call.args[0][:2] == ["git", "pull"]]
    assert pull_calls == ([["git", "pull", "--ff-only", "origin", "v3"]] if fetch and branch == "v3" else [])
