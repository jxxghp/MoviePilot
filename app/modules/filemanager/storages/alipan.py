import base64
import json
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional, Tuple, List

from aligo.response import CreateFileResponse

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.http import RequestUtils
from aligo import Aligo, BaseFile

from app.utils.singleton import Singleton
from app.utils.string import StringUtils


class AliPan(StorageBase, metaclass=Singleton):
    """
    阿里云相关操作
    """

    # 存储类型
    schema = StorageSchema.Alipan

    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
    }

    # 是否有aria2c
    _has_aria2c: bool = False

    # aligo
    aligo: Aligo = None

    # 生成二维码
    qrcode_url = ("https://passport.aliyundrive.com/newlogin/qrcode/generate.do?"
                  "appName=aliyun_drive&fromSite=52&appEntrance=web&isMobile=false"
                  "&lang=zh_CN&returnUrl=&bizParams=&_bx-v=2.0.31")
    # 二维码登录确认
    check_url = "https://passport.aliyundrive.com/newlogin/qrcode/query.do?appName=aliyun_drive&fromSite=52&_bx-v=2.0.31"

    def __init__(self):
        super().__init__()
        try:
            subprocess.run(['aria2c', '-h'], capture_output=True)
            self._has_aria2c = True
            logger.debug('【alipan】发现 aria2c, 将使用 aria2c 下载文件')
        except FileNotFoundError:
            logger.debug('【alipan】未发现 aria2c')
            self._has_aria2c = False
        self.init_storage()

    def init_storage(self):
        """
        初始化 aligo
        """

        def show_qrcode(qr_link: str):
            """
            显示二维码
            """
            logger.info(f"【alipan】请用阿里云盘 App 扫码登录：{qr_link}")

        refresh_token = self.__auth_params.get("refreshToken")
        if refresh_token:
            try:
                self.aligo = Aligo(refresh_token=refresh_token, show=show_qrcode, use_aria2=self._has_aria2c, # noqa
                                   name="MoviePilot V2", level=logging.ERROR, re_login=False)
            except Exception as err:
                logger.error(f"【alipan】初始化阿里云盘失败：{str(err)}")
                self.__clear_params()

    @property
    def __auth_params(self):
        """
        获取阿里云盘认证参数并初始化参数格式
        """
        conf = self.get_config()
        return conf.config if conf else {}

    def __update_params(self, params: dict):
        """
        设置阿里云盘认证参数
        """
        current_params = self.__auth_params
        current_params.update(params)
        self.set_config(current_params)

    def __clear_params(self):
        """
        清除阿里云盘认证参数
        """
        self.set_config({})

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        res = RequestUtils(timeout=10).get_res(self.qrcode_url)
        if res:
            data = res.json().get("content", {}).get("data")
            return {
                "codeContent": data.get("codeContent"),
                "ck": data.get("ck"),
                "t": data.get("t")
            }, ""
        elif res is not None:
            return {}, f"请求阿里云盘二维码失败：{res.status_code} - {res.reason}"
        return {}, f"请求阿里云盘二维码失败：无法连接！"

    def check_login(self, ck: str, t: str) -> Optional[Tuple[dict, str]]:
        """
        二维码登录确认
        """
        params = {
            "t": t,
            "ck": ck,
            "appName": "aliyun_drive",
            "appEntrance": "web",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "fromSite": "52",
            "bizParams": "",
            "navlanguage": "zh-CN",
            "navPlatform": "MacIntel",
        }

        body = "&".join([f"{key}={value}" for key, value in params.items()])

        status = {
            "NEW": "请用阿里云盘 App 扫码",
            "SCANED": "请在手机上确认",
            "EXPIRED": "二维码已过期",
            "CANCELED": "已取消",
            "CONFIRMED": "已确认",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }

        res = RequestUtils(headers=headers, timeout=5).post_res(self.check_url, data=body)
        if res:
            data = res.json().get("content", {}).get("data") or {}
            qrCodeStatus = data.get("qrCodeStatus")
            data["tip"] = status.get(qrCodeStatus) or "未知"
            if data.get("bizExt"):
                try:
                    bizExt = json.loads(base64.b64decode(data["bizExt"]).decode('GBK'))
                    pds_login_result = bizExt.get("pds_login_result")
                    if pds_login_result:
                        data.pop('bizExt')
                        data.update({
                            'userId': pds_login_result.get('userId'),
                            'expiresIn': pds_login_result.get('expiresIn'),
                            'nickName': pds_login_result.get('nickName'),
                            'avatar': pds_login_result.get('avatar'),
                            'tokenType': pds_login_result.get('tokenType'),
                            "refreshToken": pds_login_result.get('refreshToken'),
                            "accessToken": pds_login_result.get('accessToken'),
                            "defaultDriveId": pds_login_result.get('defaultDriveId'),
                            "updateTime": time.time(),
                        })
                        self.__update_params(data)
                        self.__update_drives()
                        self.init_storage()
                except Exception as e:
                    return {}, f"bizExt 解码失败：{str(e)}"
            return data, ""
        elif res is not None:
            return {}, f"阿里云盘登录确认失败：{res.status_code} - {res.reason}"
        return {}, "阿里云盘登录确认失败：无法连接！"

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        if not self.aligo:
            return False
        return True if self.aligo.get_user() else False

    def user_info(self) -> dict:
        """
        获取用户信息（drive_id等）
        """
        if not self.aligo:
            return {}
        return self.aligo.get_user()

    def __update_drives(self):
        """
        更新用户存储根目录
        """
        if not self.aligo:
            return
        drivers = self.aligo.list_my_drives()
        for driver in drivers:
            if driver.category == "resource":
                self.__update_params({"resourceDriveId": driver.drive_id})
            elif driver.category == "backup":
                self.__update_params({"backDriveId": driver.drive_id})

    def __get_fileitem(self, fileinfo: BaseFile, parent: Optional[str] = "/") -> schemas.FileItem:
        """
        获取文件信息
        """
        if not fileinfo:
            return schemas.FileItem()
        if fileinfo.type == "folder":
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=fileinfo.file_id,
                parent_fileid=fileinfo.parent_file_id,
                type="dir",
                path=f"{parent}{fileinfo.name}" + "/",
                name=fileinfo.name,
                basename=fileinfo.name,
                size=fileinfo.size,
                modify_time=StringUtils.str_to_timestamp(fileinfo.updated_at),
                drive_id=fileinfo.drive_id,
            )
        else:
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=fileinfo.file_id,
                parent_fileid=fileinfo.parent_file_id,
                type="file",
                path=f"{parent}{fileinfo.name}",
                name=fileinfo.name,
                basename=Path(fileinfo.name).stem,
                size=fileinfo.size,
                extension=fileinfo.file_extension,
                modify_time=StringUtils.str_to_timestamp(fileinfo.updated_at),
                thumbnail=fileinfo.thumbnail,
                drive_id=fileinfo.drive_id,
            )

    def list(self, fileitem: schemas.FileItem = None) -> List[schemas.FileItem]:
        """
        浏览文件
        limit 返回文件数量，默认 50，最大 100
        order_by created_at/updated_at/name/size
        parent_file_id 根目录为root
        type 	all | file | folder
        """
        if not self.aligo:
            return []
        # 根目录处理
        if not fileitem or not fileitem.drive_id:
            items = self.aligo.get_file_list()
            if items:
                return [self.__get_fileitem(item) for item in items]
        elif fileitem.type == "file":
            # 文件处理
            file = self.detail(fileitem)
            if file:
                return [file]
        else:
            items = self.aligo.get_file_list(parent_file_id=fileitem.fileid, drive_id=fileitem.drive_id)
            if items:
                return [self.__get_fileitem(item, parent=fileitem.path) for item in items]
        return []

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        if not self.aligo:
            return None
        item = self.aligo.create_folder(name=name, parent_file_id=fileitem.fileid, drive_id=fileitem.drive_id)
        if item:
            if isinstance(item, CreateFileResponse):
                item = self.aligo.get_file(file_id=item.file_id, drive_id=item.drive_id)
            return self.__get_fileitem(item, parent=fileitem.path)
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """

        def __find_dir(_fileitem: schemas.FileItem, _name: str) -> Optional[schemas.FileItem]:
            """
            查找下级目录中匹配名称的目录
            """
            for sub_folder in self.list(_fileitem):
                if sub_folder.type != "dir":
                    continue
                if sub_folder.name == _name:
                    return sub_folder
            return None

        # 是否已存在
        folder = self.get_item(path)
        if folder:
            return folder
        # 逐级查找和创建目录
        fileitem = schemas.FileItem(storage=self.schema.value, path="/")
        for part in path.parts:
            if part == "/":
                continue
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    return None
                fileitem = dir_file
        return fileitem

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        if not self.aligo:
            return None
        item = self.aligo.get_file_by_path(path=str(path))
        if item:
            return self.__get_fileitem(item, parent=str(path.parent))
        return None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        if not self.aligo:
            return False
        if self.aligo.move_file_to_trash(file_id=fileitem.fileid, drive_id=fileitem.drive_id):
            return True
        return False

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        if not self.aligo:
            return None
        item = self.aligo.get_file(file_id=fileitem.fileid, drive_id=fileitem.drive_id)
        if item:
            return self.__get_fileitem(item, parent=fileitem.path)
        return None

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        if not self.aligo:
            return False
        if self.aligo.rename_file(file_id=fileitem.fileid, name=name, drive_id=fileitem.drive_id):
            return True
        return False

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        下载文件，保存到本地
        """
        if not self.aligo:
            return None
        local_path = self.aligo.download_file(file_id=fileitem.fileid, drive_id=fileitem.drive_id,  # noqa
                                              local_folder=str(path or settings.TEMP_PATH))
        if local_path:
            return Path(local_path)
        return None

    def upload(self, fileitem: schemas.FileItem, path: Path,
               new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        上传文件，并标记完成
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        """
        if not self.aligo:
            return None
        # 上传文件
        result = self.aligo.upload_file(file_path=str(path), parent_file_id=fileitem.fileid,
                                        drive_id=fileitem.drive_id, name=new_name or path.name,
                                        check_name_mode="refuse")
        if result:
            item = self.aligo.get_file(file_id=result.file_id, drive_id=result.drive_id)
            if item:
                return self.__get_fileitem(item, parent=fileitem.path)
        return None

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        if not self.aligo:
            return False
        target = self.get_folder(path)
        if not target:
            return False
        if self.aligo.move_file(file_id=fileitem.fileid, drive_id=fileitem.drive_id,
                                to_parent_file_id=target.fileid, to_drive_id=target.drive_id,
                                new_name=new_name):
            return True
        return False

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        if not self.aligo:
            return False
        target = self.get_folder(path)
        if not target:
            return False
        if self.aligo.copy_file(file_id=fileitem.fileid, drive_id=fileitem.drive_id,
                                to_parent_file_id=target.fileid, to_drive_id=target.drive_id,
                                new_name=new_name):
            return True
        return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        硬链接文件
        """
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        软链接文件
        """
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        if not self.aligo:
            return None
        user_capacity = self.aligo.get_user_capacity_info()
        if user_capacity:
            drive_capacity = user_capacity.drive_capacity_details
            if drive_capacity:
                return schemas.StorageUsage(
                    total=drive_capacity.drive_total_size,
                    available=drive_capacity.drive_total_size - drive_capacity.drive_used_size
                )
        return None
