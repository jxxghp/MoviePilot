"""从隔离 Git clone 收集 PR 预览所需的变更摘要。"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Optional

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from pr_common import (  # noqa: E402
    PullRequestError,
    change_fingerprint,
    collect_changes,
    git_current_branch,
    read_json_file,
    result_payload,
    runtime_dir,
    write_json_file,
)


def collect_pull_request(source_root: str, *, state_file: Optional[str] = None) -> dict[str, Any]:
    """收集当前 clone 的 Git 变更，并保存不含源码正文的摘要文件。"""
    root = Path(source_root).expanduser().resolve()
    mode, changes = collect_changes(root)
    branch = git_current_branch(root)
    state: dict[str, Any] = {}
    if state_file:
        state = read_json_file(Path(state_file).expanduser().resolve())
        if Path(str(state.get("source_root") or "")).expanduser().resolve() != root:
            raise PullRequestError("state_mismatch", "clone 状态文件与 source_root 不匹配。")
        if state.get("branch") and state["branch"] != branch:
            raise PullRequestError("state_mismatch", "clone 状态文件与当前工作分支不匹配。")

    fingerprint = change_fingerprint(changes)
    digest = hashlib.sha256(f"{root}:{fingerprint}".encode("utf-8")).hexdigest()[:20]
    changes_file = runtime_dir() / "changes" / f"{digest}.json"
    summary = [
        {
            key: item[key]
            for key in (
                "path",
                "operation",
                "before_sha256",
                "after_sha256",
                "size_bytes",
                "mode",
            )
        }
        for item in changes
    ]
    write_json_file(
        changes_file,
        {
            "version": 1,
            "source_root": str(root),
            "branch": branch,
            "mode": mode,
            "fingerprint": fingerprint,
            "changes": summary,
        },
    )
    return {
        "success": True,
        "mode": mode,
        "source_root": str(root),
        "branch": branch,
        "state_file": state_file,
        "changes_file": str(changes_file),
        "change_count": len(summary),
        "changes": summary,
        "message": "已从 Git 工作树生成 PR 变更摘要；摘要不包含源码正文。",
    }


def _parse_args() -> argparse.Namespace:
    """解析变更采集脚本命令行参数。"""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", required=True, help="先前 git clone 的仓库根目录")
    parser.add_argument("--state-file", help="clone_repository.py 输出的状态文件")
    return parser.parse_args()


def main() -> int:
    """执行变更采集入口并输出机器可解析 JSON。"""
    try:
        arguments = _parse_args()
        result = collect_pull_request(arguments.source_root, state_file=arguments.state_file)
    except (PullRequestError, ValueError) as error:
        print(result_payload(success=False, reason=getattr(error, "reason", "invalid_request"), message=str(error)))
        return 2
    print(result_payload(**result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
