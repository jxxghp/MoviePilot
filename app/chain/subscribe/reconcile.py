"""订阅站点与规则组引用协调。"""

from app.chain.subscribe.contract import _SubscribeOwnerBase
from app.runtime.events import Event
from app.schemas.types import (
    SystemConfigKey,
)


class SubscribeReconciliationOwner(_SubscribeOwnerBase):
    """集中清理订阅持久化中的站点和规则组悬空引用。"""

    def _remove_site(self, event: Event) -> None:
        """
        从订阅中移除与站点相关的设置
        """
        if not event:
            return
        event_data = event.event_data or {}
        site_id = event_data.get("site_id")
        if not site_id:
            return
        with self.site_reference_mutation_scope() as mutation:
            mutation.apply(site_id)

    def _reconcile_rule_group_references(self, event: Event) -> None:
        """规则组定义保存后清理默认配置和已有订阅中的悬空引用。"""
        if not event:
            return
        event_data = event.event_data
        if isinstance(event_data, dict):
            changed_keys = event_data.get("key", set())
            value = event_data.get("value")
        else:
            changed_keys = getattr(event_data, "key", set())
            value = getattr(event_data, "value", None)
        if isinstance(changed_keys, str):
            changed_keys = {changed_keys}
        normalized_keys = {str(key) for key in (changed_keys or set())}
        if not normalized_keys.intersection(
            {
                SystemConfigKey.UserFilterRuleGroups.value,
                str(SystemConfigKey.UserFilterRuleGroups),
            }
        ):
            return

        definitions = [dict(group) for group in value if isinstance(group, dict)] if isinstance(value, list) else []
        with self.rule_group_mutation_scope() as mutation:
            mutation.apply(definitions, expected_rule_groups=definitions)
