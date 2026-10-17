"""
验证本地卸载的确认流程与配置数据保留边界。
"""

from __future__ import annotations

import importlib.util
import uuid
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "local_setup.py"


def load_local_setup_module():
    """
    按源码路径加载隔离的本地安装脚本模块。
    """
    module_name = f"moviepilot_local_setup_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(module_name, MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def prepare_install_tree(tmp_path: Path, request):
    """
    创建隔离的本地安装目录，并在测试结束时撤销脚本路径配置替换。

    :return: 接受 legacy_config 的构造函数，用于选择旧版或独立配置目录
    """

    def build(*, legacy_config: bool = False):
        """
        填充安装文件并返回脚本模块、根目录、配置目录、虚拟环境与环境文件。
        """
        module = load_local_setup_module()
        temp_path = tmp_path
        root_dir = temp_path / "MoviePilot"
        resource_dir = root_dir / "app" / "infrastructure"
        runtime_dir = root_dir / ".runtime"
        public_dir = root_dir / "public"
        venv_dir = root_dir / "venv"
        install_env_file = root_dir / ".moviepilot.env"
        config_dir = root_dir / "config" if legacy_config else temp_path / "moviepilot-config"
        temp_config_dir = config_dir / "temp"

        resource_dir.mkdir(parents=True)
        runtime_dir.mkdir(parents=True)
        public_dir.mkdir(parents=True)
        venv_dir.mkdir(parents=True)
        temp_config_dir.mkdir(parents=True)
        install_env_file.write_text("CONFIG_DIR=/tmp/moviepilot-config\n", encoding="utf-8")
        (root_dir / "moviepilot").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (resource_dir / "sites.py").write_text("generated\n", encoding="utf-8")
        (resource_dir / "user.sites.v3.bin").write_bytes(b"binary")
        (temp_config_dir / "moviepilot.runtime.json").write_text("{}", encoding="utf-8")
        (temp_config_dir / "moviepilot.frontend.runtime.json").write_text("{}", encoding="utf-8")

        stack = ExitStack()
        request.addfinalizer(stack.close)
        stack.enter_context(patch.object(module, "ROOT", root_dir))
        stack.enter_context(patch.object(module, "SITE_RESOURCE_DIR", resource_dir))
        stack.enter_context(patch.object(module, "RUNTIME_DIR", runtime_dir))
        stack.enter_context(patch.object(module, "PUBLIC_DIR", public_dir))
        stack.enter_context(patch.object(module, "INSTALL_ENV_FILE", install_env_file))
        stack.enter_context(patch.object(module, "LEGACY_CONFIG_DIR", root_dir / "config"))
        stack.enter_context(patch.object(module, "CONFIG_DIR", config_dir))
        stack.enter_context(patch.object(module, "TEMP_DIR", temp_config_dir))

        return module, root_dir, config_dir, venv_dir, install_env_file

    return build


def test_remove_config_data_deletes_legacy_config_directory(prepare_install_tree) -> None:
    """
    清理配置数据时删除旧版项目内配置目录及其内容。
    """
    module, _, config_dir, _, _ = prepare_install_tree(legacy_config=True)
    category_file = config_dir / "category.yaml"
    category_file.write_text("seed\n", encoding="utf-8")
    (config_dir / "logs").mkdir(exist_ok=True)
    (config_dir / "user.db").write_text("db\n", encoding="utf-8")

    removed = module._remove_config_data(config_dir)

    assert not config_dir.exists()
    assert not category_file.exists()
    assert str(config_dir.resolve()) in {str(path.resolve()) for path in removed}


def test_uninstall_keeps_config_by_default(prepare_install_tree) -> None:
    """
    默认卸载仅删除程序文件并保留配置数据。
    """
    module, root_dir, config_dir, venv_dir, install_env_file = prepare_install_tree()
    cli_dir = root_dir.parent / "bin"
    cli_dir.mkdir()
    cli_link = cli_dir / "moviepilot"
    cli_link.symlink_to(root_dir / "moviepilot")

    yes_no_answers = iter([False, True])
    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ),
        patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ),
        patch.object(module, "_stop_managed_services", return_value=None),
    ):
        result = module.uninstall_local(
            venv_dir=venv_dir,
            config_dir=config_dir,
            launch_path=str(cli_link),
        )

    assert not result["cancelled"]
    assert config_dir.exists()
    assert install_env_file.exists()
    assert not venv_dir.exists()
    assert not (root_dir / ".runtime").exists()
    assert not (root_dir / "public").exists()
    assert not (root_dir / "app" / "infrastructure" / "sites.py").exists()
    assert not (root_dir / "app" / "infrastructure" / "user.sites.v3.bin").exists()
    assert not cli_link.exists()


def test_uninstall_deletes_external_config_when_requested(prepare_install_tree) -> None:
    """
    明确要求删除数据时卸载独立配置目录。
    """
    module, _, config_dir, venv_dir, install_env_file = prepare_install_tree()
    yes_no_answers = iter([True, True])

    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ),
        patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ),
        patch.object(module, "_stop_managed_services", return_value=None),
    ):
        result = module.uninstall_local(
            venv_dir=venv_dir,
            config_dir=config_dir,
        )

    assert not result["cancelled"]
    assert result["config_deleted"]
    assert not config_dir.exists()
    assert not install_env_file.exists()


def test_uninstall_deletes_legacy_config_when_requested(prepare_install_tree) -> None:
    """
    明确要求删除数据时卸载旧版项目内配置目录。
    """
    module, _, config_dir, venv_dir, install_env_file = prepare_install_tree(legacy_config=True)
    (config_dir / "category.yaml").write_text("seed\n", encoding="utf-8")
    yes_no_answers = iter([True, True])

    with (
        patch.object(module, "_is_interactive", return_value=True),
        patch.object(
            module,
            "_prompt_yes_no",
            side_effect=lambda label, default=False: next(yes_no_answers),
        ),
        patch.object(
            module,
            "_prompt_text",
            side_effect=lambda label, default=None, allow_empty=False, secret=False: module.UNINSTALL_CONFIRM_TEXT,
        ),
        patch.object(module, "_stop_managed_services", return_value=None),
    ):
        result = module.uninstall_local(
            venv_dir=venv_dir,
            config_dir=config_dir,
        )

    assert not result["cancelled"]
    assert result["config_deleted"]
    assert not config_dir.exists()
    assert not install_env_file.exists()
