import json
from datetime import datetime
from pathlib import Path
from typing import Optional, List, Dict

import requests
from requests import Response

from app import schemas
from app.core.cache import cached
from app.core.config import settings
from app.log import logger
from app.modules.filemanager.storages import StorageBase
from app.schemas.types import StorageSchema
from app.utils.http import RequestUtils
from app.utils.singleton import Singleton
from app.utils.url import UrlUtils


class Alist(StorageBase, metaclass=Singleton):
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

    def init_storage(self):
        """
        初始化
        """
        pass

    @property
    def __get_base_url(self) -> str:
        """
        获取基础URL
        """
        url = self.get_conf().get("url")
        if url is None:
            return ""
        return UrlUtils.standardize_base_url(self.get_conf().get("url"))

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
        return self.__generate_token

    @property
    @cached(maxsize=1, ttl=60 * 60 * 24 * 2 - 60 * 5, skip_empty=True)
    def __generate_token(self) -> str:
        """
        如果设置永久令牌则返回永久令牌，否则使用账号密码生成一个临时 token
        缓存2天，提前5分钟更新
        """
        conf = self.get_conf()
        token = conf.get("token")
        if token:
            return str(token)
        resp: Response = RequestUtils(headers={
            'Content-Type': 'application/json'
        }).post_res(
            self.__get_api_url("/api/auth/login"),
            data=json.dumps({
                "username": conf.get("username"),
                "password": conf.get("password"),
            }),
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

        if resp is None:
            logger.warning("【alist】请求登录失败，无法连接alist服务")
            return ""

        if resp.status_code != 200:
            logger.warning(f"【alist】更新令牌请求发送失败，状态码：{resp.status_code}")
            return ""

        result = resp.json()

        if result["code"] != 200:
            logger.critical(f'【alist】更新令牌，错误信息：{result["message"]}')
            return ""

        logger.debug("【alist】AList获取令牌成功")
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
            password: Optional[str] = "",
            page: int = 1,
            per_page: int = 0,
            refresh: bool = False,
    ) -> List[schemas.FileItem]:
        """
        浏览文件
        :param fileitem: 文件项
        :param password: 路径密码
        :param page: 页码
        :param per_page: 每页数量
        :param refresh: 是否刷新
        """
        if fileitem.type == "file":
            item = self.get_item(Path(fileitem.path))
            if item:
                return [item]
            return []
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/list"),
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

        if resp is None:
            logger.warn(f"【alist】请求获取目录 {fileitem.path} 的文件列表失败，无法连接alist服务")
            return []
        if resp.status_code != 200:
            logger.warn(
                f"【alist】请求获取目录 {fileitem.path} 的文件列表失败，状态码：{resp.status_code}"
            )
            return []

        result = resp.json()

        if result["code"] != 200:
            logger.warn(
                f'【alist】获取目录 {fileitem.path} 的文件列表失败，错误信息：{result["message"]}'
            )
            return []

        return [
            schemas.FileItem(
                storage=self.schema.value,
                type="dir" if item["is_dir"] else "file",
                path=(Path(fileitem.path) / item["name"]).as_posix() + ("/" if item["is_dir"] else ""),
                name=item["name"],
                basename=Path(item["name"]).stem,
                extension=Path(item["name"]).suffix[1:] if not item["is_dir"] else None,
                size=item["size"] if not item["is_dir"] else None,
                modify_time=self.__parse_timestamp(item["modified"]),
                thumbnail=item["thumb"],
            )
            for item in result["data"]["content"] or []
        ]

    def create_folder(
            self, fileitem: schemas.FileItem, name: str
    ) -> Optional[schemas.FileItem]:
        """
        创建目录
        :param fileitem: 父目录
        :param name: 目录名
        """
        path = Path(fileitem.path) / name
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/mkdir"),
            json={"path": path.as_posix()},
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
        if resp is None:
            logger.warn(f"【alist】请求创建目录 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(f"【alist】请求创建目录 {path} 失败，状态码：{resp.status_code}")
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.warn(f'【alist】创建目录 {path} 失败，错误信息：{result["message"]}')
            return None

        return self.get_item(path)

    def get_folder(self, path: Path) -> Optional[schemas.FileItem]:
        """
        获取目录，如目录不存在则创建
        """
        folder = self.get_item(path)
        if folder:
            return folder
        if not folder:
            folder = self.create_folder(schemas.FileItem(
                storage=self.schema.value,
                type="dir",
                path=path.parent.as_posix(),
                name=path.name,
                basename=path.stem
            ), path.name)
        return folder

    def get_item(
            self,
            path: Path,
            password: Optional[str] = "",
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
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/get"),
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
        if resp is None:
            logger.warn(f"【alist】请求获取文件 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(f"【alist】请求获取文件 {path} 失败，状态码：{resp.status_code}")
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.debug(f'【alist】获取文件 {path} 失败，错误信息：{result["message"]}')
            return None

        return schemas.FileItem(
            storage=self.schema.value,
            type="dir" if result["data"]["is_dir"] else "file",
            path=path.as_posix() + ("/" if result["data"]["is_dir"] else ""),
            name=result["data"]["name"],
            basename=Path(result["data"]["name"]).stem,
            extension=Path(result["data"]["name"]).suffix[1:],
            size=result["data"]["size"],
            modify_time=self.__parse_timestamp(result["data"]["modified"]),
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
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/remove"),
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
        if resp is None:
            logger.warn(f"【alist】请求删除文件 {fileitem.path} 失败，无法连接alist服务")
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【alist】请求删除文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f'【alist】删除文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False
        return True

    def rename(self, fileitem: schemas.FileItem, name: str) -> bool:
        """
        重命名文件
        """
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/rename"),
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
        if not resp:
            logger.warn(f"【alist】请求重命名文件 {fileitem.path} 失败，无法连接alist服务")
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【alist】请求重命名文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f'【alist】重命名文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False

        return True

    def download(
            self,
            fileitem: schemas.FileItem,
            path: Path = None,
            password: Optional[str] = "",
    ) -> Optional[Path]:
        """
        下载文件，保存到本地，返回本地临时文件地址
        :param fileitem: 文件项
        :param path: 文件保存路径
        :param password: 文件密码
        """
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/get"),
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
        if not resp:
            logger.warn(f"【alist】请求获取文件 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(f"【alist】请求获取文件 {path} 失败，状态码：{resp.status_code}")
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.warn(f'【alist】获取文件 {path} 失败，错误信息：{result["message"]}')
            return None

        if result["data"]["raw_url"]:
            download_url = result["data"]["raw_url"]
        else:
            download_url = UrlUtils.adapt_request_url(self.__get_base_url, f"/d{fileitem.path}")
            if result["data"]["sign"]:
                download_url = download_url + "?sign=" + result["data"]["sign"]

        if not path:
            local_path = settings.TEMP_PATH / fileitem.name
        else:
            local_path = path / fileitem.name

        with requests.get(download_url, headers=self.__get_header_with_token(), stream=True) as r:
            r.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)

        if local_path.exists():
            return local_path
        return None

    def upload(
            self, fileitem: schemas.FileItem, path: Path, new_name: Optional[str] = None, task: bool = False
    ) -> Optional[schemas.FileItem]:
        """
        上传文件
        :param fileitem: 上传目录项
        :param path: 本地文件路径
        :param new_name: 上传后文件名
        :param task: 是否为任务，默认为False避免未完成上传时对文件进行操作
        """
        encoded_path = UrlUtils.quote((Path(fileitem.path) / path.name).as_posix())
        headers = self.__get_header_with_token()
        headers.setdefault("Content-Type", "application/octet-stream")
        headers.setdefault("As-Task", str(task).lower())
        headers.setdefault("File-Path", encoded_path)
        with open(path, "rb") as f:
            resp: Response = RequestUtils(headers=headers).put_res(
                self.__get_api_url("/api/fs/put"),
                data=f,
            )

        if resp.status_code != 200:
            logger.warn(f"【alist】请求上传文件 {path} 失败，状态码：{resp.status_code}")
            return None

        new_item = self.get_item(Path(fileitem.path) / path.name)
        if new_item and new_name and new_name != path.name:
            if self.rename(new_item, new_name):
                return self.get_item(Path(new_item.path).with_name(new_name))

        return new_item

    def detail(self, fileitem: schemas.FileItem) -> Optional[schemas.FileItem]:
        """
        获取文件详情
        """
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        复制文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/copy"),
            json={
                "src_dir": Path(fileitem.path).parent.as_posix(),
                "dst_dir": path.as_posix(),
                "names": [fileitem.name],
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
        if resp is None:
            logger.warn(
                f"【alist】请求复制文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【alist】请求复制文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f'【alist】复制文件 {fileitem.path} 失败，错误信息：{result["message"]}'
            )
            return False
        # 重命名
        if fileitem.name != new_name:
            self.rename(
                self.get_item(path / fileitem.name), new_name
            )
        return True

    def move(self, fileitem: schemas.FileItem, path: Path, new_name: str) -> bool:
        """
        移动文件
        :param fileitem: 文件项
        :param path: 目标目录
        :param new_name: 新文件名
        """
        # 先重命名
        if fileitem.name != new_name:
            self.rename(fileitem, new_name)
        resp: Response = RequestUtils(
            headers=self.__get_header_with_token()
        ).post_res(
            self.__get_api_url("/api/fs/move"),
            json={
                "src_dir": Path(fileitem.path).parent.as_posix(),
                "dst_dir": path.as_posix(),
                "names": [new_name],
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
        if resp is None:
            logger.warn(
                f"【alist】请求移动文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【alist】请求移动文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f'【alist】移动文件 {fileitem.path} 失败，错误信息：{result["message"]}'
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

    @staticmethod
    def __parse_timestamp(time_str: str) -> float:
        """
        直接使用 ISO 8601 格式解析时间
        """
        return datetime.fromisoformat(time_str).timestamp()
