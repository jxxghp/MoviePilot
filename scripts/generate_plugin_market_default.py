"""
根据 MoviePilot Wiki 清单生成发版快照中的插件市场默认值。
"""

import argparse
import ast
from pathlib import Path
from typing import Optional

from app.helper.market import extract_plugin_market_repos_from_wiki


OFFICIAL_PLUGIN_MARKET = "https://github.com/jxxghp/MoviePilot-Plugins"


def _parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成插件市场发版默认值")
    parser.add_argument("--wiki-file", type=Path, required=True)
    parser.add_argument("--config-file", type=Path, required=True)
    return parser.parse_args(args)


def _find_plugin_market_assignment(source: str) -> tuple[int, int, str]:
    tree = ast.parse(source)
    source_lines = source.splitlines(keepends=True)
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != "ConfigModel":
            continue
        for item in node.body:
            if not isinstance(item, ast.AnnAssign):
                continue
            if not isinstance(item.target, ast.Name):
                continue
            if item.target.id != "PLUGIN_MARKET" or item.end_lineno is None:
                continue
            start = item.lineno - 1
            indent_size = len(source_lines[start]) - len(source_lines[start].lstrip())
            indent = source_lines[start][:indent_size]
            return start, item.end_lineno, indent
    raise ValueError("未在 ConfigModel 中找到 PLUGIN_MARKET 默认值")


def _format_plugin_market_assignment(repos: list[str], indent: str) -> str:
    lines = [f"{indent}PLUGIN_MARKET: str = (\n"]
    for index, repo in enumerate(repos):
        suffix = "," if index < len(repos) - 1 else ""
        lines.append(f'{indent}    "{repo}{suffix}"\n')
    lines.append(f"{indent})\n")
    return "".join(lines)


def _generate_plugin_market_default(wiki_file: Path, config_file: Path) -> list[str]:
    markdown = wiki_file.read_text(encoding="utf-8")
    repos = extract_plugin_market_repos_from_wiki(markdown, require_markers=True)
    if not repos:
        raise ValueError("Wiki 插件仓库清单为空")
    if OFFICIAL_PLUGIN_MARKET not in repos:
        raise ValueError("Wiki 插件仓库清单缺少 MoviePilot 官方插件仓库")

    source = config_file.read_text(encoding="utf-8")
    start, end, indent = _find_plugin_market_assignment(source)
    source_lines = source.splitlines(keepends=True)
    replacement = _format_plugin_market_assignment(repos, indent)
    updated_source = (
        "".join(source_lines[:start])
        + replacement
        + "".join(source_lines[end:])
    )
    config_file.write_text(updated_source, encoding="utf-8")
    return repos


def main(args: Optional[list[str]] = None) -> int:
    """
    读取 Wiki 清单并更新指定配置文件中的插件市场默认值。
    """
    options = _parse_args(args)
    repos = _generate_plugin_market_default(options.wiki_file, options.config_file)
    print(f"已生成 PLUGIN_MARKET 默认值，共 {len(repos)} 个仓库")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
