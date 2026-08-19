"""PassKey 认证凭证应用服务。"""

from __future__ import annotations

from typing import Any, Optional, Protocol


class PasskeyRepository(Protocol):
    """PassKey 用例需要的最小同步数据端口。"""

    def list(self) -> list[Any]:
        """列出全部启用凭证。"""

    def list_by_user_id(self, user_id: int) -> list[Any]:
        """列出指定用户凭证。"""

    def get_by_credential_id(self, credential_id: str) -> Optional[Any]:
        """按凭证 ID 查找凭证。"""

    def create(self, payload: dict[str, Any]) -> Any:
        """创建凭证。"""

    def update_last_used(self, passkey: Any, sign_count: int) -> bool:
        """更新凭证使用计数。"""

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除用户凭证。"""


class PasskeyService:
    """编排 PassKey 凭证生命周期。"""

    def __init__(self, repository: PasskeyRepository) -> None:
        """注入 PassKey 数据端口。"""
        self._repository = repository

    def list(self) -> list[Any]:
        """列出全部启用凭证。"""
        return self._repository.list()

    def list_by_user_id(self, user_id: int) -> list[Any]:
        """列出指定用户凭证。"""
        return self._repository.list_by_user_id(user_id)

    def get_by_credential_id(self, credential_id: str) -> Optional[Any]:
        """按凭证 ID 查找凭证。"""
        return self._repository.get_by_credential_id(credential_id)

    def create(self, payload: dict[str, Any]) -> Any:
        """创建凭证。"""
        return self._repository.create(payload)

    def update_last_used(self, passkey: Any, sign_count: int) -> bool:
        """更新凭证使用计数。"""
        return self._repository.update_last_used(passkey, sign_count)

    def delete_by_id(self, passkey_id: int, user_id: int) -> bool:
        """删除用户凭证。"""
        return self._repository.delete_by_id(passkey_id, user_id)


_configured_passkey_service: PasskeyService | None = None


def configure_passkey_service(service: PasskeyService) -> None:
    """由启动组合根登记 PassKey 应用服务。"""
    global _configured_passkey_service
    _configured_passkey_service = service


def get_configured_passkey_service() -> PasskeyService:
    """返回启动阶段登记的 PassKey 应用服务。"""
    if _configured_passkey_service is None:
        raise RuntimeError("PassKey 服务尚未配置")
    return _configured_passkey_service
