"""
绿联内部加密接口与 Emby 兼容媒体库服务的两套独立 API 客户端。
"""

import base64
import uuid
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Union
from urllib.parse import urlsplit, urlunsplit

from app.adapters.network.http import RequestUtils
from app.foundation.url import UrlUtils
from app.modules.ugreen.crypto import UgreenCrypto
from app.runtime.log import logger


@dataclass
class ApiResult:
    """
    绿联内部接口的业务响应，保留原始负载供调用方诊断。

    :param code: 业务状态码，200 表示成功；响应无有效状态码时为 -1
    :param msg: 服务端业务说明，缺失时为空字符串
    :param data: 业务数据，形状由具体接口决定；无数据时为 None
    :param debug: 可选服务端调试信息
    :param raw: 解析后的原始响应字典；响应不是映射时为 None
    """

    code: int = -1
    msg: str = ""
    data: Any = None
    debug: Optional[str] = None
    raw: Optional[dict[str, Any]] = None

    @property
    def success(self) -> bool:
        """
        判断业务响应是否成功。

        :return: 仅 code 等于 200 时返回 True，不按 HTTP 状态码判断
        """
        return self.code == 200


class Api:
    """
    绿联内部影视接口客户端，管理登录、加密上下文及业务响应。

    业务接口统一通过 request 发送明文参数，由加密层处理查询串和请求体，并解密响应。
    当前传输实现只实际发送 GET 与 POST；不提供通用 HTTP 方法透传。
    """

    __slots__ = (
        "_host",
        "_request_utils",
        "_token",
        "_static_token",
        "_is_ugk",
        "_public_key",
        "_crypto",
        "_username",
        "_client_id",
        "_client_version",
        "_language",
        "_ug_agent",
        "_timeout",
        "_verify_ssl",
    )

    def __init__(
        self,
        host: str,
        client_version: str = "76363",
        language: str = "zh-CN",
        ug_agent: str = "PC/WEB",
        timeout: int = 20,
        verify_ssl: bool = True,
    ) -> None:
        """
        初始化绿联影视 API 客户端。

        :param host: 绿联服务端地址
        :param client_version: 绿联 Web 客户端版本号
        :param language: 请求语言
        :param ug_agent: 绿联客户端标识
        :param timeout: HTTP 请求超时时间，单位秒
        :param verify_ssl: 是否校验 HTTPS 证书
        """
        self._host = self._normalize_base_url(host)
        self._request_utils = RequestUtils(
            use_session=True,
            timeout=timeout,
            verify=verify_ssl,
        )

        self._token: Optional[str] = None
        self._static_token: Optional[str] = None
        self._is_ugk: bool = False
        self._public_key: Optional[str] = None
        self._crypto: Optional[UgreenCrypto] = None
        self._username: Optional[str] = None

        self._client_id = f"{uuid.uuid4()}-WEB"
        self._client_version = client_version
        self._language = language
        self._ug_agent = ug_agent
        self._timeout = timeout
        # 是否校验证书，默认开启；仅在用户明确配置时才应关闭。
        self._verify_ssl = bool(verify_ssl)

    @property
    def host(self) -> str:
        """
        获取规范化后的绿联服务端地址。
        """
        return self._host

    @property
    def token(self) -> Optional[str]:
        """
        获取当前登录会话 token。
        """
        return self._token

    @property
    def static_token(self) -> Optional[str]:
        """
        获取可用于静态资源访问的 token。
        """
        return self._static_token

    @property
    def is_ugk(self) -> bool:
        """
        判断当前会话是否使用 ugk 访问参数。
        """
        return self._is_ugk

    @property
    def public_key(self) -> Optional[str]:
        """
        获取当前会话加密公钥。
        """
        return self._public_key

    def close(self) -> None:
        """
        关闭底层 HTTP 会话。
        """
        self._request_utils.close()

    @staticmethod
    def _normalize_base_url(host: str) -> str:
        """
        将配置地址规整为服务根地址，移除路径、查询串和片段。

        :param host: 绿联地址，空值返回空字符串
        :return: 仅包含协议及主机端口的根地址，不带尾部斜杠
        """
        if not host:
            return ""
        host = UrlUtils.standardize_base_url(host).rstrip("/")
        parsed = urlsplit(host)
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")

    @staticmethod
    def _decode_public_key(raw: Optional[str]) -> Optional[str]:
        """
        兼容原始 PEM 与 Base64 包裹的公钥文本。

        :param raw: 登录响应中的公钥文本
        :return: 解码后的公钥；空值或 Base64 解码失败时返回 None
        """
        if not raw:
            return None
        value = str(raw).strip()
        if not value:
            return None
        if "BEGIN" in value:
            return value
        try:
            return base64.b64decode(value).decode("utf-8")
        except Exception:
            return None

    @staticmethod
    def _extract_rsa_token(resp_json: dict[str, Any], headers: Mapping[str, str]) -> Optional[str]:
        """
        按响应头、顶层负载和 data 的顺序提取登录公钥令牌。

        :param resp_json: 登录预检接口解析后的响应字典
        :param headers: HTTP 响应头
        :return: 首个可用的 RSA 令牌文本；不存在时返回 None
        """
        token = headers.get("x-rsa-token") or headers.get("X-Rsa-Token")
        if isinstance(token, str) and token:
            return token
        token = resp_json.get("xRsaToken") or resp_json.get("x-rsa-token")
        if isinstance(token, str) and token:
            return token
        data = resp_json.get("data") if isinstance(resp_json, Mapping) else None
        if isinstance(data, Mapping):
            token = data.get("xRsaToken") or data.get("x-rsa-token")
            return token if isinstance(token, str) else None
        return None

    def _common_headers(self) -> dict[str, str]:
        """
        获取绿联 Web 端通用请求头，兼容新版登录客户端标识。
        """
        return {
            "Accept": "application/json, text/plain, */*",
            "Client-Id": self._client_id,
            "Client-Version": self._client_version,
            "UG-Agent": self._ug_agent,
            "UG-Client-Id": self._client_id,
            "X-Specify-Language": self._language,
        }

    def _request_json(
        self,
        url: str,
        method: str = "GET",
        headers: Optional[dict[str, Any]] = None,
        params: Optional[dict[str, Any]] = None,
        json_data: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        """
        发送 HTTP 请求并尝试解析为 JSON。
        """
        try:
            method = method.upper()
            if method == "POST":
                resp = self._request_utils.post_res(
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                    timeout=self._timeout,
                    verify=self._verify_ssl,
                )
            else:
                resp = self._request_utils.get_res(
                    url=url,
                    headers=headers,
                    params=params,
                    timeout=self._timeout,
                    verify=self._verify_ssl,
                )
            if resp is None:
                logger.error(f"请求绿联接口失败：{url} 未收到 HTTP 响应")
                return None
            payload = resp.json()
            return payload if isinstance(payload, dict) else None
        except Exception as err:
            logger.error(f"请求绿联接口失败：{url} {err}")
            return None

    @staticmethod
    def _build_result(payload: Any) -> ApiResult:
        """
        将解密后的响应规整为业务结果对象。

        :param payload: 预期为包含 code、msg、data 的映射
        :return: 统一响应；非法映射或无法转换的状态码使用 code=-1
        """
        if not isinstance(payload, Mapping):
            return ApiResult(code=-1, msg="响应格式错误", raw=None)
        code = payload.get("code")
        try:
            code = int(code) if isinstance(code, (str, int, float)) else -1
        except Exception:
            code = -1
        return ApiResult(
            code=code,
            msg=str(payload.get("msg") or ""),
            data=payload.get("data"),
            debug=payload.get("debug"),
            raw=dict(payload),
        )

    def login(self, username: str, password: str, keepalive: bool = True) -> Optional[str]:
        """
        执行公钥预检和账号登录，成功后更新实例的加密会话。

        保存访问令牌、静态资源令牌、公钥和客户端标识；公钥损坏或加密库错误会向上传播。

        :param username: 绿联登录账号
        :param password: 密码明文，发送前使用预检公钥进行 RSA 分块加密
        :param keepalive: 是否请求服务端保持登录，默认启用
        :return: 成功时返回会话令牌；凭据为空、预检或业务登录失败时返回 None
        """
        if not username or not password:
            return None

        headers = self._common_headers()

        try:
            check_resp = self._request_utils.post_res(
                url=f"{self._host}/ugreen/v1/verify/check",
                headers=headers,
                json={"username": username},
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
            if check_resp is None:
                logger.error("绿联获取登录公钥失败：未收到 HTTP 响应")
                return None
            check_json = check_resp.json()
        except Exception as err:
            logger.error(f"绿联获取登录公钥失败：{err}")
            return None

        check_result = self._build_result(check_json)
        if not check_result.success:
            logger.error(f"绿联获取登录公钥失败：{check_result.msg}")
            return None

        rsa_token = self._extract_rsa_token(check_json, check_resp.headers)
        login_public_key = self._decode_public_key(rsa_token)
        if not login_public_key:
            logger.error("绿联获取登录公钥失败：公钥为空")
            return None

        encrypted_password = UgreenCrypto(public_key=login_public_key).rsa_encrypt_long(password)
        login_json = self._request_json(
            url=f"{self._host}/ugreen/v1/verify/login",
            method="POST",
            headers=headers,
            json_data={
                "username": username,
                "password": encrypted_password,
                "keepalive": keepalive,
                "otp": True,
                "is_simple": True,
            },
        )
        if not login_json:
            return None

        login_result = self._build_result(login_json)
        if not login_result.success or not isinstance(login_result.data, Mapping):
            logger.error(f"绿联登录失败：{login_result.msg}")
            return None

        token = str(
            login_result.data.get("token")
            or login_result.data.get("token_id")
            or login_result.data.get("tokenId")
            or ""
        ).strip()
        public_key = (
            self._decode_public_key(
                str(
                    login_result.data.get("public_key")
                    or login_result.data.get("publicKey")
                    or ""
                )
            )
            or login_public_key
        )
        if not token or not public_key:
            logger.error("绿联登录失败：未返回 token/token_id 或可用公钥")
            return None

        self._token = token
        static_token = str(
            login_result.data.get("static_token")
            or login_result.data.get("staticToken")
            or ""
        ).strip()
        self._static_token = static_token or self._token
        self._is_ugk = bool(login_result.data.get("is_ugk"))
        self._public_key = public_key
        self._crypto = UgreenCrypto(
            public_key=self._public_key,
            token=self._token,
            client_id=self._client_id,
            client_version=self._client_version,
            ug_agent=self._ug_agent,
            language=self._language,
        )
        self._username = username
        return self._token

    def export_session_state(self) -> Optional[dict[str, Any]]:
        """
        导出包含认证令牌、客户端身份和 Cookie 的会话快照。

        :return: 可供持久化的会话字典；缺少令牌或公钥时返回 None，导出内容应按凭据保护
        """
        if not self._token or not self._public_key:
            return None
        return {
            "token": self._token,
            "static_token": self._static_token,
            "is_ugk": self._is_ugk,
            "public_key": self._public_key,
            "username": self._username,
            "client_id": self._client_id,
            "client_version": self._client_version,
            "language": self._language,
            "ug_agent": self._ug_agent,
            "cookies": self._request_utils.get_cookies(),
        }

    def import_session_state(self, state: Mapping[str, Any]) -> bool:
        """
        恢复本地会话和加密上下文，不向服务端验证有效性。

        恢复客户端标识以保持服务端会话绑定关系；更新 Cookie 失败时忽略该错误。

        :param state: 之前导出的会话映射，必须包含非空 token 和可解码 public_key
        :return: 必要字段有效且完成恢复时返回 True，否则返回 False；无效公钥构造异常向上传播
        """
        if not isinstance(state, Mapping):
            return False

        token = str(state.get("token") or "").strip()
        public_key = self._decode_public_key(str(state.get("public_key") or ""))
        if not token or not public_key:
            return False

        static_token = str(state.get("static_token") or "").strip()
        is_ugk = bool(state.get("is_ugk"))

        # 会话可能与 client_id 绑定，需恢复原客户端信息
        client_id = str(state.get("client_id") or "").strip()
        if client_id:
            self._client_id = client_id

        client_version = str(state.get("client_version") or "").strip()
        if client_version:
            self._client_version = client_version

        language = str(state.get("language") or "").strip()
        if language:
            self._language = language

        ug_agent = str(state.get("ug_agent") or "").strip()
        if ug_agent:
            self._ug_agent = ug_agent

        username = str(state.get("username") or "").strip()
        self._username = username or None

        cookies = state.get("cookies")
        if isinstance(cookies, Mapping):
            try:
                self._request_utils.update_cookies(
                    {
                        str(k): str(v)
                        for k, v in cookies.items()
                        if k is not None and v is not None
                    }
                )
            except Exception:
                pass

        self._token = token
        self._static_token = static_token or self._token
        self._is_ugk = is_ugk
        self._public_key = public_key
        self._crypto = UgreenCrypto(
            public_key=self._public_key,
            token=self._token,
            client_id=self._client_id,
            client_version=self._client_version,
            ug_agent=self._ug_agent,
            language=self._language,
        )
        return True

    def logout(self) -> None:
        """
        登出并清理本地认证状态。
        """
        if not self._token or not self._crypto:
            return
        try:
            req = self._crypto.build_encrypted_request(
                url=f"{self._host}/ugreen/v1/verify/logout",
                method="GET",
                params={},
            )
            self._request_utils.get_res(
                req.url,
                headers=req.headers,
                params=req.params,
                timeout=self._timeout,
                verify=self._verify_ssl,
            )
        except Exception:
            pass
        self._token = None
        self._static_token = None
        self._is_ugk = False
        self._public_key = None
        self._crypto = None
        self._username = None

    def request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
    ) -> ApiResult:
        """
        加密查询参数、发送内部业务请求并解密响应。

        :param path: ugreen 根路径后的相对接口路径，如 v1/video/homepage/media_list
        :param method: 当前仅 POST 会发送为 POST，其他值走 GET；调用方应使用 GET 或 POST
        :param params: 明文查询参数，由本方法构造 encrypt_query
        :param data: 明文请求体；POST 时加密后发送，其他传输方法当前不发送请求体
        :return: 统一业务结果；未登录或 HTTP 请求失败时 code=-1，加解密异常向调用方传播
        """
        if not self._crypto:
            return ApiResult(code=-1, msg="未登录")

        api_path = path.strip("/")
        # 由加密工具自动构建 encrypt_query 与加密请求体
        req = self._crypto.build_encrypted_request(
            url=f"{self._host}/ugreen/{api_path}",
            method=method.upper(),
            params=params or {},
            data=data,
            encrypt_body=method.upper() in {"POST", "PUT", "PATCH"},
        )

        payload = self._request_json(
            url=req.url,
            method=method,
            headers=req.headers,
            params=req.params,
            json_data=req.json,
        )
        if payload is None:
            return ApiResult(code=-1, msg="接口请求失败")

        # 响应若包含 encrypt_resp_body，这里会自动解密
        decrypted = self._crypto.decrypt_response(payload, req.aes_key)
        return self._build_result(decrypted)

    def current_user(self) -> Optional[dict[str, Any]]:
        """
        读取内部接口当前用户信息。

        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request("v1/user/current/user")
        if not result.success or not isinstance(result.data, Mapping):
            return None
        return dict(result.data)

    def media_list(self) -> Optional[list[dict[str, Any]]]:
        """
        读取首页媒体库集合列表。

        :return: media_lib_info_list 列表；业务失败或响应结构不符时返回 None
        """
        result = self.request("v1/video/homepage/media_list")
        if not result.success or not isinstance(result.data, Mapping):
            return None
        items = result.data.get("media_lib_info_list")
        return items if isinstance(items, list) else None

    def media_lib_users(self) -> list[dict[str, Any]]:
        """
        读取内部接口媒体库用户列表。

        :return: user_info_arr 列表；业务失败或响应结构不符时返回空列表
        """
        result = self.request("v1/video/media_lib/get_user_list")
        if not result.success or not isinstance(result.data, Mapping):
            return []
        users = result.data.get("user_info_arr")
        return users if isinstance(users, list) else []

    def recently_played(self, page: int = 1, page_size: int = 12) -> Optional[dict[str, Any]]:
        """
        获取继续观看列表。

        :param page: 从 1 开始的页码
        :param page_size: 每页最多返回条目数量
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request(
            "v1/video/recently_played/get",
            params={
                "page": page,
                "page_size": page_size,
                "language": self._language,
                "create_time_order": "false",
            },
        )
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def recently_updated(self, page: int = 1, page_size: int = 20) -> Optional[dict[str, Any]]:
        """
        获取最近更新列表。

        :param page: 从 1 开始的页码
        :param page_size: 每页最多返回条目数量
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request(
            "v1/video/recently_update/get",
            params={
                "page": page,
                "page_size": page_size,
                "language": self._language,
                "create_time_order": "false",
            },
        )
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def recently_played_info(self, item_id: Union[str, int]) -> Optional[dict[str, Any]]:
        """
        获取单个视频的播放状态与基础详情信息。

        :param item_id: 绿联内部视频条目标识
        :return: 视频详情与播放状态字典；业务码 200 或 1303 均接受，其他错误或无数据时返回 None
        """
        result = self.request(
            "v1/video/recently_played/info",
            params={
                "ug_video_info_id": item_id,
                "version_control": "true",
            },
        )
        if result.code in {200, 1303} and isinstance(result.data, Mapping):
            return dict(result.data)
        return None

    def search(self, keyword: str, offset: int = 0, limit: int = 200) -> Optional[dict[str, Any]]:
        """
        搜索媒体（电影/剧集）。

        :param keyword: 媒体搜索关键词
        :param offset: 结果起始偏移，默认 0
        :param limit: 最多返回候选数量，默认 200
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request(
            "v1/video/search",
            params={
                "language": self._language,
                "search_type": 1,
                "offset": offset,
                "limit": limit,
                "keyword": keyword,
            },
        )
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def video_all(self, classification: int, page: int = 1, page_size: int = 20) -> Optional[dict[str, Any]]:
        """
        按绿联分类编号查询视频列表。

        :param classification: 分类编号，-102 为电影，-103 为电视剧
        :param page: 从 1 开始的页码
        :param page_size: 每页最多返回条目数量
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request(
            "v1/video/all",
            params={
                "page": page,
                "pageSize": page_size,
                "classification": classification,
                "sort_type": 2,
                "order_type": 2,
                "release_date_begin": -9999999999,
                "release_date_end": -9999999999,
                "identify_status": 0,
                "watch_status": -1,
                "ug_style_id": 0,
                "ug_country_id": 0,
                "clarity": -1,
            },
        )
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def poster_wall_get_folder(
        self,
        path: Optional[str] = None,
        page: int = 1,
        page_size: int = 100,
        sort_type: int = 1,
        order_type: int = 1,
    ) -> Optional[dict[str, Any]]:
        """
        获取海报墙文件夹与条目（可按目录路径递归展开）。

        :param path: 目标目录路径，空值查询海报墙根目录
        :param page: 从 1 开始的页码
        :param page_size: 每页最多返回条目数量
        :param sort_type: 服务端排序字段编号，默认 1，原样透传
        :param order_type: 服务端排序方向编号，默认 1，原样透传
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        params: dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "sort_type": sort_type,
            "order_type": order_type,
        }
        if path:
            params["path"] = path
        result = self.request("v1/video/poster_wall/media_lib/get_folder", params=params)
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def get_movie(
        self,
        item_id: Union[str, int],
        media_lib_set_id: Union[str, int],
        path: Optional[str] = None,
        folder_path: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        获取电影详情。

        :param item_id: 绿联内部视频条目标识
        :param media_lib_set_id: 电影所属媒体库集合标识
        :param path: 可选具体文件路径
        :param folder_path: 可选所在文件夹路径
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        params: dict[str, Any] = {
            "id": item_id,
            "media_lib_set_id": media_lib_set_id,
            "fileVersion": "true",
        }
        if path:
            params["path"] = path
        if folder_path:
            params["folder_path"] = folder_path
        result = self.request("v1/video/details/getMovie", params=params)
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def get_tv(self, item_id: Union[str, int], folder_path: str = "ALL") -> Optional[dict[str, Any]]:
        """
        获取剧集详情（含季/集信息）。

        :param item_id: 绿联内部视频条目标识
        :param folder_path: 目录筛选条件，默认 ALL 表示全部目录
        :return: 接口 data 中的业务字典；业务失败或 data 不是映射时返回 None
        """
        result = self.request(
            "v2/video/details/getTV",
            params={
                "ug_video_info_id": item_id,
                "folder_path": folder_path,
            },
        )
        return dict(result.data) if result.success and isinstance(result.data, Mapping) else None

    def scan(self, media_lib_set_id: Union[str, int], scan_type: int = 2, op_type: int = 2) -> bool:
        """
        触发媒体库扫描。

        :param media_lib_set_id: 要扫描的媒体库集合标识
        :param scan_type: 1 扫描新建和修改，2 补充缺失，3 覆盖扫描，默认 2
        :param op_type: 服务端操作类型，默认使用网页端的 2
        :return: 扫描请求业务成功时返回 True，否则 False；不等待扫描完成
        """
        result = self.request(
            "v1/video/media_lib/scan",
            params={
                "op_type": op_type,
                "media_lib_set_id": media_lib_set_id,
                "media_lib_scan_type": scan_type,
            },
        )
        return result.success

    def scan_status(self, only_brief: bool = True) -> list[dict[str, Any]]:
        """
        获取媒体库扫描状态。

        :param only_brief: 是否仅请求扫描概要，默认启用
        :return: 扫描状态记录列表；业务失败或响应结构不符时返回空列表
        """
        result = self.request(
            "v1/video/media_lib/scan/status",
            params={"only_brief": "true" if only_brief else "false"},
        )
        if not result.success or not isinstance(result.data, Mapping):
            return []
        arr = result.data.get("media_lib_scan_status_arr")
        return arr if isinstance(arr, list) else []

    def preferences_all(self) -> Optional[Any]:
        """
        读取影视偏好设置。

        :return: data 原始偏好数据，业务失败时返回 None
        """
        result = self.request("v1/video/preferences/all")
        return result.data if result.success else None

    def history_get(self, num: int = 10) -> Optional[Any]:
        """
        获取历史记录（`v1/video/history/get`）。

        :param num: 最多返回的历史记录数，默认 10
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request("v1/video/history/get", params={"num": num})
        return result.data if result.success else None

    def data_source_get_config(self) -> Optional[Any]:
        """
        读取元数据来源配置。

        :return: data 原始来源配置，业务失败时返回 None
        """
        result = self.request("v1/video/data_source/get_config")
        return result.data if result.success else None

    def homepage_slider(
        self, language: Optional[str] = None, app_name: str = "web"
    ) -> Optional[Any]:
        """
        获取首页轮播数据（`v1/video/homepage/slider`）。

        :param language: 请求语言，空值使用实例配置语言
        :param app_name: 客户端应用名称，默认 web
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request(
            "v1/video/homepage/slider",
            params={
                "language": language or self._language,
                "app_name": app_name,
            },
        )
        return result.data if result.success else None

    def media_lib_guide_init(self) -> Optional[Any]:
        """
        读取媒体库引导初始化信息。

        :return: data 原始引导信息，业务失败时返回 None
        """
        result = self.request("v1/video/media_lib/guide_init")
        return result.data if result.success else None

    def media_lib_filter_options(
        self, media_type: int = 0, language: Optional[str] = None
    ) -> Optional[Any]:
        """
        获取媒体库筛选项（`v1/video/media_lib/filter/options`）。

        :param media_type: 服务端筛选类型编号，默认 0
        :param language: 请求语言，空值使用实例配置语言
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request(
            "v1/video/media_lib/filter/options",
            params={
                "type": media_type,
                "language": language or self._language,
            },
        )
        return result.data if result.success else None

    def guide(self, guide_position: int = 1, client_type: int = 1) -> Optional[Any]:
        """
        获取引导位数据（`v1/video/guide`）。

        :param guide_position: 服务端引导位置编号，默认 1
        :param client_type: 服务端客户端类型编号，默认 1
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request(
            "v1/video/guide",
            params={
                "guide_position": guide_position,
                "client_type": client_type,
            },
        )
        return result.data if result.success else None

    def homepage_v2(self, language: Optional[str] = None) -> Optional[Any]:
        """
        获取新版首页聚合数据（`v2/video/homepage`）。

        :param language: 请求语言，空值使用实例配置语言
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request(
            "v2/video/homepage",
            params={"language": language or self._language},
        )
        return result.data if result.success else None

    def media_lib_init_user_permission(self) -> Optional[Any]:
        """
        请求初始化当前用户的媒体库权限。

        :return: data 原始业务结果，业务失败时返回 None
        """
        result = self.request("v1/video/media_lib/init_user_permission")
        return result.data if result.success else None

    def media_lib_get_all(
        self, req_type: int = 2, language: Optional[str] = None
    ) -> Optional[Any]:
        """
        获取全部媒体库集合（`v1/video/media_lib/get_all`）。

        :param req_type: 服务端媒体库集合请求类型，默认 2
        :param language: 请求语言，空值使用实例配置语言
        :return: 服务端 data 原始业务数据；业务失败时返回 None
        """
        result = self.request(
            "v1/video/media_lib/get_all",
            params={
                "mediaLib_get_all_req_type": req_type,
                "language": language or self._language,
            },
        )
        return result.data if result.success else None


@dataclass(frozen=True)
class UgreenMediaSession:
    """
    绿联媒体库服务认证成功后的不可变会话信息。

    :param token: 媒体库服务 AccessToken，访问接口时作为 api_key 使用
    :param user_id: 认证用户标识，用于用户范围内的媒体库查询
    :param server_id: 可选媒体服务器标识，服务端未提供时为 None
    :param user: 认证响应中的原始用户详情；未提供时为 None
    """

    token: str
    user_id: str
    server_id: Optional[str] = None
    user: Optional[dict[str, Any]] = None


class UgreenMediaApi:
    """
    绿联影视媒体库服务的 Emby 兼容 API 客户端。

    媒体库服务只在 HTTPS 9443 端口开放，认证后使用 AccessToken 作为
    `api_key` 访问用户媒体库接口，不复用 `/ugreen/v1` 的加密会话。
    """

    __slots__ = (
        "_host",
        "_request_utils",
        "_token",
        "_user_id",
        "_server_id",
        "_timeout",
        "_verify_ssl",
    )

    def __init__(self, host: str, timeout: int = 20, verify_ssl: bool = True) -> None:
        """
        初始化媒体库服务 API 客户端。

        :param host: 绿联媒体库服务地址
        :param timeout: HTTP 请求超时时间，单位秒
        :param verify_ssl: 是否校验 HTTPS 证书
        """
        normalized_host = UrlUtils.standardize_base_url(host).rstrip("/")
        parsed = urlsplit(normalized_host)
        self._host = urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")
        self._request_utils = RequestUtils(
            use_session=True,
            timeout=timeout,
            verify=verify_ssl,
        )
        self._token: Optional[str] = None
        self._user_id: Optional[str] = None
        self._server_id: Optional[str] = None
        self._timeout = timeout
        self._verify_ssl = bool(verify_ssl)

    @property
    def host(self) -> str:
        """
        获取规范化后的媒体库服务地址。
        """
        return self._host

    @property
    def token(self) -> Optional[str]:
        """
        获取当前媒体库服务 AccessToken。
        """
        return self._token

    @property
    def user_id(self) -> Optional[str]:
        """
        获取认证响应中的媒体库用户 ID。
        """
        return self._user_id

    @property
    def server_id(self) -> Optional[str]:
        """
        获取认证响应中的媒体服务器 ID。
        """
        return self._server_id

    def close(self) -> None:
        """
        关闭媒体库服务的 HTTP 会话。
        """
        self._request_utils.close()

    def _headers(self) -> dict[str, str]:
        """
        构造 Emby 兼容接口所需的客户端请求头。
        """
        token = f', Token="{self._token}"' if self._token else ""
        return {
            "X-Emby-Authorization": (
                'MediaBrowser Client="MoviePilot", Device="requests", '
                f'DeviceId="moviepilot-ugreen", Version="1.0.0"{token}'
            ),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        path: str,
        method: str = "GET",
        params: Optional[dict[str, Any]] = None,
        data: Optional[dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Optional[Any]:
        """
        调用媒体库服务 Emby 兼容接口并解析 JSON 响应。

        :param path: 接口相对路径
        :param method: HTTP 方法
        :param params: 查询参数
        :param data: JSON 请求体
        :param authenticated: 是否附加当前 AccessToken
        :return: JSON 响应，连接失败或服务端拒绝时返回 None
        """
        request_params = dict(params or {})
        if authenticated and self._token:
            request_params["api_key"] = self._token
        url = f"{self._host}/{path.strip('/')}"
        try:
            if method.upper() == "POST":
                response = self._request_utils.post_res(
                    url=url,
                    headers=self._headers(),
                    params=request_params,
                    json=data,
                    timeout=self._timeout,
                    verify=self._verify_ssl,
                )
            else:
                response = self._request_utils.get_res(
                    url=url,
                    headers=self._headers(),
                    params=request_params,
                    timeout=self._timeout,
                    verify=self._verify_ssl,
                )
            status_code = getattr(response, "status_code", None)
            if response is None or not 200 <= (status_code or 0) < 300:
                logger.error(f"请求绿联媒体库接口失败：{url} HTTP {status_code or '无响应'}")
                return None
            return response.json()
        except Exception as err:
            logger.error(f"请求绿联媒体库接口失败：{url} {err}")
            return None

    def authenticate(self, username: str, password: str) -> Optional[UgreenMediaSession]:
        """
        使用媒体库账号登录 Emby 兼容接口。

        :param username: 媒体库账号
        :param password: 媒体库密码
        :return: 登录会话，认证失败或缺少用户 ID 时返回 None
        """
        if not username or not password:
            return None
        payload = self._request(
            "emby/Users/AuthenticateByName",
            method="POST",
            data={"Username": username, "Pw": password},
            authenticated=False,
        )
        if not isinstance(payload, Mapping):
            return None

        token = str(payload.get("AccessToken") or "").strip()
        user_data = payload.get("User")
        user = dict(user_data) if isinstance(user_data, Mapping) else {}
        user_id = str(user.get("Id") or payload.get("UserId") or payload.get("UserID") or "").strip()
        if not token or not user_id:
            logger.error("绿联媒体库服务认证失败：响应缺少 AccessToken 或用户 ID")
            return None

        self._token = token
        self._user_id = user_id
        server_id = str(payload.get("ServerId") or "").strip()
        self._server_id = server_id or None
        return UgreenMediaSession(
            token=token,
            user_id=user_id,
            server_id=self._server_id,
            user=user or None,
        )

    def current_user(self) -> Optional[dict[str, Any]]:
        """
        查询认证用户详情，以检查媒体库令牌有效性。

        :return: 用户详情字典；未登录、请求失败或响应结构不符时返回 None
        """
        if not self._user_id:
            return None
        payload = self._request(f"emby/Users/{self._user_id}")
        return dict(payload) if isinstance(payload, Mapping) else None

    def views(self) -> Optional[list[dict[str, Any]]]:
        """
        读取认证用户可见的媒体库视图。

        :return: 媒体库字典列表；未登录、请求失败或响应结构不符时返回 None
        """
        if not self._user_id:
            return None
        payload = self._request(f"emby/Users/{self._user_id}/Views")
        if not isinstance(payload, Mapping):
            return None
        items = payload.get("Items")
        return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else None

    def items(
        self,
        parent_id: Optional[Union[str, int]] = None,
        include_item_types: Optional[str] = None,
        start_index: int = 0,
        limit: Optional[int] = 100,
        ids: Optional[str] = None,
        search_term: Optional[str] = None,
        sort_by: Optional[str] = None,
        sort_order: Optional[str] = None,
        fields: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """
        查询当前用户媒体库条目。

        :param parent_id: 媒体库或父条目 ID
        :param include_item_types: Emby 条目类型过滤器
        :param start_index: 分页起始位置
        :param limit: 最大返回数量，None 表示不传限制
        :param ids: 按条目 ID 过滤，多个 ID 用逗号分隔
        :param search_term: 搜索词
        :param sort_by: 排序字段
        :param sort_order: 排序方向
        :param fields: 需要返回的字段
        :return: 含 Items 和计数等字段的响应字典；未登录、请求失败或结构不符时返回 None
        """
        if not self._user_id:
            return None
        params: dict[str, Any] = {"Recursive": "true"}
        if parent_id is not None:
            params["ParentId"] = parent_id
        if include_item_types:
            params["IncludeItemTypes"] = include_item_types
        if start_index:
            params["StartIndex"] = start_index
        if limit is not None:
            params["Limit"] = limit
        if ids:
            params["Ids"] = ids
        if search_term:
            params["SearchTerm"] = search_term
        if sort_by:
            params["SortBy"] = sort_by
        if sort_order:
            params["SortOrder"] = sort_order
        if fields:
            params["Fields"] = fields
        payload = self._request(f"emby/Users/{self._user_id}/Items", params=params)
        return dict(payload) if isinstance(payload, Mapping) else None

    def item(self, item_id: Union[str, int]) -> Optional[dict[str, Any]]:
        """
        通过当前用户的条目查询接口读取单个媒体详情。

        :param item_id: 媒体库服务条目标识
        :return: 首个匹配条目的字典；标识为空、未登录、无匹配或查询失败时返回 None
        """
        if not self._user_id or not item_id:
            return None
        payload = self.items(ids=str(item_id), limit=1)
        if not payload:
            return None
        items = payload.get("Items")
        return dict(items[0]) if isinstance(items, list) and items else None

    def counts(self) -> Optional[dict[str, Any]]:
        """
        查询媒体库服务的媒体数量统计。

        :return: 服务端原始统计字典；请求失败或响应结构不符时返回 None
        """
        payload = self._request("emby/Items/Counts")
        return dict(payload) if isinstance(payload, Mapping) else None

    def resume(self, limit: int = 20) -> Optional[list[dict[str, Any]]]:
        """
        查询当前用户的继续观看列表。

        :param limit: 最多返回条目数量，默认 20
        :return: 播放条目字典列表；未登录、请求失败或响应结构不符时返回 None
        """
        if not self._user_id:
            return None
        payload = self._request(
            f"emby/Users/{self._user_id}/Items/Resume",
            params={"Limit": limit, "MediaTypes": "Video"},
        )
        if not isinstance(payload, Mapping):
            return None
        items = payload.get("Items")
        return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else None

    def episodes(
        self, series_id: Union[str, int], season: Optional[int] = None
    ) -> Optional[list[dict[str, Any]]]:
        """
        获取指定剧集的已入库集数。

        :param series_id: 剧集条目 ID
        :param season: 可选季号
        :return: 分集条目字典列表；剧集标识为空、请求失败或结构不符时返回 None
        """
        if not series_id:
            return None
        params: dict[str, Any] = {"IsMissing": "false"}
        if season is not None:
            params["Season"] = season
        payload = self._request(f"emby/Shows/{series_id}/Episodes", params=params)
        if not isinstance(payload, Mapping):
            return None
        items = payload.get("Items")
        return [dict(item) for item in items if isinstance(item, Mapping)] if isinstance(items, list) else None

    def image_url(self, item_id: Union[str, int], image_type: str = "Primary") -> Optional[str]:
        """
        构造带媒体库访问令牌的图片地址。

        :param item_id: 媒体库条目标识
        :param image_type: 图片类型，默认 Primary 主封面
        :return: 包含 api_key 的图片 URL；缺少条目标识或令牌时返回 None，不验证图片是否存在
        """
        if not item_id or not self._token:
            return None
        return f"{self._host}/emby/Items/{item_id}/Images/{image_type}?api_key={self._token}"
