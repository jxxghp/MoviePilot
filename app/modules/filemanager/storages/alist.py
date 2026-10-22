"""
OpenList 兼容存储的鉴权、分页查询、传输和目标元数据回退。
"""

import hashlib
import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, List, Optional, cast

from app.adapters.network.http import RequestUtils
from app.foundation.singleton import WeakSingleton
from app.foundation.url import UrlUtils
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.cache import CacheControls, cached
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import OperationInterrupted, StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema

# OpenList/AList 在 per_page<=0 时会退回后端默认 200，显式指定最大页大小避免大目录被截断。
OPENLIST_MAX_LIST_PAGE_SIZE = 500


class Alist(StorageBase, metaclass=WeakSingleton):
    """
    通过 OpenList 兼容接口查询文件、传输内容和管理路径。

    API 文档：https://fox.oplist.org/
    普通查询可能以空结果表示接口失败，需要覆盖保护时应使用严格查询。
    """

    # 存储类型
    schema = StorageSchema.Alist

    # 支持的整理方式
    transtype = {
        "copy": "复制",
        "move": "移动",
    }

    def __init__(self) -> None:
        """
        初始化存储配置访问服务，登录令牌按首次请求惰性获取。
        """
        super().__init__()

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """
        读取 OpenList 目录修改时间检查开关，配置更新后立即生效。

        :return: OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME 的布尔值
        """
        return bool(get_runtime_setting('OPENLIST_SNAPSHOT_CHECK_FOLDER_MODTIME'))

    def init_storage(self) -> None:
        """
        清除当前地址和账号对应的临时登录令牌缓存。
        """
        conf = self.get_conf()
        cast(CacheControls, self.__login_token).cache_delete(
            self, self.__get_base_url, conf.get("username"), conf.get("password")
        )

    def _delay_get_item(
        self, path: Path, /, refresh: bool = False
    ) -> Optional[_SchemaFileItem]:
        """
        最多两次延迟查询目标，兼容远端操作后的可见性延迟。

        每次查询前等待 2 秒，包括第一次。

        :param path: 需要查询的目标路径
        :param refresh: 是否要求服务端刷新元数据缓存
        :return: 首次查到的存储项；两次均无结果返回 None
        """
        for _ in range(2):
            time.sleep(2)
            fileitem = self.get_item(path=path, refresh=refresh)
            if fileitem:
                return fileitem
        return None

    def __build_transfer_item(
            self, source_item: _SchemaFileItem, target_path: Path
    ) -> _SchemaFileItem:
        """
        根据目标路径构造文件项，用于 OpenList 操作成功但元数据短时间不可见的场景。
        目录项路径需要遵循 FileItem 以斜杠结尾的约定。
        """
        target_path_str = target_path.as_posix()
        if source_item.type == "dir" and not target_path_str.endswith("/"):
            target_path_str = f"{target_path_str}/"

        return _SchemaFileItem(
            storage=self.schema.value,
            type=source_item.type,
            path=target_path_str,
            name=target_path.name,
            basename=target_path.stem,
            extension=target_path.suffix[1:] if source_item.type != "dir" else None,
            size=getattr(source_item, "size", None),
            modify_time=getattr(source_item, "modify_time", None),
            thumbnail=getattr(source_item, "thumbnail", None),
        )

    @property
    def __get_base_url(self) -> str:
        """
        获取基础URL。
        """
        url = self.get_conf().get("url")
        if url is None:
            return ""
        return UrlUtils.standardize_base_url(str(url))

    def __get_api_url(self, path: str) -> str:
        """
        获取API URL。

        :param path: API路径
        :return: 基础地址与接口路径组合后的完整请求地址
        """
        return UrlUtils.adapt_request_url(self.__get_base_url, path) or ""

    @property
    def __get_valuable_toke(self) -> str:
        """
        获取一个可用的token。
        如果设置永久令牌则返回永久令牌
        否则使用账号密码生成临时令牌
        """
        return self.__generate_token()

    def __generate_token(self) -> str:
        """
        如果设置永久令牌则返回永久令牌，否则使用账号密码生成一个临时 token。
        """
        conf = self.get_conf()
        token = conf.get("token")
        if token:
            return str(token)
        return self.__login_token(
            self.__get_base_url, conf.get("username"), conf.get("password")
        )

    @cached(maxsize=8, ttl=60 * 60 * 24 * 2 - 60 * 5, skip_empty=True)
    def __login_token(
        self, base_url: str, username: Optional[str], password: Optional[str]
    ) -> str:
        """
        使用账号密码生成一个临时 token。
        缓存2天，提前5分钟更新
        """
        url = UrlUtils.adapt_request_url(base_url, "/api/auth/login")
        if not url:
            return ""
        resp = RequestUtils(headers={"Content-Type": "application/json"}).post_res(
            url,
            data=json.dumps(
                {
                    "username": username,
                    "password": password,
                }
            ),
        )

        if resp is None:
            logger.warning("【OpenList】请求登录失败，无法连接alist服务")
            return ""

        if resp.status_code != 200:
            logger.warning(
                f"【OpenList】更新令牌请求发送失败，状态码：{resp.status_code}"
            )
            return ""

        result = resp.json()

        if result["code"] != 200:
            logger.critical(f"【OpenList】更新令牌，错误信息：{result['message']}")
            return ""

        logger.debug("【OpenList】AList获取令牌成功")
        token = result["data"]["token"]
        return token if isinstance(token, str) else ""

    def __get_header_with_token(self) -> dict[str, str]:
        """
        获取带有token的header。
        """
        return {"Authorization": self.__get_valuable_toke}

    def check(self) -> bool:
        """
        检查能否取得配置令牌或账号登录令牌。

        :return: 令牌非空返回 True；永久令牌不在此方法额外向服务器验证
        """
        return True if self.__generate_token() else False

    def list(
        self,
        fileitem: _SchemaFileItem,
        password: Optional[str] = "",
        page: int = 1,
        per_page: int = 0,
        refresh: bool = False,
    ) -> List[_SchemaFileItem]:
        """
        列举文件或目录，可按服务端总数自动翻页。

        自动翻页依赖服务端返回总数等分页信息，缺少总数时可能在首个非空页结束。

        :param fileitem: 目录项；文件项改为查询详情并返回单元素列表
        :param password: 可选路径访问密码
        :param page: 起始页码，从 1 开始
        :param per_page: 正数时只查一页；小于等于 0 时以每页 500 项自动翻页
        :param refresh: 是否要求服务端刷新列表缓存
        :return: 文件项列表；HTTP 或业务错误返回空列表，之前成功页的结果也会丢弃
        """
        if not fileitem.path:
            return []
        if fileitem.type == "file":
            item = self.get_item(Path(fileitem.path))
            if item:
                return [item]
            return []
        items = []
        current_page = page
        auto_page = per_page <= 0
        effective_per_page = OPENLIST_MAX_LIST_PAGE_SIZE if auto_page else per_page
        while True:
            resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
                self.__get_api_url("/api/fs/list"),
                json={
                    "path": fileitem.path,
                    "password": password,
                    "page": current_page,
                    "per_page": effective_per_page,
                    "refresh": refresh,
                },
            )

            if resp is None:
                logger.warn(
                    f"【OpenList】请求获取目录 {fileitem.path} 的文件列表失败，无法连接alist服务"
                )
                return []
            if resp.status_code != 200:
                logger.warn(
                    f"【OpenList】请求获取目录 {fileitem.path} 的文件列表失败，状态码：{resp.status_code}"
                )
                return []

            result = resp.json()

            if result["code"] != 200:
                logger.warn(
                    f"【OpenList】获取目录 {fileitem.path} 的文件列表失败，错误信息：{result['message']}"
                )
                return []

            page_data = result["data"]
            page_content = page_data.get("content") or []
            items.extend(
                [
                    _SchemaFileItem(
                        storage=self.schema.value,
                        type="dir" if item["is_dir"] else "file",
                        path=(Path(fileitem.path) / item["name"]).as_posix()
                        + ("/" if item["is_dir"] else ""),
                        name=item["name"],
                        basename=Path(item["name"]).stem,
                        extension=Path(item["name"]).suffix[1:] if not item["is_dir"] else None,
                        size=item["size"] if not item["is_dir"] else None,
                        modify_time=self.__parse_timestamp(item["modified"]),
                        thumbnail=item["thumb"],
                    )
                    for item in page_content
                ]
            )

            if not auto_page:
                return items

            total = page_data.get("filtered_total") or page_data.get("total") or 0
            pages_total = page_data.get("pages_total") or 0
            has_more = page_data.get("has_more")
            if not page_content or len(items) >= total:
                return items
            if has_more is False or (pages_total and current_page >= pages_total):
                return items

            current_page += 1

    def create_folder(
        self, fileitem: _SchemaFileItem, name: str
    ) -> Optional[_SchemaFileItem]:
        """
        调用目录创建接口，成功后延迟查询元数据。

        :param fileitem: 父目录项
        :param name: 要创建的子目录名称
        :return: 查询到的目录项；接口成功但暂不可见时按目标路径构造目录项，接口失败返回 None
        """
        if not fileitem.path:
            return None
        path = Path(fileitem.path) / name
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/mkdir"),
            json={"path": path.as_posix()},
        )
        if resp is None:
            logger.warn(f"【OpenList】请求创建目录 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求创建目录 {path} 失败，状态码：{resp.status_code}"
            )
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】创建目录 {path} 失败，错误信息：{result['message']}"
            )
            return None

        return self._delay_get_item(
            path, refresh=True
        ) or self.__build_transfer_item(
            _SchemaFileItem(
                storage=self.schema.value,
                type="dir",
                path=fileitem.path,
                name=name,
                basename=Path(name).stem,
            ),
            path,
        )

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询目标目录，查不到时向服务端请求创建。

        :param path: 目标目录路径
        :return: 目录项；创建失败返回 None，是否递归创建由服务端决定
        """
        folder = self.get_item(path)
        if folder:
            return folder
        if not folder:
            folder = self.create_folder(
                _SchemaFileItem(
                    storage=self.schema.value,
                    type="dir",
                    path=path.parent.as_posix(),
                    name=path.name,
                    basename=path.stem,
                ),
                path.name,
            )
        return folder

    def get_item(
        self,
        path: Path,
        password: Optional[str] = "",
        page: int = 1,
        per_page: int = 0,
        refresh: bool = False,
    ) -> Optional[_SchemaFileItem]:
        """
        查询单个路径的元数据。

        :param path: 目标文件或目录路径
        :param password: 可选路径访问密码
        :param page: 透传给详情接口的页码，默认 1
        :param per_page: 透传给详情接口的每页数量，默认 0
        :param refresh: 是否要求服务端刷新元数据
        :return: 文件或目录项；无响应、HTTP 错误或业务失败返回 None
        """
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/get"),
            json={
                "path": path.as_posix(),
                "password": password,
                "page": page,
                "per_page": per_page,
                "refresh": refresh,
            },
        )
        if resp is None:
            logger.warn(f"【OpenList】请求获取文件 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求获取文件 {path} 失败，状态码：{resp.status_code}"
            )
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.debug(
                f"【OpenList】获取文件 {path} 失败，错误信息：{result['message']}"
            )
            return None

        return self.__build_fileitem(path, result["data"])

    def __build_fileitem(self, path: Path, data: dict[str, Any]) -> _SchemaFileItem:
        """
        根据接口返回数据构建文件项。

        :param path: 文件路径
        :param data: 接口返回的 data 字段
        :return: 文件项
        """
        return _SchemaFileItem(
            storage=self.schema.value,
            type="dir" if data["is_dir"] else "file",
            path=path.as_posix() + ("/" if data["is_dir"] else ""),
            name=data["name"],
            basename=Path(data["name"]).stem,
            extension=Path(data["name"]).suffix[1:],
            size=data["size"],
            modify_time=self.__parse_timestamp(data["modified"]),
            thumbnail=data["thumb"],
        )

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询路径并区分服务端明确不存在与其他故障。

        :param path: 需要确认状态的路径
        :return: 文件或目录项；错误说明包含 not found 或 not exist 时返回 None
        :raises StorageQueryError: 无响应、HTTP 错误、JSON 解析失败或其他业务错误
        """
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/get"),
            json={
                "path": path.as_posix(),
                "password": "",
                "page": 1,
                "per_page": 0,
                "refresh": False,
            },
        )
        if resp is None:
            raise StorageQueryError(f"【OpenList】查询文件 {path} 失败，无法连接服务")
        if resp.status_code != 200:
            raise StorageQueryError(f"【OpenList】查询文件 {path} 失败，状态码：{resp.status_code}")
        try:
            result = resp.json()
        except Exception as err:
            raise StorageQueryError(f"【OpenList】解析查询结果失败: {path} - {err}") from err
        if result.get("code") != 200:
            message = str(result.get("message") or "")
            if "not found" in message.lower() or "not exist" in message.lower():
                return None
            raise StorageQueryError(f"【OpenList】查询文件 {path} 失败：{message}")
        return self.__build_fileitem(path, result["data"])

    def get_parent(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        按存储项路径查询父目录。

        :param fileitem: 目标存储项
        :return: 父目录信息；普通查询无结果时返回 None
        """
        if not fileitem.path:
            return None
        return self.get_folder(Path(fileitem.path).parent)

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        调用删除接口移除父目录中的指定名称。

        :param fileitem: 目标存储项；优先使用 name，否则取 path 的末段
        :return: HTTP 与业务状态均为 200 返回 True；名称为空或接口失败返回 False
        """
        if not fileitem.path:
            return False
        path = Path(fileitem.path)
        name = fileitem.name or path.name
        if not name:
            logger.warn(f"【OpenList】删除路径 {fileitem.path} 无效")
            return False

        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/remove"),
            json={
                "dir": path.parent.as_posix(),
                "names": [name],
            },
        )
        if resp is None:
            logger.warn(
                f"【OpenList】请求删除文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求删除文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False
        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】删除文件 {fileitem.path} 失败，错误信息：{result['message']}"
            )
            return False
        return True

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        请求服务端修改存储项名称。

        :param fileitem: 原存储项
        :param name: 目标名称
        :return: HTTP 与业务状态均为 200 返回 True，否则返回 False；不原地修改输入项
        """
        if not fileitem.path:
            return False
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/rename"),
            json={
                "name": name,
                "path": fileitem.path,
            },
        )
        if not resp:
            logger.warn(
                f"【OpenList】请求重命名文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求重命名文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】重命名文件 {fileitem.path} 失败，错误信息：{result['message']}"
            )
            return False

        return True

    def download(
        self,
        fileitem: _SchemaFileItem,
        path: Optional[Path] = None,
        password: Optional[str] = "",
    ) -> Optional[Path]:
        """
        取得下载地址后流式写入本地文件。

        文件下载不发送 OpenList API 令牌，使用服务端返回的直链或签名地址。
        只有流正常结束且已知大小匹配才返回成功；中断、取消或截断均删除未完成文件。

        :param fileitem: 源远端文件，提供文件名称和可选的预期大小
        :param path: 本地保存目录；为空时使用 TEMP_PATH
        :param password: 详情查询使用的可选访问密码
        :return: 完整本地目标路径；详情查询、传输、大小校验失败或取消返回 None
        """
        if not fileitem.path:
            return None
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/get"),
            json={
                "path": fileitem.path,
                "password": password,
                "page": 1,
                "per_page": 0,
                "refresh": False,
            },
        )
        if not resp:
            logger.warn(f"【OpenList】请求获取文件 {path} 失败，无法连接alist服务")
            return None
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求获取文件 {path} 失败，状态码：{resp.status_code}"
            )
            return None

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】获取文件 {path} 失败，错误信息：{result['message']}"
            )
            return None

        if result["data"]["raw_url"]:
            download_url = result["data"]["raw_url"]
        else:
            download_url = UrlUtils.adapt_request_url(
                self.__get_base_url, f"/d{fileitem.path}"
            )
            if download_url and result["data"]["sign"]:
                download_url = download_url + "?sign=" + result["data"]["sign"]

        local_path = self._build_download_path(fileitem, path or get_runtime_setting('TEMP_PATH'))
        if local_path is None or not download_url:
            return None

        # 下载使用 raw_url 或带 sign 的 /d 地址，API 令牌不属于文件服务及其重定向目标。
        request_utils = RequestUtils()
        try:
            with request_utils.get_stream(download_url, raise_exception=True) as r:
                if r is None:
                    return None
                r.raise_for_status()
                downloaded_size = 0
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if runtime_stop_state.consume_transfer_stop(fileitem.path):
                            logger.info(f"【OpenList】{fileitem.path} 下载已取消！")
                            raise IOError("OpenList 下载已取消")
                        f.write(chunk)
                        downloaded_size += len(chunk)
                expected_size = fileitem.size
                if expected_size is None:
                    expected_size = result["data"].get("size")
                if expected_size is None and r.headers.get("Content-Length"):
                    expected_size = int(r.headers["Content-Length"])
                if expected_size is not None and downloaded_size != expected_size:
                    raise IOError(f"下载大小不一致：预期 {expected_size}，实际 {downloaded_size}")
        except Exception as e:
            logger.error(f"【OpenList】下载文件 {fileitem.path} 失败：{e}")
            local_path.unlink(missing_ok=True)
            return None

        return local_path

    def upload(
        self,
        fileitem: _SchemaFileItem,
        path: Path,
        new_name: Optional[str] = None,
        task: bool = False,
    ) -> Optional[_SchemaFileItem]:
        """
        以支持取消和进度的文件流上传，并延迟查询目标元数据。

        :param fileitem: 远端目标目录
        :param path: 本地源文件路径；发送后保留
        :param new_name: 可选目标名称，默认使用本地文件名
        :param task: 是否要求服务端作为后台任务处理；默认 False，避免后续操作抢先执行
        :return: 上传后查询到的文件项；上传、取消或元数据确认失败返回 None
        """
        if not fileitem.path:
            return None
        try:
            target_name = new_name or path.name
            target_path = Path(fileitem.path) / target_name
            stat = path.stat()

            progress_callback = transfer_process(path.as_posix())

            encoded_path = UrlUtils.quote(target_path.as_posix())
            headers = self.__get_header_with_token()
            headers.setdefault("Content-Type", "application/octet-stream")
            headers.setdefault("As-Task", str(task).lower())
            headers.setdefault("File-Path", encoded_path)
            headers.setdefault("Content-Length", str(stat.st_size))
            headers.setdefault("Last-Modified", str(int(stat.st_mtime * 1000)))
            headers.update(self.__get_upload_hash_headers(path))

            class ProgressFileReader:
                """
                为上传请求提供可计长、支持取消及进度回调的文件读取器。
                """
                def __init__(
                    self, file_path: Path, callback: Optional[Callable[[int | float], None]]
                ) -> None:
                    """
                    打开源文件并记录上传进度回调。

                    :param file_path: 需要读取的本地文件，调用方负责 close 释放句柄
                    :param callback: 接收百分比的回调，可为空
                    """
                    self.file = open(file_path, "rb")
                    self.callback = callback
                    self.uploaded_size = 0
                    self.file_size = file_path.stat().st_size

                def __len__(self) -> int:
                    """
                    提供 HTTP 上传所需的完整内容长度。

                    :return: 初始化时读取的文件字节数
                    """
                    return self.file_size

                def read(self, size: int = -1) -> bytes:
                    """
                    读取上传内容并报告累计进度。

                    :param size: 最多读取的字节数；默认 -1 表示读取剩余内容
                    :return: 本次读取的字节；到文件末尾返回空字节
                    :raises OperationInterrupted: 消费到该传输的停止信号
                    """
                    if runtime_stop_state.consume_transfer_stop(path.as_posix()):
                        logger.info(f"【OpenList】{path} 上传已取消！")
                        raise OperationInterrupted(f"Upload cancelled: {path}")
                    chunk = self.file.read(size)
                    if chunk:
                        self.uploaded_size += len(chunk)
                        if self.callback:
                            percent = (self.uploaded_size * 100) / self.file_size
                            self.callback(percent)
                    return chunk

                def close(self) -> None:
                    """
                    关闭上传源文件句柄。
                    """
                    self.file.close()

            progress_reader = ProgressFileReader(path, progress_callback)
            try:
                resp = RequestUtils(headers=headers, timeout=6000).put_res(
                    self.__get_api_url("/api/fs/put"),
                    data=progress_reader,
                )
            except OperationInterrupted:
                return None
            finally:
                progress_reader.close()

            if resp is None:
                logger.warn(f"【OpenList】请求上传文件 {path} 失败")
                return None
            if resp.status_code != 200:
                logger.warn(
                    f"【OpenList】请求上传文件 {path} 失败，状态码：{resp.status_code}"
                )
                return None

            progress_callback(100)

            new_item = self._delay_get_item(target_path, refresh=True)
            if new_item and new_item.path and new_name and new_name != path.name:
                if self.rename(new_item, new_name):
                    return self._delay_get_item(
                        Path(new_item.path).with_name(new_name), refresh=True
                    )

            return new_item

        except Exception as e:
            logger.error(f"【OpenList】上传文件 {path} 失败：{e}")
            return None

    @staticmethod
    def __get_upload_hash_headers(path: Path) -> dict[str, str]:
        """
        计算 OpenList 秒传所需的文件哈希请求头。
        """
        md5_hash = hashlib.md5()
        sha1_hash = hashlib.sha1()
        sha256_hash = hashlib.sha256()
        with open(path, "rb") as file_handler:
            while True:
                chunk = file_handler.read(1024 * 1024)
                if not chunk:
                    break
                md5_hash.update(chunk)
                sha1_hash.update(chunk)
                sha256_hash.update(chunk)
        return {
            "X-File-Md5": md5_hash.hexdigest(),
            "X-File-Sha1": sha1_hash.hexdigest(),
            "X-File-Sha256": sha256_hash.hexdigest(),
        }

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        按存储项路径重新读取详情。

        :param fileitem: 目标文件或目录项
        :return: 最新文件项；普通查询失败返回 None
        """
        if not fileitem.path:
            return None
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str, **_kwargs: Any) -> bool:
        """
        请求服务端复制，再尽力把目标改为新名称。

        不在此方法确认异步复制已经完成，调用方需要目标信息时应使用 copy_item。

        :param fileitem: 源存储项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 复制接口成功返回 True，接口失败返回 False；后续重命名失败不改变成功结果
        """
        if not fileitem.path or not fileitem.name:
            return False
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/copy"),
            json={
                "src_dir": Path(fileitem.path).parent.as_posix(),
                "dst_dir": path.as_posix(),
                "names": [fileitem.name],
            },
        )
        if resp is None:
            logger.warn(
                f"【OpenList】请求复制文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求复制文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】复制文件 {fileitem.path} 失败，错误信息：{result['message']}"
            )
            return False
        if fileitem.name != new_name:
            new_item = self._delay_get_item(path / fileitem.name, refresh=True)
            if new_item:
                self.rename(new_item, new_name)
        return True

    def copy_item(
            self, fileitem: _SchemaFileItem, path: Path, new_name: str
    ) -> Optional[_SchemaFileItem]:
        """
        复制并查询目标项，兼容远端元数据暂不可见。

        :param fileitem: 源存储项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 目标项；同名复制成功但暂不可见时按源元数据构造，改名无法确认时返回 None
        """
        if not fileitem.path or not fileitem.name:
            return None
        if not self.copy(fileitem=fileitem, path=path, new_name=new_name):
            return None
        target_path = path / new_name
        target_item = self._delay_get_item(target_path, refresh=True)
        if target_item:
            return target_item
        if fileitem.name == new_name:
            return self.__build_transfer_item(fileitem, target_path)

        copied_item = self._delay_get_item(path / fileitem.name, refresh=True)
        if copied_item and self.rename(copied_item, new_name):
            return self._delay_get_item(
                target_path, refresh=True
            ) or self.__build_transfer_item(fileitem, target_path)
        return None

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        先请求源项改名，再请求服务端移动目标名称。

        :param fileitem: 源存储项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 移动接口成功返回 True，否则返回 False；前置重命名的布尔结果不单独检查
        """
        if not fileitem.path or not fileitem.name:
            return False
        if fileitem.name != new_name:
            self.rename(fileitem, new_name)
        resp = RequestUtils(headers=self.__get_header_with_token()).post_res(
            self.__get_api_url("/api/fs/move"),
            json={
                "src_dir": Path(fileitem.path).parent.as_posix(),
                "dst_dir": path.as_posix(),
                "names": [new_name],
            },
        )
        if resp is None:
            logger.warn(
                f"【OpenList】请求移动文件 {fileitem.path} 失败，无法连接alist服务"
            )
            return False
        if resp.status_code != 200:
            logger.warn(
                f"【OpenList】请求移动文件 {fileitem.path} 失败，状态码：{resp.status_code}"
            )
            return False

        result = resp.json()
        if result["code"] != 200:
            logger.warn(
                f"【OpenList】移动文件 {fileitem.path} 失败，错误信息：{result['message']}"
            )
            return False
        return True

    def move_item(
            self, fileitem: _SchemaFileItem, path: Path, new_name: str
    ) -> Optional[_SchemaFileItem]:
        """
        移动并查询目标项，元数据暂不可见时根据源项构造。

        :param fileitem: 源存储项
        :param path: 远端目标目录
        :param new_name: 目标文件名
        :return: 目标项；移动接口失败返回 None，构造结果不代表已经独立核验远端状态
        """
        if not self.move(fileitem=fileitem, path=path, new_name=new_name):
            return None
        target_path = path / new_name
        return self._delay_get_item(target_path, refresh=True) or self.__build_transfer_item(
            fileitem, target_path
        )

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留硬链接接口，OpenList 当前不实现该操作。

        :param fileitem: 兼容接口的源文件项，当前忽略
        :param target_file: 兼容接口的目标路径，当前忽略
        :return: 固定返回 False
        """
        return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留符号链接接口，OpenList 当前不实现该操作。

        :param fileitem: 兼容接口的源文件项，当前忽略
        :param target_file: 兼容接口的目标路径，当前忽略
        :return: 固定返回 False
        """
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        保留容量查询接口，OpenList 当前不提供汇总容量。

        :return: 固定返回 None
        """
        pass

    @staticmethod
    def __parse_timestamp(time_str: str) -> float:
        """
        直接使用 ISO 8601 格式解析时间。
        """
        return datetime.fromisoformat(time_str).timestamp()
