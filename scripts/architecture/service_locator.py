#!/usr/bin/env python3
"""
防止宿主消费者绕过应用层门面直接定位进程级运行时。
"""

from __future__ import annotations

from dataclasses import dataclass

if __package__:
    from scripts.architecture.baseline import discover_modules, resolve_imports
# 测试通过包导入覆盖逻辑；直接脚本入口需要从同目录解析依赖。
else:  # pragma: no cover
    from baseline import discover_modules, resolve_imports


@dataclass(frozen=True, slots=True)
class RuntimeFacadePolicy:
    """
    声明一个具体运行时模块的合法装配与兼容消费者。

    :param name: 治理规则标识，用于诊断关联具体运行时能力
    :param dependency: 禁止普通消费者直接导入的运行时模块完整路径
    :param exact_consumers: 获准直接依赖该模块的精确消费者模块名
    :param consumer_prefixes: 获准直接依赖的模块及其子包前缀，默认不额外放行子树
    """

    name: str
    dependency: str
    exact_consumers: frozenset[str]
    consumer_prefixes: tuple[str, ...] = ()

    def allows(self, module_name: str) -> bool:
        """
        判断模块是否属于明确的组合根、实现包或兼容入口。

        :param module_name: 待检查消费者的完整模块名
        :return: 命中精确名单或允许的完整子树时返回 True
        """
        if module_name in self.exact_consumers:
            return True
        return any(
            module_name == prefix or module_name.startswith(f"{prefix}.")
            for prefix in self.consumer_prefixes
        )


# 具体运行时只向列明的装配或兼容模块开放，普通消费者必须使用应用层门面。
RUNTIME_FACADE_POLICIES = (
    RuntimeFacadePolicy(
        name="scheduler",
        dependency="app.scheduler.facade",
        exact_consumers=frozenset(
            {
                "app.startup.initializers.modules",
                "app.startup.initializers.scheduler",
            }
        ),
    ),
    RuntimeFacadePolicy(
        name="module",
        dependency="app.runtime.extensions.module.manager",
        exact_consumers=frozenset(
            {
                "app.sdk.plugin.manager",
                "app.startup.composition.chain",
                "app.startup.composition.cliagent",
                "app.startup.initializers.modules",
            }
        ),
    ),
    RuntimeFacadePolicy(
        name="plugin",
        dependency="app.runtime.extensions.plugin.manager",
        exact_consumers=frozenset(
            {
                "app.sdk.plugin.manager",
                "app.startup.composition.chain",
                "app.startup.composition.cliagent",
                "app.startup.initializers.plugins",
            }
        ),
    ),
    RuntimeFacadePolicy(
        name="command",
        dependency="app.command",
        exact_consumers=frozenset(
            {
                "app.startup.composition.outbox",
                "app.startup.initializers.command",
                "app.startup.initializers.modules",
            }
        ),
    ),
    RuntimeFacadePolicy(
        name="workflow",
        dependency="app.workflow",
        exact_consumers=frozenset({"app.startup.initializers.workflow"}),
        consumer_prefixes=("app.workflow",),
    ),
)


@dataclass(frozen=True, order=True, slots=True)
class ServiceLocatorViolation:
    """
    描述一处绕过应用层门面的具体运行时依赖。

    :param policy: 命中的运行时治理规则标识
    :param source: 存在越界导入的宿主模块完整路径
    :param dependency: 被直接导入的具体运行时模块完整路径
    """

    policy: str
    source: str
    dependency: str

    def render(self) -> str:
        """
        返回适合 CI 输出的稳定诊断文本。
        """
        return (
            f"{self.source}: {self.policy} 运行时必须经 application Facade 访问，"
            f"不得直接依赖 {self.dependency}"
        )


def collect_service_locator_violations(
    graph: dict[str, set[str]] | None = None,
) -> list[ServiceLocatorViolation]:
    """
    扫描宿主依赖图，返回绕过已登记 Facade 的运行时直连。

    :param graph: 模块到依赖模块集合的映射；为 None 时从当前仓库源码构建
    :return: 按规则、来源和依赖稳定排序的违规列表
    """
    if graph is None:
        modules = discover_modules()
        known_modules = set(modules)
        graph = {
            module_name: resolve_imports(module_name, path, known_modules)
            for module_name, path in modules.items()
        }

    violations: list[ServiceLocatorViolation] = []
    for policy in RUNTIME_FACADE_POLICIES:
        for module_name, dependencies in graph.items():
            if (
                policy.dependency not in dependencies
                or policy.allows(module_name)
            ):
                continue
            violations.append(
                ServiceLocatorViolation(
                    policy=policy.name,
                    source=module_name,
                    dependency=policy.dependency,
                )
            )
    return sorted(violations)


def main() -> int:
    """
    执行进程级运行时服务定位零债务门禁。
    """
    violations = collect_service_locator_violations()
    if violations:
        print("\n".join(violation.render() for violation in violations))
        return 1
    print("进程级运行时 Facade 门禁通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
