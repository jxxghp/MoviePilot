"""插件多版本目录布局静态扫描的合同测试。"""

from __future__ import annotations

from pathlib import Path

from app.runtime.compat.plugin_version_readiness import scan_plugin_version_readiness


def _write_plugin(root: Path, plugin_id: str, files: dict[str, str]) -> Path:
    """按给定文件映射写入一个仅用于静态扫描的插件源码目录。"""
    plugin_dir = root / plugin_id
    plugin_dir.mkdir(parents=True)
    for relative_path, content in files.items():
        target = plugin_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return plugin_dir


def test_relative_imports_report_all_three_criteria_as_clean(tmp_path: Path) -> None:
    """只用相对 import、不依赖其它插件、不继承共享 Base 的插件三项判据均为否。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "cleanplugin",
        {
            "__init__.py": "from .utils import helper\n\nhelper()\n",
            "utils.py": "def helper():\n    return 1\n",
        },
    )

    readiness = scan_plugin_version_readiness("cleanplugin", plugin_dir)

    assert readiness.is_clean
    assert readiness.has_self_referential_imports is False
    assert readiness.has_cross_plugin_imports is False
    assert readiness.has_shared_base_models is False
    assert readiness.unparsed_files == ()


def test_self_referential_import_from_reports_file_line_and_suggestion(
    tmp_path: Path,
) -> None:
    """from app.plugins.<自身pid>.xxx import X 应精确报出文件、行号和相对写法建议。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {"__init__.py": "\nfrom app.plugins.myplugin.utils import helper\n", "utils.py": "def helper():\n    pass\n"},
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_self_referential_imports
    assert len(readiness.self_referential_imports) == 1
    hit = readiness.self_referential_imports[0]
    assert hit.file == "__init__.py"
    assert hit.line == 2
    assert hit.statement == "from app.plugins.myplugin.utils import helper"
    assert hit.suggestion == "from .utils import helper"


def test_self_referential_plain_import_reports_relative_module_suggestion(
    tmp_path: Path,
) -> None:
    """import app.plugins.<自身pid>.xxx 应报出改写为相对 from-import 的建议。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": "import app.plugins.myplugin.utils as utils_mod\n",
            "utils.py": "value = 1\n",
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_self_referential_imports
    hit = readiness.self_referential_imports[0]
    assert hit.file == "__init__.py"
    assert hit.line == 1
    assert hit.statement == "import app.plugins.myplugin.utils as utils_mod"
    assert hit.suggestion == "from . import utils as utils_mod"


def test_self_referential_dynamic_import_module_reports_suggestion(tmp_path: Path) -> None:
    """importlib.import_module("app.plugins.<自身pid>.xxx") 字符串常量形式应被识别。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "import importlib\n"
                'importlib.import_module("app.plugins.myplugin.utils")\n'
            ),
            "utils.py": "value = 1\n",
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_self_referential_imports
    hit = readiness.self_referential_imports[0]
    assert hit.file == "__init__.py"
    assert hit.line == 2
    assert hit.statement == 'importlib.import_module("app.plugins.myplugin.utils")'
    assert "from .utils import" in hit.suggestion


def test_cross_plugin_import_is_not_confused_with_self_reference(tmp_path: Path) -> None:
    """引用其它插件应归入跨插件依赖类，不计入自引用绝对 import。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {"__init__.py": "from app.plugins.otherplugin import Thing\n"},
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_self_referential_imports is False
    assert readiness.has_cross_plugin_imports
    hit = readiness.cross_plugin_imports[0]
    assert hit.file == "__init__.py"
    assert hit.line == 1
    assert hit.target_plugin_id == "otherplugin"
    assert hit.statement == "from app.plugins.otherplugin import Thing"


def test_cross_plugin_absolute_import_and_dynamic_import_are_both_classified(
    tmp_path: Path,
) -> None:
    """跨插件依赖的 import 与 importlib.import_module 形式都应归入跨插件类。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "import app.plugins.otherplugin.helpers\n"
                "import importlib\n"
                'importlib.import_module("app.plugins.thirdplugin.tools")\n'
            ),
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_self_referential_imports is False
    targets = {hit.target_plugin_id for hit in readiness.cross_plugin_imports}
    assert targets == {"otherplugin", "thirdplugin"}


def test_shared_base_model_via_from_app_db_import_is_reported(tmp_path: Path) -> None:
    """from app.db import Base 继承应被识别为共享基类建模。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "from app.db import Base\n"
                "from sqlalchemy.orm import Mapped, mapped_column\n\n"
                "class MyData(Base):\n"
                "    id: Mapped[int] = mapped_column(primary_key=True)\n"
            ),
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_shared_base_models
    hit = readiness.shared_base_models[0]
    assert hit.file == "__init__.py"
    assert hit.class_name == "MyData"
    assert hit.line == 4


def test_shared_base_model_via_app_db_base_module_attribute_is_reported(
    tmp_path: Path,
) -> None:
    """import app.db.base 后以 app.db.base.Base 继承也应被识别。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "import app.db.base\n\n"
                "class MyData(app.db.base.Base):\n"
                "    pass\n"
            ),
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_shared_base_models
    assert readiness.shared_base_models[0].class_name == "MyData"


def test_shared_base_model_via_module_alias_is_reported(tmp_path: Path) -> None:
    """import app.db as db 后以 db.Base 继承也应被识别。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "import app.db as db\n\n"
                "class MyData(db.Base):\n"
                "    pass\n"
            ),
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_shared_base_models
    assert readiness.shared_base_models[0].class_name == "MyData"


def test_plugin_own_base_class_is_not_confused_with_shared_base(tmp_path: Path) -> None:
    """插件自定义同名 Base 或继承非宿主基类不应被误报。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": (
                "class Base:\n"
                "    pass\n\n"
                "class MyData(Base):\n"
                "    pass\n"
            ),
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.has_shared_base_models is False


def test_syntax_error_file_does_not_crash_scan_and_is_recorded_as_unparsed(
    tmp_path: Path,
) -> None:
    """插件文件语法错误不得让扫描抛异常，应记录为无法解析并继续扫描其余文件。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": "from app.plugins.myplugin.utils import helper\n",
            "broken.py": "def broken(:\n    pass\n",
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    assert readiness.unparsed_files == ("broken.py",)
    assert readiness.has_self_referential_imports
    assert readiness.is_clean is False


def test_nested_subpackage_self_import_computes_correct_relative_dots(
    tmp_path: Path,
) -> None:
    """深层子包内的自引用绝对 import 应算出正确的多层相对 import。"""
    plugin_dir = _write_plugin(
        tmp_path,
        "myplugin",
        {
            "__init__.py": "",
            "sub/__init__.py": "",
            "sub/foo.py": "from app.plugins.myplugin.utils import helper\n",
            "utils.py": "def helper():\n    pass\n",
        },
    )

    readiness = scan_plugin_version_readiness("myplugin", plugin_dir)

    hits = [hit for hit in readiness.self_referential_imports if hit.file == "sub/foo.py"]
    assert len(hits) == 1
    assert hits[0].suggestion == "from ..utils import helper"


def test_missing_plugin_directory_returns_empty_readiness(tmp_path: Path) -> None:
    """插件目录不存在时应返回空结论而不是抛异常。"""
    readiness = scan_plugin_version_readiness("ghost", tmp_path / "does-not-exist")

    assert readiness.is_clean
