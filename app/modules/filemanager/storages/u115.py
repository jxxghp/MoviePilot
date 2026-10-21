"""
115 云盘开放接口授权、文件管理与 OSS 上传适配。
"""

import base64
import secrets
import time
from hashlib import sha256
from pathlib import Path
from threading import Lock
from typing import Any, List, Optional, Tuple, Union, cast, overload

from cryptography.hazmat.primitives import hashes

from app.adapters.network.http import HttpRequestError, RequestUtils
from app.foundation import size as size_tools
from app.foundation.singleton import WeakSingleton
from app.modules.filemanager.storages import StorageBase, transfer_process
from app.runtime.log import logger
from app.runtime.rate import QpsRateLimiter, RateStats
from app.runtime.settings import get_runtime_setting
from app.runtime.stop import runtime_stop_state
from app.schemas.exception import StorageQueryError
from app.schemas.file import FileItem as _SchemaFileItem
from app.schemas.file import StorageUsage as _SchemaStorageUsage
from app.schemas.types import StorageSchema

lock = Lock()


# 上传分片目标的最低字节数为 1 MiB；实际大小还需向上匹配预设档位。
MIN_U115_UPLOAD_PART_SIZE = 1 * 1024 * 1024
# 按约 96 片估算分片大小，以控制大文件的上传请求数量。
U115_UPLOAD_PART_COUNT_TARGET = 96
# 一般接口允许调用方处理成功码 0 和记录已存在或不存在相关码 20004。
U115_DEFAULT_ACCEPTED_CODES = (0, 20004)
# 路径详情额外接受 430004，使明确不存在可与请求失败区分。
U115_GET_INFO_ACCEPTED_CODES = (*U115_DEFAULT_ACCEPTED_CODES, 430004)
# 可选分片大小按字节递增，从 10 MiB 到 1 GiB。
U115_UPLOAD_PART_SIZE_STEPS = (
    10 * 1024 * 1024,
    16 * 1024 * 1024,
    32 * 1024 * 1024,
    64 * 1024 * 1024,
    128 * 1024 * 1024,
    256 * 1024 * 1024,
    512 * 1024 * 1024,
    1024 * 1024 * 1024,
)


class NoCheckInException(Exception):
    """
    115 存储没有可用于开放接口的登录凭据。
    """
    pass


class U115Pan(StorageBase, metaclass=WeakSingleton):
    """
    管理 115 开放接口授权、文件操作和 OSS 分片上传。

    下载地址接口与其他文件接口分别限流，风控冷却状态由本实例的所有请求共享。
    普通文件操作经开放接口调用，上传数据使用按需加载的 OSS SDK。
    """

    schema = StorageSchema.U115

    transtype = {"move": "移动", "copy": "复制"}
    base_url = "https://proapi.115.com"

    # 下载流每次读取 10 MiB，避免一次载入整个文件。
    chunk_size = 10 * 1024 * 1024

    download_endpoint = "/open/ufile/downurl"
    # 风控触发后休眠时间（秒）
    limit_sleep_seconds = 3600

    def __init__(self) -> None:
        """
        初始化复用会话、内存授权状态和共享限流统计。

        下载地址接口限速为每秒 1 次，其他文件接口为每秒 3 次；授权接口直接使用会话。
        """
        super().__init__()
        self._auth_state: dict[str, Union[str, int, None]] = {}
        self._request_utils = RequestUtils(
            use_session=True,
            timeout=20,
            verify=True,
        )
        self._init_session()
        self._download_limiter = QpsRateLimiter(1)
        self._api_limiter = QpsRateLimiter(3)
        self._limit_until = 0.0
        self._limit_lock = Lock()
        self._rate_stats = RateStats(source="115")

    def _init_session(self) -> None:
        """
        设置 115 开放接口使用的客户端标识及表单请求头。
        """
        self._request_utils.update_headers(
            {
                "User-Agent": "W115Storage/2.0",
                "Accept-Encoding": "gzip, deflate",
                "Content-Type": "application/x-www-form-urlencoded",
            }
        )

    def close(self) -> None:
        """
        关闭 115 HTTP 客户端。
        """
        self._request_utils.close()

    def _check_session(self) -> None:
        """
        按需刷新令牌并检查当前是否有可用凭据。

        :raises NoCheckInException: 无法取得有效的访问令牌
        """
        if not self.access_token:
            raise NoCheckInException("【115】请先扫码登录！")

    @property
    def access_token(self) -> Optional[str]:
        """
        串行读取令牌，过期时刷新配置并更新会话请求头。

        令牌过期时间由刷新时间的 Unix 秒数与有效秒数相加得到。

        :return: 访问令牌；缺少刷新令牌、刷新失败或没有访问令牌时返回 None
        """
        with lock:
            tokens = self.get_conf()
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                return None
            expires_in = tokens.get("expires_in", 0)
            refresh_time = tokens.get("refresh_time", 0)
            if expires_in and refresh_time + expires_in < int(time.time()):
                refreshed_tokens = self.__refresh_access_token(refresh_token)
                if refreshed_tokens:
                    tokens = refreshed_tokens
                    self.set_config({"refresh_time": int(time.time()), **tokens})
                else:
                    return None
            access_token = tokens.get("access_token")
            if access_token:
                self._request_utils.update_headers(
                    {"Authorization": f"Bearer {access_token}"}
                )
            return access_token

    def generate_auth_url(self) -> Tuple[dict[str, Any], str]:
        """
        向配置的授权服务申请 OAuth2 登录地址。

        state 仅保存在当前实例内存，后续登录轮询依赖同一实例。

        :return: 包含 authUrl 和 state 的字典及空错误文本；失败时返回空字典和原因
        """
        try:
            resp = self._request_utils.get_res(
                f"{get_runtime_setting('U115_AUTH_SERVER')}/u115/auth_url"
            )
            if resp is None:
                return {}, "无法连接到授权服务器"

            result = resp.json()
            if not result.get("success"):
                return {}, result.get("message", "获取授权URL失败")

            data = result.get("data", {})
            auth_url = data.get("auth_url")
            state = data.get("state")

            if not auth_url or not state:
                return {}, "授权服务器返回数据不完整"

            self._auth_state = {"state": state}

            return {"authUrl": auth_url, "state": state}, ""
        except HttpRequestError as e:
            logger.error(f"【115】获取授权 URL 失败: {str(e)}")
            return {}, f"获取授权 URL 失败: {str(e)}"

    def generate_qrcode(self) -> Tuple[dict[str, Any], str]:
        """
        使用 SHA256 校验值申请设备授权二维码。

        校验值使用 SHA256 摘要的 Base64 编码；设备标识、签名和校验原文仅保存在内存。

        :return: 包含 codeContent 的字典及空错误文本；失败时返回空字典和原因
        """
        code_verifier = secrets.token_urlsafe(96)[:128]
        code_challenge = base64.b64encode(
            sha256(code_verifier.encode("utf-8")).digest()
        ).decode("utf-8")
        resp = self._request_utils.post_res(
            "https://passportapi.115.com/open/authDeviceCode",
            data={
                "client_id": get_runtime_setting('U115_APP_ID'),
                "code_challenge": code_challenge,
                "code_challenge_method": "sha256",
            },
        )
        if resp is None:
            return {}, "网络错误"
        result = resp.json()
        if result.get("code") != 0:
            return {}, result.get("message")
        self._auth_state = {
            "code_verifier": code_verifier,
            "uid": result["data"]["uid"],
            "time": result["data"]["time"],
            "sign": result["data"]["sign"],
        }

        return {"codeContent": result["data"]["qrcode"]}, ""

    def check_login(self) -> Optional[Tuple[dict[str, Any], str]]:
        """
        按当前授权方式轮询登录状态，并在成功时保存令牌。

        存在 OAuth2 state 时调用授权服务，否则查询二维码状态；二维码令牌交换异常可能向上传播。

        :return: 状态与提示字典及空错误文本；缺少授权或请求失败时返回空字典和原因
        """
        if self._auth_state and self._auth_state.get("state"):
            return self.__check_oauth_login()

        if not self._auth_state:
            return {}, "生成二维码失败"
        try:
            resp = self._request_utils.get_res(
                "https://qrcodeapi.115.com/get/status/",
                params={
                    "uid": self._auth_state["uid"],
                    "time": self._auth_state["time"],
                    "sign": self._auth_state["sign"],
                },
            )
            if resp is None:
                return {}, "网络错误"
            result = resp.json()
            if result.get("code") != 0 or not result.get("data"):
                return {}, result.get("message")
            if result["data"]["status"] == 2:
                tokens = self.__get_access_token()
                self.set_config({"refresh_time": int(time.time()), **tokens})
            return {
                "status": result["data"]["status"],
                "tip": result["data"]["msg"],
            }, ""
        except HttpRequestError as e:
            return {}, str(e)

    def __check_oauth_login(self) -> Tuple[dict[str, Any], str]:
        """
        查询授权服务并转换为统一的数字登录状态。

        成功时持久化访问令牌、刷新令牌和有效时间；成功或过期后清除内存授权状态。

        :return: 状态 2 表示成功、-1 表示过期、0 表示等待；失败时返回空字典和错误文本
        """
        state = self._auth_state.get("state")
        if not state:
            return {}, "state为空"

        try:
            resp = self._request_utils.get_res(
                f"{get_runtime_setting('U115_AUTH_SERVER')}/u115/token", params={"state": state}
            )
            if resp is None:
                return {}, "无法连接到授权服务器"

            result = resp.json()
            status = result.get("status", "pending")

            if status == "completed":
                data = result.get("data", {})
                if data:
                    self.set_config(
                        {
                            "refresh_time": int(time.time()),
                            "access_token": data.get("access_token"),
                            "refresh_token": data.get("refresh_token"),
                            "expires_in": data.get("expires_in"),
                        }
                    )
                    self._auth_state = {}
                    return {"status": 2, "tip": "授权成功"}, ""
                return {}, "授权服务器返回数据不完整"
            elif status == "expired":
                self._auth_state = {}
                return {"status": -1, "tip": result.get("message", "授权已过期")}, ""
            else:
                return {"status": 0, "tip": "等待用户授权"}, ""
        except Exception as e:
            logger.error(f"【115】检查授权状态失败: {str(e)}")
            return {}, f"检查授权状态失败: {str(e)}"

    def __get_access_token(self) -> dict[str, Any]:
        """
        使用已确认的设备码和校验原文换取访问令牌。

        :raises Exception: 设备授权状态缺失、请求无响应或接口返回业务错误
        :return: 授权响应中的令牌数据
        """
        if not self._auth_state:
            raise Exception("【115】请先生成二维码")
        resp = self._request_utils.post_res(
            "https://passportapi.115.com/open/deviceCodeToToken",
            data={
                "uid": self._auth_state["uid"],
                "code_verifier": self._auth_state["code_verifier"],
            },
        )
        if resp is None:
            raise Exception("获取 access_token 失败")
        result = resp.json()
        if result.get("code") != 0:
            raise Exception(result.get("message"))
        data = result["data"]
        if not isinstance(data, dict):
            raise ValueError("授权服务器返回的令牌数据不是对象")
        return data

    def __refresh_access_token(self, refresh_token: str) -> Optional[dict[str, Any]]:
        """
        使用刷新令牌申请新的访问凭据。

        :param refresh_token: 上次授权或刷新返回的刷新令牌
        :return: 新的令牌数据；无响应、业务失败或无数据时返回 None
        """
        resp = self._request_utils.post_res(
            "https://passportapi.115.com/open/refreshToken",
            data={"refresh_token": refresh_token},
        )
        if resp is None:
            logger.error(
                "【115】刷新 access_token 失败：未收到响应"
            )
            return None
        result = resp.json()
        if result.get("code") != 0:
            logger.warn(
                f"【115】刷新 access_token 失败：{result.get('code')} - {result.get('message')}！"
            )
            return None
        data = result.get("data")
        return data if isinstance(data, dict) else None

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
        self,
        method: str,
        endpoint: str,
        result_key: Optional[str] = None,
        *,
        accepted_codes: Tuple[int, ...] = U115_DEFAULT_ACCEPTED_CODES,
        **kwargs: Any,
    ) -> Optional[Union[dict[str, Any], list[Any]]]:
        """
        执行带限流、共享冷却和有限重试的开放接口请求。

        HTTP 429 或访问上限错误触发共享风控冷却；其他 HTTP 错误按指数间隔重试。
        网络调用直接抛出的异常不重试；登录检查异常向上传播。

        :param method: HTTP 方法
        :param endpoint: 以斜杠开头的开放接口相对路径
        :param result_key: 需要提取的响应字段；为空时返回整个响应
        :param accepted_codes: 当前接口可由调用方继续处理的业务码
        :param kwargs: HTTP 请求参数；no_error_log 控制业务错误日志，retry_limit 默认允许重试 3 次
        :return: 响应或指定字段；网络错误、未接受的业务码或重试用尽时返回 None
        """
        self._check_session()

        no_error_log = kwargs.pop("no_error_log", False)
        retry_times = kwargs.pop("retry_limit", 3)

        if endpoint == self.download_endpoint:
            self._download_limiter.acquire()
        else:
            self._api_limiter.acquire()
        self._rate_stats.record()

        # 风控冷却期间阻止所有接口调用，统一等待
        with self._limit_lock:
            wait_until = self._limit_until
        if wait_until > time.time():
            wait_secs = wait_until - time.time()
            logger.info(
                f"【115】风控冷却中，本请求等待 {wait_secs:.0f} 秒后再调用接口..."
            )
            time.sleep(wait_secs)

        try:
            resp = self._request_utils.request(
                method,
                f"{self.base_url}{endpoint}",
                raise_exception=True,
                **kwargs,
            )
        except Exception as e:
            logger.error(f"【115】{method} 请求 {endpoint} 网络错误: {str(e)}")
            return None

        if resp is None:
            logger.warn(f"【115】{method} 请求 {endpoint} 失败！")
            return None

        kwargs["retry_limit"] = retry_times

        if resp.status_code == 429:
            self._rate_stats.log_stats("warning")
            if retry_times <= 0:
                logger.error(
                    f"【115】{method} 请求 {endpoint} 触发限流(429)，重试次数用尽！"
                )
                return None
            with self._limit_lock:
                self._limit_until = max(
                    self._limit_until,
                    time.time() + self.limit_sleep_seconds,
                )
            logger.warning(
                f"【115】触发限流(429)，全体接口进入风控冷却 {self.limit_sleep_seconds} 秒，随后重试..."
            )
            time.sleep(self.limit_sleep_seconds)
            kwargs["retry_limit"] = retry_times - 1
            kwargs["no_error_log"] = no_error_log
            return self._request_api(
                method,
                endpoint,
                result_key,
                accepted_codes=accepted_codes,
                **kwargs,
            )

        try:
            resp.raise_for_status()
        except Exception as e:
            if retry_times <= 0:
                logger.error(
                    f"【115】{method} 请求 {endpoint} 错误 {e}，重试次数用尽！"
                )
                return None
            kwargs["retry_limit"] = retry_times - 1
            kwargs["no_error_log"] = no_error_log
            sleep_duration = 2 ** (5 - retry_times + 1)
            logger.info(
                f"【115】{method} 请求 {endpoint} 错误 {e}，等待 {sleep_duration} 秒后重试..."
            )
            time.sleep(sleep_duration)
            return self._request_api(
                method,
                endpoint,
                result_key,
                accepted_codes=accepted_codes,
                **kwargs,
            )

        ret_data = resp.json()
        if not isinstance(ret_data, dict):
            logger.warning("开放接口返回了非对象 JSON：%s", endpoint)
            return None
        if ret_data.get("code") not in accepted_codes:
            error_msg = ret_data.get("message", "")
            if not no_error_log:
                logger.warn(f"【115】{method} 请求 {endpoint} 出错：{error_msg}")
            if "已达到当前访问上限" in error_msg:
                self._rate_stats.log_stats("warning")
                if retry_times <= 0:
                    logger.error(
                        f"【115】{method} 请求 {endpoint} 触发风控(访问上限)，重试次数用尽！"
                    )
                    return None
                with self._limit_lock:
                    self._limit_until = max(
                        self._limit_until,
                        time.time() + self.limit_sleep_seconds,
                    )
                logger.warning(
                    f"【115】触发风控(访问上限)，全体接口进入风控冷却 {self.limit_sleep_seconds} 秒，随后重试..."
                )
                time.sleep(self.limit_sleep_seconds)
                kwargs["retry_limit"] = retry_times - 1
                kwargs["no_error_log"] = no_error_log
                return self._request_api(
                    method,
                    endpoint,
                    result_key,
                    accepted_codes=accepted_codes,
                    **kwargs,
                )
            return None

        if result_key:
            value = ret_data.get(result_key)
            return value if isinstance(value, (dict, list)) else None
        return ret_data

    @staticmethod
    def _calc_sha1(filepath: Path, size: Optional[int] = None) -> str:
        """
        计算上传校验所需的完整文件或前缀 SHA1。

        :param filepath: 本地源文件
        :param size: 前缀字节数；None 或 0 表示读取整个文件
        :return: 十六进制摘要
        """
        sha1 = hashes.Hash(hashes.SHA1())
        with open(filepath, "rb") as f:
            if size:
                chunk = f.read(size)
                sha1.update(chunk)
            else:
                while chunk := f.read(8192):
                    sha1.update(chunk)
        return cast(str, sha1.finalize().hex())

    @staticmethod
    def __get_upload_part_size(file_size: int) -> int:
        """
        按约 96 个分片的目标选择上传分片档位。

        向上选择预设档位，超过最大档位时截为 1 GiB；后续 OSS SDK 仍可调整实际分片大小。

        :param file_size: 文件总字节数；非正数使用最小档位
        :return: 10 MiB 至 1 GiB 的分片字节数
        """
        if file_size <= 0:
            return U115_UPLOAD_PART_SIZE_STEPS[0]

        target_part_size = max(
            MIN_U115_UPLOAD_PART_SIZE,
            (file_size + U115_UPLOAD_PART_COUNT_TARGET - 1)
            // U115_UPLOAD_PART_COUNT_TARGET,
        )
        for part_size in U115_UPLOAD_PART_SIZE_STEPS:
            if target_part_size <= part_size:
                return part_size
        return U115_UPLOAD_PART_SIZE_STEPS[-1]

    def init_storage(self) -> None:
        """
        保留存储初始化入口；本后端无需额外初始化。
        """
        pass

    def list(self, fileitem: _SchemaFileItem) -> List[_SchemaFileItem]:
        """
        分页列出目录直接子项，文件输入则刷新其详情。

        每页读取最多 1000 项，以短页结束；目录列表请求失败时抛出 FileNotFoundError。

        :param fileitem: 待查询文件或目录；目录缺少标识时先按路径补查
        :return: 文件项列表；文件详情或目录标识查询失败时返回空列表
        """
        if not fileitem.path:
            return []
        cid: Optional[str]

        if fileitem.type == "file":
            detail_item = self.detail(fileitem)
            if detail_item:
                return [detail_item]
            return []
        if fileitem.path == "/":
            cid = "0"
        else:
            cid = fileitem.fileid
            if not cid:
                _fileitem = self.get_item(Path(fileitem.path))
                if not _fileitem:
                    logger.warn(f"【115】获取目录 {fileitem.path} 失败！")
                    return []
                cid = _fileitem.fileid

        if not cid:
            return []
        items = []
        offset = 0

        while True:
            resp = self._request_api(
                "GET",
                "/open/ufile/files",
                "data",
                params={
                    "cid": int(cid),
                    "limit": 1000,
                    "offset": offset,
                    "cur": True,
                    "show_dir": 1,
                },
            )
            if resp is None:
                raise FileNotFoundError(f"【115】{fileitem.path} 检索出错！")
            if not isinstance(resp, list) or not resp:
                break
            for item in resp:
                if not isinstance(item, dict):
                    continue
                parent_path = Path(fileitem.path)
                item_name = item["fn"]
                full_path = parent_path / item_name
                items.append(
                    _SchemaFileItem(
                        storage=self.schema.value,
                        fileid=str(item["fid"]),
                        parent_fileid=cid,
                        name=item["fn"],
                        basename=Path(item["fn"]).stem,
                        extension=item["ico"] if item["fc"] == "1" else None,
                        type="dir" if item["fc"] == "0" else "file",
                        path=full_path.as_posix() + ("/" if item["fc"] == "0" else ""),
                        size=item["fs"] if item["fc"] == "1" else None,
                        modify_time=item["upt"],
                        pickcode=item["pc"],
                    )
                )

            if len(resp) < 1000:
                break
            offset += len(resp)

        return items

    def create_folder(
        self, parent_item: _SchemaFileItem, name: str
    ) -> Optional[_SchemaFileItem]:
        """
        在指定父目录下创建目录，同名已存在时重新查询。

        :param parent_item: 父目录；根路径或缺少标识时使用目录标识 0
        :param name: 要创建的直接子目录名
        :return: 新目录或已存在的目录项；失败时返回 None
        """
        if not parent_item.path:
            return None
        new_path = Path(parent_item.path) / name
        resp = self._request_api(
            "POST",
            "/open/folder/add",
            data={
                "pid": 0 if parent_item.path == "/" else int(parent_item.fileid or 0),
                "file_name": name,
            },
        )
        if not resp:
            return None
        if not resp.get("state"):
            if resp.get("code") == 20004:
                return self.get_item(new_path)
            logger.warn(f"【115】创建目录失败: {resp.get('error')}")
            return None
        return _SchemaFileItem(
            storage=self.schema.value,
            fileid=str(resp["data"]["file_id"]),
            path=new_path.as_posix() + "/",
            name=name,
            basename=name,
            type="dir",
            modify_time=int(time.time()),
        )

    def upload(
        self,
        target_dir: _SchemaFileItem,
        local_path: Path,
        new_name: Optional[str] = None,
    ) -> Optional[_SchemaFileItem]:
        """
        上传本地文件，支持秒传校验和 OSS 分片传输。

        二次校验使用服务端指定的闭区间内容摘要；秒传后优先按远端标识查询。
        普通上传会创建新的 OSS 分片任务，凭据过期时对当前分片或完成请求最多刷新重试 5 次。
        取消不主动中止远端分片任务；分片阶段的其他异常向上传播。
        成功后若索引尚不可见，按目标路径合成文件项，不保证此时已有远端文件标识。

        :param target_dir: 带文件标识的目标目录
        :param local_path: 本地源文件，完成后保留
        :param new_name: 目标文件名；为空时沿用源文件名
        :return: 上传或已存在的目标项；初始化、完成回调失败或取消时返回 None
        """
        # oss2 连同阿里云 SDK 约 19MB，存储发现会导入全部存储实现，上传时才按需导入
        import oss2
        from oss2 import SizedFileAdapter, determine_part_size
        from oss2.models import PartInfo

        def encode_callback(cb: str) -> str:
            """
            将 OSS 回调参数编码为请求头需要的 Base64 文本。

            :param cb: 回调参数原文
            :return: OSS SDK 生成的 Base64 字符串
            """
            return cast(str, oss2.utils.b64encode_as_string(cb))

        target_name = new_name or local_path.name
        target_path = Path(cast(str, target_dir.path)) / target_name
        file_size = local_path.stat().st_size
        file_sha1 = self._calc_sha1(local_path)
        file_preid = self._calc_sha1(local_path, 128 * 1024 * 1024)

        target_cid = target_dir.fileid
        target_param = f"U_1_{target_cid}"

        init_data = {
            "file_name": target_name,
            "file_size": file_size,
            "target": target_param,
            "fileid": file_sha1,
            "preid": file_preid,
        }
        init_resp = self._request_api("POST", "/open/upload/init", data=init_data)
        if not init_resp:
            return None
        if not init_resp.get("state"):
            logger.warn(f"【115】初始化上传失败: {init_resp.get('error')}")
            return None
        init_result = cast(dict[str, Any], init_resp.get("data"))
        logger.debug(f"【115】上传 Step 1 初始化结果: {init_result}")
        bucket_name = init_result.get("bucket")
        object_name = init_result.get("object")
        callback = init_result.get("callback")
        sign_check = init_result.get("sign_check")
        pick_code = init_result.get("pick_code")
        sign_key = init_result.get("sign_key")

        if init_result.get("code") in [700, 701] and sign_check:
            # 校验区间以连字符分隔，首尾字节都计入内容摘要。
            sign_checks = sign_check.split("-")
            start = int(sign_checks[0])
            end = int(sign_checks[1])
            with open(local_path, "rb") as f:
                f.seek(start)
                chunk = f.read(end - start + 1)
                sha1 = hashes.Hash(hashes.SHA1())
                sha1.update(chunk)
                sign_val = sha1.finalize().hex().upper()
            init_data.update(
                {"pick_code": pick_code, "sign_key": sign_key, "sign_val": sign_val}
            )
            init_resp = self._request_api("POST", "/open/upload/init", data=init_data)
            if not init_resp:
                return None
            if not init_resp.get("state"):
                logger.warn(f"【115】上传二次认证失败: {init_resp.get('error')}")
                return None
            init_result = cast(dict[str, Any], init_resp.get("data"))
            logger.debug(f"【115】上传 Step 2 二次认证结果: {init_result}")
            if not pick_code:
                pick_code = init_result.get("pick_code")
            if not bucket_name:
                bucket_name = init_result.get("bucket")
            if not object_name:
                object_name = init_result.get("object")
            if not callback:
                callback = init_result.get("callback")

        if init_result.get("status") == 2:
            logger.info(f"【115】{target_name} 秒传成功")
            file_id = init_result.get("file_id", None)
            if file_id:
                logger.debug(f"【115】{target_name} 使用秒传返回ID获取文件信息")
                time.sleep(2)
                info_resp = self._request_api(
                    "GET",
                    "/open/folder/get_info",
                    "data",
                    params={"file_id": int(file_id)},
                )
                if isinstance(info_resp, dict) and info_resp:
                    return _SchemaFileItem(
                        storage=self.schema.value,
                        fileid=str(info_resp["file_id"]),
                        path=target_path.as_posix()
                        + ("/" if info_resp["file_category"] == "0" else ""),
                        type="file" if info_resp["file_category"] == "1" else "dir",
                        name=info_resp["file_name"],
                        basename=Path(info_resp["file_name"]).stem,
                        extension=Path(info_resp["file_name"]).suffix[1:]
                        if info_resp["file_category"] == "1"
                        else None,
                        pickcode=info_resp["pick_code"],
                        # 展示用 size 已舍入；清理源文件的身份校验必须使用精确字节数。
                        size=info_resp["size_byte"]
                        if info_resp["file_category"] == "1"
                        else None,
                        modify_time=info_resp["utime"],
                    )
            uploaded_item = self.get_item(target_path)
            return uploaded_item or self.__build_uploaded_fileitem(
                target_path, local_path, file_size
            )

        token_resp = self._request_api("GET", "/open/upload/get_token", "data")
        if not isinstance(token_resp, dict) or not token_resp:
            logger.warn("【115】获取上传凭证失败")
            return None
        logger.debug(f"【115】上传 Step 4 获取上传凭证结果: {token_resp}")
        endpoint = token_resp.get("endpoint")
        AccessKeyId = token_resp.get("AccessKeyId")
        AccessKeySecret = token_resp.get("AccessKeySecret")
        SecurityToken = token_resp.get("SecurityToken")

        resume_resp = self._request_api(
            "POST",
            "/open/upload/resume",
            "data",
            data={
                "file_size": file_size,
                "target": target_param,
                "fileid": file_sha1,
                "pick_code": pick_code,
            },
        )
        if isinstance(resume_resp, dict) and resume_resp:
            logger.debug(f"【115】上传 Step 5 断点续传结果: {resume_resp}")
            if resume_resp.get("callback"):
                callback = resume_resp["callback"]

        auth = oss2.StsAuth(
            access_key_id=AccessKeyId,
            access_key_secret=AccessKeySecret,
            security_token=SecurityToken,
        )
        bucket = oss2.Bucket(auth, endpoint, bucket_name)
        part_size = determine_part_size(
            file_size, preferred_size=self.__get_upload_part_size(file_size)
        )

        logger.info(
            f"【115】开始上传: {local_path} -> {target_path}，分片大小：{size_tools.format_compact_size(part_size)}"
        )
        progress_callback = transfer_process(local_path.as_posix())

        upload_id = bucket.init_multipart_upload(
            object_name, params={"encoding-type": "url", "sequential": ""}
        ).upload_id
        parts = []

        def refresh_upload_bucket() -> oss2.Bucket:
            """
            刷新临时上传凭据并重建同一存储桶的 OSS 客户端。

            :raises Exception: 无法取得新的临时上传凭据
            :return: 使用新凭据的存储桶客户端
            """
            token_resp = self._request_api("GET", "/open/upload/get_token", "data")
            if not isinstance(token_resp, dict) or not token_resp:
                raise Exception("【115】重新获取上传凭证失败")
            logger.info("【115】已重新获取上传凭证，继续上传")
            return oss2.Bucket(
                oss2.StsAuth(
                    access_key_id=token_resp.get("AccessKeyId"),
                    access_key_secret=token_resp.get("AccessKeySecret"),
                    security_token=token_resp.get("SecurityToken"),
                ),
                token_resp.get("endpoint"),
                bucket_name,
            )

        with open(local_path, "rb") as fileobj:
            part_number = 1
            offset = 0
            while offset < file_size:
                if runtime_stop_state.consume_transfer_stop(local_path.as_posix()):
                    logger.info(f"【115】{local_path} 上传已取消！")
                    return None
                num_to_upload = min(part_size, file_size - offset)
                logger.info(
                    f"【115】开始上传 {target_name} 分片 {part_number}: {offset} -> {offset + num_to_upload}"
                )
                retries = 0
                while True:
                    try:
                        # 失败重试时确保从分片起始位置重新读取
                        fileobj.seek(offset)
                        result = bucket.upload_part(
                            object_name,
                            upload_id,
                            part_number,
                            data=SizedFileAdapter(fileobj, num_to_upload),
                        )
                        break
                    except oss2.exceptions.OssError as e:
                        # 上传凭证过期时重新获取凭证并重试当前分片（最多重试 5 次）
                        if e.status == 403 and e.code == "SecurityTokenExpired" and retries < 5:
                            retries += 1
                            logger.warn(
                                f"【115】上传凭证已过期，重新获取凭证后重试 {target_name} 分片 {part_number}（第 {retries} 次）"
                            )
                            bucket = refresh_upload_bucket()
                            continue
                        raise
                parts.append(PartInfo(part_number, result.etag))
                logger.info(f"【115】{target_name} 分片 {part_number} 上传完成")
                offset += num_to_upload
                part_number += 1
                progress = (offset * 100) / file_size
                progress_callback(progress)

        progress_callback(100)

        callback = cast(dict[str, str], callback)
        headers = {
            "X-oss-callback": encode_callback(callback["callback"]),
            "x-oss-callback-var": encode_callback(callback["callback_var"]),
            "x-oss-forbid-overwrite": "false",
        }
        try:
            retries = 0
            while True:
                try:
                    result = bucket.complete_multipart_upload(
                        object_name, upload_id, parts, headers=headers
                    )
                    break
                except oss2.exceptions.OssError as e:
                    # 上传凭证过期时重新获取凭证后重试完成上传（最多重试 5 次）
                    if e.status == 403 and e.code == "SecurityTokenExpired" and retries < 5:
                        retries += 1
                        logger.warn(
                            f"【115】上传凭证已过期，重新获取凭证后重试完成上传（第 {retries} 次）"
                        )
                        bucket = refresh_upload_bucket()
                        continue
                    raise
            if result.status != 200:
                logger.warn(f"【115】{target_name} 上传失败，错误码: {result.status}")
                return None
            try:
                callback_result = result.resp.response.json()
            except Exception as e:
                logger.error(f"【115】{target_name} 上传完成回调解析失败: {str(e)}")
                return None
            logger.debug(f"【115】上传 Step 6 回调结果：{callback_result}")
            if not callback_result or not callback_result.get("state"):
                logger.warn(f"【115】{target_name} 上传完成回调失败: {callback_result}")
                return None
            logger.info(f"【115】{target_name} 上传成功")
        except oss2.exceptions.OssError as e:
            if e.code == "FileAlreadyExists":
                logger.warn(f"【115】{target_name} 已存在")
            else:
                logger.error(
                    f"【115】{target_name} 上传失败: {e.status}, 错误码: {e.code}, 详情: {e.message}"
                )
                return None
        uploaded_item = self.get_item(target_path)
        if uploaded_item:
            return uploaded_item
        logger.warn(
            f"【115】{target_name} 上传已完成但元数据暂不可见，使用目标路径构造整理结果"
        )
        return self.__build_uploaded_fileitem(target_path, local_path, file_size)

    def __build_uploaded_fileitem(
        self, target_path: Path, local_path: Path, file_size: int
    ) -> _SchemaFileItem:
        """
        为上传成功但目录索引尚不可见的文件构造临时元数据。

        :param target_path: 最终目标文件路径
        :param local_path: 本地源文件，存在时读取其修改时间
        :param file_size: 已上传文件的字节数
        :return: 不带远端文件标识的目标文件项
        """
        return _SchemaFileItem(
            storage=self.schema.value,
            path=target_path.as_posix(),
            type="file",
            name=target_path.name,
            basename=target_path.stem,
            extension=target_path.suffix[1:] or None,
            size=file_size,
            modify_time=local_path.stat().st_mtime if local_path.exists() else None,
        )

    def download(self, fileitem: _SchemaFileItem, path: Optional[Path] = None) -> Optional[Path]:
        """
        查询下载地址后将云端文件写入本地目录并报告进度。

        同名目标会被覆盖；取消时保留部分文件，异常时尝试删除部分文件。

        :param fileitem: 提供源文件路径和名称的文件项
        :param path: 本地目标目录；为空时使用临时目录，目录需已存在
        :return: 下载完成的本地路径；查询失败、取消或传输异常时返回 None
        """
        if not fileitem.path:
            return None
        detail = self.get_item(Path(fileitem.path))
        if not detail:
            logger.error(f"【115】获取文件详情失败: {fileitem.name}")
            return None

        download_info = self._request_api(
            "POST", "/open/ufile/downurl", "data", data={"pick_code": detail.pickcode}
        )
        if not isinstance(download_info, dict) or not download_info:
            logger.error(f"【115】获取下载链接失败: {fileitem.name}")
            return None

        download_url = list(download_info.values())[0].get("url", {}).get("url")
        if not download_url:
            logger.error(f"【115】下载链接为空: {fileitem.name}")
            return None

        local_path = self._build_download_path(fileitem, path or get_runtime_setting('TEMP_PATH'))
        if not local_path:
            return None

        file_size = detail.size

        logger.info(f"【115】开始下载: {fileitem.name} -> {local_path}")
        progress_callback = transfer_process(Path(fileitem.path).as_posix())

        try:
            with self._request_utils.get_stream(
                download_url, raise_exception=True
            ) as r:
                if r is None:
                    return None
                r.raise_for_status()
                downloaded_size = 0

                with open(local_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=self.chunk_size):
                        if runtime_stop_state.consume_transfer_stop(fileitem.path):
                            logger.info(f"【115】{fileitem.path} 下载已取消！")
                            r.close()
                            return None
                        f.write(chunk)
                        downloaded_size += len(chunk)
                        if file_size:
                            progress = (downloaded_size * 100) / file_size
                            progress_callback(progress)

                progress_callback(100)
                logger.info(f"【115】下载完成: {fileitem.name}")
        except HttpRequestError as e:
            logger.error(f"【115】下载网络错误: {fileitem.name} - {str(e)}")
            if local_path.exists():
                local_path.unlink()
            return None
        except Exception as e:
            logger.error(f"【115】下载失败: {fileitem.name} - {str(e)}")
            if local_path.exists():
                local_path.unlink()
            return None

        return local_path

    def check(self) -> bool:
        """
        检查本地凭据是否能提供访问令牌。

        :return: 存在访问令牌时返回 True；不代表已执行远端权限检查
        """
        return self.access_token is not None

    def delete(self, fileitem: _SchemaFileItem) -> bool:
        """
        向 115 提交文件或目录删除请求。

        :param fileitem: 带远端文件标识的待删除项
        :return: 接口明确返回成功时为 True；请求失败或业务拒绝时为 False
        """
        if not fileitem.fileid:
            return False
        try:
            resp = self._request_api(
                "POST", "/open/ufile/delete", data={"file_ids": int(fileitem.fileid)}
            )
            return bool(isinstance(resp, dict) and resp.get("state"))
        except HttpRequestError:
            return False

    def rename(self, fileitem: _SchemaFileItem, name: str) -> bool:
        """
        修改远端文件或目录名称。

        :param fileitem: 带远端文件标识的目标项
        :param name: 新的名称
        :return: 响应中的 state 为真时返回 True；无响应或失败时返回 False
        """
        if not fileitem.fileid:
            return False
        resp = self._request_api(
            "POST",
            "/open/ufile/update",
            data={"file_id": int(fileitem.fileid), "file_name": name},
        )
        if not resp:
            return False
        if resp["state"]:
            return True
        return False

    def __get_info_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询路径详情并区分目标不存在与状态无法确认。

        业务码 20004 和 430004 分别表示记录、路径不存在。

        :raises StorageQueryError: 请求失败或业务码不属于可确认的结果
        :param path: 云盘内的绝对路径
        :return: 文件或目录项；可接受响应缺少数据或文件标识时返回 None
        """
        resp = self._request_api(
            "POST",
            "/open/folder/get_info",
            data={"path": path.as_posix()},
            no_error_log=True,
            accepted_codes=U115_GET_INFO_ACCEPTED_CODES,
        )
        if resp is None:
            raise StorageQueryError(f"【115】无法确认文件状态（请求失败或接口错误）: {path}")
        data = resp.get("data") if isinstance(resp, dict) else None
        if not data or not data.get("file_id"):
            # 115 对记录不存在和路径不存在返回不同业务码，两者都可确认目标不存在
            return None
        return _SchemaFileItem(
            storage=self.schema.value,
            fileid=str(data["file_id"]),
            path=path.as_posix() + ("/" if data["file_category"] == "0" else ""),
            type="file" if data["file_category"] == "1" else "dir",
            name=data["file_name"],
            basename=Path(data["file_name"]).stem,
            extension=Path(data["file_name"]).suffix[1:]
            if data["file_category"] == "1"
            else None,
            pickcode=data["pick_code"],
            size=data["size_byte"] if data["file_category"] == "1" else None,
            modify_time=data["utime"],
        )

    def get_item(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        宽松查询路径，将查询异常转换为未找到。

        :param path: 云盘内的绝对路径
        :return: 文件或目录项；不存在或查询异常时返回 None
        """
        try:
            return self.__get_info_item(path)
        except Exception as e:
            logger.debug(f"【115】获取文件信息失败: {str(e)}")
            return None

    def get_item_strict(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        严格查询路径，保留状态无法确认的错误。

        :raises StorageQueryError: 网络、授权或其他错误导致状态无法确认
        :param path: 云盘内的绝对路径
        :return: 文件或目录项；明确不存在时返回 None
        """
        try:
            return self.__get_info_item(path)
        except StorageQueryError:
            raise
        except Exception as e:
            raise StorageQueryError(f"【115】查询文件信息失败: {path} - {e}") from e

    def get_folder(self, path: Path) -> Optional[_SchemaFileItem]:
        """
        查询目标路径，缺失时从根目录逐级查找或创建。

        已存在的目标直接返回，不额外验证其是否为目录。

        :param path: 云盘内的绝对目录路径
        :return: 已存在的项或创建后的目录；创建失败时返回 None
        """

        def __find_dir(
            _fileitem: _SchemaFileItem, _name: str
        ) -> Optional[_SchemaFileItem]:
            """
            在父目录直接子项中查找精确同名的目录。

            :param _fileitem: 待列举的父目录
            :param _name: 目标子目录名称
            :return: 匹配目录；不存在时返回 None
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
        fileitem = _SchemaFileItem(storage=self.schema.value, path="/")
        for part in path.parts[1:]:
            dir_file = __find_dir(fileitem, part)
            if dir_file:
                fileitem = dir_file
            else:
                dir_file = self.create_folder(fileitem, part)
                if not dir_file:
                    logger.warn(f"【115】创建目录 {fileitem.path}{part} 失败！")
                    return None
                fileitem = dir_file
        return fileitem

    def detail(self, fileitem: _SchemaFileItem) -> Optional[_SchemaFileItem]:
        """
        按文件项路径刷新远端元数据。

        :param fileitem: 提供查询路径的文件或目录项
        :return: 查到的项；不存在或查询失败时返回 None
        """
        if not fileitem.path:
            return None
        return self.get_item(Path(fileitem.path))

    def copy(self, fileitem: _SchemaFileItem, path: Path, new_name: str, **_kwargs: Any) -> bool:
        """
        兼容布尔复制接口，使用隔离暂存目录避免操作目标目录中的同名旧文件。

        :param fileitem: 源文件项；缺少远端标识时先按路径查询
        :param path: 已存在的目标目录路径
        :param new_name: 复制后的名称
        :return: 完成复制、改名和目标身份核验时为 True，否则为 False
        """
        try:
            return self.copy_item(fileitem, path, new_name) is not None
        except StorageQueryError as error:
            logger.warn(f"【115】复制未完成: {error}")
            return False

    def copy_item(
            self, fileitem: _SchemaFileItem, path: Path, new_name: str,
    ) -> Optional[_SchemaFileItem]:
        """
        在独立目录内复制和改名，再按文件 ID 移入目标，返回严格确认的文件项。

        115 复制接口先保留源名；直接复制进媒体目录可能生成编号副本，按原名查询还
        可能误改已有文件。暂存目录使用随机名称，异常时保留现场并交由人工复核。
        复制请求不做 HTTP 层自动重放，空目录清理失败不撤销已经确认的传输成功。

        :param fileitem: 带大小和远端文件 ID 的源文件
        :param path: 已存在的目标目录
        :param new_name: 最终文件名，已有同名目标时拒绝操作
        :return: 最终文件项；明确失败时为 None
        :raises StorageQueryError: 复制结果缺少身份或无法确认，保留暂存文件
        """
        destination = self.get_item_strict(path)
        if not destination or not destination.fileid or destination.type != "dir":
            return None
        if self.get_item_strict(path / new_name) is not None:
            return None
        if not fileitem.path:
            raise StorageQueryError("【115】复制源缺少路径")
        source = self.get_item_strict(Path(fileitem.path))
        if (not source or not source.fileid or not source.name
                or source.type == "file" and source.size is None):
            raise StorageQueryError(f"【115】复制源身份无法确认: {fileitem.path}")
        if fileitem.fileid and fileitem.fileid != source.fileid:
            raise StorageQueryError(f"【115】复制源已被替换: {fileitem.path}")
        if source.size != fileitem.size or source.type != fileitem.type:
            raise StorageQueryError(f"【115】复制源大小或类型已变化: {fileitem.path}")
        staging = self.create_folder(destination, f"mp-transfer-{secrets.token_hex(16)}")
        if not staging or not staging.fileid or not staging.path:
            return None
        if self.list(staging):
            raise StorageQueryError(f"【115】复制暂存目录非空，保留现场: {staging.path}")
        resp = self._request_api(
            "POST", "/open/ufile/copy", retry_limit=0,
            data={"file_id": int(source.fileid), "pid": int(staging.fileid)},
        )
        if not isinstance(resp, dict) or not resp.get("state"):
            raise StorageQueryError(f"【115】复制结果无法确认，保留暂存目录: {staging.path}")
        copied = self.get_item_strict(Path(staging.path) / source.name)
        if (not copied or not copied.fileid or copied.fileid == source.fileid
                or copied.size != source.size or copied.type != source.type):
            raise StorageQueryError(f"【115】复制文件身份无法确认，保留暂存目录: {staging.path}")
        if copied.name != new_name:
            if not self.rename(copied, new_name):
                raise StorageQueryError(f"【115】复制后改名失败，保留暂存目录: {staging.path}")
            copied = self.__confirm_transfer_item(copied, Path(staging.path) / new_name)
        result = self.move_item(copied, path, new_name)
        if result is None:
            raise StorageQueryError(f"【115】复制后改名或移动未完成，保留暂存目录: {staging.path}")
        self.__cleanup_transfer_directory(staging)
        return result

    def __cleanup_transfer_directory(self, staging: _SchemaFileItem) -> None:
        """
        只清理已经移出成功副本的空目录，清理失败不得再次执行复制。

        :param staging: 本次复制创建的独立暂存目录；非空或查询失败时保留
        """
        try:
            if self.list(staging) or not self.delete(staging):
                logger.warn(f"【115】文件整理完成，暂存目录未清理: {staging.path}")
        except Exception as error:
            logger.warn(f"【115】文件整理完成，暂存目录清理失败: {staging.path} - {error}")

    def move(self, fileitem: _SchemaFileItem, path: Path, new_name: str) -> bool:
        """
        兼容布尔移动接口，始终按源文件 ID 操作并严格确认最终目标。

        :param fileitem: 源文件项；缺少远端标识时先按路径查询
        :param path: 已存在的目标目录路径
        :param new_name: 移动后的名称
        :return: 移动、改名及目标身份核验成功时为 True，否则为 False
        """
        try:
            return self.move_item(fileitem, path, new_name) is not None
        except StorageQueryError as error:
            logger.warn(f"【115】移动未完成: {error}")
            return False

    def move_item(
            self, fileitem: _SchemaFileItem, path: Path, new_name: str,
    ) -> Optional[_SchemaFileItem]:
        """
        按 ID 移动和改名，避免查询原名时误操作目标目录中的旧文件。

        改名或移动失败可能已有外部副作用，持久整理必须进入人工复核而非自动重放。

        :param fileitem: 待移动文件的路径和冻结身份
        :param path: 已存在的目标目录
        :param new_name: 最终文件名
        :return: 严格核验 ID、类型和大小后的目标；明确失败时为 None
        :raises StorageQueryError: 源或目标身份变化，或无法取得严格查询结果
        """
        if not fileitem.path:
            raise StorageQueryError("【115】移动源缺少路径")
        source = self.get_item_strict(Path(fileitem.path))
        if not source or not source.fileid:
            return None
        if fileitem.fileid and fileitem.fileid != source.fileid:
            raise StorageQueryError(f"【115】移动源已被替换: {fileitem.path}")
        if source.size != fileitem.size or source.type != fileitem.type:
            raise StorageQueryError(f"【115】移动源大小或类型已变化: {fileitem.path}")
        dest_fileitem = self.get_item_strict(path)
        if not dest_fileitem or not dest_fileitem.fileid or dest_fileitem.type != "dir":
            return None
        if self.get_item_strict(path / new_name) is not None:
            return None
        resp = self._request_api(
            "POST", "/open/ufile/move", retry_limit=0,
            data={"file_ids": int(source.fileid), "to_cid": int(dest_fileitem.fileid)},
        )
        if not isinstance(resp, dict) or not resp.get("state"):
            return None
        if source.name != new_name and not self.rename(source, new_name):
            return None
        return self.__confirm_transfer_item(source, path / new_name)

    def __confirm_transfer_item(self, source: _SchemaFileItem, path: Path) -> _SchemaFileItem:
        """
        严格确认改名或移动后的对象仍具有原 ID、类型及精确大小。

        :param source: 操作前已确认的对象身份
        :param path: 操作后应出现的路径
        :return: 严格查询确认的文件项
        :raises StorageQueryError: 目标缺失、被替换或大小类型发生变化
        """
        result = self.get_item_strict(path)
        if (not result or result.fileid != source.fileid
                or result.type != source.type or result.size != source.size):
            raise StorageQueryError(f"【115】传输目标身份无法确认: {path}")
        return result

    def link(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留硬链接入口；115 云盘不支持此操作。

        :param fileitem: 源文件项，当前不使用
        :param target_file: 链接目标路径，当前不使用
        :return: 固定返回 False，不创建链接
        """
        return False

    def softlink(self, fileitem: _SchemaFileItem, target_file: Path) -> bool:
        """
        保留软链接入口；115 云盘不支持此操作。

        :param fileitem: 源文件项，当前不使用
        :param target_file: 链接目标路径，当前不使用
        :return: 固定返回 False，不创建链接
        """
        return False

    def usage(self) -> Optional[_SchemaStorageUsage]:
        """
        查询账号空间的总容量与可用容量。

        :return: 以字节计的容量信息；未登录或接口无数据时返回 None
        """
        try:
            resp = self._request_api("GET", "/open/user/info", "data")
            if not isinstance(resp, dict) or not resp:
                return None
            space = resp["rt_space_info"]
            return _SchemaStorageUsage(
                total=space["all_total"]["size"], available=space["all_remain"]["size"]
            )
        except NoCheckInException:
            return None
