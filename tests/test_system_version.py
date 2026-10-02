from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.chain import system as system_module
from app.chain.system import SystemChain
from app.runtime import version as runtime_version


def test_installed_frontend_version_prefers_deployed_resource(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """当前前端版本优先读取已部署资源中的版本声明。"""
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
    """未部署前端时按调用约定回退到构建声明。"""
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


@pytest.fixture
def release_http(monkeypatch):
    """隔离发布查询的 HTTP 与运行配置，禁止访问真实 GitHub。"""
    http = Mock()
    monkeypatch.setattr(system_module, "_system_ports_snapshot", lambda: (http, None))
    monkeypatch.setattr(
        system_module,
        "get_chain_runtime_config_snapshot",
        lambda: SimpleNamespace(proxy=None, github_headers={}),
    )
    return http


@pytest.mark.parametrize(
    ("getter", "repository"),
    [
        (SystemChain._SystemChain__get_server_release_version, "MoviePilot"),
        (SystemChain._SystemChain__get_front_release_version, "MoviePilot-Frontend"),
    ],
)
@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["v2.15.6", "v3.0.9", "v3.1.0", "v3.0.10", "v4.0.0"], "v3.1.0"),
        (["v3.0.9", "v3.0.10", "v3.0.10-1"], "v3.0.10-1"),
        (["v2.15.6", "v30.1.0", "v4.0.0"], None),
        ([], None),
    ],
)
def test_remote_version_selects_v3(release_http, getter, repository, tags, expected):
    """前后端只比较 V3 标签的数值版本，缺少 V3 时不回退到其他代际。"""
    response = release_http.get.return_value
    response.json.return_value = [{"tag_name": tag} for tag in tags]

    assert getter() == expected
    release_http.get.assert_called_once_with(
        f"https://api.github.com/repos/jxxghp/{repository}/releases",
        proxies=None,
        headers={},
    )
    response.close.assert_called_once_with()


def test_remote_version_closes_invalid_response(release_http):
    """发布响应解析失败仍释放连接，并维持查询失败返回空值的约定。"""
    response = release_http.get.return_value
    response.json.side_effect = ValueError("invalid JSON")

    assert SystemChain._SystemChain__get_server_release_version() is None
    response.close.assert_called_once_with()


def test_version_command_reports_current_v3(release_http, monkeypatch):
    """重现当前前后端均为 V3 时，命令应报告最新版本而非 V2 远程版本。"""
    release_http.get.return_value.json.return_value = [
        {"tag_name": "v2.15.6"},
        {"tag_name": "v3.1.0"},
    ]
    monkeypatch.setattr(runtime_version, "get_app_version", lambda: "v3.1.0")
    monkeypatch.setattr(runtime_version, "get_frontend_version", lambda: "v3.1.0")
    post_message = Mock()
    monkeypatch.setattr(SystemChain, "post_message", post_message)

    SystemChain.version(object.__new__(SystemChain), channel=None, userid="test")

    post_message.assert_called_once()
    assert post_message.call_args.args[0].title == (
        "当前后端版本：v3.1.0，已是最新版本\n"
        "当前前端版本：v3.1.0，已是最新版本"
    )
