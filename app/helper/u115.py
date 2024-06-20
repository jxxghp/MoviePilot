import base64
from typing import Optional, Tuple, Generator

import py115
from py115 import Cloud
from py115.types import LoginTarget, QrcodeSession, QrcodeStatus, Credential, File, DownloadTicket

from app.db.systemconfig_oper import SystemConfigOper
from app.log import logger
from app.schemas.types import SystemConfigKey
from app.utils.singleton import Singleton


class U115Helper(metaclass=Singleton):
    """
    115相关操作
    """

    cloud: Optional[Cloud] = None
    _session: QrcodeSession = None

    def __init__(self):
        self.systemconfig = SystemConfigOper()

    def __init_cloud(self) -> bool:
        """
        初始化Cloud
        """
        credential = self.credential
        if not credential:
            logger.warn("115未登录，请先登录！")
            return False
        try:
            if not self.cloud:
                self.cloud = py115.connect(credential)
        except Exception as err:
            logger.error(f"115连接失败，请重新扫码登录：{str(err)}")
            self.clear_credential()
            return False
        return True

    @property
    def credential(self) -> Optional[Credential]:
        """
        获取已保存的115认证参数
        """
        cookie_dict = self.systemconfig.get(SystemConfigKey.User115Params)
        if not cookie_dict:
            return None
        return Credential.from_dict(cookie_dict)

    def save_credentail(self, credential: Credential):
        """
        设置115认证参数
        """
        self.systemconfig.set(SystemConfigKey.User115Params, credential.to_dict())

    def clear_credential(self):
        """
        清除115认证参数
        """
        self.systemconfig.delete(SystemConfigKey.User115Params)

    def generate_qrcode(self) -> Optional[str]:
        """
        生成二维码
        """
        try:
            self.cloud = py115.connect()
            self._session = self.cloud.qrcode_login(LoginTarget.Web)
            image_bin = self._session.image_data
            if not image_bin:
                logger.warn("115生成二维码失败：未获取到二维码数据！")
                return None
            # 转换为base64图片格式
            image_base64 = base64.b64encode(image_bin).decode()
            return f"data:image/png;base64,{image_base64}"
        except Exception as e:
            logger.warn(f"115生成二维码失败：{str(e)}")
        return None

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        二维码登录确认
        """
        if not self._session:
            return {}, "请先生成二维码！"
        try:
            if not self.cloud:
                return {}, "请先生成二维码！"
            status = self.cloud.qrcode_poll(self._session)
            if status == QrcodeStatus.Done:
                # 确认完成，保存认证信息
                self.save_credentail(self.cloud.export_credentail())
                result = {
                    "status": 1,
                    "tip": "登录成功！"
                }
            elif status == QrcodeStatus.Waiting:
                result = {
                    "status": 0,
                    "tip": "请使用微信或115客户端扫码"
                }
            elif status == QrcodeStatus.Expired:
                result = {
                    "status": -1,
                    "tip": "二维码已过期，请重新刷新！"
                }
                self.cloud = None
            elif status == QrcodeStatus.Failed:
                result = {
                    "status": -2,
                    "tip": "登录失败，请重试！"
                }
                self.cloud = None
            else:
                result = {
                    "status": -3,
                    "tip": "未知错误，请重试！"
                }
                self.cloud = None
            return result, ""
        except Exception as e:
            return {}, f"115登录确认失败：{str(e)}"

    def list_files(self, parent_file_id: str = '0') -> Optional[Generator[File, None, None]]:
        """
        浏览文件
        """
        if not self.__init_cloud():
            return None
        try:
            return self.cloud.storage().list(dir_id=parent_file_id)
        except Exception as e:
            logger.error(f"浏览115文件失败：{str(e)}")
        return None

    def create_folder(self, parent_file_id: str, name: str) -> Optional[File]:
        """
        创建目录
        """
        if not self.__init_cloud():
            return None
        try:
            return self.cloud.storage().make_dir(parent_file_id, name)
        except Exception as e:
            logger.error(f"创建115目录失败：{str(e)}")
        return None

    def delete_file(self, file_id: str) -> bool:
        """
        删除文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().delete(file_id)
            return True
        except Exception as e:
            logger.error(f"删除115文件失败：{str(e)}")
        return False

    def rename_file(self, file_id: str, name: str) -> bool:
        """
        重命名文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().rename(file_id, name)
            return True
        except Exception as e:
            logger.error(f"重命名115文件失败：{str(e)}")
        return False

    def download(self, pickcode: str) -> Optional[DownloadTicket]:
        """
        获取下载链接
        """
        if not self.__init_cloud():
            return None
        try:
            return self.cloud.storage().request_download(pickcode)
        except Exception as e:
            logger.error(f"115下载失败：{str(e)}")
        return None

    def get_storage(self) -> Optional[Tuple[int, int]]:
        """
        获取存储空间
        """
        if not self.__init_cloud():
            return None
        try:
            return self.cloud.storage().space()
        except Exception as e:
            logger.error(f"获取115存储空间失败：{str(e)}")
        return None

    def move(self, file_id: str, target_id: str) -> bool:
        """
        移动文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().move(file_id, target_id)
            return True
        except Exception as e:
            logger.error(f"移动115文件失败：{str(e)}")
        return False
