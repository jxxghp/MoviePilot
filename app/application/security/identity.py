"""第三方身份绑定应用服务，供端点查看和解绑当前用户的绑定。"""

from __future__ import annotations

from typing import Any, List, Optional, Protocol


class UserIdentityRepository(Protocol):
    """第三方身份绑定用例所需的最小数据端口。"""

    def list_by_user_id(self, user_id: int) -> List[Any]:
        """列出指定用户的全部身份绑定。"""

    def unbind(self, identity_id: int, user_id: int) -> bool:
        """解绑指定用户名下的身份绑定，不属于该用户时返回 False。"""


class UserIdentityService:
    """编排第三方身份绑定的查看与解绑。"""

    def __init__(self, repository: UserIdentityRepository) -> None:
        """注入第三方身份绑定数据端口。"""
        self._repository = repository

    def list_by_user_id(self, user_id: int) -> List[Any]:
        """列出指定用户的全部身份绑定。"""
        return self._repository.list_by_user_id(user_id)

    def unbind(self, identity_id: int, user_id: int) -> bool:
        """解绑指定用户名下的身份绑定，不属于该用户时返回 False。"""
        return self._repository.unbind(identity_id, user_id)
