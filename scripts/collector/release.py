"""比较采集器发布输入，按独立 Release 递增版本并写入构建副本。"""

import argparse
import re
import subprocess
from pathlib import Path
from typing import Optional

TAG_PREFIX = "site-adapter-collector-v"
RELEASE_PATHS = (
    "scripts/site_adapter_collector.py",
    "scripts/site_adapter_collector.spec",
    "scripts/site_adapter_collector_requirements.txt",
    "scripts/start-site-adapter-collector.command",
    "scripts/collect-site-adapter.sh",
    "scripts/collector",
    ".github/workflows/site-adapter-collector.yml",
)
VERSION_PATTERN = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
SOURCE_VERSION_PATTERN = re.compile(r'^COLLECTOR_VERSION = "([\d.]+)"$', re.MULTILINE)


def _version_parts(version: str) -> tuple[int, int, int]:
    """校验独立版本，避免外部标签进入 Git 参数或构建源码。"""
    match = VERSION_PATTERN.fullmatch(version)
    if not match:
        raise ValueError(f"无效采集器版本：{version}")
    return tuple(int(part) for part in match.groups())


def latest_release_tag(tags: list[str]) -> Optional[str]:
    """从已正式发布的标签中选取采集器最高版本，忽略后端版本。"""
    collector_tags = [
        tag for tag in tags
        if tag.startswith(TAG_PREFIX) and VERSION_PATTERN.fullmatch(tag[len(TAG_PREFIX):])
    ]
    return max(collector_tags, key=lambda tag: _version_parts(tag[len(TAG_PREFIX):]), default=None)


def release_plan(repository: Path, previous_tag: Optional[str]) -> dict[str, str]:
    """仅比较采集器输入；未变化时跳过发布，首次沿用源码版本。"""
    source = repository / "scripts/site_adapter_collector.py"
    source_match = SOURCE_VERSION_PATTERN.search(source.read_text(encoding="utf-8"))
    if not source_match:
        raise ValueError("采集器源码缺少唯一版本声明")
    source_version = source_match.group(1)
    source_parts = _version_parts(source_version)
    source_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repository, text=True,
    ).strip()
    changed = True
    version = source_version
    if previous_tag:
        previous_parts = _version_parts(previous_tag.removeprefix(TAG_PREFIX))
        result = subprocess.run(
            ["git", "diff", "--quiet", previous_tag, "HEAD", "--", *RELEASE_PATHS],
            cwd=repository, check=False,
        )
        if result.returncode not in (0, 1):
            raise RuntimeError(f"无法比较采集器 Release：{previous_tag}")
        changed = result.returncode == 1
        if changed:
            next_parts = max(source_parts, (*previous_parts[:2], previous_parts[2] + 1))
            version = ".".join(str(part) for part in next_parts)
        else:
            version = previous_tag[len(TAG_PREFIX):]
    return {
        "changed": str(changed).lower(),
        "version": version,
        "tag": f"{TAG_PREFIX}{version}",
        "previous_tag": previous_tag or "",
        "source_sha": source_sha,
    }


def stamp_version(source: Path, version: str) -> None:
    """只修改 Runner 构建副本，使程序及脱敏包版本与独立 Release 一致。"""
    _version_parts(version)
    content, count = SOURCE_VERSION_PATTERN.subn(
        f'COLLECTOR_VERSION = "{version}"', source.read_text(encoding="utf-8"),
    )
    if count != 1:
        raise ValueError("采集器源码缺少唯一版本声明")
    source.write_text(content, encoding="utf-8")


def main() -> None:
    """供工作流选择独立发布版本或给构建副本写入版本。"""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("--tags-file", type=Path, required=True)
    plan_parser.add_argument("--output", type=Path, required=True)
    stamp_parser = commands.add_parser("stamp")
    stamp_parser.add_argument("--version", required=True)
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[2]
    if args.command == "stamp":
        stamp_version(repository / "scripts/site_adapter_collector.py", args.version)
        return
    tags = args.tags_file.read_text(encoding="utf-8").splitlines()
    previous_tag = latest_release_tag(tags)
    if previous_tag:
        subprocess.run(
            ["git", "fetch", "--no-tags", "origin", f"refs/tags/{previous_tag}:refs/tags/{previous_tag}"],
            cwd=repository, check=True,
        )
    plan = release_plan(repository, previous_tag)
    with args.output.open("a", encoding="utf-8") as output:
        for name, value in plan.items():
            output.write(f"{name}={value}\n")
    print(f"采集器版本：{plan['version']}；输入变化：{plan['changed']}")


if __name__ == "__main__":
    main()
