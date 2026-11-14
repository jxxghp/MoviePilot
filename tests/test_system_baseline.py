"""
验证 Docker 镜像系统基线：发布声明、镜像构建契约与应用内/Dev 更新门禁。
"""

import json
import re
import subprocess
import textwrap
import threading
import zipfile
from datetime import date
from pathlib import Path

import pytest

from app.adapters.system import update as update_module

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "docker" / "update.sh"
_BASELINE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _release_declarations() -> dict[str, str]:
    """
    按发布工作流 release-gate 的 sed 规则解析 version.py。
    """
    declarations: dict[str, str] = {}
    pattern = re.compile(
        r"^(APP_VERSION|FRONTEND_VERSION|MIN_SYSTEM_BASELINE)[ \t]*=[ \t]*'([^']+)'[ \t]*$"
    )
    for line in (ROOT / "version.py").read_text(encoding="utf-8").splitlines():
        match = pattern.match(line)
        if match:
            assert match.group(1) not in declarations
            declarations[match.group(1)] = match.group(2)
    return declarations


def test_release_declares_valid_min_system_baseline() -> None:
    """
    发布声明的最低系统基线必须是有效日期，且不高于本仓镜像写入的基线。
    """
    declarations = _release_declarations()
    image_baseline = (ROOT / "docker" / "system-baseline").read_text(encoding="utf-8").strip()

    assert set(declarations) == {"APP_VERSION", "FRONTEND_VERSION", "MIN_SYSTEM_BASELINE"}
    for value in (declarations["MIN_SYSTEM_BASELINE"], image_baseline):
        assert _BASELINE_PATTERN.fullmatch(value)
        date.fromisoformat(value)
    # 同一发布构建的镜像必须满足自身声明，否则新镜像也会拒绝自己的更新包。
    assert declarations["MIN_SYSTEM_BASELINE"] <= image_baseline
    assert (
        update_module.SystemUpdateManager._LEGACY_SYSTEM_BASELINE
        <= declarations["MIN_SYSTEM_BASELINE"]
    )


def test_dockerfile_keeps_system_refresh_and_browser_deps_cache_boundaries() -> None:
    """
    系统刷新层位于虚拟环境之后、--link 载荷之前；浏览器依赖层不再依赖虚拟环境内容。
    """
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    final_stage = dockerfile[dockerfile.index("FROM prepare_browser_deps AS final") :]
    browser_stage = dockerfile[
        dockerfile.index("FROM prepare_package AS prepare_browser_deps") : dockerfile.index(
            "# 准备 Python 环境的公共输入"
        )
    ]

    assert "playwright install-deps chromium" in browser_stage
    assert "${VENV_PATH}" not in browser_stage
    assert "COPY --from=prepare_playwright_requirement" in browser_stage
    assert "--require-hashes" in browser_stage
    assert "playwright install-deps" not in final_stage

    venv_copy = final_stage.index("COPY --from=prepare_venv --chmod=777 ${VENV_PATH} ${VENV_PATH}")
    refresh_arg = final_stage.index('ARG MOVIEPILOT_SYSTEM_REFRESH_ID=""')
    stamp = final_stage.index("COPY docker/system-baseline /etc/moviepilot/system-baseline")
    upgrade = final_stage.index("upgrade; \\")
    first_payload = final_stage.index("COPY --link --from=prepare_frontend")
    assert venv_copy < refresh_arg < stamp < upgrade < first_payload
    # 刷新键之后只能是不继承父层的 --link 载荷，避免每周刷新连带重建其他层。
    tail = final_stage[upgrade:]
    assert "\nRUN " not in tail
    assert all(
        line.startswith("COPY --link")
        for line in tail.splitlines()
        if line.startswith("COPY ")
    )


def _manager(monkeypatch, tmp_path: Path, *, docker: bool = True, stamp: str | None):
    """
    创建使用临时状态目录和指定镜像基线文件的更新管理器。
    """
    monkeypatch.setattr(update_module, "get_app_version", lambda: "v3.0.0")
    monkeypatch.setattr(
        update_module,
        "get_runtime_setting",
        lambda key: tmp_path if key == "TEMP_PATH" else None,
    )
    monkeypatch.setattr(update_module, "is_docker", lambda: docker)
    baseline_file = tmp_path / "etc" / "system-baseline"
    if stamp is not None:
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(stamp, encoding="utf-8")
    monkeypatch.setattr(
        update_module.SystemUpdateManager, "_SYSTEM_BASELINE_FILE", baseline_file
    )
    manager = object.__new__(update_module.SystemUpdateManager)
    manager._lock = threading.RLock()
    manager._download_active = False
    manager._active_target = None
    return manager


def _prepare_download(monkeypatch, tmp_path: Path, manager, min_baseline: str | None) -> list[str]:
    """
    准备离线的后端与前端归档，返回实际发起的下载记录。
    """
    version_source = "APP_VERSION = 'v3.1.0'\nFRONTEND_VERSION = 'v3.1.0'\n"
    if min_baseline is not None:
        version_source += f"MIN_SYSTEM_BASELINE = '{min_baseline}'\n"
    backend_fixture = tmp_path / "source-backend.zip"
    frontend_fixture = tmp_path / "source-frontend.zip"
    with zipfile.ZipFile(backend_fixture, "w") as archive:
        archive.writestr("MoviePilot-v3.1.0/version.py", version_source)
        archive.writestr("MoviePilot-v3.1.0/pyproject.toml", "[project]\n")
        archive.writestr("MoviePilot-v3.1.0/uv.lock", "version = 1\n")
    with zipfile.ZipFile(frontend_fixture, "w") as archive:
        archive.writestr("dist/index.html", "ok")
        archive.writestr("dist/version.txt", "v3.1.0\n")

    fixtures = iter((backend_fixture, frontend_fixture))
    downloads: list[str] = []

    def download(url, destination, downloaded_before, _total_hint):
        downloads.append(url)
        source = next(fixtures)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        size = destination.stat().st_size
        return downloaded_before + size, size

    monkeypatch.setattr(manager, "_download_file", download)
    monkeypatch.setattr(manager, "_prepare_local_backend_ref", lambda _version: None)
    monkeypatch.setattr(
        manager,
        "_fetch_frontend_release",
        lambda _version: {
            "assets": [
                {
                    "name": "dist.zip",
                    "size": frontend_fixture.stat().st_size,
                    "browser_download_url": "https://example.invalid/dist.zip",
                    "digest": f"sha256:{manager._sha256(frontend_fixture)}",
                }
            ]
        },
    )
    manager._write_state(state="downloading", version="v3.1.0")
    manager._active_target = "application"
    return downloads


@pytest.mark.parametrize(
    ("stamp", "min_baseline"),
    [
        ("2026-10-22\n", "2026-10-22"),
        ("2026-11-01", "2026-10-22"),
        (None, "2026-01-01"),
        (None, None),
        ("2026-01-01", None),
    ],
    ids=["equal", "newer-image", "missing-stamp-at-legacy-floor", "undeclared", "undeclared-with-stamp"],
)
def test_download_accepts_image_meeting_system_baseline(monkeypatch, tmp_path, stamp, min_baseline):
    """
    镜像基线满足要求、目标未声明或要求不高于旧镜像下限时，更新包照常进入可安装状态。
    """
    manager = _manager(monkeypatch, tmp_path, stamp=stamp)
    _prepare_download(monkeypatch, tmp_path, manager, min_baseline)

    manager._download_update("application")

    status = manager.get_status()
    assert status.state == "ready"
    assert status.error is None
    assert (manager._root / "prepared.json").exists()


@pytest.mark.parametrize(
    ("stamp", "current"),
    [
        ("2026-10-22", "2026-10-22"),
        (None, "未标记"),
        ("not-a-date", "未标记"),
        ("2026-02-30", "未标记"),
    ],
    ids=["older-image", "missing-stamp", "malformed-stamp", "impossible-date-stamp"],
)
def test_download_refuses_image_below_system_baseline(monkeypatch, tmp_path, stamp, current):
    """
    镜像基线低于目标要求（缺失或无效视为最旧）时拒绝更新，不下载前端也不进入可安装状态。
    """
    manager = _manager(monkeypatch, tmp_path, stamp=stamp)
    downloads = _prepare_download(monkeypatch, tmp_path, manager, "2026-11-05")

    manager._download_update("application")

    status = manager.get_status()
    item = next(item for item in status.updates if item.type == "application")
    assert item.state == "failed"
    assert item.can_install is False
    assert item.error == (
        "该版本包含系统组件安全更新，需要先更新 Docker 镜像"
        f"（要求系统基线 2026-11-05，当前 {current}）"
    )
    assert len(downloads) == 1
    assert not (manager._root / "prepared.json").exists()


def test_download_refuses_malformed_target_system_baseline(monkeypatch, tmp_path):
    """
    目标版本声明了无效系统基线时按失败处理，避免错误发布绕过门禁。
    """
    manager = _manager(monkeypatch, tmp_path, stamp="2026-10-22")
    _prepare_download(monkeypatch, tmp_path, manager, "2026/11/05")

    manager._download_update("application")

    item = next(item for item in manager.get_status().updates if item.type == "application")
    assert item.state == "failed"
    assert item.error == "目标版本声明的系统基线无效：2026/11/05"


def test_non_docker_download_skips_system_baseline(monkeypatch, tmp_path):
    """
    本地源码部署的系统组件不随镜像发布，不执行镜像基线门禁。
    """
    manager = _manager(monkeypatch, tmp_path, docker=False, stamp=None)
    _prepare_download(monkeypatch, tmp_path, manager, "2099-01-01")

    manager._download_update("application")

    assert manager.get_status().state == "ready"
    prepared = json.loads((manager._root / "prepared.json").read_text(encoding="utf-8"))
    assert prepared["version"] == "v3.1.0"


def _run_update_check(tmp_path: Path, version_source: str, stamp: str | None) -> subprocess.CompletedProcess:
    """
    以 Dev 更新入口运行 update.sh，下载被替换为写入指定 version.py 的离线归档。
    """
    update_dir = tmp_path / "update"
    app_dir = update_dir / "App"
    app_dir.mkdir(parents=True)
    (app_dir / "version.py").write_text(version_source, encoding="utf-8")
    (app_dir / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
    (app_dir / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    baseline_file = tmp_path / "system-baseline"
    if stamp is not None:
        baseline_file.write_text(stamp, encoding="utf-8")
    route_marker = tmp_path / "route-called"
    script = textwrap.dedent(
        f"""\
        CONFIG_DIR="$1"
        TMP_PATH="$2"
        ROUTE_MARKER="$3"
        export MOVIEPILOT_SYSTEM_BASELINE_FILE="$4"
        PIP_PROXY= PROXY_HOST=
        source {UPDATER!s}
        INFO() {{ :; }}
        ERROR() {{ :; }}
        download_and_unzip() {{ return 0; }}
        dependency_manifests_changed() {{ touch "${{ROUTE_MARKER}}"; return 1; }}
        install_backend_and_download_resources heads/v3.zip
        """
    )
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "system-baseline-test",
            str(tmp_path / "config"),
            str(update_dir),
            str(route_marker),
            str(baseline_file),
        ],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.mark.parametrize(
    ("version_source", "stamp", "message"),
    [
        (
            "MIN_SYSTEM_BASELINE = '2026-11-05'\n",
            "2026-10-22\n",
            "要求系统基线 2026-11-05，当前 2026-10-22",
        ),
        ("MIN_SYSTEM_BASELINE = '2026-11-05'\n", None, "要求系统基线 2026-11-05，当前 未标记"),
        ("MIN_SYSTEM_BASELINE = '2026-11-05'\n", "garbage", "要求系统基线 2026-11-05，当前 未标记"),
        ("MIN_SYSTEM_BASELINE = 'soon'\n", "2026-10-22", "更新包声明的系统基线无效（soon）"),
    ],
    ids=["older-image", "missing-stamp", "malformed-stamp", "malformed-target"],
)
def test_dev_update_refuses_image_below_system_baseline(tmp_path, version_source, stamp, message):
    """
    Dev 更新在依赖检查和载荷替换之前拒绝不满足系统基线的镜像，并输出警告。
    """
    result = _run_update_check(tmp_path, version_source, stamp)

    assert result.returncode == 1
    assert message in result.stdout
    assert "WARN" in result.stdout
    assert not (tmp_path / "route-called").exists()


@pytest.mark.parametrize(
    ("version_source", "stamp"),
    [
        ("MIN_SYSTEM_BASELINE = '2026-10-22'\n", "2026-10-22"),
        ("MIN_SYSTEM_BASELINE = '2026-10-22'\n", "2026-12-01"),
        ("MIN_SYSTEM_BASELINE = '2026-01-01'\n", None),
        ("FRONTEND_VERSION = 'v3.0.1'\n", None),
    ],
    ids=["equal", "newer-image", "missing-stamp-at-legacy-floor", "undeclared"],
)
def test_dev_update_continues_when_image_meets_system_baseline(tmp_path, version_source, stamp):
    """
    镜像满足系统基线时 Dev 更新继续进入依赖检查阶段。
    """
    result = _run_update_check(tmp_path, version_source, stamp)

    assert "系统基线" not in result.stdout
    assert (tmp_path / "route-called").exists()
