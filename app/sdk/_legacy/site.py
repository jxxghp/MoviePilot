"""
保留旧站点 Oper 导入路径和无 Session 插件调用形态。
"""

import inspect
from typing import Optional

from app.db.oper.site import SiteOper as CanonicalSiteOper


class SiteOper(CanonicalSiteOper):
    """
    继承表级站点 Oper，并将旧插件入口隔离在 Legacy SDK。
    """

    def success(self, domain: str, seconds: Optional[int] = None) -> None:
        """
        转发站点成功统计，并保留旧插件可见的调用签名。
        """
        super().success(domain, seconds)

    def fail(self, domain: str) -> None:
        """
        转发站点失败统计，并保留旧插件可见的调用签名。
        """
        super().fail(domain)


# 旧插件通过 inspect.signature 校验这两个入口没有显式返回注解；转发方法
# 保持类型检查所需的完整返回合同，再把仅用于反射的签名恢复为历史形态。
_legacy_success_signature = inspect.signature(CanonicalSiteOper.success).replace(
    return_annotation=inspect.Signature.empty,
)
_legacy_fail_signature = inspect.signature(CanonicalSiteOper.fail).replace(
    return_annotation=inspect.Signature.empty,
)
setattr(SiteOper.success, "__signature__", _legacy_success_signature)
setattr(SiteOper.fail, "__signature__", _legacy_fail_signature)


__all__ = ["SiteOper"]
