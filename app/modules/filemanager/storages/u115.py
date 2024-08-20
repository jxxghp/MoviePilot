import base64
from pathlib import Path
from typing import Optional, Tuple, List

import oss2
import py115
from py115 import Cloud
from py115.types import LoginTarget, QrcodeSession, QrcodeStatus, Credential

from app import schemas
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton


class U115Pan(StorageBase, metaclass=Singleton):
    """
    115相关操作
    """

    # 存储类型
    schema = StorageSchema.U115

    # 支持的整理方式
    transtype = {
        "move": "移动"
    }

    cloud: Optional[Cloud] = None
    _session: QrcodeSession = None

    def __init_cloud(self) -> bool:
        """
        初始化Cloud
        """
        credential = self.__credential
        if not credential:
            logger.warn("115未登录，请先登录！")
            return False
        try:
            if not self.cloud:
                self.cloud = py115.connect(credential)
        except Exception as err:
            logger.error(f"115连接失败，请重新扫码登录：{str(err)}")
            self.__clear_credential()
            return False
        return True

    @property
    def __credential(self) -> Optional[Credential]:
        """
        获取已保存的115认证参数
        """
        cookie_dict = self.get_config()
        if not cookie_dict:
            return None
        return Credential.from_dict(cookie_dict.dict().get("config"))

    def __save_credentail(self, credential: Credential):
        """
        设置115认证参数
        """
        self.set_config(credential.to_dict())

    def __clear_credential(self):
        """
        清除115认证参数
        """
        self.set_config({})

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        try:
            self.cloud = py115.connect()
            self._session = self.cloud.qrcode_login(LoginTarget.Linux)
            image_bin = self._session.image_data
            if not image_bin:
                logger.warn("115生成二维码失败：未获取到二维码数据！")
                return None
            # 转换为base64图片格式
            image_base64 = base64.b64encode(image_bin).decode()
            return {
                "codeContent": f"data:image/jpeg;base64,{image_base64}"
            }, ""
        except Exception as e:
            logger.warn(f"115生成二维码失败：{str(e)}")
            return {}, f"115生成二维码失败：{str(e)}"

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
                self.__save_credentail(self.cloud.export_credentail())
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

    def storage(self) -> Optional[Tuple[int, int]]:
        """
        获取存储空间
        """
        if not self.__init_cloud():
            return None
        try:
            return self.cloud.storage().space()
        except Exception as e:
            logger.error(f"115获取存储空间失败：{str(e)}")
        return None

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        return True if self.list(schemas.FileItem(
            fileid="0"
        )) else False

    def list(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        """
        if not self.__init_cloud():
            return None
        try:
            items = self.cloud.storage().list(dir_id=fileitem.fileid)
            return [schemas.FileItem(
                storage=self.schema.value,
                fileid=item.file_id,
                parent_fileid=item.parent_id,
                type="dir" if item.is_dir else "file",
                path=f"{fileitem.path}{item.name}" + ("/" if item.is_dir else ""),
                name=item.name,
                size=item.size,
                extension=Path(item.name).suffix[1:],
                modify_time=item.modified_time.timestamp() if item.modified_time else 0,
                pickcode=item.pickcode
            ) for item in items]
        except Exception as e:
            logger.error(f"115浏览文件失败：{str(e)}")
        return None

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        if not self.__init_cloud():
            return None
        try:
            result = self.cloud.storage().make_dir(fileitem.parent_fileid, name)
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=result.file_id,
                parent_fileid=result.parent_id,
                type="dir",
                path=f"{fileitem.path}{name}/",
                name=name,
                modify_time=result.modified_time.timestamp() if result.modified_time else 0,
                pickcode=result.pickcode
            )
        except Exception as e:
            logger.error(f"115创建目录失败：{str(e)}")
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_file in self.list(_fileitem):
                if sub_file.type != "dir":
                    continue
                if sub_file.name == _name:
                    return sub_file
            return None

        # 逐级查找和创建目录
        fileitem = schemas.FileItem(fileid="0")
        for part in path.parts:
            if part == "/":
                continue
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(dir_file, part)
                if not dir_file:
                    logger.warn(f"115创建目录 {fileitem.path}{part} 失败!")
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """

        def __find_item(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录或文件
            """
            for sub_file in self.list(_fileitem):
                if sub_file.name == _name:
                    return sub_file
            return None

        # 逐级查找和创建目录
        fileitem = schemas.FileItem(fileid="0")
        for part in path.parts:
            if part == "/":
                continue
            item = __find_item(fileitem, part)
            if not item:
                return None
            fileitem = item
        return fileitem

    def detail(self, fileitm: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        pass

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().delete(fileitem.fileid)
            return True
        except Exception as e:
            logger.error(f"115删除文件失败：{str(e)}")
        return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().rename(fileitem.fileid, name)
            return True
        except Exception as e:
            logger.error(f"115重命名文件失败：{str(e)}")
        return False

    def download(self, fileitem: schemas.FileItem, path: Path) -> bool:
        """
        获取下载链接
        """
        if not self.__init_cloud():
            return False
        try:
            ticket = self.cloud.storage().request_download(fileitem.pickcode)
            if ticket:
                res = RequestUtils(headers=ticket.headers).get_res(ticket.url)
                if res:
                    with open(path, "wb") as f:
                        f.write(res.content)
                    return True
        except Exception as e:
            logger.error(f"115下载失败：{str(e)}")
        return False

    def upload(self, fileitem: schemas.FileItem, path: Path) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        if not self.__init_cloud():
            return None
        try:
            ticket = self.cloud.storage().request_upload(dir_id=fileitem.fileid, file_path=str(path))
            if ticket is None:
                logger.warn(f"115请求上传出错")
                return None
            elif ticket.is_done:
                logger.warn(f"115请求上传失败：文件已存在")
                return None
            else:
                auth = oss2.Auth(**ticket.oss_token)
                bucket = oss2.Bucket(
                    auth=auth,
                    endpoint=ticket.oss_endpoint,
                    bucket_name=ticket.bucket_name,
                )
                por = bucket.put_object_from_file(
                    key=ticket.object_key,
                    filename=str(path),
                    headers=ticket.headers,
                )
                result = por.resp.response.json()
                if result:
                    fileitem = result.get('data')
                    logger.info(f"115上传文件成功：{fileitem}")
                    return schemas.FileItem(
                        storage=self.schema.value,
                        fileid=fileitem.get('file_id'),
                        parent_fileid=fileitem.fileid,
                        type="file",
                        name=fileitem.get('file_name'),
                        path=f"{fileitem.path}{fileitem.get('file_name')}",
                        size=fileitem.get('file_size'),
                        extension=Path(fileitem.get('file_name')).suffix[1:],
                        pickcode=fileitem.get('pickcode')
                    )
                else:
                    logger.warn(f"115上传文件失败：{por.resp.response.text}")
                    return None
        except Exception as e:
            logger.error(f"115上传文件失败：{str(e)}")
        return None

    def move(self, fileitem: schemas.FileItem, target: schemas.FileItem) -> bool:
        """
        移动文件
        """
        if not self.__init_cloud():
            return False
        try:
            self.cloud.storage().move(fileitem.fileid, target.fileid)
            return True
        except Exception as e:
            logger.error(f"115移动文件失败：{str(e)}")
        return False

    def copy(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def link(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitm: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        info = self.storage()
        if info:
            total, used = info
            return schemas.StorageUsage(
                total=total,
                available=total - used
            )
        return schemas.StorageUsage()
