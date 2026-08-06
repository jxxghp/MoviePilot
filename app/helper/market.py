import re
from typing import Optional
from urllib.parse import urlparse


PLUGIN_MARKET_WIKI_START = "<!-- plugin-market-repos:start -->"
PLUGIN_MARKET_WIKI_END = "<!-- plugin-market-repos:end -->"
PLUGIN_MARKET_WIKI_URL = (
    "https://raw.githubusercontent.com/jxxghp/MoviePilot-Wiki/main/plugin.md"
)
PLUGIN_MARKET_REPO_PATTERN = re.compile(
    r"https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+(?:\.git)?/?",
    re.IGNORECASE,
)


def normalize_plugin_market_repo_url(repo_url: str) -> Optional[str]:
    """
    规范化插件仓库地址，便于跨来源合并去重。
    """
    repo_url = (repo_url or "").strip().rstrip("/")
    if not repo_url:
        return None
    repo_url = repo_url.removesuffix(".git")
    parsed_url = urlparse(repo_url)
    if parsed_url.scheme not in {"http", "https"}:
        return None
    if (parsed_url.hostname or "").lower() != "github.com":
        return None
    paths = [item for item in parsed_url.path.split("/") if item]
    if len(paths) < 2:
        return None
    return f"https://github.com/{paths[0]}/{paths[1]}"


def split_plugin_market_repo_urls(value: Optional[str]) -> list[str]:
    """
    拆分插件市场仓库配置并保持原有顺序去重。
    """
    repos: list[str] = []
    seen_repos = set()
    for item in re.split(r"[\n,，]+", value or ""):
        normalized_repo = normalize_plugin_market_repo_url(item)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return repos


def extract_plugin_market_repos_from_wiki(
    markdown: str, require_markers: bool = False
) -> list[str]:
    """
    从 Wiki 插件文档中提取插件仓库地址。

    :param markdown: Wiki 插件文档 Markdown 内容
    :param require_markers: 是否要求文档包含唯一且有序的清单边界标记
    :return: 规范化并按文档顺序去重的插件仓库地址
    """
    content = markdown or ""
    start_count = content.count(PLUGIN_MARKET_WIKI_START)
    end_count = content.count(PLUGIN_MARKET_WIKI_END)
    start_index = content.find(PLUGIN_MARKET_WIKI_START)
    end_index = content.find(PLUGIN_MARKET_WIKI_END)
    if start_count == 1 and end_count == 1 and start_index < end_index:
        content = content[
            start_index + len(PLUGIN_MARKET_WIKI_START):end_index
        ]
    elif require_markers:
        raise ValueError("Wiki 插件仓库清单必须包含唯一且有序的开始和结束标记")

    repos: list[str] = []
    seen_repos = set()
    for item in PLUGIN_MARKET_REPO_PATTERN.findall(content):
        normalized_repo = normalize_plugin_market_repo_url(item)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return repos


def merge_plugin_market_repos(
    local_repos: list[str], wiki_repos: list[str]
) -> list[str]:
    """
    合并本地与 Wiki 插件仓库地址，保留本地顺序并追加 Wiki 新地址。
    """
    merged_repos: list[str] = []
    seen_repos = set()
    for repo in local_repos + wiki_repos:
        normalized_repo = normalize_plugin_market_repo_url(repo)
        if not normalized_repo or normalized_repo.lower() in seen_repos:
            continue
        merged_repos.append(normalized_repo)
        seen_repos.add(normalized_repo.lower())
    return merged_repos
