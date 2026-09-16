"""主程序运行依赖的轻量可用性探针。"""

import argparse
import os
import shutil
from importlib import import_module
import sys
import sysconfig
import warnings

if os.name == "nt":
    psql_exe = shutil.which("psql")
    if psql_exe:
        getattr(os, "add_dll_directory")(os.path.dirname(psql_exe))


CORE_MODULES = (
    "alembic",
    "cloakbrowser",
    "fastapi",
    "pydantic",
    "pydantic_core",
    "pydantic_settings",
    "sqlalchemy",
    "starlette",
    "uvicorn",
)
NATIVE_MODULES = (
    ("asyncpg", "asyncpg"),
    ("bcrypt", "bcrypt._bcrypt"),
    ("brotli", "brotli"),
    ("crcmod", "crcmod.crcmod"),
    ("cryptography", "cryptography.hazmat.bindings._rust"),
    ("greenlet", "greenlet._greenlet"),
    ("lxml", "lxml.etree"),
    ("orjson", "orjson"),
    ("oss2", "oss2"),
    ("pillow", "PIL._imaging"),
    ("pillow-avif-plugin", "pillow_avif"),
    ("pydantic-core", "pydantic_core._pydantic_core"),
    ("zstandard", "zstandard"),
)


def _verify_text_capabilities() -> None:
    """验证标准与 free-threaded profile 共同依赖的文本能力。"""
    moviepilot_rust = import_module("moviepilot_rust")
    if not moviepilot_rust.is_available() or not moviepilot_rust.jieba_cut("中文分词"):
        raise RuntimeError("中文分词运行依赖不可用")

    converted = moviepilot_rust.zhconv_fast("后台", "zh-hant")
    if not converted:
        raise RuntimeError("中文转换运行依赖不可用")


def _verify_native_profile(*, free_threaded: bool) -> None:
    """验证 ABI 敏感依赖提供预期的原生能力。"""
    warnings.filterwarnings(
        "ignore",
        message=r'"\\&" is an invalid escape sequence\..*',
        category=SyntaxWarning,
    )
    imported = {}
    profile_modules = (
        (("psycopg", "psycopg"),)
        if free_threaded
        else (("psycopg2", "psycopg2"),)
    )
    for name, module_name in (*NATIVE_MODULES, *profile_modules):
        imported[name] = import_module(module_name)
        if free_threaded and sys._is_gil_enabled():
            raise RuntimeError(f"原生依赖 {name} 启用了 GIL")

    if not imported["crcmod"]._usingExtension:
        raise RuntimeError("crcmod 原生实现不可用")
    if free_threaded and imported["psycopg"].pq.__impl__ != "c":
        raise RuntimeError("psycopg C 实现不可用")


def main(*, full: bool = False) -> None:
    """验证 Web 栈和启动关键能力可导入、可执行。"""
    free_threaded = sysconfig.get_config_var("Py_GIL_DISABLED") == 1
    for module_name in CORE_MODULES:
        import_module(module_name)

    _verify_text_capabilities()
    if full:
        _verify_native_profile(free_threaded=free_threaded)
    if free_threaded and sys._is_gil_enabled():
        raise RuntimeError("核心运行依赖启用了 GIL")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true")
    main(full=parser.parse_args().full)
