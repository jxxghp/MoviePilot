"""
验证部署 secret 的读取、来源顺序、坏文件隔离与配置写回保护。
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from app.runtime.config import Settings, _get_secrets_dir


@pytest.fixture
def secret_config(tmp_path, monkeypatch):
    """
    为每个用例隔离环境、环境文件和 secret 目录。

    :yields: 配置构造函数、secret 目录、环境文件路径和收集的安全告警
    """
    for name in Settings.model_fields:
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / "secrets"
    directory.mkdir()
    env_file = tmp_path / "app.env"
    env_file.write_text("# isolated\n", encoding="utf-8")
    messages = []
    monkeypatch.setattr("app.runtime.config.get_env_path", lambda: env_file)
    monkeypatch.setattr("app.runtime.config.logger.warning", messages.append)

    def create(**kwargs: Any) -> Settings:
        """
        在临时目录内构造真实配置实例。
        """
        options = dict(_secrets_dir=directory, _env_file=env_file, CONFIG_DIR=str(tmp_path))
        options.update(kwargs)
        return Settings(**options)

    yield create, directory, env_file, messages


def test_secrets_priority_and_no_environment_export(secret_config, monkeypatch):
    """
    构造参数、进程环境、dotenv、secret 与默认值按原有顺序生效。
    """
    create, directory, env_file, _messages = secret_config
    (directory / "LOG_MAX_FILE_SIZE").write_text("7\n", encoding="utf-8")
    assert create().LOG_MAX_FILE_SIZE == 7
    env_file.write_text("LOG_MAX_FILE_SIZE='8'\n", encoding="utf-8")
    assert create().LOG_MAX_FILE_SIZE == 8
    monkeypatch.setenv("LOG_MAX_FILE_SIZE", "9")
    assert create().LOG_MAX_FILE_SIZE == 9
    assert create(LOG_MAX_FILE_SIZE=10).LOG_MAX_FILE_SIZE == 10
    monkeypatch.delenv("LOG_MAX_FILE_SIZE")
    env_file.write_text("# isolated\n", encoding="utf-8")
    (directory / "LOG_MAX_FILE_SIZE").unlink()
    assert create().LOG_MAX_FILE_SIZE == 5
    token = "file-token-0123456789"
    (directory / "API_TOKEN").write_text(token + "\n", encoding="utf-8")
    before = env_file.read_bytes()
    assert create().API_TOKEN == token
    assert "API_TOKEN" not in os.environ
    assert env_file.read_bytes() == before


@pytest.mark.parametrize("name, raw, expected", [
    ("DEBUG", "true", True),
    ("LOG_MAX_FILE_SIZE", "9", 9),
    ("ALLOWED_HOSTS", '["https://example.test"]', ["https://example.test"]),
    ("DB_CONNECT_ARGS", '{"connect_timeout":10}', {"connect_timeout": 10}),
    ("GITHUB_TOKEN", "\ufefffile-token-0123456789\r\n", "file-token-0123456789"),
    ("GITHUB_TOKEN", "first\nsecond\n", "first\nsecond"),
    ("GITHUB_TOKEN", "'quoted-value'", "'quoted-value'"),
])
def test_secrets_formats(secret_config, name, raw, expected):
    """
    标量与 JSON 复合值按字段解析，BOM 和末尾换行不进入生效值。
    """
    create, directory, env_file, _messages = secret_config
    (directory / name).write_text(raw, encoding="utf-8")
    before = env_file.read_bytes()
    assert getattr(create(), name) == expected
    assert env_file.read_bytes() == before


@pytest.mark.parametrize("name, raw", [
    ("API_TOKEN", b"short-token"),
    ("API_TOKEN", b"\xff\xfe"),
    ("DEBUG", b"notabool-sensitive"),
    ("LOG_MAX_FILE_SIZE", b"notanumber-sensitive"),
    ("LOG_LEVEL", b""),
    ("ALLOWED_HOSTS", b"not-json-sensitive"),
    ("ALLOWED_HOSTS", b"{}"),
    ("DB_CONNECT_ARGS", b"[]"),
    ("DB_CONNECT_ARGS", b"not-json-sensitive"),
    ("API_WORKERS", b"0"),
])
def test_invalid_secret_is_skipped_without_write_or_leak(secret_config, name, raw):
    """
    不可读编码、空值、类型和约束错误只影响单项，不写环境文件或泄露原文。
    """
    create, directory, env_file, messages = secret_config
    baseline = create()
    (directory / name).write_bytes(raw)
    (directory / "GITHUB_TOKEN").write_text("valid-other-file", encoding="utf-8")
    before = env_file.read_bytes()
    config = create()
    assert getattr(config, name) == getattr(baseline, name)
    assert config.GITHUB_TOKEN == "valid-other-file"
    assert config.update_setting(name, "replacement")[0] is False
    assert env_file.read_bytes() == before
    assert messages and name in messages[-1]
    assert "sensitive" not in " ".join(messages)
    assert "short-token" not in " ".join(messages)


def test_secret_is_readonly_even_when_shadowed(secret_config):
    """
    已存在 secret 的字段不能被界面改写，历史 dotenv 遮蔽需手工移除。
    """
    create, directory, env_file, _messages = secret_config
    (directory / "DEBUG").write_text("true", encoding="utf-8")
    config = create()
    assert config.update_setting("DEBUG", True) == (None, "")
    success, message = config.update_setting("DEBUG", False)
    assert success is False and "secret" in message
    assert config.DEBUG is True
    env_file.write_text("DEBUG='False'\n", encoding="utf-8")
    config = create()
    assert config.DEBUG is False
    assert config.update_setting("DEBUG", True)[0] is False
    env_file.write_text("# removed override\n", encoding="utf-8")
    assert create().DEBUG is True


def test_shadowed_secret_prevents_automatic_correction_write(secret_config):
    """
    即使高优先级文件需要自动修正，也不能覆盖外部管理的 secret 项。
    """
    create, directory, env_file, _messages = secret_config
    (directory / "DEBUG").write_text("true", encoding="utf-8")
    env_file.write_text("DEBUG='bad'\n", encoding="utf-8")
    before = env_file.read_bytes()
    assert create().DEBUG is False
    assert env_file.read_bytes() == before


def test_secrets_rotation_and_instance_isolation(secret_config):
    """
    文件轮换仅新实例读取；禁用 secrets 的实例不会继承其他实例的写回锁。
    """
    create, directory, _env_file, _messages = secret_config
    path = directory / "GITHUB_TOKEN"
    path.write_text("before", encoding="utf-8")
    first = create()
    path.write_text("after", encoding="utf-8")
    assert first.GITHUB_TOKEN == "before"
    assert create().GITHUB_TOKEN == "after"
    plain = create(_secrets_dir=())
    assert plain.update_setting("GITHUB_TOKEN", "local")[0] is True


@pytest.mark.parametrize("kind", ["missing", "file", "unreadable", "entry_directory"])
def test_unavailable_secret_paths(secret_config, monkeypatch, kind):
    """
    缺失目录不告警，错误目录或无权限文件按项告警并保留默认值。
    """
    create, directory, _env_file, messages = secret_config
    if kind == "missing":
        directory.rmdir()
    elif kind == "file":
        directory.rmdir()
        directory.write_text("not-a-directory", encoding="utf-8")
    elif kind == "entry_directory":
        (directory / "API_TOKEN").mkdir()
    else:
        (directory / "API_TOKEN").write_text("unreadable-token", encoding="utf-8")
        original = Path.read_text

        def read(path, *args, **kwargs):
            """
            仅对目标 secret 模拟运行用户无读取权限。
            """
            if path == directory / "API_TOKEN":
                raise PermissionError
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", read)
    assert create().API_TOKEN is None
    assert bool(messages) is (kind != "missing")


def test_kubernetes_symlink_and_exact_case(secret_config):
    """
    K8s 的投影符号链接可读，小写文件名不能冒充大写配置名。
    """
    create, directory, env_file, _messages = secret_config
    (directory / "api_token").write_text("lowercase-token-012345", encoding="utf-8")
    assert create().API_TOKEN is None
    (directory / "api_token").unlink()
    data = directory / "..data"
    data.mkdir()
    (data / "API_TOKEN").write_text("projected-token-012345", encoding="utf-8")
    (directory / "API_TOKEN").symlink_to("..data/API_TOKEN")
    before = env_file.read_bytes()
    assert create().API_TOKEN == "projected-token-012345"
    assert env_file.read_bytes() == before


@pytest.mark.parametrize("value", ["", "false", "off", "0", "FALSE"])
def test_secrets_directory_can_be_disabled(monkeypatch, value):
    """
    环境变量可显式关闭 secrets，专用变量优先于通用变量。
    """
    monkeypatch.setenv("SECRETS_DIR", "/other/secrets")
    monkeypatch.setenv("MOVIEPILOT_SECRETS_DIR", value)
    assert _get_secrets_dir() is None


def test_secrets_directory_default_and_override(monkeypatch):
    """
    默认路径与两个进程环境变量均有明确优先级。
    """
    monkeypatch.delenv("MOVIEPILOT_SECRETS_DIR", raising=False)
    monkeypatch.delenv("SECRETS_DIR", raising=False)
    assert _get_secrets_dir() == "/run/secrets"
    monkeypatch.setenv("SECRETS_DIR", "/custom/secrets")
    assert _get_secrets_dir() == "/custom/secrets"
    monkeypatch.setenv("MOVIEPILOT_SECRETS_DIR", "/preferred/secrets")
    assert _get_secrets_dir() == "/preferred/secrets"


def test_secrets_directory_is_indexed_once(secret_config, monkeypatch):
    """
    无关文件增多时仍只扫描一次目录，而非逐字段重复扫描。
    """
    create, directory, _env_file, _messages = secret_config
    for index in range(100):
        (directory / f"unrelated-{index}").touch()
    (directory / "DEBUG").write_text("true", encoding="utf-8")
    original = Path.iterdir
    scanned = []

    def scan(path):
        """
        记录目标目录扫描次数。
        """
        if path == directory:
            scanned.append(path)
        return original(path)

    monkeypatch.setattr(Path, "iterdir", scan)
    assert create().DEBUG is True
    assert scanned == [directory]


def test_unreadable_secret_directory_is_skipped(secret_config, monkeypatch):
    """
    整个目录无权限时保留配置默认值且明确告警。
    """
    create, directory, _env_file, messages = secret_config
    original = Path.iterdir

    def scan(path):
        """
        模拟运行用户无法列出 secret 目录。
        """
        if path == directory:
            raise PermissionError
        return original(path)

    monkeypatch.setattr(Path, "iterdir", scan)
    assert create().API_TOKEN is None
    assert messages == ["secrets 目录不可用，请检查目录类型与读取权限"]


@pytest.mark.parametrize("selector", ["MOVIEPILOT_SECRETS_DIR", "SECRETS_DIR", "disabled"])
def test_module_singleton_loads_secret_without_constructor_override(secret_config, selector):
    """
    真实导入单例会自动读取进程指定目录，关闭开关则不读取文件。
    """
    _create, directory, env_file, _messages = secret_config
    (directory / "API_TOKEN").write_text("module-token-0123456789", encoding="utf-8")
    environment = dict(os.environ)
    environment.pop("MOVIEPILOT_SECRETS_DIR", None)
    environment.pop("SECRETS_DIR", None)
    environment["CONFIG_DIR"] = str(env_file.parent)
    environment["SECRETS_DIR" if selector == "disabled" else selector] = str(directory)
    if selector == "disabled":
        environment["MOVIEPILOT_SECRETS_DIR"] = "false"
    before = env_file.read_bytes()
    result = subprocess.run(
        [sys.executable, "-c", (
            "import json, os; from app.runtime.config import settings; "
            "print(json.dumps([settings.API_TOKEN, 'API_TOKEN' in os.environ]))"
        )],
        env=environment, check=True, capture_output=True, text=True, timeout=30,
    )
    expected = None if selector == "disabled" else "module-token-0123456789"
    assert json.loads(result.stdout.strip().splitlines()[-1]) == [expected, False]
    assert env_file.read_bytes() == before
