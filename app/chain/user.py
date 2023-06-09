from typing import Optional

from app.chain import ChainBase


class UserChain(ChainBase):

    def user_authenticate(self, name, password) -> Optional[str]:
        """
        辅助完成用户认证
        :param name: 用户名
        :param password: 密码
        :return: token
        """
        return self.run_module("user_authenticate", name=name, password=password)
