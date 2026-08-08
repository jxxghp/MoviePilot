# -*- coding: utf-8 -*-
"""
兼容垫片：ModuleHelper 已迁移至 app.core.module_loader。

此文件保留为 re-export 垫片，使既有导入路径（含插件 app.plugins.autosignin）无需改动即可继续工作。
"""
from app.core.module_loader import FilterFuncType, ModuleHelper  # noqa: F401

__all__ = ["ModuleHelper", "FilterFuncType"]
