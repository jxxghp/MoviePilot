"""
渠道公共核心的协议依赖约束；平台实现和装配注册拥有明确的独立边界。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE = (
    "app/modules/__init__.py",
    "app/modules/_base/channel.py",
    "app/domain/authorization.py",
    "app/application/security/authorization.py",
    "app/db/oper/authorization.py",
    "app/db/adapters/authorization.py",
    "app/application/messaging/attachment.py",
    "app/application/messaging/ingress.py",
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


class _PlatformFlow(ast.NodeVisitor):
    """
    在各函数作用域跟踪选择器、枚举及字面量表的别名；不把平台事实比较当作协议实现。
    """

    def __init__(self) -> None:
        """
        保存当前作用域符号事实与架构诊断。
        """
        self.environment: dict[str, frozenset[str]] = {"NotificationChannel": frozenset({"enum"})}
        self.issues: list[str] = []

    def symbol(self, node: ast.AST) -> frozenset[str]:
        """
        解析选择器或调度表的来源事实，不执行用户源码。
        """
        if isinstance(node, ast.Name):
            return self.environment.get(
                node.id, frozenset({"selector"}) if node.id in {"provider", "channel"} else frozenset()
            )
        if isinstance(node, ast.Attribute):
            if node.attr == "import_module":
                return frozenset({"import_loader"})
            if node.attr == "NotificationChannel":
                return frozenset({"enum"})
            if node.attr in {"provider", "channel"}:
                return frozenset({"selector"})
            return self.symbol(node.value)
        if isinstance(node, ast.Dict) and any(
            isinstance(key, ast.Constant) and isinstance(key.value, str) for key in node.keys
        ):
            return frozenset({"table"})
        if isinstance(node, ast.Subscript):
            if isinstance(node.slice, ast.Constant) and node.slice.value in {"provider", "channel"}:
                return frozenset({"selector"})
            return self.symbol(node.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return frozenset({"literal"})
        if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
            return frozenset({"literal"}) if any("literal" in self.symbol(item) for item in node.elts) else frozenset()
        if isinstance(node, ast.Call):
            return self._call_symbol(node)
        return frozenset()

    def _call_symbol(self, node: ast.Call) -> frozenset[str]:
        """
        传播 str/getattr 与配置字典 get 的选择器事实。
        """
        if isinstance(node.func, ast.Name) and node.func.id in {"str", "getattr"} and node.args:
            if (
                node.func.id == "getattr"
                and len(node.args) > 1
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in {"provider", "channel"}
            ):
                return frozenset({"selector"})
            return self.symbol(node.args[0])
        if isinstance(node.func, ast.Attribute):
            if (
                node.func.attr == "get"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value in {"provider", "channel"}
            ):
                return frozenset({"selector"})
            return self.symbol(node.func.value)
        return frozenset()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        """
        跟踪枚举导入别名，并拒绝具体模块静态依赖。
        """
        if node.module and (node.module == "app.modules" or node.module.startswith("app.modules.")):
            self.issues.append(f"{node.lineno}: 公共核心不得导入具体模块")
        for alias in node.names:
            if node.module == "importlib" and alias.name == "import_module":
                self.environment[alias.asname or alias.name] = frozenset({"import_loader"})
            if alias.name == "NotificationChannel":
                self.environment[alias.asname or alias.name] = frozenset({"enum"})

    def visit_Import(self, node: ast.Import) -> None:
        """
        拒绝通过模块导入别名隐藏具体实现依赖。
        """
        if any(alias.name == "app.modules" or alias.name.startswith("app.modules.") for alias in node.names):
            self.issues.append(f"{node.lineno}: 公共核心不得导入具体模块")

    def visit_Assign(self, node: ast.Assign) -> None:
        """
        顺序传播局部变量及字典别名，重新赋值仍保留已发现的选择器来源。
        """
        value = self.symbol(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                self.environment[target.id] = self.environment.get(target.id, frozenset()) | value
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """
        类型注解不能掩盖同一选择器或调度表。
        """
        if isinstance(node.target, ast.Name) and node.value is not None:
            self.environment[node.target.id] = self.symbol(node.value)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """
        函数继承模块事实，并隔离本地别名，避免同名局部变量污染其他函数。
        """
        outer = self.environment.copy()
        self.generic_visit(node)
        self.environment = outer

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """
        枚举本名、导入别名和赋值别名均不能用于平台分派。
        """
        if "enum" in self.symbol(node.value) and node.attr in PLATFORM_NAMES:
            self.issues.append(f"{node.lineno}: 公共核心不得按具体平台枚举分派")
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        """
        检查平台选择器与直接字面量的比较，允许两个动态平台事实比较。
        """
        values = [node.left, *node.comparators]
        for left, right in zip(values, values[1:]):
            symbols = (self.symbol(left), self.symbol(right))
            if any("selector" in symbol for symbol in symbols) and any("literal" in symbol for symbol in symbols):
                self.issues.append(f"{node.lineno}: 平台选择器分支应由能力或模块契约替代")
        self.generic_visit(node)

    def visit_Match(self, node: ast.Match) -> None:
        """
        模式匹配不能成为平台分支的另一种写法。
        """
        if "selector" in self.symbol(node.subject) and any(
            isinstance(child, ast.Constant) and isinstance(child.value, str)
            for case in node.cases
            for child in ast.walk(case.pattern)
        ):
            self.issues.append(f"{node.lineno}: 公共核心不得按平台模式分派")
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """
        字面量平台表的索引不是能力注册；别名查表也必须拒绝。
        """
        if "table" in self.symbol(node.value) and "selector" in self.symbol(node.slice):
            self.issues.append(f"{node.lineno}: 公共核心不得通过平台字典查表分派")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """
        检查 get 查表和常量动态具体模块导入。
        """
        if isinstance(node.func, ast.Attribute) and node.func.attr == "get" and node.args:
            if "table" in self.symbol(node.func.value) and "selector" in self.symbol(node.args[0]):
                self.issues.append(f"{node.lineno}: 公共核心不得通过平台字典 get 分派")
        if node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
            function = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else ""
            )
            if (function in {"import_module", "__import__"} or "import_loader" in self.symbol(node.func)) and node.args[
                0
            ].value.startswith("app.modules."):
                self.issues.append(f"{node.lineno}: 公共核心不得动态导入具体模块")
        self.generic_visit(node)


def source_violations(source: str) -> list[str]:
    """
    结合语法节点和局部数据流检查，文档、动态能力映射与分享 provider 比较不构成违规。

    :param source: 公共核心 Python 源码
    :return: 导入、协议常量或平台分派诊断
    """
    tree = ast.parse(source)
    flow = _PlatformFlow()
    flow.visit(tree)
    docs = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docs
            and PROTOCOL.search(node.value)
        ):
            flow.issues.append(f"{node.lineno}: 平台协议或配置常量应归所属模块")
    return sorted(set(flow.issues))


# 旧回环只保留在显式兼容实现中，内置渠道不能借别名重新调用。
LEGACY_INGRESS = frozenset({"forward_message_to_host", "async_forward_message_to_host", "submit_message_to_host"})
# 这些能力创建或接受可信来源，普通插件公开 SDK 不得暴露。
TRUSTED_INGRESS = frozenset({
    "AdmissionProof", "bind_platform_payload", "submit_bound_payload", "submit_platform_payload",
    "configure_platform_payload_receiver", "configure_verified_event_claim", "claim_verified_event",
    "configure_verified_message_receiver", "submit_verified_message",
})
# 第一版只公开现有规范类型；新增服务、实现、注册和装配须另行审查契约版本。
CHANNEL_SDK_EXPORTS = frozenset({
    "CHANNEL_EXTENSION_API_VERSION", "ChannelActor", "ChannelConnection", "ChannelConnectionInfo",
    "ChannelPlatform", "ChannelPlatformCapabilities", "IncomingMessage", "InteractionPolicy", "Message",
    "MessageAttachmentRead", "NotificationChannel", "NotificationRoute", "NotificationRule",
})


CHANNEL_SDK_OWNERS = {
    "CHANNEL_EXTENSION_API_VERSION": "app.schemas.channel",
    "ChannelActor": "app.domain.authorization", "ChannelPlatform": "app.application.messaging.channel.platform",
    "ChannelConnection": "app.schemas.channel", "ChannelConnectionInfo": "app.schemas.channel",
    "ChannelPlatformCapabilities": "app.schemas.channel", "InteractionPolicy": "app.schemas.channel",
    "NotificationRoute": "app.schemas.channel", "NotificationRule": "app.schemas.channel",
    "IncomingMessage": "app.schemas.message", "Message": "app.schemas.message",
    "MessageAttachmentRead": "app.schemas.message", "NotificationChannel": "app.schemas.types", "Final": "typing",
}


def legacy_ingress_violations(source: str) -> list[str]:
    """
    根据导入来源及局部别名识别旧回环调用，不限制普通用户定义的同名方法。

    :param source: 宿主 canonical 源码
    :return: 依赖旧 helper 的导入或调用位置
    """
    tree = ast.parse(source)
    bindings: dict[str, str] = {}
    issues = []

    def origin(node: ast.AST) -> str:
        """
        解析静态模块和局部别名的来源；不执行反射或用户源码。
        """
        if isinstance(node, ast.Name):
            return bindings.get(node.id, "")
        if isinstance(node, ast.Attribute) and (parent := origin(node.value)):
            return parent + "." + node.attr
        return ""

    def legacy(value: str) -> bool:
        """
        仅识别规范实现和保留的旧模块/SDK 中的显式回环符号。
        """
        owner, _, name = value.rpartition(".")
        return name in LEGACY_INGRESS and (
            owner in {"app.application.messaging.ingress", "app.sdk._legacy.ingress"}
            or owner.startswith("app.modules.")
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                value = node.module + "." + alias.name
                bindings[alias.asname or alias.name] = value
                if legacy(value) or (alias.name == "*" and (
                    node.module in {"app.application.messaging.ingress", "app.sdk._legacy.ingress"}
                    or node.module.startswith("app.modules.")
                )):
                    issues.append(f"{node.lineno}: 宿主不得导入旧 HTTP 回环 helper")
        if isinstance(node, ast.Import):
            for alias in node.names:
                bindings[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
    for _ in range(len(list(ast.walk(tree)))):
        previous = bindings.copy()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if value := origin(node.value):
                    bindings.update({target.id: value for target in targets if isinstance(target, ast.Name)})
        if previous == bindings:
            break
    for node in ast.walk(tree):
        if isinstance(node, (ast.Attribute, ast.Name)) and legacy(origin(node)):
            issues.append(f"{node.lineno}: 宿主不得通过模块或局部别名调用旧 HTTP 回环")
    return sorted(set(issues))


def sdk_source_violations(source: str, *, channel_facade: bool = False) -> list[str]:
    """
    约束公开 SDK 的可信入口泄漏，并固化第一版渠道类型门面的精确公开集合。

    :param source: SDK 文件源码
    :param channel_facade: 是否为仅允许类型导出的渠道门面
    :return: 不允许的导出、装配依赖或运行代码
    """
    tree = ast.parse(source)
    issues = []
    exports = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if (node.module is not None and node.module.startswith("app.modules")) or (
                node.module == "app.application.messaging.ingress" and (names & TRUSTED_INGRESS or "*" in names)
            ) or (
                node.module == "app.domain.admission" and ("AdmissionProof" in names or "*" in names)
            ) or (node.module == "app.application.messaging" and "ingress" in names) or (
                node.module == "app.domain" and "admission" in names
            ) or (
                node.module is not None and node.module.startswith("app.startup")
            ):
                issues.append(f"{node.lineno}: 公开 SDK 不得导入可信入口或私有证明")
            if channel_facade and any(
                CHANNEL_SDK_OWNERS.get(alias.name) != node.module or alias.asname not in (None, alias.name)
                for alias in node.names
            ):
                issues.append(f"{node.lineno}: 第一版渠道 SDK 必须使用类型的规范 owner")
        if isinstance(node, ast.Import) and any(
            alias.name in {"app.application.messaging.ingress", "app.domain.admission"}
            or alias.name.startswith(("app.startup", "app.modules")) for alias in node.names
        ):
            issues.append(f"{node.lineno}: 公开 SDK 不得暴露整个可信入口模块")
        if channel_facade and isinstance(node, (ast.Call, ast.FunctionDef, ast.AsyncFunctionDef, ast.Import)):
            issues.append(f"{node.lineno}: 渠道类型 SDK 不得隐藏运行服务或动态装配")
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            try:
                exports = set(ast.literal_eval(node.value))
            except (ValueError, TypeError):
                issues.append(f"{node.lineno}: SDK 公开集合必须可静态核对")
    if channel_facade and exports != CHANNEL_SDK_EXPORTS:
        issues.append("渠道 SDK 的公开集合偏离第一版契约")
    return issues


def sdk_boundary_violations(root: Path) -> list[str]:
    """
    允许已登记的旧实现及 SDK 私有兼容适配，检查其余宿主和公开 SDK 文件。
    """
    issues = []
    for path in (root / "app").rglob("*.py"):
        relative = path.relative_to(root).as_posix()
        if relative.startswith("app/sdk/") and "/_legacy/" not in relative:
            found = sdk_source_violations(path.read_text(encoding="utf-8-sig"), channel_facade=relative == "app/sdk/channel.py")
        elif relative.startswith(("app/sdk/", "app/runtime/compat/", "app/plugins/")) or relative == "app/application/messaging/ingress.py":
            continue
        else:
            found = legacy_ingress_violations(path.read_text(encoding="utf-8-sig"))
        issues.extend(f"{relative}:{issue}" for issue in found)
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
    return sdk_boundary_violations(root) + [
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
