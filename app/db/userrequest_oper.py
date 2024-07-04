from typing import Optional

from app.db import DbOper
from app.db.models.userrequest import UserRequest


class UserRequestOper(DbOper):
    """
    用户请求管理
    """

    def get_need_approve(self) -> Optional[UserRequest]:
        """
        获取待审批申请
        """
        return UserRequest.get_by_status(self._db, 0)

    def get_my_requests(self, username: str) -> Optional[UserRequest]:
        """
        获取我的申请
        """
        return UserRequest.get_by_req_user(self._db, username)

    def approve(self, rid: int) -> bool:
        """
        审批申请
        """
        user_request = UserRequest.get(self._db, rid)
        if user_request:
            user_request.update(self._db, {"status": 1})
            return True
        return False

    def deny(self, rid: int) -> bool:
        """
        拒绝申请
        """
        user_request = UserRequest.get(self._db, rid)
        if user_request:
            user_request.update(self._db, {"status": 2})
            return True
        return False
