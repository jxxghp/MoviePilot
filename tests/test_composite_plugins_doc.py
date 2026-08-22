"""`docs/composite-plugins.md` 与代码的漂移检测。

该文档曾经整篇失真：演示的十个 `provides_*` 钩子名与代码里真实存在的十二个钩子名
零交集，`app.core.auth.redirect` 早已不存在，文末引用的回归测试文件也从未被创建过——
没有任何东西在检查这篇文档，它漂移了很久都没人发现。

本文件不维护一份"当前有效钩子名"的清单去和文档对拍：那本身就是又一份会腐烂的副本。
判据直接查代码本身——`app.sdk.extension._PluginBase` 上真实存在哪些 `provides_*`
方法、`importlib` 能不能解析文档代码块里的每一条 import——因此钩子改名、新增或删除时，
这些测试不需要跟着改，会自动感知落差。
"""

import ast
import importlib
import re
from pathlib import Path
from typing import Iterator, Optional, Set, Tuple

import pytest

from app.sdk.extension import _PluginBase

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOC_PATH = PROJECT_ROOT / "docs" / "composite-plugins.md"

# 插件侧允许 import 的公开面前缀，与 tests/test_plugin_import_boundary.py 判定插件源码
# 越界与否的规则一致：app.sdk 是唯一门面，app.schemas 是生成的惰性兼容聚合入口
_ALLOWED_APP_IMPORT_PREFIXES = ("app.sdk", "app.schemas")


def _doc_text() -> str:
    """读取文档全文。"""
    return DOC_PATH.read_text(encoding="utf-8")


def _python_code_blocks(text: str) -> Iterator[str]:
    """按 ```python ... ``` 围栏取出代码块正文。

    :param text: 文档全文
    :return: 每个代码块的源码文本
    """
    for match in re.finditer(r"```python\n(.*?)```", text, re.DOTALL):
        yield match.group(1)


def _mentioned_provides_hooks(text: str) -> Set[str]:
    """取出文档全文中出现的每一个 ``provides_*`` 词形。

    :param text: 文档全文
    :return: 钩子名集合
    """
    return set(re.findall(r"\bprovides_[a-z_]+\b", text))


def _imports_in_code_blocks(text: str) -> Iterator[Tuple[str, Optional[str]]]:
    """遍历文档全部 python 代码块，逐条产出其中的 import 目标。

    :param text: 文档全文
    :return: ``(模块路径, 符号名)``；整模块导入时符号名为 None
    """
    for block in _python_code_blocks(text):
        tree = ast.parse(block)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    yield node.module, alias.name
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    yield alias.name, None


def test_doc_mentions_at_least_one_hook():
    """文档至少要提到一个 ``provides_*`` 钩子，否则下面两条测试形同虚设。"""
    assert _mentioned_provides_hooks(_doc_text())


def test_every_mentioned_hook_exists_on_plugin_base():
    """文档提到的每个 ``provides_*`` 钩子名都必须是 ``_PluginBase`` 上真实存在的可调用方法。

    这条测试不硬编码钩子清单：判据直接查 ``_PluginBase`` 本身，因此钩子改名或增删时
    这条测试自动感知，不需要另外维护一份对照表。
    """
    for hook in sorted(_mentioned_provides_hooks(_doc_text())):
        member = getattr(_PluginBase, hook, None)
        assert callable(member), (
            f"docs/composite-plugins.md 提到的钩子 {hook!r} 在 "
            "app.sdk.extension._PluginBase 上不存在或不可调用，文档已经与代码脱节"
        )


def test_doc_has_importable_code_blocks():
    """文档至少要有一个可解析出 import 的 python 代码块，否则下面两条测试形同虚设。"""
    assert list(_imports_in_code_blocks(_doc_text()))


def test_every_import_in_doc_code_blocks_resolves():
    """文档 python 代码块里的每一条 import 都必须在当前代码库里能解析成功。"""
    for module_name, attr in _imports_in_code_blocks(_doc_text()):
        try:
            module = importlib.import_module(module_name)
        except ImportError as error:
            pytest.fail(
                f"docs/composite-plugins.md 的代码示例 import 了模块 "
                f"{module_name!r}，当前代码库无法导入：{error}"
            )
            continue
        if attr is None:
            continue
        assert hasattr(module, attr), (
            f"docs/composite-plugins.md 的代码示例引用了 {module_name}.{attr}，"
            "该符号在当前代码库里不存在"
        )


def test_doc_code_block_imports_stay_within_plugin_boundary():
    """文档示例里的插件侧 import 必须落在 app.sdk / app.schemas 这两个公开面之内。

    与 tests/test_plugin_import_boundary.py 判定插件源码越界与否的规则一致：插件不得
    import 宿主内部路径，文档里的示例不能悄悄示范一条插件实际写了会被门禁拒绝的写法。
    """
    for module_name, _attr in _imports_in_code_blocks(_doc_text()):
        if not module_name.startswith("app."):
            continue
        assert module_name.startswith(_ALLOWED_APP_IMPORT_PREFIXES), (
            f"docs/composite-plugins.md 的代码示例 import 了 {module_name!r}，"
            f"越出插件公开面（仅允许 {', '.join(_ALLOWED_APP_IMPORT_PREFIXES)}）"
        )


def _referenced_repo_paths(text: str) -> Set[str]:
    """取出文档里反引号包住、形如仓库路径的引用。

    允许路径后跟 ``::符号名``（例如 ``a/b.py::ClassName``），只取路径部分核对存在性。

    :param text: 文档全文
    :return: 相对仓库根目录的路径集合
    """
    return set(
        re.findall(r"`([\w./-]+/[\w./-]+\.(?:py|md|toml))(?:::[\w.]+)?`", text)
    )


def test_doc_mentions_at_least_one_repo_path():
    """文档至少要引用一个可核对的仓库路径，否则下面这条测试形同虚设。"""
    assert _referenced_repo_paths(_doc_text())


def test_referenced_repo_paths_exist():
    """文档里反引号包住、形如路径的引用必须指向仓库里真实存在的文件。

    专治"回归测试见 tests/xxx.py"这类死链——文件被删除或改名后这条测试报红，而不是
    留在文档里把下一个读者带去一个不存在的地方。
    """
    for relative_path in sorted(_referenced_repo_paths(_doc_text())):
        path = PROJECT_ROOT / relative_path
        assert path.is_file(), (
            f"docs/composite-plugins.md 引用的路径 {relative_path!r} 在仓库里不存在"
        )
