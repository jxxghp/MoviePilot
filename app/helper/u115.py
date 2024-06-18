from typing import Optional, Tuple, List

import py115
from py115 import Cloud
from py115.types import LoginTarget, QrcodeSession, QrcodeStatus

from app.db.systemconfig_oper import SystemConfigOper
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton
from app.utils.system import SystemUtils


class U115Helper(metaclass=Singleton):
    """
    115相关操作
    """

    cloud: Cloud = None
    session: QrcodeSession = None

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    @property
    def cookies(self):
        """
        获取115认证参数并初始化参数格式
        """
        return self.systemconfig.get(SystemConfigKey.User115Params) or {}

    def save_credentail(self, cookies: dict):
        """
        设置115认证参数
        """
        self.systemconfig.set(SystemConfigKey.User115Params, cookies)

    def clear_params(self):
        """
        清除115认证参数
        """
        self.systemconfig.delete(SystemConfigKey.User115Params)

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """

        def __get_os():
            """
            获取操作系统名称
            """
            if SystemUtils.is_windows():
                return LoginTarget.Windows
            elif SystemUtils.is_macos():
                return LoginTarget.Mac
            else:
                return LoginTarget.Linux

        try:
            self.cloud = py115.connect()
            self.session = self.cloud.qrcode_login(__get_os)
            return self.session.image_data, ""
        except Exception as e:
            return None, f"115生成二维码失败：{str(e)}"

    def check_login(self, ck: str, t: str) -> Optional[Tuple[dict, str]]:
        """
        二维码登录确认
        """
        if not self.session:
            return None, "请先生成二维码！"
        try:
            status = self.cloud.qrcode_poll(self.session)
            if status == QrcodeStatus.Done:
                # 确认完成，保存认证信息
                self.save_credentail(self.cloud.export_credentail())
            elif status == QrcodeStatus.Waiting:
                return {
                    "status": 0,
                    "tip": "等待扫码确认..."
                }, ""
            elif status == QrcodeStatus.Expired:
                return {
                    "status": -1,
                    "tip": "二维码已过期，请重新刷新！"
                }, ""
            elif status == QrcodeStatus.Failed:
                return {
                    "status": -2,
                    "tip": "登录失败，请重试！"
                }, ""
            return None, "登录确认失败！"
        except Exception as e:
            return None, f"115登录确认失败：{str(e)}"

    def list_files(self, parent_file_id: str = '0') -> List[dict]:
        """
        浏览文件
        """
        cookies = self.cookies
        if not cookies:
            return []
        return self.cloud.storage().list(dir_id=parent_file_id)

    def create_folder(self, parent_file_id: str, name: str) -> bool:
        """
        创建目录
        """
        cookies = self.cookies
        if not cookies:
            return False
        return self.cloud.storage().make_dir(parent_file_id, name)

    def delete_file(self, file_id: str) -> bool:
        """
        删除文件
        """
        cookies = self.cookies
        if not cookies:
            return False
        return self.cloud.storage().delete(file_id)

    def get_file_detail(self, file_id: str) -> Optional[dict]:
        """
        获取文件详情
        """
        pass

    def rename_file(self, file_id: str, name: str) -> bool:
        """
        重命名文件
        """
        cookies = self.cookies
        if not cookies:
            return False
        return self.cloud.storage().rename(file_id, name)

    def get_download_url(self, file_id: str) -> Optional[str]:
        """
        获取下载链接
        """
        pass
