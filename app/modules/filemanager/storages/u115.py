import hashlib
import os
import time
from pathlib import Path
from typing import List, Dict, Optional, Tuple

import qrcode
import requests

from app import schemas
from app.log import logger
from app.modules.filemanager import StorageBase
from app.schemas.types import StorageSchema
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

    # 访问token
    access_token = None

    # 基础url
    base_url = "https://api.115.com"

    # CID和路径缓存
    _cid_cache: Dict[str | int, str | int] = {}

    def __init__(self):
        super().__init__()
        self.session = requests.Session()
        self._init_session()

    def _init_session(self):
        """
        初始化带速率限制的会话
        """
        adapter = requests.adapters.HTTPAdapter(
            max_retries=3,
            pool_connections=10,
            pool_maxsize=50
        )
        self.session.mount('https://', adapter)
        self.session.headers.update({
            "User-Agent": "W115Storage/2.0",
            "Accept-Encoding": "gzip, deflate"
        })

    def generate_qrcode(self) -> Tuple[dict, str]:
        """
        生成设备授权二维码
        """
        resp = self.session.post(
            f"{self.base_url}/oauth/device",
            data={"client_id": self.get_conf().get("app_id")}
        ).json()
        qr = qrcode.make(f"115AUTH|{resp['device_code']}")
        return resp, qr.png_as_base64_str()

    def check_login(self, device_code: str) -> Optional[Dict]:
        """
        检查授权状态
        """
        try:
            resp = self.session.post(f"{self.base_url}/oauth/token", data={
                "grant_type": "device",
                "device_code": device_code,
                "client_secret": self.get_conf().get("app_secret")
            }, timeout=10)
            if resp.status_code == 200:
                token_data = resp.json()
                self.access_token = token_data["access_token"]
                # 持久化配置
                self.set_config({"access_token": self.access_token})
                return {"status": "success"}
            return {"status": "pending"}
        except requests.exceptions.RequestException:
            return {"status": "error"}

    def init_storage(self):
        """
        初始化存储连接
        """
        if conf := self.get_conf():
            self.access_token = conf.get("access_token")
            self.session.headers.update({"Authorization": f"Bearer {self.access_token}"})

    def list(self, fileitem: schemas.FileItem) -> List[schemas.FileItem]:
        """
        目录遍历实现
        """
        cid = self._path_to_cid(fileitem.path)
        items = []
        offset = 0

        while True:
            resp = self._request_api(
                "GET", "/files",
                params={"cid": cid, "limit": 1000, "offset": offset}
            )
            batch = resp["data"]
            for item in batch:
                path = self._cid_to_path(item["cid"])
                items.append(schemas.FileItem(
                    path=path,
                    name=item["name"],
                    type="dir" if item["is_dir"] else "file",
                    size=item["size"],
                    modify_time=item["modified"]
                ))
                self._cid_cache[path] = item["cid"]  # 更新缓存

            if len(batch) < 1000:
                break
            offset += len(batch)

        return items

    def create_folder(self, parent_item: schemas.FileItem, name: str) -> schemas.FileItem:
        """
        创建目录
        """
        parent_cid = self._path_to_cid(parent_item.path)
        resp = self._request_api(
            "POST", "/file/mkdir",
            json={"cid": parent_cid, "name": name}
        )
        new_path = os.path.join(parent_item.path, name)
        # 缓存新目录
        self._cid_cache[new_path] = resp["cid"]
        self._cid_cache[resp["cid"]] = new_path
        return schemas.FileItem(
            path=new_path,
            name=name,
            type="dir",
            modify_time=int(time.time())
        )

    def upload(self, target_dir: schemas.FileItem, local_path: Path, new_name: str = None) -> schemas.FileItem:
        """
        断点续传实现
        """
        file_name = new_name or local_path.name
        file_size = local_path.stat().st_size
        file_hash = self._calc_sha1(local_path)

        # 初始化上传任务
        upload_info = self._request_api(
            "POST", "/open/upload/init",
            json={
                "file_name": file_name,
                "file_size": file_size,
                "file_sha1": file_hash,
                "target_dir": self._path_to_cid(target_dir.path)
            }
        )

        # 分片上传
        with open(local_path, "rb") as f:
            offset = 0
            # 4MB分片
            while chunk := f.read(4 * 1024 * 1024):
                self.session.put(
                    f"{self.base_url}/open/upload/{upload_info['upload_id']}",
                    data=chunk,
                    headers={"Content-Range": f"bytes {offset}-{offset + len(chunk) - 1}/{file_size}"}
                )
                offset += len(chunk)

        return self.get_item(Path(target_dir.path) / file_name)

    def download(self, fileitem: schemas.FileItem, save_path: Path = None) -> Path:
        """
        带限速处理的下载
        """
        download_url = self._request_api(
            "GET", "/file/download",
            params={"cid": self._path_to_cid(fileitem.path)}
        )["url"]

        local_path = save_path or Path("/tmp") / fileitem.name
        with self.session.get(download_url, stream=True) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
        return local_path

    def _request_api(self, method: str, endpoint: str, **kwargs):
        """
        带错误处理和速率限制的API请求
        """
        if not self.access_token:
            raise Exception("未授权，请先完成OAuth认证")

        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"

        resp = self.session.request(
            method, f"{self.base_url}{endpoint}",
            headers=headers, **kwargs
        )

        if resp is None:
            logger.error(f"请求 115 API 失败: {method} {endpoint}")
            return None

        # 处理速率限制
        if resp.status_code == 429:
            reset_time = int(resp.headers.get("X-RateLimit-Reset", 60))
            time.sleep(reset_time + 5)
            return self._request_api(method, endpoint, **kwargs)

        resp.raise_for_status()
        return resp.json()

    def _path_to_cid(self, path: str) -> str:
        """
        路径转CID（带缓存机制）
        """
        if path in self._cid_cache:
            return self._cid_cache[path]

        # 递归解析路径
        current_cid = "0"  # 根目录CID
        for part in Path(path).parts[1:]:  # 忽略根目录
            resp = self._request_api(
                "GET", "/files",
                params={"cid": current_cid, "search_value": part}
            )
            for item in resp["data"]:
                if item["name"] == part:
                    current_cid = item["cid"]
                    break
            else:
                raise FileNotFoundError(f"路径不存在: {path}")
        self._cid_cache[path] = current_cid
        return current_cid

    def _cid_to_path(self, cid: str) -> str:
        """
        CID转路径（带双向缓存）
        """

        # 根目录特殊处理
        if cid == "0":
            return "/"

        # 优先从缓存读取
        if cid in self._cid_cache.values():
            return next(k for k, v in self._cid_cache.items() if v == cid)

        # 递归构建路径
        path_parts = []
        current_cid = cid

        while current_cid != "0":
            # 从API获取当前节点信息
            detail = self._request_api(
                "GET", "/file/detail",
                params={"cid": current_cid}
            )

            # 处理可能的空数据（如已删除文件）
            if not detail:
                raise FileNotFoundError(f"CID {current_cid} 不存在")

            parent_cid = detail["parent_id"]
            path_parts.append(detail["name"])

            # 检查父节点缓存
            if parent_cid in self._cid_cache.values():
                parent_path = next(k for k, v in self._cid_cache.items() if v == parent_cid)
                path_parts.reverse()
                full_path = os.path.join(parent_path, *path_parts)
                # 更新正向缓存
                self._cid_cache[full_path] = cid
                return str(full_path)

            current_cid = parent_cid

        # 构建完整路径
        full_path = "/" + "/".join(reversed(path_parts))
        # 缓存新路径
        self._cid_cache[full_path] = cid
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

    def check(self) -> bool:
        return self.access_token is not None

    def delete(self, fileitem: schemas.FileItem) -> bool:
        try:
            self._request_api(
                "POST", "/file/delete",
                json={"cid": self._path_to_cid(fileitem.path)}
            )
            return True
        except requests.exceptions.HTTPError:
            return False

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        new_path = Path(fileitem.path).parent / name
        resp = self._request_api(
            "POST", "/file/rename",
            json={
                "cid": self._path_to_cid(fileitem.path),
                "new_name": name
            }
        )
        if resp["state"]:
            self._cid_cache[str(new_path)] = resp["cid"]
            old_path = fileitem.path
            new_path = Path(fileitem.path).parent / name
            # 删除旧路径
            del self._cid_cache[old_path]
            self._cid_cache[new_path.as_posix()] = resp["cid"]
            # 更新反向缓存
            self._cid_cache[resp["cid"]] = new_path.as_posix()
            return True
        return False

    def get_item(self, path: Path) -> Optional[schemas.FileItem]:
        try:
            cid = self._path_to_cid(str(path))
            resp = self._request_api(
                "GET", "/file/detail",
                params={"cid": cid}
            )
            return schemas.FileItem(
                path=str(path),
                name=resp["name"],
                type="dir" if resp["is_dir"] else "file",
                size=resp["size"],
                modify_time=resp["modified"]
            )
        except Exception as e:
            logger.debug(f"获取文件信息失败: {str(e)}")
            return None

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取指定路径的文件夹元数据
        """
        item = self.get_item(path)
        if item and item.type == "dir":
            return item
        return None

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件/目录详细信息
        """
        try:
            cid = self._path_to_cid(fileitem.path)
            resp = self._request_api("GET", "/file/detail", params={"cid": cid})
            return schemas.FileItem(
                path=fileitem.path,
                name=resp["name"],
                type="dir" if resp["is_dir"] else "file",
                size=resp["size"],
                modify_time=resp["modified"],
                pickcode=resp.get("pick_code")
            )
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 404:
                return None
            raise

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        企业级复制实现（支持目录递归复制）
        """
        src_cid = self._path_to_cid(fileitem.path)
        dest_cid = self._path_to_cid(str(path))

        resp = self._request_api(
            "POST", "/file/copy",
            json={
                "cid": src_cid,
                "pid": dest_cid,
                "name": new_name,
                "overwrite": 0  # 0:不覆盖 1:覆盖
            }
        )

        if resp["state"]:
            # 更新目标路径缓存
            new_path = str(Path(path) / new_name)
            self._cid_cache[new_path] = resp["cid"]
            return True
        return False

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        原子性移动操作实现
        """
        src_cid = self._path_to_cid(fileitem.path)
        dest_cid = self._path_to_cid(str(path))

        resp = self._request_api(
            "POST", "/file/move",
            json={
                "cid": src_cid,
                "pid": dest_cid,
                "name": new_name,
                "overwrite": 0
            }
        )

        if resp["state"]:
            # 更新缓存
            old_path = fileitem.path
            new_path = str(Path(path) / new_name)
            del self._cid_cache[old_path]
            self._cid_cache[new_path] = src_cid
            return True
        return False

    def link(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def softlink(self, fileitem: schemas.FileItem, target_file: Path) -> bool:
        pass

    def usage(self) -> Optional[schemas.StorageUsage]:
        """获取带有企业级配额信息的存储使用情况"""
        try:
            resp = self._request_api("GET", "/user/info")
            space = resp["data"]["space_info"]
            return schemas.StorageUsage(
                total=space["total"],
                available=space["free"]
            )
        except KeyError:
            return None
