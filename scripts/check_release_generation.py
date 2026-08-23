"""阻止旧的正式发布任务覆盖较新的已发布载荷。"""

import re
import sys


_GENERATION_PATTERN = re.compile(r"([1-9][0-9]*)\.([1-9][0-9]*)")


def parse_generation(value: str) -> tuple[int, int]:
    """解析 GitHub Actions run id 与 attempt 组成的发布代际。"""
    match = _GENERATION_PATTERN.fullmatch(value)
    if not match:
        raise ValueError(f"无效的发布代际: {value}")
    return int(match.group(1)), int(match.group(2))


def main() -> int:
    """仅允许当前代际不早于已经公开的正式发布代际。"""
    if len(sys.argv) != 3:
        print(
            "用法: check_release_generation.py <candidate> <published>",
            file=sys.stderr,
        )
        return 2

    try:
        candidate = parse_generation(sys.argv[1])
        published = parse_generation(sys.argv[2])
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    if candidate < published:
        print(
            f"拒绝旧发布代际 {sys.argv[1]} 覆盖已发布代际 {sys.argv[2]}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
