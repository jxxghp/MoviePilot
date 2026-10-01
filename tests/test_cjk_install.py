"""验证原生扩展安装、目标解释器验收及部署入口，不访问用户数据。"""

import importlib.util
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from app.agent.history import schema
from app.agent.history.storage import SqliteRecallRepository
from app.application.messaging.recall import RecallMessage, RecallQuery, RecallSession

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "native/fts5_cjk/install.py"


def load_script(path: Path):
    """独立加载纯标准库脚本，避免安装脚本的全局状态污染其他测试。"""
    spec = importlib.util.spec_from_file_location(f"test_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def installed_extension(tmp_path_factory):
    """在临时目录用生产安装入口真实编译一次，不写入测试环境或用户目录。"""
    if not any(shutil.which(name) for name in ("cc", "gcc", "clang", "cl")):
        pytest.skip("原生安装验收需要本机 C 编译器")
    extension = tmp_path_factory.mktemp("cjk-install") / "libfts5_cjk.so"
    subprocess.run([sys.executable, str(INSTALLER), "--output", str(extension)], check=True, capture_output=True)
    return extension


def test_installed_extension_activates_history_and_backfills_old_messages(tmp_path, monkeypatch, installed_extension):
    """补装后旧消息完成后台回填，两字搜索切换为 cjk，用户数据仍位于配置目录。"""
    bundled = tmp_path / "venv/lib/moviepilot/libfts5_cjk.so"
    monkeypatch.setattr(schema, "BUNDLED_CJK_EXTENSION", bundled)
    repository = SqliteRecallRepository(tmp_path / "config/agent/runtime")
    repository.append("alice", RecallSession("old"), (RecallMessage("m", "user", "电影订阅成功"),))
    assert repository.search("alice", RecallQuery(query="订阅"))["search_path"] == "like"
    bundled.parent.mkdir(parents=True)
    shutil.copy2(installed_extension, bundled)
    while repository.maintain("alice")["pending"]:
        pass
    result = repository.search("alice", RecallQuery(query="订阅"))
    assert result["search_path"] == "cjk" and result["count"] == 1
    assert result["index_status"]["messages_fts_cjk"]
    assert repository.database_path("alice").is_relative_to(tmp_path / "config")


def test_repeated_install_validates_without_recompiling(monkeypatch, installed_extension):
    """同源码和架构重复安装不需要编译器，检查模式同样只读。"""
    installer = load_script(INSTALLER)
    previous = installed_extension.stat().st_mtime_ns

    def unexpected_compile(_output):
        """有有效缓存时调用编译器即为回归。"""
        pytest.fail("有效扩展不应重新编译")

    monkeypatch.setattr(installer, "compiler_command", unexpected_compile)
    installer.install(installed_extension)
    subprocess.run([sys.executable, str(INSTALLER), "--output", str(installed_extension), "--check"],
                   check=True, capture_output=True)
    assert installed_extension.stat().st_mtime_ns == previous


def test_cli_installs_into_selected_venv_and_check_is_readonly(tmp_path, installed_extension):
    """真实 CLI 在独立 venv 中安装、检查，运行时可从该解释器前缀加载。"""
    assert installed_extension.is_file()
    venv = tmp_path / "selected-venv"
    subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(venv)], check=True, capture_output=True)
    setup = ROOT / "scripts/local_setup.py"
    command = [sys.executable, str(setup), "install-cjk", "--venv", str(venv)]
    result = subprocess.run(command, check=True, capture_output=True, text=True)
    extension = venv / "lib/moviepilot/libfts5_cjk.so"
    assert extension.is_file() and "两字中文索引验证" in result.stdout
    previous = extension.stat().st_mtime_ns
    subprocess.run([*command, "--check"], check=True, capture_output=True)
    assert extension.stat().st_mtime_ns == previous
    python = venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
    probe = (
        "import sqlite3; from pathlib import Path; from app.agent.history.schema import load_cjk; "
        "assert load_cjk(sqlite3.connect(':memory:'), Path('missing-legacy-extension'))"
    )
    subprocess.run([str(python), "-c", probe], cwd=ROOT, check=True, capture_output=True)


def test_failed_rebuild_keeps_working_extension(tmp_path, monkeypatch, installed_extension):
    """编译失败不覆盖已安装文件，也不留下临时编译目录。"""
    installer = load_script(INSTALLER)
    extension = tmp_path / "libfts5_cjk.so"
    shutil.copy2(installed_extension, extension)
    original = extension.read_bytes()
    monkeypatch.setattr(installer, "compiler_command", lambda _output: [sys.executable, "-c", "raise SystemExit(1)"])
    with pytest.raises(subprocess.CalledProcessError):
        installer.install(extension)
    assert extension.read_bytes() == original
    assert not list(tmp_path.glob(".fts5-cjk-*"))
    installer.verify(extension)


def test_invalid_bundled_extension_falls_back_to_legacy_and_disables_loading(tmp_path, monkeypatch, installed_extension):
    """跨架构或损坏的自带文件不屏蔽旧手工安装，扩展加载权限始终关闭。"""
    invalid = tmp_path / "libfts5_cjk.so"
    invalid.write_bytes(b"invalid library")
    monkeypatch.setattr(schema, "BUNDLED_CJK_EXTENSION", invalid)
    with sqlite3.connect(":memory:") as connection:
        assert schema.load_cjk(connection, installed_extension)
        with pytest.raises(sqlite3.OperationalError, match="not authorized"):
            connection.load_extension(str(installed_extension))


def test_missing_compiler_reports_repair_command(monkeypatch):
    """缺少编译器时明确说明补装方式，不能宣称扩展已启用。"""
    installer = load_script(INSTALLER)
    monkeypatch.delenv("CC", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda _name: None)
    with pytest.raises(RuntimeError, match="moviepilot install cjk"):
        installer.compiler_command(Path("unused"))


def test_cli_uses_selected_interpreter_and_distinguishes_optional_failure(tmp_path, monkeypatch, capsys):
    """自动安装告警降级，显式补装失败返回错误；两者都使用指定 venv。"""
    setup = load_script(ROOT / "scripts/local_setup.py")
    python = tmp_path / "custom-venv/bin/python"
    calls = []

    def fail(command, **_kwargs):
        """模拟目标解释器缺少扩展加载能力。"""
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr="SQLite extension loading unavailable")

    monkeypatch.setattr(setup.subprocess, "run", fail)
    setup.install_cjk(python, required=False)
    assert "回退" in capsys.readouterr().out
    with pytest.raises(RuntimeError, match="SQLite extension loading unavailable"):
        setup.install_cjk(python, check=True)
    assert calls == [[str(python), str(INSTALLER)], [str(python), str(INSTALLER), "--check"]]


def test_dependency_install_includes_cjk_and_parser_supports_repair(tmp_path, monkeypatch):
    """deps 是 setup 和后端升级的共用入口，每次同步后都检查 CJK 扩展。"""
    setup = load_script(ROOT / "scripts/local_setup.py")
    calls = []
    monkeypatch.setattr(setup, "ensure_supported_python", lambda _python: None)
    monkeypatch.setattr(setup, "require_uv", lambda: tmp_path / "uv")
    monkeypatch.setattr(setup, "expose_uv_to_venv", lambda *_args: None)
    monkeypatch.setattr(setup, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(setup, "install_browser_runtime", lambda _python: None)
    monkeypatch.setattr(setup, "install_cjk", lambda python, **kwargs: calls.append((python, kwargs)))
    python = setup.install_deps(python_bin=sys.executable, venv_dir=tmp_path / "venv", recreate=False)
    assert calls == [(python, {"required": False})]
    args = setup.build_parser().parse_args(["install-cjk", "--venv", str(tmp_path), "--check"])
    assert args.venv == str(tmp_path) and args.check


def test_docker_packages_extension_outside_config_and_verifies_selected_python():
    """镜像按目标架构构建并随 venv 交付，普通和 free-threaded 环境均需真实验收。"""
    dockerfile = (ROOT / "docker/Dockerfile").read_text(encoding="utf-8")
    stage = dockerfile.split("FROM base AS prepare_cjk", 1)[1].split("FROM base AS prepare_package", 1)[0]
    assert "build-essential" in stage
    assert "install.py --output /native/libfts5_cjk.so" in stage
    selected = dockerfile.split("FROM verify_venv_${MOVIEPILOT_PYTHON_VARIANT} AS prepare_venv", 1)[1]
    assert "COPY --from=prepare_cjk /native/ ${VENV_PATH}/lib/moviepilot/" in selected
    assert '"${VENV_PATH}/bin/python" /tmp/verify-cjk.py --check' in selected
    assert "/config/" not in stage
