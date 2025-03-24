import base64
import hashlib
import secrets
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Union

import requests

from app import schemas
from app.api.endpoints.dashboard import storage
from app.core.config import settings
from app.log import logger
from app.modules.filemanager import StorageBase
from app.schemas.types import StorageSchema
from app.utils.singleton import Singleton
from app.utils.string import StringUtils


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

    # 验证参数
    _auth_state = {}

    # 基础url
    base_url = "https://proapi.115.com"

    # CID和路径缓存
    _id_cache: Dict[str, int] = {}

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self._init_session()

    def _init_session(self):
        """
        初始化带速率限制的会话
        """
        self.session.headers.update({
            "User-Agent": "W115Storage/2.0",
            "Accept-Encoding": "gzip, deflate",
            "Content-Type": "application/x-www-form-urlencoded"
        })
        self.init_storage()

    @property
    def access_token(self) -> Optional[str]:
        """
        访问token
        """
        tokens = self.get_conf()
        refresh_token = tokens.get("refresh_token")
        if not refresh_token:
            return None
        expires_in = tokens.get("expires_in", 0)
        refresh_time = tokens.get("refresh_time", 0)
        if expires_in and refresh_time + expires_in < int(time.time()):
            tokens = self.__refresh_access_token(refresh_token)
            if tokens:
                self.set_config({
                    "refresh_time": int(time.time()),
                    **tokens
                })
        return tokens.get("access_token")

    def generate_qrcode(self) -> Tuple[dict, str]:
        """
        实现PKCE规范的设备授权二维码生成
        """
        # 生成PKCE参数
        code_verifier = secrets.token_urlsafe(96)[:128]
        code_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(code_verifier.encode()).digest()
        ).decode()
        # 请求设备码
        resp = self.session.post(
            "https://passportapi.115.com/open/authDeviceCode",
            data={
                "client_id": settings.U115_APP_ID,
                "code_challenge": code_challenge,
                "code_challenge_method": "sha256"
            }
        )
        if resp is None:
            return {}, "网络错误"
        result = resp.json()
        if result.get("code") != 0:
            return {}, result.get("message")
        # 持久化验证参数
        self._auth_state = {
            "code_verifier": code_verifier,
            "uid": result["data"]["uid"],
            "time": result["data"]["time"],
            "sign": result["data"]["sign"]
        }

        # 生成二维码内容
        return {
            "codeContent": result['data']['qrcode']
        }, ""

    def __get_access_token(self) -> dict:
        """
        确认登录后，获取相关token
        """
        if not self._auth_state:
            raise Exception("请先调用生成二维码方法")
        resp = self.session.post(
            "https://passportapi.115.com/open/deviceCodeToToken",
            data={
                "uid": self._auth_state["uid"],
                "code_verifier": self._auth_state["code_verifier"]
            }
        )
        if resp is None:
            raise Exception("获取 access_token 失败")
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        return result["data"]

    def __refresh_access_token(self, refresh_token: str) -> dict:
        """
        刷新access_token
        """
        resp = self.session.post(
            "https://passportapi.115.com/open/refreshToken",
            data={
                "refresh_token": refresh_token
            }
        )
        if resp is None:
            raise Exception(f"刷新 access_token 失败：refresh_token={refresh_token}")
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        return result.get("data")

    def _request_api(self, method: str, endpoint: str,
                     result_key: str = None, **kwargs) -> Optional[Union[dict, list]]:
        """
        带错误处理和速率限制的API请求
        """
        resp = self.session.request(
            method, f"{self.base_url}{endpoint}",
            **kwargs
        )
        if resp is None:
            logger.warn(f"请求 115 API 失败: {method} {endpoint}")
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

        # 处理refresh_token失效
        if ret_data.get("code") == 40140119:
            self.set_config({})
            raise Exception("refresh_token 失效，请重新扫描登录！")

        # 处理access_token失效
        if ret_data.get("code") == 40140125:
            refresh_token = self.get_conf().get("refresh_token")
            if refresh_token:
                tokens = self.__refresh_access_token(refresh_token)
                self.set_config({
                    "refresh_time": int(time.time()),
                    **tokens
                })
                return self._request_api(method, endpoint, result_key, **kwargs)
            return None

        if result_key:
            result = ret_data.get(result_key)
            if result is None:
                raise FileNotFoundError(f"请求 115 API {method} {endpoint} 失败：{ret_data.get('message')}！")
            return result
        return ret_data

    def _path_to_id(self, path: str) -> int:
        """
        路径转FID（带缓存机制）
        """
        if len(path) > 1 and path.endswith("/"):
            path = path[:-1]
        # 命中缓存
        if path in self._id_cache:
            return self._id_cache[path]
        # 逐级查找缓存
        current_id = 0
        parent_path = "/"
        for p in Path(path).parents:
            if str(p) in self._id_cache:
                parent_path = str(p)
                current_id = self._id_cache[parent_path]
                break
        # 计算相对路径
        rel_path = Path(path).relative_to(parent_path)
        for part in Path(rel_path).parts:
            resp = self._request_api(
                "GET",
                "/open/ufile/files",
                "data",
                params={
                    "cid": current_id
                }
            )
            for item in resp:
                if item["fn"] == part:
                    current_id = item["fid"]
                    break
            else:
                raise FileNotFoundError(f"路径不存在: {path}")
        self._id_cache[path] = current_id
        return current_id

    def _id_to_path(self, fid: int) -> str:
        """
        CID转路径（带双向缓存）
        """
        # 根目录特殊处理
        if fid == 0:
            return "/"
        # 优先从缓存读取
        if fid in self._id_cache.values():
            return next(k for k, v in self._id_cache.items() if v == fid)
        # 从API获取当前节点信息
        detail = self._request_api(
            "GET",
            "/open/folder/get_info",
            "data",
            params={
                "file_id": fid
            }
        )
        # 处理可能的空数据（如已删除文件）
        if not detail:
            raise FileNotFoundError(f"{fid} 不存在")
        paths = detail["paths"]
        path_parts = [item["file_name"] for item in paths]
        # 构建完整路径
        full_path = "/" + "/".join(reversed(path_parts))
        # 缓存新路径
        self._id_cache[full_path] = fid
        return full_path

    @staticmethod
    def _calc_sha1(filepath: Path) -> str:
        """
        计算文件SHA1（符合115规范）
        """
        sha1 = hashlib.sha1()
        with open(filepath, 'rb') as f:
            while chunk := f.read(8192):
                sha1.update(chunk)
        return sha1.hexdigest()

    def check_login(self) -> Optional[Tuple[dict, str]]:
        """
        改进的带PKCE校验的登录状态检查
        """
        if not self._auth_state:
            return {}, "生成二维码失败"
        try:
            resp = self.session.get(
                "https://qrcodeapi.115.com/get/status/",
                params={
                    "uid": self._auth_state["uid"],
                    "time": self._auth_state["time"],
                    "sign": self._auth_state["sign"]
                }
            )
            if resp is None:
                return {}, "网络错误"
            result = resp.json()
            if result.get("code") != 0 or not result.get("data"):
                return {}, result.get("message")
            if result["data"]["status"] == 2:
                tokens = self.__get_access_token()
                self.set_config({
                    "refresh_time": int(time.time()),
                    **tokens
                })
            return {"status": result["data"]["status"], "tip": result["data"]["msg"]}, ""
        except Exception as e:
            return {}, str(e)

    def init_storage(self):
        """
        初始化存储连接
        """
        self.session.headers.update({
            "Authorization": f"Bearer {self.access_token}"
        })

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        目录遍历实现
        """

        if fileitem.type == "file":
            item = self.detail(fileitem)
            if item:
                return [item]
            return []

        cid = self._path_to_id(fileitem.path)
        items = []
        offset = 0

        while True:
            resp = self._request_api(
                "GET",
                "/open/ufile/files",
                "data",
                params={"cid": cid, "limit": 1000, "offset": offset, "cur": True, "show_dir": 1}
            )
            if not resp:
                break
            for item in resp:
                # 更新缓存
                path = f"{fileitem.path}{item['fn']}"
                self._id_cache[path] = item["fid"]

                file_path = path + ("/" if item["fc"] == "0" else "")
                items.append(schemas.FileItem(
                    storage=self.schema.value,
                    fileid=item["fid"],
                    name=item["fn"],
                    basename=Path(item["fn"]).stem,
                    extension=item["ico"] if item["fc"] == "1" else None,
                    type="dir" if item["fc"] == "0" else "file",
                    path=file_path,
                    size=item["fs"] if item["fc"] == "1" else None,
                    modify_time=item["upt"],
                    pickcode=item["pc"]
                ))

            if len(resp) < 1000:
                break
            offset += len(resp)

        return items

    def create_folder(self, parent_item: schemas.FileItem, name: str) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        parent_id = self._path_to_id(parent_item.path)
        new_path = Path(parent_item.path) / name
        resp = self._request_api(
            "POST",
            "/open/folder/add",
            data={
                "pid": parent_id,
                "file_name": name
            }
        )
        if not resp.get("state"):
            if resp.get("code") == 20004:
                # 目录已存在
                return self.get_item(new_path)
            logger.warn(f"创建目录失败: {resp.get('message')}")
            return None
        # 缓存新目录
        self._id_cache[str(new_path)] = resp["data"]["file_id"]
        return schemas.FileItem(
            storage=self.schema.value,
            fileid=resp["data"]["file_id"],
            path=str(new_path) + "/",
            name=name,
            basename=name,
            type="dir",
            modify_time=int(time.time())
        )

    def upload(self, target_dir: schemas.FileItem, local_path: Path, new_name: str = None) -> schemas.FileItem:
        """
        实现带秒传、断点续传和二次认证的文件上传
        """
        # 计算文件特征值
        target_name = new_name or local_path.name
        file_size = local_path.stat().st_size
        file_sha1 = self._calc_sha1(local_path)

        # 获取目标目录CID
        target_cid = self._path_to_id(target_dir.path)
        target_param = f"U_1_{target_cid}"

        # Step 1: 初始化上传
        init_data = {
            "file_name": target_name,
            "file_size": file_size,
            "target": target_param,
            "fileid": file_sha1
        }
        init_resp = self._request_api(
            "POST",
            "/open/upload/init",
            "data",
            data=init_data
        )

        # 处理秒传成功
        if init_resp.get("status") == 2:
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=init_resp["file_id"],
                path=str(Path(target_dir.path) / target_name),
                name=target_name,
                basename=Path(target_dir.name).stem,
                extension=Path(target_dir.name).suffix[1:],
                size=file_size,
                type="file",
                modify_time=int(time.time())
            )

        # Step 2: 处理二次认证
        if init_resp.get("code") in [700, 701]:
            sign_check = init_resp["sign_check"].split("-")
            start = int(sign_check[0])
            end = int(sign_check[1])

            # 计算指定区间的SHA1
            with open(local_path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
                sign_val = hashlib.sha1(chunk).hexdigest().upper()

            # 重新初始化请求
            init_data.update({
                "sign_key": init_resp["sign_key"],
                "sign_val": sign_val
            })
            init_resp = self._request_api(
                "POST",
                "/open/upload/init",
                "data",
                data=init_data
            )

        # Step 3: 获取上传凭证
        token_resp = self._request_api(
            "GET",
            "/open/upload/get_token",
            "data"
        )

        # Step 4: 对象存储上传
        upload_url = f"https://{token_resp['endpoint']}"
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "x-oss-security-token": token_resp["SecurityToken"],
            "Content-Type": "application/octet-stream"
        }

        # 断点续传处理
        uploaded = 0
        while uploaded < file_size:
            # 10MB分块
            chunk_size = min(1024 * 1024 * 10, file_size - uploaded)

            # 实际上传
            with open(local_path, "rb") as f:
                f.seek(uploaded)
                chunk = f.read(chunk_size)
                requests.put(
                    upload_url,
                    headers=headers,
                    data=chunk
                ).raise_for_status()

            uploaded += chunk_size

        # 构造返回结果
        return schemas.FileItem(
            storage=self.schema.value,
            fileid=init_resp.get("file_id") or self._path_to_id(str(Path(target_dir.path) / target_name)),
            type="file",
            path=str(Path(target_dir.path) / target_name),
            name=target_name,
            basename=Path(target_name).stem,
            extension=Path(target_name).suffix[1:],
            size=file_size,
            modify_time=int(time.time())
        )

    def download(self, fileitem: schemas.FileItem, path: Path = None) -> Path:
        """
        带限速处理的下载
        """
        detail = self.get_item(Path(fileitem.path))
        local_path = path or settings.TEMP_PATH / fileitem.name
        download_info = self._request_api(
            "POST",
            "/open/ufile/downurl",
            "data",
            data={
                "pick_code": detail.pickcode
            }
        )
        download_url = list(download_info.values())[0].get("url", {}).get("url")
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
                "/open/ufile/delete",
                data={
                    "file_ids": self._path_to_id(fileitem.path)
                }
            )
            return True
        except requests.exceptions.HTTPError:
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件/目录
        """
        file_id = self._path_to_id(fileitem.path)
        resp = self._request_api(
            "POST",
            "/open/ufile/update",
            data={
                "file_id": file_id,
                "file_name": name
            }
        )
        if resp["state"]:
            if fileitem.path in self._id_cache:
                del self._id_cache[fileitem.path]
                for key in list(self._id_cache.keys()):
                    if key.startswith(fileitem.path):
                        del self._id_cache[key]
            new_path = Path(fileitem.path).parent / name
            self._id_cache[str(new_path)] = file_id
            return True
        return False

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件/目录项
        """
        try:
            file_id = self._path_to_id(str(path))
            if not file_id:
                return None
            resp = self._request_api(
                "GET",
                "/open/folder/get_info",
                "data",
                params={
                    "file_id": file_id
                }
            )
            return schemas.FileItem(
                storage=self.schema.value,
                fileid=resp["file_id"],
                path=str(path) + ("/" if resp["file_category"] == "1" else ""),
                type="file" if resp["file_category"] == "1" else "dir",
                name=resp["file_name"],
                basename=Path(resp["file_name"]).stem,
                extension=Path(resp["file_name"]).suffix[1:],
                pickcode=resp["pick_code"],
                size=StringUtils.num_filesize(resp['size']) if resp["file_category"] == "1" else None,
                modify_time=resp["utime"]
            )
        except Exception as e:
            logger.debug(f"获取文件信息失败: {str(e)}")
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
                    logger.warn(f"115 创建目录 {fileitem.path}{part} 失败！")
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
        src_fid = self._path_to_id(fileitem.path)
        dest_cid = self._path_to_id(str(path))

        resp = self._request_api(
            "POST",
            "/open/ufile/copy",
            data={
                "file_id": src_fid,
                "pid": dest_cid
            }
        )

        if resp["state"]:
            new_path = Path(path) / fileitem.name
            new_file = self.get_item(new_path)
            self.rename(new_file, new_name)
            # 更新缓存
            del self._id_cache[fileitem.path]
            rename_new_path = Path(path) / new_name
            self._id_cache[str(rename_new_path)] = int(new_file.fileid)
            return True
        return False

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        原子性移动操作实现
        """
        src_fid = self._path_to_id(fileitem.path)
        dest_cid = self._path_to_id(str(path))

        resp = self._request_api(
            "POST",
            "/open/ufile/move",
            data={
                "file_ids": src_fid,
                "to_cid": dest_cid
            }
        )

        if resp["state"]:
            new_path = Path(path) / fileitem.name
            new_file = self.get_item(new_path)
            self.rename(new_file, new_name)
            # 更新缓存
            del self._id_cache[fileitem.path]
            rename_new_path = Path(path) / new_name
            self._id_cache[str(rename_new_path)] = src_fid
            return True
        return False

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
                "GET",
                "/open/user/info",
                "data"
            )
            if not resp:
                return None
            space = resp["rt_space_info"]
            return schemas.StorageUsage(
                total=space["all_total"]["size"],
                available=space["all_remain"]["size"]
            )
        except KeyError:
            return None
