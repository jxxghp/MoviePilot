"""将 uv 导出的锁定 URL 依赖转换为漏洞审计可识别的精确版本。"""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


DIRECT_REFERENCE = re.compile(
    r"^(?P<indent>\s*)(?P<name>[A-Za-z0-9_.-]+)\s+@\s+"
    r"(?P<url>\S+)(?P<suffix>.*)$"
)


def _canonicalize_name(name: str) -> str:
    """按 Python 包索引规则规范化分发包名。"""
    return re.sub(r"[-_.]+", "-", name).lower()


def _locked_url_versions(lock_file: Path) -> dict[tuple[str, str], str]:
    """读取锁文件中 URL 来源与已解析版本的唯一映射。"""
    with lock_file.open("rb") as file:
        document = tomllib.load(file)

    versions: dict[tuple[str, str], str] = {}
    for package in document.get("package", []):
        source = package.get("source") or {}
        url = source.get("url")
        name = package.get("name")
        version = package.get("version")
        if not all(isinstance(value, str) and value for value in (url, name, version)):
            continue
        key = (_canonicalize_name(name), url)
        previous = versions.setdefault(key, version)
        if previous != version:
            raise ValueError(f"锁文件包含冲突的 URL 依赖版本：{name} @ {url}")
    return versions


def normalize_requirements(requirements: str, lock_file: Path) -> str:
    """保留 marker 和注释，将直接引用替换为锁文件中的精确版本。"""
    versions = _locked_url_versions(lock_file)
    normalized_lines = []
    for line in requirements.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        newline = line[len(body):]
        match = DIRECT_REFERENCE.match(body)
        if not match:
            normalized_lines.append(line)
            continue

        key = (_canonicalize_name(match.group("name")), match.group("url"))
        version = versions.get(key)
        if not version:
            raise ValueError(
                "导出的 URL 依赖无法在锁文件中定位精确版本："
                f"{match.group('name')} @ {match.group('url')}"
            )
        normalized_lines.append(
            f"{match.group('indent')}{match.group('name')}=={version}"
            f"{match.group('suffix')}{newline}"
        )

    normalized = "".join(normalized_lines)
    if any(DIRECT_REFERENCE.match(line) for line in normalized.splitlines()):
        raise ValueError("审计清单仍包含未规范化的 URL 依赖")
    return normalized


def main() -> None:
    """读取 uv 导出文件并写入适合 pip-audit 的锁定版本清单。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.write_text(
        normalize_requirements(args.input.read_text(encoding="utf-8"), args.lock),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
