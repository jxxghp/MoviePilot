"""服务实例配置载荷在接口层的密钥掩码与回填。

服务实例配置的 ``config`` 列里装着 token、password、client_secret 这类凭据原值。列表
接口面向的是浏览器，密钥一旦随列表下发就会落进前端内存、日志与浏览器缓存，因此下发
的载荷里凭据一律换成掩码。

掩码换来的问题是「改一个端口号要不要重新输入密码」。这里的答案是不用：下发时被掩码
的字段，前端原样回传掩码即表示「这一项没动」，服务端从库里那一行取回原值；回传其它
内容即表示用户确实改了密码，按新值落库。判定只在「该字段被掩码过」且「库里确有原值」
时成立，因此掩码不会被当成密码存进库。

字段是不是凭据按键名判定，判据与 Agent 侧回执脱敏共用一份实现
（`app.agent.policy.secret_fields`），两处不会出现「这边算凭据、那边不算」的分歧。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, List, Tuple

from app.agent.policy.secret_fields import is_secret_setting_key

# 凭据下发时的占位取值。前端原样回传即表示该项未改动
SECRET_MASK = "********"

# 掩码路径中层级之间的分隔符，与嵌套对象的键名拼接方式一致
_PATH_SEPARATOR = "."

__all__ = ["SECRET_MASK", "mask_secret_values", "restore_masked_secrets"]


def mask_secret_values(value: Any, prefix: str = "") -> Tuple[Any, List[str]]:
    """
    递归地把载荷里的凭据原值换成掩码。

    只有非空取值才换掩码：空串与 None 表示这一项还没配过，掩掉会让前端把「未配置」
    显示成「已配置」，用户因而看不出自己漏填了哪一项。列表按下标编入路径，嵌套对象
    按键名逐层拼接，因此路径可以精确指到某一个被掩码的叶子。

    :param value: 待掩码的载荷，接受任意可 JSON 序列化的结构
    :param prefix: 当前层在整份载荷中的路径前缀，顶层为空串
    :return: ``(掩码后的载荷, 被掩码的字段路径列表)``
    """
    if isinstance(value, Mapping):
        masked: dict = {}
        paths: List[str] = []
        for key, item in value.items():
            path = f"{prefix}{_PATH_SEPARATOR}{key}" if prefix else str(key)
            if is_secret_setting_key(key) and _has_secret_value(item):
                masked[key] = SECRET_MASK
                paths.append(path)
                continue
            masked[key], nested = mask_secret_values(item, path)
            paths.extend(nested)
        return masked, paths
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        items: List[Any] = []
        paths = []
        for index, item in enumerate(value):
            masked_item, nested = mask_secret_values(item, f"{prefix}[{index}]")
            items.append(masked_item)
            paths.extend(nested)
        return items, paths
    return value, []


def restore_masked_secrets(submitted: Any, stored: Any) -> Any:
    """
    把提交载荷里原样回传的掩码换回库里的原值。

    掩码本身绝不落库：凭据字段提交掩码时，库里有原值就取回原值，库里没有就把这一项
    整个丢掉——从一份已掩码的列表复制出一条新配置时提交的正是掩码，存下去等于把
    ``********`` 当成了密码。其余一律按提交内容生效，用户确实改了密码时新值照常落库。

    :param submitted: 提交的载荷
    :param stored: 库中该配置现有的载荷
    :return: 回填后的载荷
    """
    if isinstance(submitted, Mapping):
        stored_mapping = stored if isinstance(stored, Mapping) else {}
        restored: dict = {}
        for key, item in submitted.items():
            stored_item = stored_mapping.get(key)
            if is_secret_setting_key(key) and item == SECRET_MASK:
                if _has_secret_value(stored_item):
                    restored[key] = stored_item
                continue
            restored[key] = restore_masked_secrets(item, stored_item)
        return restored
    if isinstance(submitted, Sequence) and not isinstance(submitted, (str, bytes)):
        stored_items = stored if isinstance(stored, Sequence) and not isinstance(
            stored, (str, bytes)
        ) else []
        return [
            restore_masked_secrets(
                item, stored_items[index] if index < len(stored_items) else None
            )
            for index, item in enumerate(submitted)
        ]
    return submitted


def _has_secret_value(value: Any) -> bool:
    """
    判断一个凭据字段当前有没有存着取值。

    :param value: 字段取值
    :return: 有取值时为 True；None、空串与空容器为 False
    """
    if value is None:
        return False
    if isinstance(value, (str, bytes, Mapping)) or (
        isinstance(value, Sequence) and not isinstance(value, (str, bytes))
    ):
        return len(value) > 0
    return True
