"""插件数据目录解析的包含性回归测试。

插件标识在重置、卸载、彻底清理等路径上直接来自 HTTP 路径段，而插件数据根是配置根下
的一层子目录。把标识就地拼进路径再交给文件系统，一个 ``..`` 就足以让删除动作抬到配置
根上。本文件按「编码形式」逐个钉住这条边界：只挡住字面量 ``..`` 是不够的，URL 编码、
反斜杠、绝对路径与软链都能绕过字符过滤，可靠的判据只有「解析出来的真实路径确实落在
允许的根目录之内」。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import app.db.plugin.registry as registry_module
import app.runtime.extensions.plugin.datadir as datadir_module
from app.db.plugin.locator import DATABASE_FILENAME, sqlite_database_path
from app.runtime.extensions.plugin.datadir import (
    remove_plugin_data_directory,
    resolve_plugin_data_directory,
)

# 用户可控标识到达文件系统前可能呈现的形态。服务端拿到的是 ASGI 解码之后的路径段，
# 因而 ``%2e%2e`` 与 ``..``、``..%2f`` 与 ``../`` 在处理函数里完全等价——只在路由层面
# 过滤字面量 ``..`` 拦不住任何一种。
TRAVERSAL_IDS = (
    "..",
    "../",
    "..\\",
    "./..",
    ".././..",
    "a/../..",
    "/etc",
    "C:\\Windows",
    "",
    ".",
)


@pytest.fixture(name="config_root")
def fixture_config_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """搭出「配置根 / 插件数据根 / 同级受保护内容」这一真实布局。

    插件数据根在生产里就是 ``CONFIG_PATH/plugins``，逃出一层即落在配置根上。受保护
    目录、宿主库文件与哨兵内容用来证明越界删除确实发生过，而不是只看函数返回值。
    """
    config_root = tmp_path / "config"
    plugin_root = config_root / "plugins"
    (plugin_root / "DemoPlugin").mkdir(parents=True)
    (plugin_root / "DemoPlugin" / "cache.bin").write_text("x", encoding="utf-8")
    protected = config_root / "protected"
    protected.mkdir()
    (protected / "keep.txt").write_text("must survive", encoding="utf-8")
    (config_root / "user.db").write_text("must survive", encoding="utf-8")
    (config_root / DATABASE_FILENAME).write_text("must survive", encoding="utf-8")
    monkeypatch.setattr(
        datadir_module,
        "get_runtime_setting",
        lambda key, default=None: plugin_root if key == "PLUGIN_DATA_PATH" else default,
    )
    return config_root


def _sentinels_intact(config_root: Path) -> bool:
    """插件数据根之外的内容是否原封不动。"""
    return (
        (config_root / "protected" / "keep.txt").is_file()
        and (config_root / "user.db").is_file()
        and (config_root / DATABASE_FILENAME).is_file()
    )


# --------------------------------------------------------------------------- #
# 目录解析
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("plugin_id", TRAVERSAL_IDS)
def test_resolution_rejects_every_encoding_of_an_escaping_id(plugin_id: str) -> None:
    """越界标识的每一种写法都要在解析阶段被拒。"""
    with pytest.raises(ValueError):
        resolve_plugin_data_directory(plugin_id)


def test_resolution_keeps_legacy_ids_with_separators_in_their_name(
    config_root: Path,
) -> None:
    """判据是「必须是一个路径段」而不是字符白名单，带下划线、连字符的旧插件照常可用。

    插件标识历史上就是 Python 类名，``My_Plugin`` 一类写法一直存在；把它们一并拒掉
    是纯粹的功能回退，而它们本来就逃不出数据根。
    """
    for plugin_id in ("My_Plugin", "My-Plugin", "plugin.v2", "中文插件"):
        assert resolve_plugin_data_directory(plugin_id) == (
            config_root / "plugins" / plugin_id
        )


def test_percent_encoded_looking_names_are_ordinary_directory_names(
    config_root: Path,
) -> None:
    """``%2e%2e`` 这类字面量只是古怪的目录名，不该被当成越界而拒绝。

    百分号编码是传输层的事：服务端拿到的路径段已经解码过，真正危险的是解码结果。
    把未解码的形态也一并拒掉属于在错误的层次上猜测，反而会挡住合法目录名。
    """
    for plugin_id in ("%2e%2e", "..%2f", "%2f"):
        assert resolve_plugin_data_directory(plugin_id) == (
            config_root / "plugins" / plugin_id
        )


# --------------------------------------------------------------------------- #
# 目录删除
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("plugin_id", TRAVERSAL_IDS)
def test_directory_removal_rejects_escaping_ids_on_its_own(
    config_root: Path,
    plugin_id: str,
) -> None:
    """删除函数自身即拒绝越界标识，不指望调用方先做过校验。

    目录删除是不可逆的终点：它必须自带包含性判断，将来新增调用方忘了校验也不至于把
    配置目录整个删掉。
    """
    with pytest.raises(ValueError):
        remove_plugin_data_directory(plugin_id)

    assert _sentinels_intact(config_root)


def test_directory_removal_deletes_only_the_matching_plugin_directory(
    config_root: Path,
) -> None:
    """合法标识照常删除，且只删它自己那一层。"""
    assert remove_plugin_data_directory("DemoPlugin") is True

    assert not (config_root / "plugins" / "DemoPlugin").exists()
    assert (config_root / "plugins").is_dir()
    assert _sentinels_intact(config_root)


def test_directory_removal_reports_a_missing_directory_without_deleting(
    config_root: Path,
) -> None:
    """目录本就不存在时如实回报未删除，而不是谎报清理过它。"""
    assert remove_plugin_data_directory("NeverExisted") is False


@pytest.mark.skipif(
    sys.platform.startswith("win"),
    reason="Windows 下建软链需要额外权限，逃逸语义由 POSIX 用例覆盖",
)
def test_directory_removal_refuses_to_follow_a_symlink_out_of_the_root(
    config_root: Path,
) -> None:
    """数据根下的软链指向根外时拒绝删除，而不是顺着它把目标删掉。

    ``resolve()`` 会跟随软链，因此包含性判断必须建立在解析之后的真实路径上；只比较
    拼接出来的字面路径，一条 ``plugins/Evil -> /config/protected`` 的软链就能骗过它。
    """
    (config_root / "plugins" / "Evil").symlink_to(
        config_root / "protected", target_is_directory=True
    )

    with pytest.raises(ValueError):
        remove_plugin_data_directory("Evil")

    assert _sentinels_intact(config_root)


# --------------------------------------------------------------------------- #
# 自有数据库文件定位与销毁
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("plugin_id", TRAVERSAL_IDS)
def test_database_path_rejects_escaping_ids(plugin_id: str) -> None:
    """库文件路径同样按统一入口解析；销毁会 unlink 它，越界即拒。"""
    with pytest.raises(ValueError):
        sqlite_database_path(plugin_id)


def test_database_path_stays_inside_the_plugin_directory(config_root: Path) -> None:
    """合法标识下库文件仍落在该插件自己的数据目录里。"""
    assert sqlite_database_path("DemoPlugin") == (
        config_root / "plugins" / "DemoPlugin" / DATABASE_FILENAME
    )


def test_destroying_a_database_with_an_escaping_id_unlinks_nothing(
    config_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """销毁入口拿到越界标识时什么都不删，只留一条日志。

    这条路径在 ``GET /plugin/reset/{plugin_id}`` 上是可达的：重置按 ``force`` 删数据，
    不检查插件是否存在，``..`` 会把库文件路径解析到配置根下的同名文件上。
    """
    monkeypatch.setattr(
        registry_module,
        "get_runtime_setting",
        lambda key, default=None: "sqlite" if key == "DB_TYPE" else default,
    )

    registry_module.destroy_database("..")

    assert _sentinels_intact(config_root)
