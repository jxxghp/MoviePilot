"""
根据 MoviePilot Wiki 清单生成发版快照中的插件市场默认值。
"""

import argparse
import ast
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


OFFICIAL_PLUGIN_MARKET = "https://github.com/jxxghp/MoviePilot-Plugins"
PLUGIN_MARKET_WIKI_START = "<!-- plugin-market-repos:start -->"
PLUGIN_MARKET_WIKI_END = "<!-- plugin-market-repos:end -->"
PLUGIN_MARKET_REPO_PATTERN = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
    re.IGNORECASE,
)


def _parse_args(args: Optional[list[str]] = None) -> argparse.Namespace:
    """解析独立构建脚本需要的 Wiki 和配置文件路径。"""
    parser = argparse.ArgumentParser(description="生成插件市场发版默认值")
    parser.add_argument("--wiki-file", type=Path, required=True)
    parser.add_argument("--config-file", type=Path, required=True)
    return parser.parse_args(args)


def _normalize_plugin_market_repo_url(repo_url: str) -> Optional[str]:
    """规范化 GitHub 插件仓库地址，供清单解析阶段去重。"""
    repo_url = (repo_url or "").strip().rstrip("/").removesuffix(".git")
    if not repo_url:
        return None
    parsed_url = urlparse(repo_url)
    if parsed_url.scheme not in {"http", "https"}:
        return None
    if (parsed_url.hostname or "").lower() != "github.com":
        return None
    paths = [item for item in parsed_url.path.split("/") if item]
    if len(paths) < 2:
        return None
    return f"https://github.com/{paths[0]}/{paths[1]}"


def _extract_plugin_market_repos_from_wiki(markdown: str) -> list[str]:
    """从唯一且有序的 Wiki 标记区域读取插件仓库清单。"""
    start_count = markdown.count(PLUGIN_MARKET_WIKI_START)
    end_count = markdown.count(PLUGIN_MARKET_WIKI_END)
    start_index = markdown.find(PLUGIN_MARKET_WIKI_START)
    end_index = markdown.find(PLUGIN_MARKET_WIKI_END)
    if start_count != 1 or end_count != 1 or start_index >= end_index:
        raise ValueError("Wiki 插件仓库清单必须包含唯一且有序的开始和结束标记")

    content = markdown[
        start_index + len(PLUGIN_MARKET_WIKI_START):end_index
    ]
    repos: list[str] = []
    seen_repos: set[str] = set()
    for item in PLUGIN_MARKET_REPO_PATTERN.findall(content):
        normalized_repo = _normalize_plugin_market_repo_url(item)
        identity = normalized_repo.lower() if normalized_repo else ""
        if not normalized_repo or identity in seen_repos:
            continue
        repos.append(normalized_repo)
        seen_repos.add(identity)
    return repos


def _find_plugin_market_assignment(source: str) -> tuple[int, int, str]:
    """定位 ConfigModel 中插件市场默认值对应的源码范围。"""
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
    """按配置文件现有缩进格式生成插件市场默认值源码。"""
    lines = [f"{indent}PLUGIN_MARKET: str = (\n"]
    for index, repo in enumerate(repos):
        suffix = "," if index < len(repos) - 1 else ""
        lines.append(f'{indent}    "{repo}{suffix}"\n')
    lines.append(f"{indent})\n")
    return "".join(lines)


def _generate_plugin_market_default(wiki_file: Path, config_file: Path) -> list[str]:
    """读取 Wiki 清单并原地更新配置文件中的插件市场默认值。"""
    markdown = wiki_file.read_text(encoding="utf-8")
    repos = _extract_plugin_market_repos_from_wiki(markdown)
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
