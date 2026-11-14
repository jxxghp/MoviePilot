"""
本地安装、迁移和 shell 启动入口的隔离审查回归。
"""

import importlib.util
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[1]


def _load_script(relative, monkeypatch):
    """
    加载待测脚本，并恢复临时模块登记。
    """
    spec = importlib.util.spec_from_file_location("setup_review_" + Path(relative).stem, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    return module


def _run_python(source, tmp_path):
    """
    用全新进程和独立 SQLite 配置检验正式 CLI 装配。
    """
    environment = {**os.environ, "CONFIG_DIR": str(tmp_path / "config"), "DEBUG": "false",
                   "DB_BACKUP_ENABLE": "false", "SUPERUSER": "review_admin",
                   "SUPERUSER_PASSWORD": "ReviewPassword123!", "API_TOKEN": "review-only-token-123456"}
    return subprocess.run([sys.executable, "-c", source], cwd=ROOT, env=environment,
                          capture_output=True, text=True, timeout=45)


def test_migration_generator_loads_all_canonical_models(tmp_path):
    """
    生成器在全新进程加载完整模型；真实 Alembic 比较不把现有表识别为删除。
    """
    result = _run_python('''
import runpy
from unittest.mock import patch
from sqlalchemy import create_engine
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from app.db.base import Base
from app.db.models.user import User
engine = create_engine("sqlite://")
User.__table__.create(engine)
def check_revision(config, version, autogenerate):
    assert autogenerate is True
    assert "site" in {name.lower() for name in Base.metadata.tables}
    with engine.connect() as connection:
        changes = compare_metadata(MigrationContext.configure(connection), Base.metadata)
    assert not any(change[0] == "remove_table" for change in changes)
with patch("builtins.input", return_value="review-only"), patch("alembic.command.revision", check_revision):
    runpy.run_path("database/gen.py", run_name="__main__")
engine.dispose()
''', tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("wizard", [False, True])
def test_admin_initialization_uses_valid_transaction(tmp_path, wizard):
    """
    非向导完整初始化及向导更新已存在管理员均提交真实短事务。
    """
    result = _run_python('''
import importlib.util
from sqlalchemy import select
from app.runtime.config import settings
from app.db.session import SessionFactory, async_session_scope
from app.db.models.user import User
from app.db.adapters.transaction import TransactionalWriteRunner
from app.db import uow
from app.application.security.token import get_password_hash, verify_password
spec = importlib.util.spec_from_file_location("setup", "scripts/local_setup.py")
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)
''' + ('''
from app.startup.initializers.database import prepare_database
runner = TransactionalWriteRunner(sync_session=SessionFactory, async_session=async_session_scope)
uow.configure_transaction_runners(sync=runner.sync, async_=runner.async_)
prepare_database()
from app.db.oper.user import UserOper
if UserOper().get_by_name("review_admin") is None:
    UserOper().add(name="review_admin", email="review@example.invalid", hashed_password=get_password_hash("OldPassword123!"), is_active=False, is_superuser=False, avatar="")
settings.SUPERUSER_PASSWORD = "ChangedPassword123!"
setup._apply_local_system_config_inner({})
expected_password = settings.SUPERUSER_PASSWORD
''' if wizard else '''
assert uow._sync_transaction_runner is None
setup._sync_superuser_account_inner()
expected_password = settings.SUPERUSER_PASSWORD
''') + '''
with SessionFactory() as session:
    user = session.execute(select(User).where(User.name == "review_admin")).scalar_one()
    assert user.is_active and user.is_superuser
    assert verify_password(expected_password, user.hashed_password)
''', tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_standalone_admin_cli_from_outside_checkout(tmp_path):
    """
    真实脚本入口不依赖调用目录或PYTHONPATH，且只初始化临时配置库。
    """
    config = tmp_path / "config"
    environment = {**os.environ, "PYTHONPATH": "", "CONFIG_DIR": str(config), "DEBUG": "false",
                   "DB_BACKUP_ENABLE": "false", "SUPERUSER": "review_admin",
                   "SUPERUSER_PASSWORD": "ReviewPassword123!", "API_TOKEN": "review-only-token-123456"}
    result = subprocess.run([sys.executable, str(ROOT / "scripts/local_setup.py"), "sync-superuser",
                             "--config-dir", str(config)], cwd=tmp_path, env=environment,
                            capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (config / "user.db").is_file()


@pytest.mark.parametrize("password", ['quote"slash\\secret', " leading and trailing ", "single'quote"])
def test_env_password_roundtrip_does_not_duplicate_escaping(tmp_path, monkeypatch, password):
    """
    向导留空保留旧密码时，读取序列化值必须还原原始字符。
    """
    module = _load_script("scripts/local_setup.py", monkeypatch)
    monkeypatch.setattr(module, "ENV_FILE", tmp_path / "app.env")
    monkeypatch.setattr(module, "ensure_local_dirs", lambda: None)
    module.write_env_value("SUPERUSER_PASSWORD", password)
    assert module.read_env_value("SUPERUSER_PASSWORD") == password


def test_frontend_dependency_failure_preserves_existing_installation(tmp_path, monkeypatch):
    """
    npm 安装失败保留旧静态文件和原 node_modules，不发布不完整的新版本。
    """
    module = _load_script("scripts/local_setup.py", monkeypatch)
    public = tmp_path / "public"
    (public / "node_modules").mkdir(parents=True)
    (public / "index.html").write_text("old page")
    (public / "node_modules/old").write_text("old dependency")
    archive = tmp_path / "dist.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("dist/version.txt", "v9.9.9")
        bundle.writestr("dist/index.html", "new page")
    npm = tmp_path / "npm"
    npm.touch()
    monkeypatch.setattr(module, "PUBLIC_DIR", public)
    monkeypatch.setattr(module, "install_node_runtime", lambda _version: tmp_path / "node")
    monkeypatch.setattr(module, "get_npm_bin", lambda: npm)

    def fail_dependencies(*_args, **_kwargs):
        """
        替换 npm 进程边界，不执行安装或联网。
        """
        raise subprocess.CalledProcessError(1, "isolated npm")

    monkeypatch.setattr(module, "run", fail_dependencies)
    with pytest.raises(subprocess.CalledProcessError):
        module.install_frontend("v9.9.9", "20", archive=archive)
    assert (public / "index.html").read_text() == "old page"
    assert (public / "node_modules/old").read_text() == "old dependency"


def test_frontend_publication_failure_restores_previous_directory(tmp_path, monkeypatch):
    """
    候选已经准备完成但发布rename失败时，旧页面仍被恢复到原目录。
    """
    module = _load_script("scripts/local_setup.py", monkeypatch)
    public, staged = tmp_path / "public", tmp_path / "staged"
    public.mkdir()
    staged.mkdir()
    (public / "index.html").write_text("old")
    (staged / "index.html").write_text("new")
    monkeypatch.setattr(module, "PUBLIC_DIR", public)
    original_rename = Path.rename

    def fail_publication(path, destination):
        """
        仅模拟候选目录的最终原子发布失败，旧备份恢复使用真实rename。
        """
        if path == staged:
            raise OSError("fixture publication failure")
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", fail_publication)
    with pytest.raises(OSError, match="fixture publication"):
        module._publish_frontend_installation(staged)
    assert (public / "index.html").read_text() == "old"
    assert not list(tmp_path.glob(".public.backup-*"))


@pytest.mark.parametrize("failure", [False, True])
def test_local_database_transaction_scope_restores_existing_runners(monkeypatch, failure):
    """
    命令不遗留全局事务执行器，操作成功与异常都恢复进入时的原端口。
    """
    from app.db import uow
    module = _load_script("scripts/local_setup.py", monkeypatch)
    previous = (uow._sync_transaction_runner, uow._async_transaction_runner)

    def operation():
        """
        观察执行范围内真实runner已装配，并按参数产生受控异常。
        """
        assert uow._sync_transaction_runner is not None
        assert uow._sync_transaction_runner is not previous[0]
        if failure:
            raise RuntimeError("fixture operation failure")

    if failure:
        with pytest.raises(RuntimeError, match="fixture operation"):
            module._run_with_local_database_transactions(operation)
    else:
        module._run_with_local_database_transactions(operation)
    assert (uow._sync_transaction_runner, uow._async_transaction_runner) == previous


def test_duplicate_config_migration_never_logs_secret_values(tmp_path, monkeypatch):
    """
    真实迁移仍删除旧重复项，日志仅含键与行号。
    """
    module = _load_script("database/versions/41ef1dd7467c_2_2_2.py", monkeypatch)
    engine = create_engine("sqlite:///" + str(tmp_path / "migration.db"))
    logger = Mock()
    monkeypatch.setattr(module, "logger", logger)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE SystemConfig (id INTEGER PRIMARY KEY, key TEXT, value TEXT)"))
            connection.execute(text("INSERT INTO SystemConfig VALUES (1, 'Password', 'fixture-secret'), (2, 'Password', 'new-secret')"))
            monkeypatch.setattr(module.op, "get_bind", lambda: connection)
            module.upgrade()
            assert connection.execute(text("SELECT id FROM SystemConfig")).scalars().all() == [2]
        assert "fixture-secret" not in str(logger.mock_calls)
        assert "Password" in str(logger.mock_calls)
    finally:
        engine.dispose()


def test_bootstrap_explicit_link_path_survives_os_detection(tmp_path):
    """
    正式参数解析和系统检测保留用户显式指定的命令路径。
    """
    prefix = (ROOT / "scripts/bootstrap-local.sh").read_text().split("\ndetect_os\n", 1)[0]
    path = tmp_path / "custom-bin/moviepilot"
    result = subprocess.run(["bash", "-c", prefix + '\ndetect_os\nprintf "%s\\n" "$LINK_PATH"',
                             "review", "--link-path", str(path)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(path)


def test_noninteractive_bootstrap_with_tty_skips_wizard():
    """
    有终端的自动安装仍遵守 --non-interactive。
    """
    source = (ROOT / "scripts/bootstrap-local.sh").read_text()
    prefix, main = source.split("\ndetect_os\n", 1)
    decision = main[main.index('if [[ "$NON_INTERACTIVE" != "true"'):main.index('\nmkdir -p "$WORKDIR"')]
    script = prefix + '\nHAS_TTY=true\n' + decision + '\nprintf "%s\\n" "$RUN_WIZARD"'
    result = subprocess.run(["bash", "-c", script, "review", "--non-interactive"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == "false"


@pytest.mark.parametrize("has_environment", [False, True])
def test_start_local_help_and_documented_dotvenv(tmp_path, has_environment):
    """
    无环境可查看帮助；uv sync 创建的 .venv 可执行实际启动分支。
    """
    (tmp_path / "scripts").mkdir()
    start = tmp_path / "scripts/start-local.sh"
    start.write_bytes((ROOT / "scripts/start-local.sh").read_bytes())
    cli = tmp_path / "moviepilot"
    cli.write_text("#!/bin/bash\nexit 99\n")
    cli.chmod(0o755)
    if has_environment:
        python = tmp_path / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.write_text('#!/bin/bash\nprintf "fixture python %s\\n" "$*"\n')
        python.chmod(0o755)
    result = subprocess.run(["bash", str(start), "backend" if has_environment else "help"],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert ("fixture python -m app.main" if has_environment else "用法") in result.stdout


@pytest.mark.parametrize("initial", ["0", "1"])
def test_pending_release_install_is_reachable_before_workers(tmp_path, initial):
    """
    不论自动更新是否已经完成，持久化 Release 安装清单都进入启动前恢复。
    """
    config = tmp_path / "config"
    manifest = config / "temp/moviepilot-update/install.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text("{}")
    control = tmp_path / "control"
    control.mkdir()
    (control / "update.sh").write_text("recover_pending_update() { return 0; }\nrun_moviepilot_update() { :; }\n")
    source = (ROOT / "docker/entrypoint.sh").read_text()
    begin = source.index('source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/update.sh"')
    end = source.index('source "${MP_CONTROL_DIR:-/usr/local/lib/moviepilot/control}/browser.sh"', begin)
    script = '''
INFO() { :; }
ERROR() { echo "$*" >&2; }
maybe_reexec_control_bundle() { :; }
cd() { :; }
apply_pending_release_update_at_startup() { echo APPLIED; return 0; }
reexec_entrypoint() { echo REEXEC; }
''' + source[begin:end]
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=5,
                            env={**os.environ, "CONFIG_DIR": str(config), "MP_CONTROL_DIR": str(control),
                                 "MOVIEPILOT_BOOTSTRAP_UPDATE_DONE": initial, "ONE_SHOT_DEV_UPDATE": "false"})
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["APPLIED", "REEXEC"]
