"""发布工作流使用的原始提交记录生成器，不分类或改写提交标题。"""

import argparse
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote


def command(*args: str) -> str:
    """以参数列表执行发布工具；查询失败时终止，避免发布不完整记录。"""
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def previous_tag(tag: str) -> str | None:
    """选取低于当前版本的最近正式标签，兼容历史数字修订后缀，排除本版本和命名预发布标签。"""
    version = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-(\d+))?", tag)
    if version is None:
        raise ValueError(f"不支持的正式版本标签：{tag}")
    current = tuple(int(part or 0) for part in version.groups())
    candidates = []
    for candidate in command("git", "tag", "--list", "v*").splitlines():
        match = re.fullmatch(r"v(\d+)\.(\d+)\.(\d+)(?:-(\d+))?", candidate)
        if match is not None:
            number = tuple(int(part or 0) for part in match.groups())
            if number[0] == current[0] and number < current:
                candidates.append((number, candidate))
    return max(candidates)[1] if candidates else None


def collect_commits(repository: str, source: str, previous: str | None) -> list[dict[str, Any]]:
    """分页读取指定发布范围的提交及真实 GitHub 作者账号，支持直接提交。"""
    if previous:
        endpoint = f"repos/{repository}/compare/{quote(previous, safe='')}...{source}?per_page=100"
        pages = json.loads(command("gh", "api", "--paginate", "--slurp", endpoint))
        commits = [commit for page in pages for commit in page["commits"]]
    else:
        endpoint = f"repos/{repository}/commits?sha={source}&per_page=100"
        pages = json.loads(command("gh", "api", "--paginate", "--slurp", endpoint))
        commits = [commit for page in pages for commit in page]
        commits.reverse()
    return commits


def changelog(commits: list[dict[str, Any]]) -> str:
    """保留提交标题、作者与链接，仅排除合并和自动发布快照，不合并同名提交。"""
    entries = []
    seen = set()
    for commit in commits:
        sha = commit["sha"]
        if sha in seen:
            continue
        seen.add(sha)
        subject = commit["commit"]["message"].splitlines()[0]
        if len(commit.get("parents", [])) > 1 or subject.startswith("Merge "):
            continue
        if subject.startswith("build(plugin-market): sync default from MoviePilot-Wiki@"):
            continue
        author = commit.get("author") or {}
        login = author.get("login")
        # 未关联 GitHub 账号的提交保留显示名，不伪造 @账号。
        attribution = f"@{login}" if login else commit["commit"]["author"]["name"]
        entries.append(f"- {subject} by {attribution} ([{sha[:7]}]({commit['html_url']}))")
    return "\n".join(entries)


def manual_summary(body: str) -> str:
    """保留手工摘要及内部 Markdown；旧版自动记录不迁入用户摘要。"""
    match = re.search(r"^## 本次更新内容[ \t]*\r?$", body, re.MULTILINE)
    if match:
        remaining = body[match.end():]
        boundary = re.search(r"^## (?:完整变更记录|版本对比)[ \t]*\r?$", remaining, re.MULTILINE)
        return remaining[:boundary.start()].strip() if boundary else remaining.strip()
    # 兼容已有手工 Release 正文，避免首次切换模板时丢失公告。
    legacy_generated = re.search(r"^### (?:✨ 新功能|🐛 修复|🔧 其他)", body, re.MULTILINE)
    return body[:legacy_generated.start()].strip() if legacy_generated else body.strip()


def release_body(summary: str, records: str, repository: str, previous: str | None, tag: str) -> str:
    """生成后端固定三章节；首版无比较起点时仅保留空的版本对比章节。"""
    comparison = ""
    if previous:
        url = f"https://github.com/{repository}/compare/{previous}...{tag}"
        comparison = f"[完整 Compare]({url})"
    return f"## 本次更新内容\n\n{summary}\n\n## 完整变更记录\n\n{records}\n\n## 版本对比\n\n{comparison}\n"


def main() -> None:
    """向发布工作流传递完整正文；前端模式仅输出原始 changelog。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--changelog-only", action="store_true")
    args = parser.parse_args()
    previous = previous_tag(args.tag)
    records = changelog(collect_commits(args.repository, args.source, previous))
    body = records if args.changelog_only else release_body(
        manual_summary(os.environ.get("RELEASE_BODY", "")),
        records, args.repository, previous, args.tag,
    )
    delimiter = f"RELEASE_NOTES_{uuid.uuid4().hex}"
    with Path(os.environ["GITHUB_ENV"]).open("a", encoding="utf-8") as output:
        output.write(f"RELEASE_BODY<<{delimiter}\n{body}\n{delimiter}\n")


if __name__ == "__main__":
    main()
