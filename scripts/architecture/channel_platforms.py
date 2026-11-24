"""
渠道公共核心的协议依赖约束；平台实现和装配注册拥有明确的独立边界。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = (
    "app/modules/_base/channel.py",
    "app/domain/authorization.py",
    "app/application/security/authorization.py",
    "app/db/oper/authorization.py",
    "app/db/adapters/authorization.py",
    "app/application/messaging/attachment.py",
    "app/schemas/channel.py",
    "app/startup/initializers/modules.py",
    "app/chain/message.py",
)
PROTOCOL = re.compile(
    r"(?:^(?:tg|wxwork|wxbot|wxclaw|feishu|slack|discord|qq|vocechat|synology)://|^(?:group:|GID#|UID#)|^(?:TELEGRAM|WECHAT|WECHATCLAWBOT|FEISHU|QQ|QQBOT|DISCORD|SLACK|VOCECHAT|SYNOLOGYCHAT|DINGTALK|WEBPUSH|PUSH)_)"
)
PLATFORM_NAMES = {
    "Telegram",
    "Wechat",
    "WechatClawBot",
    "Feishu",
    "QQ",
    "Discord",
    "Slack",
    "VoceChat",
    "SynologyChat",
    "DingTalk",
    "WebPush",
    "AppPush",
}


def source_violations(source: str) -> list[str]:
    """
    检查实际语法节点，忽略注释、说明文字和平台格式分享的通用比较。

    :param source: 公共核心 Python 源码
    :return: 导入、协议常量或平台条件依赖诊断
    """
    tree = ast.parse(source)
    docs = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    issues = []
    for node in ast.walk(tree):
        imports = []
        if isinstance(node, ast.ImportFrom) and node.module:
            imports = [node.module]
        elif isinstance(node, ast.Import):
            imports = [alias.name for alias in node.names]
        if any(name == "app.modules" or name.startswith("app.modules.") for name in imports):
            issues.append(f"{node.lineno}: 公共核心不得导入具体模块")
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and PROTOCOL.search(node.value)
        ):
            issues.append(f"{node.lineno}: 平台协议或配置常量应归所属模块")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "NotificationChannel"
            and node.attr in PLATFORM_NAMES
        ):
            issues.append(f"{node.lineno}: 公共核心不得按具体平台枚举分派")
        if isinstance(node, ast.Compare):
            expressions = [node.left, *node.comparators]
            provider = any(isinstance(item, ast.Attribute) and item.attr == "provider" for item in expressions)
            literal = any(
                isinstance(child, ast.Constant) and isinstance(child.value, str) and child.value
                for item in expressions
                for child in ast.walk(item)
            )
            if provider and literal:
                issues.append(f"{node.lineno}: 平台类型分支应由能力或模块契约替代")
    return issues


def violations(root: Path = ROOT) -> list[str]:
    """
    检查固定公共边界及新增的渠道应用文件，不扫描平台实现或冻结升级代码。

    :param root: 仓库根路径
    :return: 带源码位置的全部违规
    """
    paths = set(CORE) | {
        str(path.relative_to(root)) for path in (root / "app/application/messaging/channel").glob("*.py")
    }
    return [
        f"{path}:{issue}"
        for path in sorted(paths)
        for issue in source_violations((root / path).read_text(encoding="utf-8"))
    ]


def main() -> int:
    """
    作为独立架构门禁执行，不包含写基线或自动豁免模式。
    """
    issues = violations()
    print("\n".join(issues) if issues else "渠道核心与平台实现边界检查通过")
    return int(bool(issues))


if __name__ == "__main__":
    raise SystemExit(main())
