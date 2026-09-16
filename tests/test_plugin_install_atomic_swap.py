"""插件安装的换入原子性：准备失败不得删掉已装插件，换入失败必须还原运行目录。"""

import errno
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.adapters.system.plugin.package import (
    PluginContentSwapError,
    PluginPackageManager,
)

PLUGIN_ID = "DemoPlugin"
REPO_URL = "https://github.com/demo/MoviePilot-Plugins"
INSTALLED_MARK = "installed"
DOWNLOAD_ERROR = "文件 plugins.v2/demoplugin/__init__.py 下载失败！"


def _swap(staging_dir: Path, final_dir: Path) -> None:
    """调用被测的换入实现，保持用例只表达行为约定。"""
    PluginPackageManager._PluginPackageManager__swap_staged_plugin_content(  # type: ignore[attr-defined]
        staging_dir, final_dir
    )


def _installed_manager(monkeypatch, tmp_path: Path) -> tuple[PluginPackageManager, Path]:
    """构造隔离目录的包 owner，并预置一个已安装且可用的插件运行目录。"""
    plugin_root = tmp_path / "plugins"
    settings = SimpleNamespace(
        ROOT_PATH=tmp_path,
        TEMP_PATH=tmp_path / "temp",
        CONFIG_PATH=tmp_path / "config",
    )
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.get_runtime_setting",
        lambda key: getattr(settings, key),
    )
    plugin_dir = plugin_root / PLUGIN_ID.lower()
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(INSTALLED_MARK, encoding="utf-8")
    manager = PluginPackageManager(source=Mock(), plugin_root=plugin_root)
    return manager, plugin_dir


def _stub_market_lookup(manager: PluginPackageManager, monkeypatch) -> None:
    """把远端市场查询固定下来，让内容准备的成败成为唯一变量。"""
    monkeypatch.setattr(
        "app.adapters.system.plugin.package.SystemUtils.is_frozen", lambda: False
    )
    monkeypatch.setattr(manager, "is_local_repo_url", lambda _repo_url: False)
    monkeypatch.setattr(manager, "get_repo_info", lambda _repo_url: ("demo", "repo"))
    monkeypatch.setattr(manager, "get_plugin_package_version", lambda *_args: "v2")
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__get_plugin_meta",
        lambda *_args: {"release": False, "version": "1.0.0"},
    )

    async def async_package_version(*_args) -> str:
        """异步入口复用同一索引代际。"""
        return "v2"

    async def async_meta(*_args) -> dict:
        """异步入口复用同一插件元数据。"""
        return {"release": False, "version": "1.0.0"}

    monkeypatch.setattr(
        manager, "async_get_plugin_package_version", async_package_version
    )
    monkeypatch.setattr(
        manager, "_PluginPackageManager__async_get_plugin_meta", async_meta
    )


def _staging_root(tmp_path: Path) -> Path:
    """返回安装暂存根目录。"""
    return tmp_path / "temp" / "plugin_install_staging"


def _assert_no_staging_residue(tmp_path: Path) -> None:
    """安装结束后不得残留暂存目录。"""
    staging_root = _staging_root(tmp_path)
    assert not staging_root.exists() or not list(staging_root.iterdir())


def _assert_no_swap_residue(plugin_dir: Path) -> None:
    """换入结束后不得在插件根目录残留换入用的回滚材料。"""
    assert not list(plugin_dir.parent.glob(f".{plugin_dir.name}.previous-*"))


@pytest.mark.parametrize("force_install", [True, False])
def test_sync_install_keeps_installed_plugin_when_content_preparation_fails(
    monkeypatch, tmp_path: Path, force_install: bool
) -> None:
    """同步安装在下载失败时必须原样保留已装插件，无论本次是否留了备份。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    _stub_market_lookup(manager, monkeypatch)
    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__prepare_content_via_filelist_sync",
        lambda *_args: (False, DOWNLOAD_ERROR),
    )

    success, message = manager.install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=force_install
    )

    assert not success
    assert message == DOWNLOAD_ERROR
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    _assert_no_staging_residue(tmp_path)
    _assert_no_swap_residue(plugin_dir)


@pytest.mark.asyncio
@pytest.mark.parametrize("force_install", [True, False])
async def test_async_install_keeps_installed_plugin_when_content_preparation_fails(
    monkeypatch, tmp_path: Path, force_install: bool
) -> None:
    """异步安装与同步保持一致的失败语义，下载失败不得删掉已装插件。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    _stub_market_lookup(manager, monkeypatch)

    async def failing_filelist(*_args) -> tuple[bool, str]:
        """模拟资产瞬时不可用导致的内容准备失败。"""
        return False, DOWNLOAD_ERROR

    monkeypatch.setattr(
        manager,
        "_PluginPackageManager__prepare_content_via_filelist_async",
        failing_filelist,
    )

    success, message = await manager.async_install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=force_install
    )

    assert not success
    assert message == DOWNLOAD_ERROR
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    _assert_no_staging_residue(tmp_path)
    _assert_no_swap_residue(plugin_dir)


def test_sync_install_replaces_installed_plugin_when_content_is_ready(
    monkeypatch, tmp_path: Path
) -> None:
    """同步安装成功时用新内容整体替换运行目录，不与旧文件混合。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    (plugin_dir / "stale.py").write_text("stale", encoding="utf-8")
    _stub_market_lookup(manager, monkeypatch)

    def prepare(_pid, _user_repo, _package_version, dest_root: Path) -> tuple[bool, str]:
        """把新版本内容写进本次分配的暂存目录。"""
        dest_root.mkdir(parents=True, exist_ok=True)
        (dest_root / "__init__.py").write_text("upgraded", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_sync", prepare
    )

    success, message = manager.install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=True
    )

    assert (success, message) == (True, "")
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "upgraded"
    assert not (plugin_dir / "stale.py").exists()
    _assert_no_staging_residue(tmp_path)
    _assert_no_swap_residue(plugin_dir)


@pytest.mark.asyncio
async def test_async_install_replaces_installed_plugin_when_content_is_ready(
    monkeypatch, tmp_path: Path
) -> None:
    """异步安装成功时同样整体替换运行目录。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    (plugin_dir / "stale.py").write_text("stale", encoding="utf-8")
    _stub_market_lookup(manager, monkeypatch)

    async def prepare(
        _pid, _user_repo, _package_version, dest_root: Path
    ) -> tuple[bool, str]:
        """把新版本内容写进本次分配的暂存目录。"""
        dest_root.mkdir(parents=True, exist_ok=True)
        (dest_root / "__init__.py").write_text("upgraded", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_async", prepare
    )

    success, message = await manager.async_install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=True
    )

    assert (success, message) == (True, "")
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == "upgraded"
    assert not (plugin_dir / "stale.py").exists()
    _assert_no_staging_residue(tmp_path)
    _assert_no_swap_residue(plugin_dir)


def test_sync_install_keeps_installed_plugin_when_swap_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """换入自身失败时运行目录必须仍是换入前那一份，并报出失败原因。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    _stub_market_lookup(manager, monkeypatch)

    def prepare(_pid, _user_repo, _package_version, dest_root: Path) -> tuple[bool, str]:
        """内容准备成功，但随后的换入会被模拟为失败。"""
        dest_root.mkdir(parents=True, exist_ok=True)
        (dest_root / "__init__.py").write_text("upgraded", encoding="utf-8")
        return True, ""

    def failing_swap(_staging_dir: Path, _final_dir: Path) -> None:
        """模拟运行目录不可写导致的换入失败。"""
        raise OSError(errno.EACCES, "permission denied")

    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_sync", prepare
    )
    monkeypatch.setattr(
        manager, "_PluginPackageManager__swap_staged_plugin_content", failing_swap
    )

    success, message = manager.install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=True
    )

    assert not success
    assert "写入插件内容失败" in message
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    _assert_no_staging_residue(tmp_path)


def _staged_pair(tmp_path: Path) -> tuple[Path, Path]:
    """准备一份待换入内容和一个已有旧内容的目标目录。"""
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir()
    (staging_dir / "__init__.py").write_text("upgraded", encoding="utf-8")
    final_dir = tmp_path / "plugins" / "demoplugin"
    final_dir.mkdir(parents=True)
    (final_dir / "__init__.py").write_text(INSTALLED_MARK, encoding="utf-8")
    (final_dir / "stale.py").write_text("stale", encoding="utf-8")
    return staging_dir, final_dir


def _break_replace(monkeypatch, *, source: Path, code: int) -> None:
    """让指定源路径的原子改名失败，模拟跨文件系统或权限受限。"""
    original = Path.replace

    def guarded(self: Path, target):
        """只拦截被指定的源路径，其它改名仍走真实实现。"""
        if self == source:
            raise OSError(code, "simulated")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", guarded)


def _break_rmtree(monkeypatch, *, target: Path, failures: int) -> None:
    """让指定目录的前若干次删除在删掉部分内容后失败，模拟删到一半中断。

    逐次按逆字典序删掉一个文件再抛错，既留下"目录还在但内容残缺"的现场，
    也让后续断言能稳定指出是哪一份内容丢了。
    """
    original = shutil.rmtree
    remaining = {"count": failures}

    def guarded(path, *args, **kwargs):
        """只拦截被指定的目录，其它删除仍走真实实现。"""
        if Path(path) == target and remaining["count"] > 0:
            remaining["count"] -= 1
            for child in sorted(Path(path).iterdir(), reverse=True):
                if child.is_file():
                    child.unlink()
                    break
            raise OSError(errno.EIO, "simulated")
        return original(path, *args, **kwargs)

    monkeypatch.setattr("app.adapters.system.plugin.package.shutil.rmtree", guarded)


def test_swap_publishes_new_content_and_drops_old_content(tmp_path: Path) -> None:
    """换入后运行目录只剩新内容，回滚材料被清理。"""
    staging_dir, final_dir = _staged_pair(tmp_path)

    _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == "upgraded"
    assert not (final_dir / "stale.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_falls_back_to_copy_when_staging_crosses_filesystems(
    monkeypatch, tmp_path: Path
) -> None:
    """暂存目录与插件根目录跨文件系统时退化为复制，结果与改名一致。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=staging_dir, code=errno.EXDEV)

    _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == "upgraded"
    assert not (final_dir / "stale.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_falls_back_to_copy_when_old_directory_cannot_be_renamed(
    monkeypatch, tmp_path: Path
) -> None:
    """overlayfs 拒绝改名旧目录时先复制出回滚材料，再继续换入新内容。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=final_dir, code=errno.EXDEV)

    _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == "upgraded"
    assert not (final_dir / "stale.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_keeps_old_content_when_staging_is_missing(tmp_path: Path) -> None:
    """待换入内容不存在时运行目录必须原样退回，而不是被清空。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    shutil.rmtree(staging_dir)

    with pytest.raises(OSError):
        _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (final_dir / "stale.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_keeps_old_content_when_old_directory_cannot_be_moved_aside(
    monkeypatch, tmp_path: Path
) -> None:
    """旧目录挪不开时必须整体放弃换入，绝不先删后写。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=final_dir, code=errno.EACCES)

    with pytest.raises(OSError):
        _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (final_dir / "stale.py").exists()
    assert (staging_dir / "__init__.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_rolls_back_when_cross_filesystem_copy_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """跨文件系统复制中途失败时删掉半成品并把旧目录换回。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=staging_dir, code=errno.EXDEV)

    def failing_copytree(src, dst, *_args, **_kwargs):
        """模拟复制到一半磁盘写入失败，留下半份目标目录。"""
        Path(dst).mkdir(parents=True, exist_ok=True)
        (Path(dst) / "partial.py").write_text("partial", encoding="utf-8")
        raise OSError(errno.ENOSPC, "no space left on device")

    monkeypatch.setattr(
        "app.adapters.system.plugin.package.shutil.copytree", failing_copytree
    )

    with pytest.raises(OSError):
        _swap(staging_dir, final_dir)

    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (final_dir / "stale.py").exists()
    assert not (final_dir / "partial.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_rolls_back_when_removing_old_directory_fails_midway(
    monkeypatch, tmp_path: Path
) -> None:
    """旧目录退化为复制后删到一半失败，也必须回滚出完整的旧内容。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=final_dir, code=errno.EXDEV)
    _break_rmtree(monkeypatch, target=final_dir, failures=1)

    with pytest.raises(PluginContentSwapError) as failure:
        _swap(staging_dir, final_dir)

    assert failure.value.runtime_intact is True
    assert (final_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (final_dir / "stale.py").exists()
    assert (staging_dir / "__init__.py").exists()
    _assert_no_swap_residue(final_dir)


def test_swap_reports_broken_runtime_and_keeps_material_when_rollback_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """回滚也失败时必须上报运行目录未恢复，并把完整旧内容留在恢复材料里。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=final_dir, code=errno.EXDEV)
    _break_rmtree(monkeypatch, target=final_dir, failures=2)

    with pytest.raises(PluginContentSwapError) as failure:
        _swap(staging_dir, final_dir)

    assert failure.value.runtime_intact is False
    materials = list(final_dir.parent.glob(f".{final_dir.name}.previous-*"))
    assert len(materials) == 1
    assert (materials[0] / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (materials[0] / "stale.py").exists()


def test_swap_reports_intact_runtime_when_old_directory_cannot_be_moved_aside(
    monkeypatch, tmp_path: Path
) -> None:
    """旧目录连挪都没挪动时运行目录天然完好，不该让上层白走一次备份还原。"""
    staging_dir, final_dir = _staged_pair(tmp_path)
    _break_replace(monkeypatch, source=final_dir, code=errno.EACCES)

    with pytest.raises(PluginContentSwapError) as failure:
        _swap(staging_dir, final_dir)

    assert failure.value.runtime_intact is True
    _assert_no_swap_residue(final_dir)


def test_sync_install_restores_backup_when_swap_rollback_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """回滚也失败时上层必须用本次备份补齐运行目录，而不是看目录还在就丢掉备份。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    (plugin_dir / "stale.py").write_text("stale", encoding="utf-8")
    _stub_market_lookup(manager, monkeypatch)

    def prepare(_pid, _user_repo, _package_version, dest_root: Path) -> tuple[bool, str]:
        """内容准备成功，失败只发生在随后的换入阶段。"""
        dest_root.mkdir(parents=True, exist_ok=True)
        (dest_root / "__init__.py").write_text("upgraded", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_sync", prepare
    )
    _break_replace(monkeypatch, source=plugin_dir, code=errno.EXDEV)
    _break_rmtree(monkeypatch, target=plugin_dir, failures=2)

    success, message = manager.install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=False
    )

    assert not success
    assert "写入插件内容失败" in message
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (plugin_dir / "stale.py").exists()
    _assert_no_staging_residue(tmp_path)


@pytest.mark.asyncio
async def test_async_install_restores_backup_when_swap_rollback_fails(
    monkeypatch, tmp_path: Path
) -> None:
    """异步流程的备份处置同样只认回滚结论，不认运行目录是否还在。"""
    manager, plugin_dir = _installed_manager(monkeypatch, tmp_path)
    (plugin_dir / "stale.py").write_text("stale", encoding="utf-8")
    _stub_market_lookup(manager, monkeypatch)

    async def prepare(
        _pid, _user_repo, _package_version, dest_root: Path
    ) -> tuple[bool, str]:
        """内容准备成功，失败只发生在随后的换入阶段。"""
        dest_root.mkdir(parents=True, exist_ok=True)
        (dest_root / "__init__.py").write_text("upgraded", encoding="utf-8")
        return True, ""

    monkeypatch.setattr(
        manager, "_PluginPackageManager__prepare_content_via_filelist_async", prepare
    )
    _break_replace(monkeypatch, source=plugin_dir, code=errno.EXDEV)
    _break_rmtree(monkeypatch, target=plugin_dir, failures=2)

    success, message = await manager.async_install_raw(
        PLUGIN_ID, REPO_URL, package_version="v2", force_install=False
    )

    assert not success
    assert "写入插件内容失败" in message
    assert (plugin_dir / "__init__.py").read_text(encoding="utf-8") == INSTALLED_MARK
    assert (plugin_dir / "stale.py").exists()
    _assert_no_staging_residue(tmp_path)
