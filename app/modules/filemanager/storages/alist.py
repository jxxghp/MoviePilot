import logging
from pathlib import Path
from typing import Optional, Tuple, List, Dict, Union

from cachetools import cached, TTLCache
from requests import Response

from app import schemas
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.http import RequestUtils
from app.utils.url import UrlUtils


class Alist(StorageBase):
    """
    Alist相关操作
    api文档：https://alist.nn.ci/zh/guide/api
    """

    # 存储类型
    schema = StorageSchema.Alist

    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
    }

    def __init__(self):
        super().__init__()

    def check_login(self, *args, **kwargs) -> Optional[Dict[str, str]]:
        pass

    def get_config(self) -> Optional[schemas.StorageConf]:
        """
        获取配置
        """
        return self.storagehelper.get_storage(self.schema.value)

    def set_config(self, conf: dict):
        """
        设置配置
        """
        self.storagehelper.set_storage(self.schema.value, conf)

    def support_transtype(self) -> dict:
        """
        支持的整理方式
        """
        return self.transtype

    def is_support_transtype(self, transtype: str) -> bool:
        """
        是否支持整理方式
        """
        return transtype in self.transtype

    @property
    def __get_base_url(self) -> str:
        """
        获取基础URL
        """
        url = self.get_config().config.get("url")
        return UrlUtils.standardize_base_url(url) or ""

    def __get_api_url(self, path: str) -> str:
        """
        获取API URL
        """
        return UrlUtils.adapt_request_url(self.__get_base_url, path)

    @property
    def __get_valuable_toke(self) -> str:
        """
        获取一个可用的token
        如果设置永久令牌则返回永久令牌
        否则使用账号密码生成临时令牌
        """
        token = self.get_config().config.get("token")
        if token:
            return token
        return self.__generate_token

    @property
    @cached(cache=TTLCache(maxsize=1, ttl=60 * 60 * 24 * 2 - 60 * 5))
    def __generate_token(self) -> str:
        """
        使用账号密码生成一个临时token
        缓存2天，提前5分钟更新
        """
        conf = self.get_config().config
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/auth/login"),
            json={
                "username": conf.get("username"),
                "password": conf.get("password"),
            },
        )
        """
        {
            "username": "{{alist_username}}",
            "password": "{{alist_password}}"
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": {
                "token": "abcd"
            }
        }
        """

        if resp.status_code != 200:
            logger.warning(f"更新令牌请求发送失败，状态码：{resp.status_code}")

        result = resp.json()

        if result["code"] != 200:
            logger.critical(f'更新令牌，错误信息：{result["message"]}')

        logger.debug("更新令牌成功")
        return result["data"]["token"]

    def __get_header_with_token(self) -> dict:
        """
        获取带有token的header
        """
        return {"Authorization": self.__get_valuable_toke}

    def check(self) -> bool:
        """
        检查存储是否可用
        """
        pass

    def list(
            self,
            fileitem: schemas.FileItem,
            password: str = "",
            page: int = 1,
            per_page: int = 0,
            refresh: bool = False,
    ) -> Optional[List[schemas.FileItem]]:
        """
        浏览文件
        :param fileitem: 文件项
        :param password: 路径密码
        :param page: 页码
        :param per_page: 每页数量
        :param refresh: 是否刷新
        """
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/list"),
            headers=self.__get_header_with_token(),
            json={
                "path": fileitem.path,
                "password": password,
                "page": page,
                "per_page": per_page,
                "refresh": refresh,
            },
        )
        """
        {
            "path": "/t",
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": false
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": {
                "content": [
                {
                    "name": "Alist V3.md",
                    "size": 1592,
                    "is_dir": false,
                    "modified": "2024-05-17T13:47:55.4174917+08:00",
                    "created": "2024-05-17T13:47:47.5725906+08:00",
                    "sign": "",
                    "thumb": "",
                    "type": 4,
                    "hashinfo": "null",
                    "hash_info": null
                }
                ],
                "total": 1,
                "readme": "",
                "header": "",
                "write": true,
                "provider": "Local"
            }
        }
        """

        if resp.status_code != 200:
            logging.warning(
                f"请求获取目录 {fileitem.path} 的文件列表失败，状态码：{resp.status_code}"
            )
            return

        result = resp.json()

        if result["code"] != 200:
            logging.warning(
                f'获取目录 {fileitem.path} 的文件列表失败，错误信息：{result["message"]}'
            )
            return

        return [
            schemas.FileItem(
                storage=self.schema.value,
                type="dir" if item["is_dir"] else "file",
                path=fileitem.path + "/" + item["name"],
                name=item["name"],
                basename=Path(item["name"]).stem,
                extension=Path(item["name"]).suffix,
                size=item["size"],
                modify_time=item["modified"],
                thumbnail=item["thumb"],
            )
            for item in result["data"]["content"]
        ]

    def create_folder(
            self, fileitem: schemas.FileItem, name: str
    ) -> Optional[schemas.FileItem]:
        """
        创建目录
        """
        path = fileitem.path + "/" + name
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/auth/login"),
            headers=self.__get_header_with_token(),
            json={"path": path},
        )
        """
        {
            "path": "/tt"
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": null
        }
        """
        if resp.status_code != 200:
            logging.warning(f"请求创建目录 {path} 失败，状态码：{resp.status_code}")
            return

        result = resp.json()
        if result["code"] != 200:
            logging.warning(f'创建目录 {path} 失败，错误信息：{result["message"]}')
            return

        return self.get_item(path)

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        folder = self.get_item(path)
        if folder is None:
            folder = self.create_folder(self.get_parent(path), path.name)
        return folder

    def get_item(
            self,
            path: Path,
            password: str = "",
            page: int = 1,
            per_page: int = 0,
            refresh: bool = False,
    ) -> Optional[schemas.FileItem]:
        """
        获取文件或目录，不存在返回None
        :param path: 文件路径
        :param password: 路径密码
        :param page: 页码
        :param per_page: 每页数量
        :param refresh: 是否刷新
        """
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/get"),
            headers=self.__get_header_with_token(),
            json={
                "path": path.as_posix(),
                "password": password,
                "page": page,
                "per_page": per_page,
                "refresh": refresh,
            },
        )
        """
        {
            "path": "/t",
            "password": "",
            "page": 1,
            "per_page": 0,
            "refresh": false
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": {
                "name": "Alist V3.md",
                "size": 2618,
                "is_dir": false,
                "modified": "2024-05-17T16:05:36.4651534+08:00",
                "created": "2024-05-17T16:05:29.2001008+08:00",
                "sign": "",
                "thumb": "",
                "type": 4,
                "hashinfo": "null",
                "hash_info": null,
                "raw_url": "http://127.0.0.1:5244/p/local/Alist%20V3.md",
                "readme": "",
                "header": "",
                "provider": "Local",
                "related": null
            }
        }
        """
        if resp.status_code != 200:
            logging.warning(f"请求获取文件 {path} 失败，状态码：{resp.status_code}")
            return

        result = resp.json()
        if result["code"] != 200:
            logging.warning(f'获取文件 {path} 失败，错误信息：{result["message"]}')
            return

        return schemas.FileItem(
            storage=self.schema.value,
            type="dir" if result["data"]["is_dir"] else "file",
            path=path,
            name=result["data"]["name"],
            basename=Path(result["data"]["name"]).stem,
            extension=Path(result["data"]["name"]).suffix,
            size=result["data"]["size"],
            modify_time=result["data"]["modified"],
            thumbnail=result["data"]["thumb"],
        )

    def get_parent(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取父目录
        """
        return self.get_folder(Path(fileitem.path).parent)

    def delete(self, fileitem: schemas.FileItem) -> bool:
        """
        删除文件
        """
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/delete"),
            headers=self.__get_header_with_token(),
            json={
                "dir": Path(fileitem.path).parent.as_posix(),
                "names": [fileitem.name],
            },
        )
        """
        {
            "names": [
                "string"
            ],
            "dir": "string"
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": null
        }
        """
        if resp.status_code != 200:
            logging.warning(
                f"请求删除文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logging.warning(
                f'删除文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False
        return True

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/rename"),
            headers=self.__get_header_with_token(),
            json={
                "name": name,
                "path": fileitem.path,
            },
        )
        """
        {
            "name": "test3",
            "path": "/阿里云盘/test2"
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": null
        }
        """
        if resp.status_code != 200:
            logging.warning(
                f"请求重命名文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logging.warning(
                f'重命名文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False

        return True

    def download(
            self,
            fileitem: schemas.FileItem,
            path: Path = None,
            password: str = "",
            raw_url: bool = False,
    ) -> Path:
        """
        下载文件，保存到本地，返回本地临时文件地址
        :param fileitem: 文件项
        :param path: 文件保存路径
        :param password: 文件密码
        :param raw_url: 是否使用原始链接下载
        """
        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/get"),
            headers=self.__get_header_with_token(),
            json={
                "path": fileitem.path,
                "password": password,
                "page": 1,
                "per_page": 0,
                "refresh": False,
            },
        )
        """
        {
            "code": 200,
            "message": "success",
            "data": {
                "name": "[ANi]輝夜姬想讓人告白～天才們的戀愛頭腦戰～[01][1080P][Baha][WEB-DL].mp4",
                "size": 924933111,
                "is_dir": false,
                "modified": "1970-01-01T00:00:00Z",
                "created": "1970-01-01T00:00:00Z",
                "sign": "1v0xkMQz_uG8fkEOQ7-l58OnbB-g4GkdBlUBcrsApCQ=:0",
                "thumb": "",
                "type": 2,
                "hashinfo": "null",
                "hash_info": null,
                "raw_url": "xxxxxx",
                "readme": "",
                "header": "",
                "provider": "UrlTree",
                "related": null
            }
        }
        """
        if resp.status_code != 200:
            logging.warning(f"请求获取文件 {path} 失败，状态码：{resp.status_code}")
            return

        result = resp.json()
        if result["code"] != 200:
            logging.warning(f'获取文件 {path} 失败，错误信息：{result["message"]}')
            return

        if raw_url:
            download_url = result["data"]["raw_url"]
        else:
            download_url = UrlUtils.adapt_request_url(self.__get_base_url, f"/d{fileitem.path}")
            if result["data"]["sign"]:
                download_url = download_url + "?sign=" + result["data"]["sign"]

        resp = RequestUtils.get(download_url)
        with open(path, "wb") as f:
            f.write(resp.content)

        if path.exists():
            return path
        return None

    def upload(
            self, fileitem: schemas.FileItem, path: Path, task: bool = False
    ) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param task: 是否为任务，默认为False避免未完成上传时对文件进行操作
        """
        encoded_path = UrlUtils.quote(fileitem.path)
        headers = self.__get_header_with_token()
        headers.setdefault("Content-Type", "multipart/form-data")
        headers.setdefault("As-Task", str(task).lower())
        headers.setdefault("File-Path", encoded_path)
        with open(path, "rb") as f:
            resp: Response = RequestUtils.put(
                self.__get_api_url("/api/fs/form"),
                headers=headers,
                data={"file": f},
            )

        if resp.status_code != 200:
            logging.warning(f"请求上传文件 {path} 失败，状态码：{resp.status_code}")
            return

        return fileitem

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        return self.get_item(fileitem.path)

    def __get_copy_and_move_data(
            self, fileitem: schemas.FileItem, target: Union[schemas.FileItem, Path]
    ) -> Tuple[str, str, List[str], bool]:
        """
        获取复制或移动文件需要的数据

        :param fileitem: 文件项
        :param target: 目标文件项或目标路径
        :return: 源目录，目标目录，文件名列表，是否有效
        """
        name = Path(target).name
        if fileitem.name != name:
            return "", "", [], False

        src_dir = Path(fileitem.path).parent.as_posix()
        if isinstance(target, schemas.FileItem):
            traget_dir = Path(target.path).parent.as_posix()
        else:
            traget_dir = target.parent.as_posix()

        return src_dir, traget_dir, [name], True

    def copy(
            self, fileitem: schemas.FileItem, target: Union[schemas.FileItem, Path]
    ) -> bool:
        """
        复制文件

        源文件名和目标文件名必须相同
        """
        src_dir, dst_dir, names, is_valid = self.__get_copy_and_move_data(
            fileitem, target
        )
        if not is_valid:
            return False

        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/copy"),
            headers=self.__get_header_with_token(),
            json={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
            },
        )
        """
        {
            "src_dir": "string",
            "dst_dir": "string",
            "names": [
                "string"
            ]
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": null
        }
        """
        if resp.status_code != 200:
            logging.warning(
                f"请求复制文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logging.warning(
                f'复制文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False
        return True

    def move(
            self, fileitem: schemas.FileItem, target: Union[schemas.FileItem, Path]
    ) -> bool:
        """
        移动文件
        """
        src_dir, dst_dir, names, is_valid = self.__get_copy_and_move_data(
            fileitem, target
        )
        if not is_valid:
            return False

        resp: Response = RequestUtils.post(
            self.__get_api_url("/api/fs/move"),
            headers=self.__get_header_with_token(),
            json={
                "src_dir": src_dir,
                "dst_dir": dst_dir,
                "names": names,
            },
        )
        """
        {
            "src_dir": "string",
            "dst_dir": "string",
            "names": [
                "string"
            ]
        }
        ======================================
        {
            "code": 200,
            "message": "success",
            "data": null
        }
        """
        if resp.status_code != 200:
            logging.warning(
                f"请求移动文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logging.warning(
                f'移动文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False
        return True

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
        pass

    def snapshot(self, path: Path) -> Dict[str, float]:
        """
        快照文件系统，输出所有层级文件信息（不含目录）
        """
        files_info = {}

        def __snapshot_file(_fileitm: schemas.FileItem):
            """
            递归获取文件信息
            """
            if _fileitm.type == "dir":
                for sub_file in self.list(_fileitm):
                    __snapshot_file(sub_file)
            else:
                files_info[_fileitm.path] = _fileitm.size

        fileitem = self.get_item(path)
        if not fileitem:
            return {}

        __snapshot_file(fileitem)

        return files_info
