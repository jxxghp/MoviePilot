from pathlib import Path
from typing import Optional, Tuple, List

from p115 import P115Client, P115Path

from app import schemas
from app.core.config import settings
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
        "move": "移动",
        "copy": "复制"
    }

    # 115二维码登录地址
    qrcode_url = "https://qrcodeapi.115.com/api/1.0/web/1.0/token/"
    # 115登录状态检查
    login_check_url = "https://qrcodeapi.115.com/get/status/"
    # 115登录完成 alipaymini
    login_done_api = f"https://passportapi.115.com/app/1.0/alipaymini/1.0/login/qrcode/"

    client: P115Client = None
    session_info: dict = None

    def __init__(self):
        super().__init__()
        self.init_storage()

    def init_storage(self) -> bool:
        """
        初始化Cloud
        """
        if not self.__credential:
            return False
        try:
            self.client = P115Client(self.__credential, app="alipaymini",
                                     check_for_relogin=True, console_qrcode=False)
        except Exception as err:
            logger.error(f"115连接失败，请重新登录：{str(err)}")
            self.__clear_credential()
            return False
        return True

    @property
    def __credential(self) -> Optional[str]:
        """
        获取已保存的115 Cookie
        """
        conf = self.get_config()
        if not conf:
            return None
        if not conf.config:
            return None
        return conf.config.get("cookie")

    def __save_credential(self, credential: dict):
        """
        设置115认证参数
        """
        self.set_config(credential)

    def __clear_credential(self):
        """
        清除115认证参数
        """
        self.set_config({})

    def generate_qrcode(self) -> Optional[Tuple[dict, str]]:
        """
        生成二维码
        """
        res = RequestUtils(timeout=10).get_res(self.qrcode_url)
        if res:
            self.session_info = res.json().get("data")
            qrcode_content = self.session_info.pop("qrcode")
            if not qrcode_content:
                logger.warn("115生成二维码失败：未获取到二维码数据！")
                return {}, ""
            return {
                "codeContent": qrcode_content
            }, ""
        elif res is not None:
            return {}, f"115生成二维码失败：{res.status_code} - {res.reason}"
        return {}, f"115生成二维码失败：无法连接！"

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        二维码登录确认
        """
        if not self.session_info:
            return {}, "请先生成二维码！"
        try:
            resp = RequestUtils(timeout=10).get_res(self.login_check_url, params=self.session_info)
            if not resp:
                return {}, "115登录确认失败：无法连接！"
            result = resp.json()
            match result["data"].get("status"):
                case 0:
                    result = {
                        "status": 0,
                        "tip": "请使用微信或115客户端扫码"
                    }
                case 1:
                    result = {
                        "status": 1,
                        "tip": "已扫码"
                    }
                case 2:
                    # 确认完成，保存认证信息
                    resp = RequestUtils(timeout=10).post_res(self.login_done_api,
                                                             data={"account": self.session_info.get("uid")})
                    if not resp:
                        return {}, "115登录确认失败：无法连接！"
                    if resp:
                        # 保存认证信息
                        result = resp.json()
                        cookie_dict = result["data"]["cookie"]
                        cookie_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])
                        cookie_dict.update({"cookie": cookie_str})
                        self.__save_credential(cookie_dict)
                        self.init_storage()
                    result = {
                        "status": 2,
                        "tip": "登录成功！"
                    }
                case -1:
                    result = {
                        "status": -1,
                        "tip": "二维码已过期，请重新刷新！"
                    }
                case -2:
                    result = {
                        "status": -2,
                        "tip": "登录失败，请重试！"
                    }
                case _:
                    result = {
                        "status": -3,
                        "tip": "未知错误，请重试！"
                    }
            return result, ""
        except Exception as e:
            return {}, f"115登录确认失败：{str(e)}"

    def storage(self) -> Optional[Tuple[int, int]]:
        """
        获取存储空间
        """
        if not self.client:
            return None
        try:
            usage = self.client.fs.space_summury()
            if usage:
                return usage['rt_space_info']['all_total']['size'], usage['rt_space_info']['all_remain']['size']
        except Exception as e:
            logger.error(f"115获取存储空间失败：{str(e)}")
        return None

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        return True if self.list(schemas.FileItem()) else False

    def list(self, fileitem: schemas.FileItem) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        """
        if not self.client:
            return []
        try:
            if fileitem.type == "file":
                return [fileitem]
            items: List[P115Path] = self.client.fs.list(fileitem.path)
            return [schemas.FileItem(
                storage=self.schema.value,
                type="dir" if item.is_dir() else "file",
                path=item.path + ("/" if item.is_dir() else ""),
                name=item.name,
                basename=item.stem,
                size=item.stat().st_size,
                extension=item.suffix[1:] if not item.is_dir() else None,
                modify_time=item.stat().st_mtime
            ) for item in items if item]
        except Exception as e:
            logger.error(f"115浏览文件失败：{str(e)}")
        return []

    def create_folder(self, fileitem: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        if not self.client:
            return None
        try:
            result = self.client.fs.makedirs(Path(fileitem.path) / name, exist_ok=True)
            if result:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir",
                    path=f"{result.path}/",
                    name=name,
                    basename=Path(result.name).stem,
                    modify_time=result.mtime
                )
        except Exception as e:
            logger.error(f"115创建目录失败：{str(e)}")
        return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        根据文件路程获取目录，不存在则创建
        """
        if not self.client:
            return None
        try:
            result = self.client.fs.makedirs(path, exist_ok=True)
            if result:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir",
                    path=result.path + "/",
                    name=result.name,
                    basename=Path(result.name).stem,
                    modify_time=result.mtime
                )
        except Exception as e:
            logger.error(f"115获取目录失败：{str(e)}")
        return None

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        """
        if not self.client:
            return None
        try:
            try:
                item = self.client.fs.attr(path)
            except FileNotFoundError:
                return None
            if item:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir" if item.is_directory else "file",
                    path=item.path + ("/" if item.is_directory else ""),
                    name=item.name,
                    size=item.size,
                    extension=Path(item.name).suffix[1:] if not item.is_directory else None,
                    modify_time=item.mtime,
                    thumbnail=item.get("thumb")
                )
        except Exception as e:
            logger.info(f"115获取文件失败：{str(e)}")
        return None

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        if not self.client:
            return None
        try:
            try:
                item = self.client.fs.attr(fileitem.path)
            except FileNotFoundError:
                return None
            if item:
                return schemas.FileItem(
                    storage=self.schema.value,
                    type="dir" if item.is_directory else "file",
                    path=item.path + ("/" if item.is_directory else ""),
                    name=item.name,
                    size=item.size,
                    extension=Path(item.name).suffix[1:] if not item.is_directory else None,
                    modify_time=item.mtime,
                    thumbnail=item.get("thumb")
                )
        except Exception as e:
            logger.error(f"115获取文件详情失败：{str(e)}")
        return None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        if not self.client:
            return False
        try:
            self.client.fs.remove(fileitem.path)
            return True
        except Exception as e:
            logger.error(f"115删除文件失败：{str(e)}")
        return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        if not self.client:
            return False
        try:
            self.client.fs.rename(fileitem.path, Path(fileitem.path).with_name(name))
            return True
        except Exception as e:
            logger.error(f"115重命名文件失败：{str(e)}")
        return False

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        获取下载链接
        """
        if not self.client:
            return None
        local_file = (path or settings.TEMP_PATH) / fileitem.name
        try:
            task = self.client.fs.download(fileitem.path, file=local_file)
            if task:
                return local_file
        except Exception as e:
            logger.error(f"115下载文件失败：{str(e)}")
        return None

    def upload(self, fileitem: schemas.FileItem, path: Path, new_name: str = None) -> Optional[schemas.FileItem]:
        """
        上传文件
        """
        if not self.client:
            return None
        try:
            new_path = Path(fileitem.path) / (new_name or path.name)
            with open(path, "rb") as f:
                result = self.client.fs.upload(f, new_path)
                if result:
                    return schemas.FileItem(
                        storage=self.schema.value,
                        type="file",
                        path=str(path),
                        name=result.name,
                        basename=Path(result.name).stem,
                        size=result.size,
                        extension=Path(result.name).suffix[1:],
                        modify_time=result.mtime
                    )
        except Exception as e:
            logger.error(f"115上传文件失败：{str(e)}")
        return None

    def move(self, fileitem: schemas.FileItem, target: schemas.FileItem) -> bool:
        """
        移动文件
        """
        if not self.client:
            return False
        try:
            self.client.fs.move(fileitem.path, target.path)
            return True
        except Exception as e:
            logger.error(f"115移动文件失败：{str(e)}")
        return False

    def copy(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        """
        复制文件
        """
        if not self.client:
            return False
        try:
            self.client.fs.copy(fileitem.path, target_file)
            return True
        except Exception as e:
            logger.error(f"115复制文件失败：{str(e)}")
        return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        存储使用情况
        """
        info = self.storage()
        if info:
            total, free = info
            return schemas.StorageUsage(
                total=total,
                available=free
            )
        return schemas.StorageUsage()
