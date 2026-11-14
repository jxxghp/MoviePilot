"""
阿里云盘开放接口的授权、文件管理和分片传输适配。
"""

import base64
import hashlib
import secrets
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Union, cast, overload

if TYPE_CHECKING:
    from requests import Response

from app.adapters.network.http import HttpRequestError, RequestUtils
from app.foundation import temporal as time_tools
from app.foundation.singleton import WeakSingleton
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema

lock = threading.Lock()


class NoCheckInException(Exception):
    """
    阿里云盘尚未登录或缺少已授权存储空间。
    """
    pass


class SessionInvalidException(Exception):
    """
    设备授权状态缺失或刷新凭据失效。
    """
    pass


class AliPan(StorageBase, metaclass=WeakSingleton):
    """
    通过阿里云盘开放接口管理文件、设备授权和分片传输。

    访问令牌按需刷新并写回存储配置；文件操作使用文件标识与存储空间标识。
    复制和移动只在源文件所属空间内执行，不提供本地链接能力。
    """

    schema = StorageSchema.Alipan

    transtype = {"move": "移动", "copy": "复制"}

    base_url = "https://openapi.alipan.com"

    # 下载流每次读取 10 MiB，避免一次载入整个文件。
    chunk_size = 10 * 1024 * 1024

    def __init__(self) -> None:
        """
        创建复用的 HTTP 会话和内存中的设备授权状态。
        """
        super().__init__()
        self._auth_state: dict[str, Optional[str]] = {}
        self._request_utils = RequestUtils(
            use_session=True,
            timeout=20,
            verify=True,
        )
        self._init_session()

    @property
    def snapshot_check_folder_modtime(self) -> bool:
        """
        读取目录修改时间检查开关，使配置更新立即生效。

        :return: 是否在快照扫描时检查目录修改时间
        """
        return bool(get_runtime_setting('ALIPAN_SNAPSHOT_CHECK_FOLDER_MODTIME'))

    def _init_session(self) -> None:
        """
        为开放接口会话设置 JSON 请求头。
        """
        self._request_utils.update_headers({"Content-Type": "application/json"})

    def _check_session(self) -> None:
        """
        读取并刷新访问令牌，拒绝无可用凭据的操作。

        :raises NoCheckInException: 没有可用的访问令牌
        """
        if not self.access_token:
            raise NoCheckInException("【阿里云盘】请先扫码登录！")

    @property
    def _default_drive_id(self) -> str:
        """
        按资源库、备份盘、默认盘的优先级选择存储空间。

        :raises NoCheckInException: 配置中没有已授权的空间标识
        :return: 配置中的首个非空空间标识
        """
        conf = self.get_conf()
        drive_id = (
            conf.get("resource_drive_id")
            or conf.get("backup_drive_id")
            or conf.get("default_drive_id")
        )
        if not drive_id:
            raise NoCheckInException("【阿里云盘】请先扫码登录！")
        return cast(str, drive_id)

    @property
    def access_token(self) -> Optional[str]:
        """
        串行读取访问令牌，过期时刷新并更新配置和请求头。

        过期判断使用刷新时间的 Unix 秒数与有效秒数之和。刷新流程的异常会向上传播，
        刷新请求无响应时也不保证返回 None。

        :return: 可用访问令牌；未配置令牌时返回 None
        """
        with lock:
            tokens = self.get_conf()
            refresh_token = tokens.get("refresh_token")
            expires_in = tokens.get("expires_in", 0)
            refresh_time = tokens.get("refresh_time", 0)
            if expires_in and refresh_time + expires_in < int(time.time()):
                if not isinstance(refresh_token, str) or not refresh_token:
                    raise SessionInvalidException("【阿里云盘】会话失效，请重新扫码登录！")
                refreshed_tokens = self.__refresh_access_token(refresh_token)
                if not refreshed_tokens:
                    return None
                tokens = {**tokens, **refreshed_tokens}
                self.set_config({**tokens, "refresh_time": int(time.time())})
            access_token = tokens.get("access_token")
            if access_token:
                self._request_utils.update_headers(
                    {"Authorization": f"Bearer {access_token}"}
                )
            return access_token

    def generate_qrcode(self) -> Tuple[dict[str, Any], str]:
        """
        创建使用 PKCE plain 校验方式的设备授权二维码。

        会话标识和校验值仅保存在当前实例内存，后续轮询依赖同一实例。

        :return: 二维码地址字典和空错误文本；请求失败时返回空字典与错误原因
        """

        code_verifier = secrets.token_urlsafe(96)[:128]
        resp = self._request_utils.post_res(
            f"{self.base_url}/oauth/authorize/qrcode",
            json={
                "client_id": get_runtime_setting('ALIPAN_APP_ID'),
                "scopes": [
                    "user:base",
                    "file:all:read",
                    "file:all:write",
                    "file:share:write",
                ],
                "code_challenge": code_verifier,
                "code_challenge_method": "plain",
            },
        )
        if resp is None:
            return {}, "网络错误"
        result = resp.json()
        if not isinstance(result, dict):
            raise SessionInvalidException("【阿里云盘】授权接口返回无效数据")
        if result.get("code"):
            return {}, str(result.get("message") or "生成二维码失败")
        self._auth_state = {"sid": result.get("sid"), "code_verifier": code_verifier}
        return {"codeUrl": result.get("qrCodeUrl")}, ""

    def check_login(self) -> Optional[Tuple[dict[str, Any], str]]:
        """
        轮询二维码状态，登录成功后持久化令牌和授权空间信息。

        成功状态来自扫码接口；令牌交换异常仍可能向上传播。

        :return: 状态及提示字典与空错误文本；缺少二维码或请求异常时返回空字典和原因
        """

        _status_text = {
            "WaitLogin": "等待登录",
            "ScanSuccess": "扫码成功",
            "LoginSuccess": "登录成功",
            "QRCodeExpired": "二维码过期",
        }

        if not self._auth_state:
            return {}, "生成二维码失败"
        try:
            resp = self._request_utils.get_res(
                f"{self.base_url}/oauth/qrcode/{self._auth_state['sid']}/status"
            )
            if resp is None:
                return {}, "网络错误"
            result = resp.json()
            if not isinstance(result, dict):
                raise SessionInvalidException("【阿里云盘】授权接口返回无效数据")
            status = result.get("status")
            if status == "LoginSuccess":
                authCode = result.get("authCode")
                self._auth_state["authCode"] = authCode
                tokens = self.__get_access_token()
                if tokens:
                    self.set_config({"refresh_time": int(time.time()), **tokens})
                    self.__get_drive_id()
            return {"status": status, "tip": _status_text.get(str(status), "未知错误")}, ""
        except HttpRequestError as e:
            return {}, str(e)

    def __get_access_token(self) -> dict[str, Any]:
        """
        以扫码授权码和内存中的校验值换取令牌。

        :raises SessionInvalidException: 没有设备授权状态或请求未收到响应
        :raises Exception: 开放接口返回业务错误
        :return: 开放接口返回的令牌字典
        """
        if not self._auth_state:
            raise SessionInvalidException("【阿里云盘】请先生成二维码")
        resp = self._request_utils.post_res(
            f"{self.base_url}/oauth/access_token",
            json={
                "client_id": get_runtime_setting('ALIPAN_APP_ID'),
                "grant_type": "authorization_code",
                "code": self._auth_state["authCode"],
                "code_verifier": self._auth_state["code_verifier"],
            },
        )
        if resp is None:
            raise SessionInvalidException("【阿里云盘】获取 access_token 失败")
        result = resp.json()
        if not isinstance(result, dict):
            raise SessionInvalidException("【阿里云盘】授权接口返回无效数据")
        if result.get("code"):
            raise Exception(
                f"【阿里云盘】{result.get('code')} - {result.get('message')}！"
            )
        return result

    def __refresh_access_token(self, refresh_token: str) -> Optional[dict[str, Any]]:
        """
        使用刷新令牌获取新凭据。

        :raises SessionInvalidException: 刷新令牌为空
        :param refresh_token: 此前授权返回的刷新令牌
        :return: 响应字典；请求无响应时返回 None，业务错误字典仍会返回
        """
        if not refresh_token:
            raise SessionInvalidException("【阿里云盘】会话失效，请重新扫码登录！")
        resp = self._request_utils.post_res(
            f"{self.base_url}/oauth/access_token",
            json={
                "client_id": get_runtime_setting('ALIPAN_APP_ID'),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        if resp is None:
            logger.error(
                "【阿里云盘】刷新 access_token 失败：未收到响应"
            )
            return None
        result = resp.json()
        if not isinstance(result, dict):
            raise SessionInvalidException("【阿里云盘】授权接口返回无效数据")
        if result.get("code"):
            logger.warn(
                f"【阿里云盘】刷新 access_token 失败：{result.get('code')} - {result.get('message')}！"
            )
        return result

    def __get_drive_id(self) -> None:
        """
        查询已授权空间并合并写入当前存储配置。

        请求失败或返回业务错误时只记录日志，不更新配置。
        """
        resp = self._request_utils.post_res(
            f"{self.base_url}/adrive/v1.0/user/getDriveInfo"
        )
        if resp is None:
            logger.error("获取默认存储桶ID失败")
            return None
        result = resp.json()
        if not isinstance(result, dict):
            raise SessionInvalidException("【阿里云盘】授权接口返回无效数据")
        if result.get("code"):
            logger.warn(
                f"获取默认存储ID失败：{result.get('code')} - {result.get('message')}！"
            )
            return None
        conf = self.get_conf()
        conf.update(result)
        self.set_config(conf)
        return None

    @overload
    def _request_api(
        self, method: str, endpoint: str, result_key: None = None,
        **kwargs: Any,
    ) -> Optional[dict[str, Any]]:
        """
        返回开放接口的完整 JSON 对象。
        """
        ...

    @overload
    def _request_api(
        self, method: str, endpoint: str, result_key: Optional[str],
        **kwargs: Any,
    ) -> Optional[Union[dict[str, Any], list[Any]]]:
        """
        返回调用方选取的 JSON 对象或列表字段。
        """
        ...

    def _request_api(
        self, method: str, endpoint: str, result_key: Optional[str] = None, **kwargs: Any
    ) -> Optional[Union[dict[str, Any], list[Any]]]:
        """
        调用开放接口，并在 HTTP 限流后等待重试。

        先校验登录状态；HTTP 429 的 X-RateLimit-Reset 按等待秒数处理并额外等待 5 秒，
        递归重试没有固定次数上限。业务错误仅记录日志，不统一转成异常。

        :param method: HTTP 方法
        :param endpoint: 以斜杠开头的开放接口相对路径
        :param result_key: 需要提取的响应字段；为空时返回整个响应
        :param kwargs: 传递给 HTTP 客户端的选项；no_error_log 控制本次业务错误日志
        :return: 响应对象或指定字段；网络异常或无响应时返回 None
        """
        self._check_session()

        no_error_log = kwargs.pop("no_error_log", False)

        try:
            resp = self._request_utils.request(
                method,
                f"{self.base_url}{endpoint}",
                raise_exception=True,
                **kwargs,
            )
        except Exception as e:
            logger.error(f"【阿里云盘】{method} 请求 {endpoint} 网络错误: {str(e)}")
            return None

        if resp is None:
            logger.warn(f"【阿里云盘】{method} 请求 {endpoint} 失败！")
            return None

        if resp.status_code == 429:
            reset_time = int(resp.headers.get("X-RateLimit-Reset", 60))
            time.sleep(reset_time + 5)
            return self._request_api(method, endpoint, result_key, **kwargs)

        ret_data = resp.json()
        if not isinstance(ret_data, dict):
            logger.warning("开放接口返回了非对象 JSON：%s", endpoint)
            return None
        if ret_data.get("code"):
            if not no_error_log:
                logger.warn(
                    f"【阿里云盘】{method} {endpoint} 返回：{ret_data.get('code')} {ret_data.get('message')}"
                )

        if result_key:
            value = ret_data.get(result_key)
            return value if isinstance(value, (dict, list)) else None
        return ret_data

    def __get_fileitem(self, fileinfo: dict[str, Any], parent: str = "/") -> _SchemaFileItem:
        """
        将开放接口文件元数据转换为存储文件项。

        :param fileinfo: 接口返回的文件或目录字典
        :param parent: 父目录路径，默认根目录
        :return: 包含空间标识和 Unix 修改时间的文件项；输入为空时返回空文件项
        """
        if not fileinfo:
            return _SchemaFileItem()
        if not parent.endswith("/"):
            parent += "/"
        if fileinfo.get("type") == "folder":
            return _SchemaFileItem(
                storage=self.schema.value,
                fileid=fileinfo.get("file_id"),
                parent_fileid=fileinfo.get("parent_file_id"),
                type="dir",
                path=f"{parent}{fileinfo.get('name')}" + "/",
                name=fileinfo.get("name"),
                basename=fileinfo.get("name"),
                size=fileinfo.get("size"),
                modify_time=time_tools.parse_timestamp(str(fileinfo.get("updated_at") or "")),
                drive_id=fileinfo.get("drive_id"),
            )
        else:
            return _SchemaFileItem(
                storage=self.schema.value,
                fileid=fileinfo.get("file_id"),
                parent_fileid=fileinfo.get("parent_file_id"),
                type="file",
                path=f"{parent}{fileinfo.get('name')}",
                name=fileinfo.get("name"),
                basename=Path(str(fileinfo.get("name") or "")).stem,
                size=fileinfo.get("size"),
                extension=fileinfo.get("file_extension"),
                modify_time=time_tools.parse_timestamp(str(fileinfo.get("updated_at") or "")),
                thumbnail=fileinfo.get("thumbnail"),
                drive_id=fileinfo.get("drive_id"),
            )

    @staticmethod
    def _calc_sha1(filepath: Path, size: Optional[int] = None) -> str:
        """
        计算文件全部内容或指定前缀的 SHA1 摘要。

        :param filepath: 待读取的本地文件
        :param size: 读取的前缀字节数；None 或 0 表示读取全部内容
        :return: 十六进制摘要
        """
        sha1 = hashlib.sha1()
        with open(filepath, "rb") as f:
            if size:
                chunk = f.read(size)
                sha1.update(chunk)
            else:
                while chunk := f.read(8192):
                    sha1.update(chunk)
        return sha1.hexdigest()

    def init_storage(self) -> None:
        """
        保留存储初始化入口；本后端无需额外初始化。
        """
        pass

    def close(self) -> None:
        """
        关闭阿里云盘 HTTP 客户端。
        """
        self._request_utils.close()

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        分页列出目录直接子项，文件输入则刷新自身详情。

        每页最多读取 100 项，没有下一页标记时停止；无响应抛出 FileNotFoundError。
        业务错误未统一抛出，可能返回已获取的部分结果。

        :param fileitem: 待查询项；非根目录必须带有文件标识和空间标识
        :return: 已读取的文件项列表
        """
        if fileitem.type == "file":
            item = self.detail(fileitem)
            if item:
                return [item]
            return []

        parent_file_id: Optional[str]
        drive_id: Optional[str]
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
                },
            )
            if resp is None:
                raise FileNotFoundError(f"【阿里云盘】{fileitem.path} 检索出错！")
            if not resp:
                break
            next_marker = resp.get("next_marker")
            current_items = resp.get("items") or []
            for item in current_items:
                items.append(self.__get_fileitem(item, parent=str(fileitem.path)))
            if not next_marker:
                break
        return items

    def _delay_get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        等待云端索引可见后按默认空间路径查询目标。

        每次查询前等待 2 秒，最多查询两次。

        :param path: 目标路径
        :return: 首次查到的文件项；两次查询均未查到时返回 None
        """
        for _ in range(2):
            time.sleep(2)
            fileitem = self.get_item(path)
            if fileitem:
                return fileitem
        return None

    def create_folder(
        self, parent_item: _SchemaFileItem, name: str
    ) -> Optional[_SchemaFileItem]:
        """
        在父目录所属空间创建目录并等待元数据可见。

        创建后的路径查询使用默认空间。

        :param parent_item: 父目录，提供空间和目录标识；无目录标识时使用根目录
        :param name: 待创建的直接子目录名
        :return: 延迟查询到的目录项；创建或查询失败时返回 None
        """
        if not parent_item.path:
            return None
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/create",
            json={
                "drive_id": parent_item.drive_id,
                "parent_file_id": parent_item.fileid or "root",
                "name": name,
                "type": "folder",
            },
        )
        if not resp:
            return None
        if resp.get("code"):
            logger.warn(f"【阿里云盘】创建目录失败: {resp.get('message')}")
            return None
        new_path = Path(parent_item.path) / name
        return self._delay_get_item(new_path)

    @staticmethod
    def _calculate_pre_hash(file_path: Path) -> str:
        """
        计算秒传预检查使用的文件前 1024 字节摘要。

        :param file_path: 待上传的本地文件
        :return: SHA1 十六进制摘要
        """
        sha1 = hashlib.sha1()
        with open(file_path, "rb") as f:
            data = f.read(1024)
            sha1.update(data)
        return sha1.hexdigest()

    def _calculate_proof_code(self, file_path: Path) -> str:
        """
        按访问令牌确定抽样偏移，生成秒传内容证明。

        取访问令牌 MD5 的前 16 位十六进制数对文件长度取模，读取该位置起最多 8 字节。

        :param file_path: 待上传的本地文件
        :return: 最多 8 字节样本的 Base64 文本；空文件返回空字符串
        """
        file_size = file_path.stat().st_size
        if file_size == 0:
            return ""

        access_token = self.access_token
        if not access_token:
            raise SessionInvalidException("【阿里云盘】会话失效，请重新扫码登录！")
        md5 = hashlib.md5(access_token.encode()).hexdigest()
        hex_str = md5[:16]

        try:
            tmp_int = int(hex_str, 16)
        except ValueError:
            raise ValueError(
                "【阿里云盘】Invalid hex string for proof code calculation"
            )

        index = tmp_int % file_size
        start = index
        end = index + 8
        if end > file_size:
            end = file_size

        with open(file_path, "rb") as f:
            f.seek(start)
            chunk = f.read(end - start)

        return base64.b64encode(chunk).decode()

    @staticmethod
    def _calculate_content_hash(file_path: Path) -> str:
        """
        计算秒传校验使用的完整文件 SHA1 摘要。

        :param file_path: 待上传的本地文件
        :return: 十六进制摘要
        """
        sha1 = hashlib.sha1()
        with open(file_path, "rb") as f:
            while True:
                chunk = f.read(8192)
                if not chunk:
                    break
                sha1.update(chunk)
        return sha1.hexdigest()

    def _create_file(
        self,
        drive_id: str,
        parent_file_id: str,
        file_name: str,
        file_path: Path,
        check_name_mode: str = "refuse",
        chunk_size: int = 1 * 1024 * 1024 * 1024,
    ) -> dict[str, Any]:
        """
        创建远端上传任务，预摘要匹配时提交秒传证明。

        首次预摘要匹配后计算完整摘要并再次请求；无响应或秒传确认业务失败会抛出异常。

        :param drive_id: 目标空间标识
        :param parent_file_id: 目标父目录标识
        :param file_name: 远端文件名
        :param file_path: 用于计算长度和内容摘要的本地文件
        :param check_name_mode: 同名处理模式，默认 refuse 拒绝同名
        :param chunk_size: 上传分片字节数，默认 1 GiB
        :return: 创建或秒传确认响应，包含文件和上传任务信息
        """
        file_size = file_path.stat().st_size
        pre_hash = self._calculate_pre_hash(file_path)
        num_parts = (file_size + chunk_size - 1) // chunk_size

        part_info_list = [{"part_number": i + 1} for i in range(num_parts)]

        data = {
            "drive_id": drive_id,
            "parent_file_id": parent_file_id,
            "name": file_name,
            "type": "file",
            "check_name_mode": check_name_mode,
            "size": file_size,
            "pre_hash": pre_hash,
            "part_info_list": part_info_list,
        }
        resp = self._request_api("POST", "/adrive/v1.0/openFile/create", json=data)
        if not resp:
            raise Exception("【阿里云盘】创建文件失败！")
        if resp.get("code") == "PreHashMatched":
            proof_code = self._calculate_proof_code(file_path)
            content_hash = self._calculate_content_hash(file_path)
            data.pop("pre_hash")
            data.update(
                {
                    "proof_code": proof_code,
                    "proof_version": "v1",
                    "content_hash": content_hash,
                    "content_hash_name": "sha1",
                }
            )
            resp = self._request_api("POST", "/adrive/v1.0/openFile/create", json=data)
            if not resp:
                raise Exception("【阿里云盘】创建文件失败！")
            if resp.get("code"):
                raise Exception(resp.get("message"))
        return resp

    def _refresh_upload_urls(
        self, drive_id: str, file_id: str, upload_id: str, part_numbers: List[int]
    ) -> List[dict[str, Any]]:
        """
        为指定分片重新申请上传地址。

        :param drive_id: 目标空间标识
        :param file_id: 远端文件标识
        :param upload_id: 上传任务标识
        :param part_numbers: 需要刷新地址的分片编号，从 1 开始
        :return: 分片信息列表；请求失败或业务错误时抛出异常
        """
        data = {
            "drive_id": drive_id,
            "file_id": file_id,
            "upload_id": upload_id,
            "part_info_list": [{"part_number": num} for num in part_numbers],
        }
        resp = self._request_api(
            "POST", "/adrive/v1.0/openFile/getUploadUrl", json=data
        )
        if not resp:
            raise Exception("【阿里云盘】刷新分片上传地址失败！")
        if resp.get("code"):
            raise Exception(resp.get("message"))
        return cast(list[dict[str, Any]], resp.get("part_info_list", []))

    @staticmethod
    def _upload_part(upload_url: str, data: bytes) -> Optional[Response]:
        """
        通过独立 HTTP 请求上传一个分片。

        :param upload_url: 开放接口颁发的分片上传地址
        :param data: 当前分片的原始字节
        :return: HTTP 响应；调用方负责检查状态码并决定重试
        """
        return RequestUtils(timeout=60, verify=True).put_res(upload_url, data=data)

    def _list_uploaded_parts(self, drive_id: str, file_id: str, upload_id: str) -> dict[str, Any]:
        """
        查询上传任务已经接收的分片以便跳过重复传输。

        :param drive_id: 目标空间标识
        :param file_id: 远端文件标识
        :param upload_id: 上传任务标识
        :return: 含已上传分片信息的响应；失败时抛出异常
        """
        data = {"drive_id": drive_id, "file_id": file_id, "upload_id": upload_id}
        resp = self._request_api(
            "POST", "/adrive/v1.0/openFile/listUploadedParts", json=data
        )
        if not resp:
            raise Exception("【阿里云盘】获取已上传分片失败！")
        if resp.get("code"):
            raise Exception(resp.get("message"))
        return resp

    def _complete_upload(self, drive_id: str, file_id: str, upload_id: str) -> dict[str, Any]:
        """
        请求云端合并分片并完成上传。

        :param drive_id: 目标空间标识
        :param file_id: 远端文件标识
        :param upload_id: 上传任务标识
        :return: 完成后的文件信息响应；失败时抛出异常
        """
        data = {"drive_id": drive_id, "file_id": file_id, "upload_id": upload_id}
        resp = self._request_api("POST", "/adrive/v1.0/openFile/complete", json=data)
        if not resp:
            raise Exception("【阿里云盘】完成上传失败！")
        if resp.get("code"):
            raise Exception(resp.get("message"))
        return resp

    def upload(
        self,
        target_dir: _SchemaFileItem,
        local_path: Path,
        new_name: Optional[str] = None,
    ) -> Optional[_SchemaFileItem]:
        """
        上传本地文件，优先秒传并支持跳过已接收的分片。

        按 10 MiB 分片，每片最多尝试 3 次，重试时刷新上传地址。
        取消时保留远端未完成任务；分片或合并失败可抛出异常。

        :param target_dir: 目标目录，提供空间标识和目录标识
        :param local_path: 本地源文件；上传后保留原文件
        :param new_name: 目标文件名；为空时沿用源文件名
        :return: 上传后的文件项；取消、创建失败或目标暂不可见时可能返回 None
        """
        target_dir_path = cast(str, target_dir.path)
        drive_id = cast(str, target_dir.drive_id)
        parent_file_id = cast(str, target_dir.fileid)
        target_name = new_name or local_path.name
        target_path = Path(target_dir_path) / target_name
        file_size = local_path.stat().st_size

        chunk_size = 10 * 1024 * 1024
        create_res = self._create_file(
            drive_id=drive_id,
            parent_file_id=parent_file_id,
            file_name=target_name,
            file_path=local_path,
            chunk_size=chunk_size,
        )
        if create_res.get("rapid_upload", False):
            logger.info(f"【阿里云盘】{target_name} 秒传完成！")
            return self._delay_get_item(target_path)

        if create_res.get("exist", False):
            logger.info(f"【阿里云盘】{target_name} 已存在")
            return self.get_item(target_path)

        file_id = create_res.get("file_id")
        if not file_id:
            logger.warn(f"【阿里云盘】创建 {target_name} 文件失败！")
            return None
        upload_id = cast(str, create_res.get("upload_id"))
        part_info_list = cast(list[dict[str, Any]], create_res.get("part_info_list", []))
        uploaded_parts = set()

        uploaded_info = self._list_uploaded_parts(
            drive_id=drive_id, file_id=file_id, upload_id=upload_id
        )
        for part in uploaded_info.get("uploaded_parts", []):
            uploaded_parts.add(part["part_number"])

        logger.info(
            f"【阿里云盘】开始上传: {local_path} -> {target_path}，分片数：{len(part_info_list)}"
        )
        progress_callback = transfer_process(local_path.as_posix())

        uploaded_size = 0
        with open(local_path, "rb") as f:
            for part_info in part_info_list:
                if runtime_stop_state.consume_transfer_stop(local_path.as_posix()):
                    logger.info(f"【阿里云盘】{target_name} 上传已取消！")
                    return None

                part_num = part_info["part_number"]
                start = (part_num - 1) * chunk_size
                end = min(start + chunk_size, file_size)
                current_chunk_size = end - start

                if part_num in uploaded_parts:
                    uploaded_size += current_chunk_size
                    progress_callback((uploaded_size * 100) / file_size)
                    continue

                f.seek(start)
                data = f.read(current_chunk_size)

                success = False
                for attempt in range(3):
                    try:
                        if attempt > 0:
                            new_urls = self._refresh_upload_urls(
                                drive_id=drive_id,
                                file_id=file_id,
                                upload_id=upload_id,
                                part_numbers=[part_num],
                            )
                            upload_url = new_urls[0]["upload_url"]
                        else:
                            upload_url = part_info["upload_url"]
                        logger.info(
                            f"【阿里云盘】开始 第{attempt + 1}次 上传 {target_name} 分片 {part_num} ..."
                        )
                        response = self._upload_part(upload_url=upload_url, data=data)
                        if response is None:
                            continue
                        if response.status_code == 200:
                            success = True
                            break
                        else:
                            logger.warn(
                                f"【阿里云盘】{target_name} 分片 {part_num} 第 {attempt + 1} 次上传失败：{response.text}！"
                            )
                    except Exception as e:
                        logger.warn(
                            f"【阿里云盘】{target_name} 分片 {part_num} 上传异常: {str(e)}！"
                        )

                if success:
                    uploaded_parts.add(part_num)
                    uploaded_size += current_chunk_size
                    progress_callback((uploaded_size * 100) / file_size)
                else:
                    raise Exception(
                        f"【阿里云盘】{target_name} 分片 {part_num} 上传失败！"
                    )

        progress_callback(100)

        result = self._complete_upload(
            drive_id=drive_id, file_id=file_id, upload_id=upload_id
        )
        if not result:
            raise Exception("【阿里云盘】完成上传失败！")
        if result.get("code"):
            logger.warn(
                f"【阿里云盘】{target_name} 上传失败：{result.get('message')}！"
            )
        return self.__get_fileitem(result, parent=target_dir_path)

    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        下载云端文件到本地目录并报告进度。

        覆盖同名目标文件。取消时保留部分文件，传输异常时尝试删除部分文件。

        :param fileitem: 带空间、文件标识和大小的源文件项
        :param path: 目标目录；为空时使用配置中的临时目录，目录需已存在
        :return: 下载完成的本地路径；地址获取失败、取消或传输异常时返回 None
        """
        if not fileitem.path:
            return None
        download_info = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/getDownloadUrl",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
            },
        )
        if not download_info:
            logger.error(f"【阿里云盘】获取下载链接失败: {fileitem.name}")
            return None

        download_url = download_info.get("url")
        if not download_url:
            logger.error(f"【阿里云盘】下载链接为空: {fileitem.name}")
            return None

        local_path = self._build_download_path(fileitem, path or get_runtime_setting('TEMP_PATH'))
        if not local_path:
            return None

        file_size = fileitem.size

        logger.info(f"【阿里云盘】开始下载: {fileitem.name} -> {local_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            headers = {
                "User-Agent": get_runtime_setting('NORMAL_USER_AGENT'),
                "Referer": "https://www.aliyundrive.com/",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "cross-site",
            }

            if self.access_token:
                headers["Authorization"] = f"Bearer {self.access_token}"

            request_utils = RequestUtils(headers=headers)
            with request_utils.get_stream(download_url, raise_exception=True) as r:
                if r is None:
                    return None
                r.raise_for_status()
                downloaded_size = 0
                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=self.chunk_size):
                        if runtime_stop_state.consume_transfer_stop(fileitem.path):
                            logger.info(f"【阿里云盘】{fileitem.path} 下载已取消！")
                            return None
                        if chunk:
                            f.write(chunk)
                            downloaded_size += len(chunk)
                            if file_size:
                                progress = (downloaded_size * 100) / file_size
                                progress_callback(progress)

                progress_callback(100)
                logger.info(f"【阿里云盘】下载完成: {fileitem.name}")
                return local_path
        except Exception as e:
            logger.error(f"【阿里云盘】下载失败: {fileitem.name} - {str(e)}")
            if local_path.exists():
                local_path.unlink()
            return None

    def check(self) -> bool:
        """
        检查本地凭据是否能提供访问令牌。

        :return: 访问令牌不为 None 时返回 True；此结果不代表远端权限校验成功
        """
        return self.access_token is not None

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        请求将文件或目录移入云盘回收站。

        :param fileitem: 携带空间和文件标识的待删除项
        :return: 收到无业务错误的 JSON 响应时返回 True；拒绝、无响应或请求异常返回 False
        """
        try:
            response = self._request_api(
                "POST",
                "/adrive/v1.0/openFile/recyclebin/trash",
                json={"drive_id": fileitem.drive_id, "file_id": fileitem.fileid},
            )
            return isinstance(response, dict) and not response.get("code")
        except HttpRequestError:
            return False

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        修改同一目录内文件或目录的名称。

        :param fileitem: 携带空间和文件标识的目标项
        :param name: 新的名称
        :return: 接口返回非空且没有业务错误时为 True，否则为 False
        """
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/update",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
                "name": name,
            },
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】重命名失败: {resp.get('message')}")
            return False
        return True

    def __get_by_path_item(self, path: Path, drive_id: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        按路径查询并区分目标不存在与状态无法确认。

        :raises StorageQueryError: 请求失败或返回其他业务错误
        :param path: 空间内的绝对路径
        :param drive_id: 目标空间标识；为空时选择默认空间
        :return: 查到的文件项；错误码含 NotFound 时返回 None
        """
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/get_by_path",
            json={
                "drive_id": drive_id or self._default_drive_id,
                "file_path": path.as_posix(),
            },
            no_error_log=True,
        )
        if resp is None:
            raise StorageQueryError(f"【阿里云盘】无法确认文件状态（请求失败）: {path}")
        code = resp.get("code")
        if code:
            if "NotFound" in str(code):
                return None
            raise StorageQueryError(
                f"【阿里云盘】查询文件信息出错: {path} - {code} {resp.get('message')}")
        return self.__get_fileitem(resp, parent=str(path.parent))

    def get_item(self, path: Path, drive_id: Optional[str] = None) -> Optional[_SchemaFileItem]:
        """
        宽松查询空间内的路径，将查询异常转换为未找到。

        :param path: 空间内的绝对路径
        :param drive_id: 目标空间标识；为空时选择默认空间
        :return: 文件或目录项；不存在或查询异常时返回 None
        """
        try:
            return self.__get_by_path_item(path, drive_id=drive_id)
        except Exception as e:
            logger.debug(f"【阿里云盘】获取文件信息失败: {str(e)}")
            return None

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        严格查询默认空间中的路径，保留状态不确定的错误。

        :raises StorageQueryError: 网络、授权或其他错误导致无法确认状态
        :param path: 默认空间内的绝对路径
        :return: 文件或目录项；明确不存在时返回 None
        """
        try:
            return self.__get_by_path_item(path)
        except StorageQueryError:
            raise
        except Exception as e:
            raise StorageQueryError(f"【阿里云盘】查询文件信息失败: {path} - {e}") from e

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询默认空间中的目标路径，缺失时逐级创建目录。

        已查到的目标直接返回，不额外验证其是否为目录。

        :param path: 默认空间内的绝对目录路径
        :return: 已存在的项或最终创建的目录；创建失败时返回 None
        """

        def __find_dir(
            _fileitem: _SchemaFileItem, _name: str
        ) -> Optional[_SchemaFileItem]:
            """
            在目录的直接子目录中查找精确名称。

            :param _fileitem: 待列举的父目录
            :param _name: 目标子目录名
            :return: 匹配目录；未找到时返回 None
            """
            for sub_folder in self.list(_fileitem):
                if sub_folder.type != "dir":
                    continue
                if sub_folder.name == _name:
                    return sub_folder
            return None

        folder = self.get_item(path)
        if folder:
            return folder
        fileitem = _SchemaFileItem(
            storage=self.schema.value, path="/", drive_id=self._default_drive_id
        )
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

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        按输入路径重新查询默认空间中的文件详情。

        :param fileitem: 提供查询路径的文件项；其 drive_id 不参与本次查询
        :return: 查到的文件项；不存在或查询失败时返回 None
        """
        if not fileitem.path:
            return None
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str, **_kwargs: Any) -> bool:
        """
        在源空间内复制文件，再尝试改为目标名称。

        复制后在默认空间延迟查询新文件；查询不到时后续重命名可能抛出异常。
        重命名返回值未纳入结果，因此 True 不保证目标名称已经生效。

        :param fileitem: 携带空间和文件标识的源文件项
        :param path: 源空间中已存在的目标目录路径
        :param new_name: 复制后的目标文件名
        :return: 目录有效且复制接口成功时为 True，前置检查或复制失败时为 False
        """
        if not fileitem.name:
            return False
        dest_fileitem = self.get_item(path, drive_id=fileitem.drive_id)
        if not dest_fileitem or dest_fileitem.type != "dir":
            logger.warn(f"【阿里云盘】目标路径 {path} 不存在或不是目录！")
            return False
        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/copy",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": fileitem.fileid,
                "to_drive_id": fileitem.drive_id,
                "to_parent_file_id": dest_fileitem.fileid,
            },
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】复制文件失败: {resp.get('message')}")
            return False
        new_path = Path(path) / fileitem.name
        new_file = self._delay_get_item(new_path)
        if new_file is None:
            return False
        return self.rename(new_file, new_name)

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        在源空间内移动文件并设置目标名称。

        :param fileitem: 携带空间和文件标识的源文件项
        :param path: 源空间中已存在的目标目录路径
        :param new_name: 移动后的目标文件名
        :return: 目录有效且移动接口无业务错误时为 True，否则为 False
        """
        src_fid = fileitem.fileid
        target_fileitem = self.get_item(path, drive_id=fileitem.drive_id)
        if not target_fileitem or target_fileitem.type != "dir":
            logger.warn(f"【阿里云盘】目标路径 {path} 不存在或不是目录！")
            return False

        resp = self._request_api(
            "POST",
            "/adrive/v1.0/openFile/move",
            json={
                "drive_id": fileitem.drive_id,
                "file_id": src_fid,
                "to_parent_file_id": target_fileitem.fileid,
                "new_name": new_name,
            },
        )
        if not resp:
            return False
        if resp.get("code"):
            logger.warn(f"【阿里云盘】移动文件失败: {resp.get('message')}")
            return False
        return True

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留硬链接入口；阿里云盘不支持此操作。

        :param fileitem: 源文件项，当前不使用
        :param target_file: 链接目标路径，当前不使用
        :return: 固定返回 False，不创建链接
        """
        return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留软链接入口；阿里云盘不支持此操作。

        :param fileitem: 源文件项，当前不使用
        :param target_file: 链接目标路径，当前不使用
        :return: 固定返回 False，不创建链接
        """
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        查询个人空间的总量与剩余容量。

        剩余容量由总量减去已用量计算，缺失容量字段按 0 处理。

        :return: 以字节计的容量信息；未登录、会话失效或请求无结果时返回 None
        """
        try:
            resp = self._request_api("POST", "/adrive/v1.0/user/getSpaceInfo")
            if not resp:
                return None
            space = resp.get("personal_space_info") or {}
            total_size = space.get("total_size") or 0
            used_size = space.get("used_size") or 0
            return _SchemaStorageUsage(
                total=total_size, available=total_size - used_size
            )
        except NoCheckInException:
            return None
        except SessionInvalidException:
            return None
