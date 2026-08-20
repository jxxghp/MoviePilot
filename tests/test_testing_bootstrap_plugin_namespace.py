"""验证插件仓测试引导复现生产运行时的插件导入命名空间。"""

from importlib import import_module
from pathlib import Path
import sys

import pytest

from app.testing import bootstrap


@pytest.mark.parametrize(
    ("prepare_name", "source_dir"),
    (
        ("prepare_v1_backend", "plugins"),
        ("prepare_v2_backend", "plugins.v2"),
        ("prepare_v3_backend", "plugins.v3"),
    ),
)
def test_prepare_plugin_backend_exposes_runtime_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    prepare_name: str,
    source_dir: str,
) -> None:
    """市场仓源码只通过 ``app.plugins.<id>`` 规范身份导入。"""
    plugin_id = f"namespace_probe_{source_dir.replace('.', '_')}"
    plugin_dir = tmp_path / source_dir / plugin_id
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "__init__.py").write_text(
        f"PLUGIN_ID = {plugin_id!r}\n",
        encoding="utf-8",
    )

    plugins_package = import_module("app.plugins")
    monkeypatch.setattr(plugins_package, "__path__", list(plugins_package.__path__))
    monkeypatch.setattr(bootstrap, "prepare_backend", lambda: None)

    runtime_name = f"app.plugins.{plugin_id}"
    try:
        getattr(bootstrap, prepare_name)(tmp_path)

        runtime = import_module(runtime_name)

        assert Path(runtime.__file__).resolve() == plugin_dir / "__init__.py"
        with pytest.raises(ModuleNotFoundError):
            import_module(plugin_id)
    finally:
        sys.modules.pop(runtime_name, None)
