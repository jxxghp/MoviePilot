"""站点解析器注册表：按站点配置的 schema 标识查得解析器类。

登记由解析器类定义驱动：SiteParserBase 子类一旦定义完成即按其声明的
schema 标识自动登记，新增解析器无需修改本文件或 SiteSchema 枚举。
"""

from __future__ import annotations

import importlib
import pkgutil
from typing import Any, Optional, Tuple

from app.modules.indexer.parser import PARSER_REGISTRY, site_parser_identity

__all__ = [
    "load_builtin_parsers",
    "registered_schemas",
    "resolve_parser_class",
    "site_parser_identity",
]


def load_builtin_parsers() -> None:
    """
    导入内建站点解析器包下的全部一级模块，使其中的解析器类完成定义与登记
    """
    package = importlib.import_module("app.modules.indexer.parser")
    for _, module_name, _ in pkgutil.iter_modules(package.__path__):
        if module_name.startswith("_") or module_name == "registry":
            continue
        importlib.import_module(f"app.modules.indexer.parser.{module_name}")


def resolve_parser_class(schema: Optional[str]) -> Optional[Any]:
    """
    按站点配置的 schema 标识解析对应的站点解析器类

    :param schema: 站点配置的 schema 标识
    :return: 匹配的站点解析器类；未登记该标识时为 None
    """
    if not schema:
        return None
    return PARSER_REGISTRY.get(schema)


def registered_schemas() -> Tuple[str, ...]:
    """
    列出当前已登记的全部站点标识

    :return: 站点标识元组
    """
    return tuple(PARSER_REGISTRY.keys())
