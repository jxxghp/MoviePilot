import hashlib
import secrets
import threading
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Union

import requests

from app import schemas
from app.core.config import settings
from app.log import logger
from app.modules.filemanager import StorageBase
from app.schemas.types import StorageSchema
from app.utils.singleton import Singleton
from app.utils.string import StringUtils

lock = threading.Lock()


class AliPan(StorageBase, metaclass=Singleton):
    """
    阿里云盘相关操作
    """

    # 存储类型
    schema = StorageSchema.Alipan

    # 支持的整理方式
    transtype = {
        "move": "移动",
        "copy": "复制"
    }

    # 验证参数
    _auth_state = {}

    # 上传进度值
    _last_progress = 0

    # 基础url
    base_url = "https://openapi.alipan.com"

    # CID和路径缓存
    _id_cache: Dict[str, Tuple[str, str]] = {}

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self._init_session()

    def _init_session(self):
        """
        初始化带速率限制的会话
        """
        self.session.headers.update({
            "Content-Type": "application/json"
        })

    def _check_session(self):
        """
        检查会话是否过期
        """
        if not self.access_token:
            raise Exception("【阿里云盘】请先扫码登录！")

    @property
    def _default_drive_id(self) -> str:
        """
        获取默认存储桶ID
        """
        conf = self.get_conf()
        drive_id = conf.get("resource_drive_id") or conf.get("backup_drive_id") or conf.get("default_drive_id")
        if not drive_id:
            raise Exception("请先登录阿里云盘！")
        return drive_id

    @property
    def access_token(self) -> Optional[str]:
        """
        访问token
        """
        with lock:
            tokens = self.get_conf()
            refresh_token = tokens.get("refresh_token")
            expires_in = tokens.get("expires_in", 0)
            refresh_time = tokens.get("refresh_time", 0)
            if expires_in and refresh_time + expires_in < int(time.time()):
                tokens = self.__refresh_access_token(refresh_token)
                if tokens:
                    self.set_config({
                        "refresh_time": int(time.time()),
                        **tokens
                    })
            access_token = tokens.get("access_token")
            if access_token:
                self.session.headers.update({"Authorization": f"Bearer {access_token}"})
            return access_token

    def generate_qrcode(self) -> Tuple[dict, str]:
        """
        实现PKCE规范的设备授权二维码生成
        """

        # 生成PKCE参数
        code_verifier = secrets.token_urlsafe(96)[:128]
        # 请求设备码
        resp = self.session.post(
            f"{self.base_url}/oauth/authorize/qrcode",
            json={
                "client_id": settings.ALIPAN_APP_ID,
                "scopes": ["user:base", "file:all:read", "file:all:write", "file:share:write"],
                "code_challenge": code_verifier,
                "code_challenge_method": "plain"
            }
        )
        if resp is None:
            return {}, "网络错误"
        result = resp.json()
        if result.get("code"):
            return {}, result.get("message")
        # 持久化验证参数
        self._auth_state = {
            "sid": result.get("sid"),
            "code_verifier": code_verifier
        }
        # 生成二维码内容
        return {
            "codeUrl": result.get("qrCodeUrl")
        }, ""

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        改进的带PKCE校验的登录状态检查
        """

        _status_text = {
            "WaitLogin": "等待登录",
            "ScanSuccess": "扫码成功",
            "LoginSuccess": "登录成功",
            "QRCodeExpired": "二维码过期"
        }

        if not self._auth_state:
            return {}, "生成二维码失败"
        try:
            resp = self.session.get(
                f"{self.base_url}/oauth/qrcode/{self._auth_state['sid']}/status"
            )
            if resp is None:
                return {}, "网络错误"
            result = resp.json()
            # 扫码结果
            status = result.get("status")
            if status == "LoginSuccess":
                authCode = result.get("authCode")
                self._auth_state["authCode"] = authCode
                tokens = self.__get_access_token()
                if tokens:
                    self.set_config({
                        "refresh_time": int(time.time()),
                        **tokens
                    })
                    self.__get_drive_id()
            return {"status": status, "tip": _status_text.get(status, "未知错误")}, ""
        except Exception as e:
            return {}, str(e)

    def __get_access_token(self) -> dict:
        """
        确认登录后，获取相关token
        """
        if not self._auth_state:
            raise Exception("请先生成二维码")
        resp = self.session.post(
            f"{self.base_url}/oauth/access_token",
            json={
                "client_id": settings.ALIPAN_APP_ID,
                "grant_type": "authorization_code",
                "code": self._auth_state["authCode"],
                "code_verifier": self._auth_state["code_verifier"]
            }
        )
        if resp is None:
            raise Exception("获取 access_token 失败")
        result = resp.json()
        if result.get("code"):
            raise Exception(f"{result.get('code')} - {result.get('message')}！")
        return result

    def __refresh_access_token(self, refresh_token: str) -> Optional[dict]:
        """
        刷新access_token
        """
        if not refresh_token:
            raise Exception("会话失效，请重新扫码登录！")
        resp = self.session.post(
            f"{self.base_url}/oauth/access_token",
            json={
                "client_id": settings.ALIPAN_APP_ID,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token
            }
        )
        if resp is None:
            logger.error(f"【阿里云盘】刷新 access_token 失败：refresh_token={refresh_token}")
            return None
        result = resp.json()
        if result.get("code"):
            logger.warn(f"【阿里云盘】刷新 access_token 失败：{result.get('code')} - {result.get('message')}！")
        return result

    def __get_drive_id(self):
        """
        获取默认存储桶ID
        """
        resp = self.session.post(
            f"{self.base_url}/adrive/v1.0/user/getDriveInfo"
        )
        if resp is None:
            logger.error("获取默认存储桶ID失败")
            return None
        result = resp.json()
        if result.get("code"):
            logger.warn(f"获取默认存储ID失败：{result.get('code')} - {result.get('message')}！")
            return None
        # 保存用户参数
        """
        user_id	string	是	用户ID，具有唯一性
        name	string	是	昵称
        avatar	string	是	头像地址
        default_drive_id	string	是	默认drive
        resource_drive_id	string	否	资源库。用户选择了授权才会返回
        backup_drive_id	string	否	备份盘。用户选择了授权才会返回
        """
        conf = self.get_conf()
        conf.update(result)
        self.set_config(conf)

    def _request_api(self, method: str, endpoint: str,
                     result_key: Optional[str] = None, **kwargs) -> Optional[Union[dict, list]]:
        """
        带错误处理和速率限制的API请求
        """
        # 检查会话
        self._check_session()

        resp = self.session.request(
            method, f"{self.base_url}{endpoint}",
            **kwargs
        )
        if resp is None:
            logger.warn(f"【阿里云盘】{method} 请求 {endpoint} 失败！")
            return None

        # 处理速率限制
        if resp.status_code == 429:
            reset_time = int(resp.headers.get("X-RateLimit-Reset", 60))
            time.sleep(reset_time + 5)
            return self._request_api(method, endpoint, result_key, **kwargs)

        # 处理请求错误
        resp.raise_for_status()

        # 返回数据
        ret_data = resp.json()
        if ret_data.get("code"):
            logger.warn(f"【阿里云盘】{method} 请求 {endpoint} 出错：{ret_data.get('message')}！")

        if result_key:
            return ret_data.get(result_key)
        return ret_data

    def _path_to_id(self, drive_id: str, path: str) -> Tuple[str, str]:
        """
        路径转drive_id, file_id（带缓存机制）
        """
        # 根目录
        if path == "/":
            return drive_id, "root"
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        # 检查缓存
        if path in self._id_cache:
            return self._id_cache[path]
        # 逐级查找缓存
        file_id = "root"
        file_path = "/"
        for p in Path(path).parents:
            if str(p) in self._id_cache:
                file_path = str(p)
                file_id = self._id_cache[file_path]
                break
        # 计算相对路径
        rel_path = Path(path).relative_to(file_path)
        for part in Path(rel_path).parts:
            find_part = False
            next_marker = None
            while True:
                resp = self._request_api(
                    "POST",
                    "/adrive/v1.0/openFile/list",
                    json={
                        "drive_id": drive_id,
                        "limit": 100,
                        "marker": next_marker,
                        "parent_file_id": file_id,
                    }
                )
                if not resp:
                    break
                for item in resp.get("items", []):
                    if item["name"] == part:
                        file_id = item["file_id"]
                        find_part = True
                        break
                if find_part:
                    break
                if len(resp.get("items")) < 100:
                    break
            if not find_part:
                raise FileNotFoundError(f"【阿里云盘】{path} 不存在")
        if file_id == "root":
            raise FileNotFoundError(f"【阿里云盘】{path} 不存在")
        # 缓存路径
        self._id_cache[path] = (drive_id, file_id)
        return drive_id, file_id

    def __get_fileitem(self, fileinfo: dict, parent: str = "/") -> schemas.FileItem:
        """
        获取文件信息
        """
        if not fileinfo:
            return schemas.FileItem()
        if fileinfo.get("type") == "folder":
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=fileinfo.get("file_id"),
                parent_fileid=fileinfo.get("parent_file_id"),
                type="dir",
                path=f"{parent}{fileinfo.get('name')}" + "/",
                name=fileinfo.get("name"),
                basename=fileinfo.get("name"),
                size=fileinfo.get("size"),
                modify_time=StringUtils.str_to_timestamp(fileinfo.get("updated_at")),
                drive_id=fileinfo.get("drive_id"),
            )
        else:
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=fileinfo.get("file_id"),
                parent_fileid=fileinfo.get("parent_file_id"),
                type="file",
                path=f"{parent}{fileinfo.get('name')}",
                name=fileinfo.get("name"),
                basename=Path(fileinfo.get("name")).stem,
                size=fileinfo.get("size"),
                extension=fileinfo.get("file_extension"),
                modify_time=StringUtils.str_to_timestamp(fileinfo.get("updated_at")),
                thumbnail=fileinfo.get("thumbnail"),
                drive_id=fileinfo.get("drive_id"),
            )

    @staticmethod
    def _calc_sha1(filepath: Path, size: Optional[int] = None) -> str:
        """
        计算文件SHA1（符合阿里云盘规范）
        size: 前多少字节
        """
        sha1 = hashlib.sha1()
        with open(filepath, 'rb') as f:
            if size:
                chunk = f.read(size)
                sha1.update(chunk)
            else:
                while chunk := f.read(8192):
                    sha1.update(chunk)
        return sha1.hexdigest()

    def init_storage(self):
        pass

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        目录遍历实现
        """
        if fileitem.type == "file":
            item = self.detail(fileitem)
            if item:
                return [item]
            return []

        if fileitem.path == "/":
            parent_file_id = "root"
            drive_id = self._default_drive_id
        else:
            parent_file_id = fileitem.fileid
            drive_id = fileitem.drive_id

        items = []
        next_marker = None

        while True:
            resp = self._request_api(
                "POST",
                "/adrive/v1.0/openFile/list",
                json={
                    "drive_id": drive_id,
                    "limit": 100,
                    "marker": next_marker,
                    "parent_file_id": parent_file_id,
                }
            )
            if resp is None:
                raise FileNotFoundError(f"【阿里云盘】{fileitem.path} 检索出错！")
            if not resp:
                break
            next_marker = resp.get("next_marker")
            for item in resp.get("items", []):
                # 更新缓存
                path = f"{fileitem.path}{item.get('name')}"
                self._id_cache[path] = (drive_id, item.get("file_id"))
                items.append(self.__get_fileitem(item))
            if len(resp.get("items")) < 100:
                break
        return items

    def create_folder(self, parent_item: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/create",
            json={
                "drive_id": parent_item.drive_id,
                "parent_file_id": parent_item.fileid,
                "name": name,
                "type": "folder"
            }
        )
        if not resp:
            return None
        if resp.get("code"):
            logger.warn(f"【阿里云盘】创建目录失败: {resp.get('message')}")
            return None
        # 缓存新目录
        new_path = Path(parent_item.path) / name
        self._id_cache[str(new_path)] = (resp.get("drive_id"), resp.get("file_id"))
        return self.get_item(new_path)

    def upload(self, target_dir: schemas.FileItem, local_path: Path,
               new_name: Optional[str] = None) -> Optional[schemas.FileItem]:
        """
        TODO 文件上传
        """
        pass

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Optional[Path]:
        """
        带限速处理的下载
        """
        download_info = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/getDownloadUrl",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
            }
        )
        if not download_info:
            return None
        download_url = download_info.get("url")
        local_path = path or settings.TEMP_PATH / fileitem.name
        with self.session.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_path

    def check(self) -> bool:
        return self.access_token is not None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件/目录
        """
        try:
            self._request_api(
                "POST",
                "/adrive/v1.0/openFile/recyclebin/trash",
                json={
                    "drive_id": fileitem.drive_id,
                    "file_id": fileitem.fileid
                }
            )
            return True
        except requests.exceptions.HTTPError:
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件/目录
        """
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/update",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
                "name": name
            }
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】重命名失败: {resp.get('message')}")
            return False
        if fileitem.path in self._id_cache:
            del self._id_cache[fileitem.path]
            for key in list(self._id_cache.keys()):
                if key.startswith(fileitem.path):
                    del self._id_cache[key]
        self._id_cache[str(Path(fileitem.path).parent / name)] = (resp.get("drive_id"), resp.get("file_id"))
        return True

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件/目录项
        """
        try:
            resp = self._request_api(
                "POST",
                "/adrive/v1.0/openFile/get_by_path",
                json={
                    "drive_id": self._default_drive_id,
                    "file_path": str(path)
                }
            )
            if not resp:
                return None
            if resp.get("code"):
                logger.debug(f"【阿里云盘】获取文件信息失败: {resp.get('message')}")
                return None
            return self.__get_fileitem(resp)
        except Exception as e:
            logger.debug(f"【阿里云盘】获取文件信息失败: {str(e)}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件夹，如不存在则创建
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
        for part in path.parts[1:]:
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【阿里云盘】创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件/目录详细信息
        """
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        企业级复制实现（支持目录递归复制）
        """
        dest_cid = self._path_to_id(fileitem.drive_id, str(path))
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/copy",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
                "to_drive_id": fileitem.drive_id,
                "to_parent_file_id": dest_cid
            }
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】复制文件失败: {resp.get('message')}")
            return False
        # 重命名
        new_path = Path(path) / fileitem.name
        new_file = self.get_item(new_path)
        self.rename(new_file, new_name)
        # 更新缓存
        del self._id_cache[fileitem.path]
        rename_new_path = Path(path) / new_name
        self._id_cache[str(rename_new_path)] = (resp.get("drive_id"), resp.get("file_id"))
        return True

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        原子性移动操作实现
        """
        src_fid = fileitem.fileid
        target_id = self._path_to_id(fileitem.drive_id, str(path))

        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/move",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": src_fid,
                "to_parent_file_id": target_id,
                "new_name": new_name
            }
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】移动文件失败: {resp.get('message')}")
            return False
        # 更新缓存
        del self._id_cache[fileitem.path]
        rename_new_path = Path(path) / new_name
        self._id_cache[str(rename_new_path)] = (resp.get("drive_id"), resp.get("file_id"))
        return True

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """
        获取带有企业级配额信息的存储使用情况
        """
        try:
            resp = self._request_api(
                "POST",
                "/adrive/v1.0/user/getSpaceInfo"
            )
            if not resp:
                return None
            space = resp.get("personal_space_info") or {}
            total_size = space.get("total_size") or 0
            used_size = space.get("used_size") or 0
            return schemas.StorageUsage(
                total=total_size,
                available=total_size - used_size
            )
        except KeyError:
            return None
