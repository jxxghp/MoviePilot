"""架构规则文档的可执行一致性约束。"""

import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[1]
RULE_DOCUMENTS = tuple(sorted((PROJECT_ROOT / "docs" / "rules").glob("*.md")))
NO_SESSION_OPER = re.compile(r"\b[A-Z][A-Za-z0-9]*Oper\(\)")
COMPATIBILITY_MARKERS = ("legacy plugin abi", "compatibility", "兼容")


def test_no_session_oper_examples_are_explicitly_compatibility_only() -> None:
    """无 Session Oper 示例必须紧邻 Legacy/Compat 说明，不能冒充宿主规范。"""
    violations: list[str] = []
    for path in RULE_DOCUMENTS:
        content = path.read_text(encoding="utf-8")
        for occurrence in NO_SESSION_OPER.finditer(content):
            context = content[
                max(0, occurrence.start() - 500):occurrence.end() + 200
            ].lower()
            if not any(marker in context for marker in COMPATIBILITY_MARKERS):
                line = content.count("\n", 0, occurrence.start()) + 1
                violations.append(f"{path.relative_to(PROJECT_ROOT)}:{line}")

    assert violations == [], (
        "无 Session Oper 仅允许出现在明确标注的插件 Legacy/Compat 示例中: "
        + ", ".join(violations)
    )
