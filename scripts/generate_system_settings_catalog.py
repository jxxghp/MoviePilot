#!/usr/bin/env python3
"""从配置源码注释生成 Agent 系统设置来源目录。"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.runtime.config import Settings  # noqa: E402
from app.schemas.types import SystemConfigKey  # noqa: E402

OUTPUT_PATH = (
    PROJECT_ROOT
    / "app"
    / "application"
    / "settings"
    / "resources"
    / "system_settings_catalog.json"
)
SECTION_PATTERN = re.compile(r"^#\s*=+\s*(.+?)\s*=+\s*$")
SECTION_GROUPS = {
    "基础应用配置": "application",
    "安全认证配置": "authentication",
    "数据库配置": "database",
    "数据库备份配置": "database_backup",
    "数据清理配置": "data_cleanup",
    "缓存配置": "cache",
    "网络代理配置": "network",
    "媒体元数据配置": "media_metadata",
    "TMDB配置": "tmdb",
    "Bangumi配置": "bangumi",
    "音乐配置": "music",
    "TVDB配置": "tvdb",
    "Fanart配置": "fanart",
    "云盘配置": "cloud_storage",
    "系统升级配置": "system_update",
    "媒体文件格式配置": "media_formats",
    "媒体服务器配置": "media_servers",
    "订阅配置": "subscriptions",
    "站点配置": "sites",
    "搜索配置": "search",
    "下载配置": "downloads",
    "目录监控配置": "directory_monitor",
    "CookieCloud配置": "cookiecloud",
    "整理配置": "transfer",
    "服务地址配置": "services",
    "个性化": "personalization",
    "插件配置": "plugins",
    "技能配置": "skills",
    "Github & PIP": "dependencies",
    "飞书通知配置": "feishu",
    "性能配置": "performance",
    "安全配置": "security",
    "工作流配置": "workflow",
    "存储配置": "storage",
    "AI智能体配置": "ai_agent",
}


def _load_module(path: Path) -> tuple[ast.Module, list[str]]:
    """解析一个 Python 模块并返回 AST 与原始行。"""
    text = path.read_text(encoding="utf-8")
    return ast.parse(text), text.splitlines()


def _find_class(module: ast.Module, class_name: str) -> ast.ClassDef:
    """按名称查找一个顶层类定义。"""
    for node in module.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise ValueError(f"找不到类定义: {class_name}")


def _assignment_name(node: ast.stmt) -> str | None:
    """返回简单类字段赋值的字段名。"""
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        return node.targets[0].id
    return None


def _preceding_comments(lines: list[str], line_number: int) -> list[str]:
    """读取字段前连续注释，并排除章节分隔标题。"""
    index = line_number - 2
    comments: list[str] = []
    while index >= 0:
        stripped = lines[index].strip()
        if not stripped.startswith("#"):
            break
        if not SECTION_PATTERN.match(stripped):
            comments.append(stripped[1:].strip())
        index -= 1
    return [comment for comment in reversed(comments) if comment]


def _sections(lines: list[str]) -> list[tuple[int, str]]:
    """返回源码中出现的章节标题及其行号。"""
    results: list[tuple[int, str]] = []
    for line_number, line in enumerate(lines, start=1):
        match = SECTION_PATTERN.match(line.strip())
        if match:
            results.append((line_number, match.group(1).strip()))
    return results


def _section_before(
    sections: list[tuple[int, str]],
    *,
    line_number: int,
) -> str | None:
    """返回指定源码行之前最近的章节标题。"""
    return next(
        (name for section_line, name in reversed(sections) if section_line < line_number),
        None,
    )


def _extract_model_fields(
    path: Path,
    class_name: str,
    *,
    default_group: str | None = None,
) -> dict[str, dict[str, Any]]:
    """提取 Pydantic 配置模型字段的说明、分类和源码位置。"""
    module, lines = _load_module(path)
    class_node = _find_class(module, class_name)
    sections = _sections(lines)
    results: dict[str, dict[str, Any]] = {}
    for node in class_node.body:
        field_name = _assignment_name(node)
        if not field_name or field_name == "model_config" or field_name in results:
            continue
        description = " ".join(_preceding_comments(lines, node.lineno)).strip()
        section = _section_before(sections, line_number=node.lineno)
        group = default_group or SECTION_GROUPS.get(section or "")
        results[field_name] = {
            "description": description,
            "group": group,
            "source_file": str(path.relative_to(PROJECT_ROOT)),
            "source_line": node.lineno,
        }
    return results


def _extract_systemconfig_fields(path: Path) -> dict[str, dict[str, Any]]:
    """提取 SystemConfigKey 枚举值的业务说明和源码位置。"""
    module, lines = _load_module(path)
    class_node = _find_class(module, "SystemConfigKey")
    results: dict[str, dict[str, Any]] = {}
    for node in class_node.body:
        field_name = _assignment_name(node)
        if not field_name or not isinstance(node, ast.Assign):
            continue
        value = ast.literal_eval(node.value)
        results[str(value)] = {
            "enum_name": field_name,
            "description": " ".join(_preceding_comments(lines, node.lineno)).strip(),
            "source_file": str(path.relative_to(PROJECT_ROOT)),
            "source_line": node.lineno,
        }
    return results


def generate_catalog() -> dict[str, Any]:
    """生成覆盖当前 Settings 与 SystemConfigKey 的稳定来源目录。"""
    runtime_fields = _extract_model_fields(
        PROJECT_ROOT / "app" / "runtime" / "config.py",
        "ConfigModel",
    )
    log_fields = _extract_model_fields(
        PROJECT_ROOT / "app" / "runtime" / "log.py",
        "LogConfigModel",
        default_group="logging",
    )
    for field_name, metadata in log_fields.items():
        runtime_fields.setdefault(field_name, metadata)

    expected_runtime = set(Settings.model_fields)
    missing_runtime = expected_runtime - runtime_fields.keys()
    undocumented_runtime = {
        field_name
        for field_name in expected_runtime
        if not runtime_fields[field_name].get("description")
        or not runtime_fields[field_name].get("group")
    }
    if missing_runtime or undocumented_runtime:
        raise ValueError(
            "Settings 来源目录不完整: "
            f"missing={sorted(missing_runtime)}, undocumented={sorted(undocumented_runtime)}"
        )
    runtime_fields = {
        field_name: runtime_fields[field_name]
        for field_name in sorted(expected_runtime)
    }

    systemconfig_fields = _extract_systemconfig_fields(
        PROJECT_ROOT / "app" / "schemas" / "types.py"
    )
    expected_systemconfig = {item.value for item in SystemConfigKey}
    missing_systemconfig = expected_systemconfig - systemconfig_fields.keys()
    undocumented_systemconfig = {
        field_name
        for field_name in expected_systemconfig
        if not systemconfig_fields[field_name].get("description")
    }
    if missing_systemconfig or undocumented_systemconfig:
        raise ValueError(
            "SystemConfigKey 来源目录不完整: "
            f"missing={sorted(missing_systemconfig)}, undocumented={sorted(undocumented_systemconfig)}"
        )
    systemconfig_fields = {
        field_name: systemconfig_fields[field_name]
        for field_name in sorted(expected_systemconfig)
    }
    return {
        "schema_version": 1,
        "settings": runtime_fields,
        "systemconfig": systemconfig_fields,
    }


def main() -> int:
    """写入格式稳定的系统设置来源目录。"""
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(generate_catalog(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"generated {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
