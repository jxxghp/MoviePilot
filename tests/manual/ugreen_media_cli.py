"""
手工调用绿联 NAS 登录和媒体库接口的诊断命令。

运行主入口会访问指定服务器，并可把解密结果写入指定文件；不属于自动化离线测试。
仅导入模块或解析参数不会发起网络请求，凭据由参数或交互输入提供。
"""

from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import sys
import uuid
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit

# 直接运行时移除脚本目录并加入项目根目录，避免测试目录中的同名模块遮蔽正式实现。
if __name__ == "__main__" and __package__ is None:
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.abspath(os.path.join(script_dir, "..", ".."))
    if script_dir in sys.path:
        sys.path.remove(script_dir)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

import requests

from app.modules.ugreen.crypto import UgreenCrypto


class UgreenLoginError(Exception):
    """
    手工登录或媒体库响应不符合预期时的诊断异常。
    """
    pass


def _normalize_base_url(raw: str) -> str:
    """
    将用户输入规范为不带路径的 HTTP 或 HTTPS 服务地址。

    :raises UgreenLoginError: 地址为空或没有有效网络位置
    :param raw: 服务器地址，可省略协议，此时使用 http
    :return: 仅包含协议和网络位置的基础地址
    """
    value = (raw or "").strip()
    if not value:
        raise UgreenLoginError("服务器地址不能为空")
    if not value.startswith(("http://", "https://")):
        value = f"http://{value}"
    parsed = urlsplit(value)
    if not parsed.netloc:
        raise UgreenLoginError(f"无效服务器地址: {raw}")
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


def _json_or_raise(resp: requests.Response, stage: str) -> dict[str, Any]:
    """
    读取接口响应并要求顶层为 JSON 对象。

    :raises UgreenLoginError: 不是 JSON 或顶层不是字典
    :param resp: 服务器 HTTP 响应
    :param stage: 用于错误提示的当前操作名称
    :return: 解析后的响应字典
    """
    try:
        data = resp.json()
    # 手工诊断保留非 JSON 响应片段；该网络失败分支不要求自动覆盖。
    except Exception as exc:  # pragma: no cover
        raise UgreenLoginError(
            f"{stage} 返回非 JSON，HTTP {resp.status_code}，响应片段: {resp.text[:200]}"
        ) from exc
    if not isinstance(data, dict):
        raise UgreenLoginError(f"{stage} 返回格式异常: {type(data).__name__}")
    return data


def _decode_public_key(raw: str) -> str:
    """
    读取直接提供或经 Base64 编码的公钥文本。

    :raises UgreenLoginError: 公钥为空或 Base64 内容无法解码
    :param raw: 接口返回的公钥；包含 BEGIN 时按原文使用
    :return: 可供 RSA 加密使用的公钥文本
    """
    value = (raw or "").strip()
    if not value:
        raise UgreenLoginError("未获取到公钥")
    if "BEGIN" in value:
        return value
    try:
        return base64.b64decode(value).decode("utf-8")
    except Exception as exc:
        raise UgreenLoginError("公钥解码失败") from exc


def _raise_if_failed(payload: Mapping[str, Any], stage: str) -> None:
    """
    要求业务响应码为 200，否则生成阶段性错误。

    :raises UgreenLoginError: code 不为 200
    :param payload: 接口返回的业务响应映射
    :param stage: 当前操作名称，加入错误提示
    """
    if payload.get("code") == 200:
        return
    raise UgreenLoginError(
        f"{stage}失败: code={payload.get('code')} msg={payload.get('msg')}"
    )


def _build_common_headers(
    client_id: str, client_version: str, language: str
) -> dict[str, str]:
    """
    构造绿联网页客户端使用的通用请求头。

    :param client_id: 本次客户端标识
    :param client_version: 兼容客户端的版本号文本
    :param language: 服务端响应语言标识
    :return: 包含客户端身份和语言的请求头字典
    """
    return {
        "Accept": "application/json, text/plain, */*",
        "Client-Id": client_id,
        "Client-Version": client_version,
        "UG-Agent": "PC/WEB",
        "X-Specify-Language": language,
    }


def _login_and_get_access(
    session: requests.Session,
    base_url: str,
    username: str,
    password: str,
    keepalive: bool,
    headers: Mapping[str, str],
    timeout: float,
    verify_ssl: bool,
) -> tuple[str, str]:
    """
    获取登录公钥、加密密码并换取访问令牌及会话公钥。

    业务失败或缺失凭据抛出 UgreenLoginError；网络异常向上传播。

    :param session: 复用的 requests 会话，由调用方提供
    :param base_url: 已规范化的服务根地址
    :param username: 登录用户名
    :param password: 明文密码，仅在客户端加密后放入登录请求
    :param keepalive: 是否请求保持登录
    :param headers: 登录接口通用请求头
    :param timeout: 各请求超时秒数
    :param verify_ssl: 是否校验 HTTPS 证书
    :return: 访问令牌和解码后的会话公钥
    """
    check_resp = session.post(
        f"{base_url}/ugreen/v1/verify/check",
        json={"username": username},
        headers=dict(headers),
        timeout=timeout,
        verify=verify_ssl,
    )
    check_json = _json_or_raise(check_resp, "获取登录公钥")
    _raise_if_failed(check_json, "获取登录公钥")

    rsa_token = (
        check_resp.headers.get("x-rsa-token")
        or check_resp.headers.get("X-Rsa-Token")
        or check_json.get("xRsaToken")
        or check_json.get("x-rsa-token")
    )
    if not rsa_token:
        data = check_json.get("data")
        if isinstance(data, Mapping):
            rsa_token = data.get("xRsaToken") or data.get("x-rsa-token")
    if not rsa_token:
        raise UgreenLoginError("登录公钥为空（x-rsa-token）")

    login_public_key = _decode_public_key(str(rsa_token))
    encrypted_password = UgreenCrypto(public_key=login_public_key).rsa_encrypt_long(
        password
    )

    login_payload = {
        "username": username,
        "password": encrypted_password,
        "keepalive": keepalive,
        "otp": True,
        "is_simple": True,
    }
    login_resp = session.post(
        f"{base_url}/ugreen/v1/verify/login",
        json=login_payload,
        headers=dict(headers),
        timeout=timeout,
        verify=verify_ssl,
    )
    login_json = _json_or_raise(login_resp, "登录")
    _raise_if_failed(login_json, "登录")

    data = login_json.get("data")
    if not isinstance(data, Mapping):
        raise UgreenLoginError("登录成功但响应 data 为空")

    token = str(data.get("token") or "").strip()
    public_key = str(data.get("public_key") or "").strip()
    if not token:
        raise UgreenLoginError("登录成功但未拿到 token")
    if not public_key:
        raise UgreenLoginError("登录成功但未拿到 public_key")
    return token, _decode_public_key(public_key)


def _fetch_media_lib(
    session: requests.Session,
    base_url: str,
    token: str,
    public_key: str,
    client_id: str,
    client_version: str,
    language: str,
    page: int,
    page_size: int,
    timeout: float,
    verify_ssl: bool,
) -> Any:
    """
    构造加密请求并解密指定页的媒体库列表响应。

    :param session: 复用的 requests 会话
    :param base_url: 已规范化的服务根地址
    :param token: 登录得到的访问令牌
    :param public_key: 用于请求加密的会话公钥
    :param client_id: 客户端标识，需与登录使用值一致
    :param client_version: 客户端兼容版本
    :param language: 响应语言标识
    :param page: 请求页码，默认入口从 1 开始
    :param page_size: 每页条目数
    :param timeout: 请求超时秒数
    :param verify_ssl: 是否校验 HTTPS 证书
    :return: 解密后的接口响应；业务成功码由上层检查
    """
    crypto = UgreenCrypto(
        public_key=public_key,
        token=token,
        client_id=client_id,
        client_version=client_version,
        ug_agent="PC/WEB",
        language=language,
    )
    req = crypto.build_encrypted_request(
        url=f"{base_url}/ugreen/v1/video/homepage/media_list",
        method="GET",
        params={"page": page, "page_size": page_size},
    )
    media_resp = session.get(
        req.url,
        headers=req.headers,
        params=req.params,
        timeout=timeout,
        verify=verify_ssl,
    )
    media_json = _json_or_raise(media_resp, "获取媒体库")
    return crypto.decrypt_response(media_json, req.aes_key)


def parse_args(argv: list[str]) -> argparse.Namespace:
    """
    解析手工诊断选项，不读取交互输入或访问服务器。

    :param argv: 不含脚本名称的命令行参数
    :return: 包含连接、分页及输出选项的参数对象
    """
    parser = argparse.ArgumentParser(
        description="登录绿联 NAS 并调用媒体库接口（自动处理请求加密/响应解密）"
    )
    parser.add_argument("--host", help="服务器地址，例如: http://192.168.20.101:9999")
    parser.add_argument("--username", help="用户名")
    parser.add_argument("--password", help="密码（不传则交互输入）")
    parser.add_argument("--client-id", help="可选，默认自动生成 UUID-WEB")
    parser.add_argument("--client-version", default="76363", help="默认: 76363")
    parser.add_argument("--language", default="zh-CN", help="默认: zh-CN")
    parser.add_argument("--page", type=int, default=1, help="默认: 1")
    parser.add_argument("--page-size", type=int, default=50, help="默认: 50")
    parser.add_argument("--timeout", type=float, default=20.0, help="默认: 20 秒")
    parser.add_argument("--insecure", action="store_true", help="忽略 HTTPS 证书校验")
    parser.add_argument(
        "--no-keepalive",
        action="store_true",
        help="关闭保持登录（默认保持登录）",
    )
    parser.add_argument("--pretty", action="store_true", help="美化输出 JSON")
    parser.add_argument("--output", help="将解密后的结果写入文件")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """
    登录指定 NAS，读取一页媒体库并输出解密结果。

    缺少地址或凭据时交互读取；--output 指定时覆盖目标文件，否则输出到标准输出。
    文件写入和未归类异常继续向上传播；此入口会执行真实网络请求，不应由普通单元测试直接调用。

    :param argv: 命令行参数；None 或空列表时读取 sys.argv
    :return: 成功返回 0，业务响应错误返回 1，网络错误返回 2
    """
    args = parse_args(argv or sys.argv[1:])

    host = args.host or input("服务器地址: ").strip()
    username = args.username or input("用户名: ").strip()
    password = args.password or getpass.getpass("密码: ")
    client_id = (args.client_id or f"{uuid.uuid4()}-WEB").strip()
    keepalive = not args.no_keepalive
    verify_ssl = not args.insecure

    try:
        base_url = _normalize_base_url(host)
        if args.insecure:
            # requests 动态转发 urllib3，类型声明不包含该兼容属性。
            requests.packages.urllib3.disable_warnings()  # type: ignore[attr-defined]

        session = requests.Session()
        headers = _build_common_headers(
            client_id=client_id,
            client_version=args.client_version,
            language=args.language,
        )

        token, public_key = _login_and_get_access(
            session=session,
            base_url=base_url,
            username=username,
            password=password,
            keepalive=keepalive,
            headers=headers,
            timeout=args.timeout,
            verify_ssl=verify_ssl,
        )
        decoded = _fetch_media_lib(
            session=session,
            base_url=base_url,
            token=token,
            public_key=public_key,
            client_id=client_id,
            client_version=args.client_version,
            language=args.language,
            page=args.page,
            page_size=args.page_size,
            timeout=args.timeout,
            verify_ssl=verify_ssl,
        )

        if isinstance(decoded, Mapping):
            if decoded.get("code") != 200:
                raise UgreenLoginError(
                    f"媒体库接口失败: code={decoded.get('code')} msg={decoded.get('msg')}"
                )
            media_count = None
            data = decoded.get("data")
            if isinstance(data, Mapping) and isinstance(data.get("media_lib_info_list"), list):
                media_count = len(data["media_lib_info_list"])
            print(
                f"调用成功: code={decoded.get('code')} msg={decoded.get('msg')} "
                f"media_lib_info_list={media_count}"
            )

        text = json.dumps(
            decoded,
            ensure_ascii=False,
            indent=2 if args.pretty else None,
            separators=(",", ":") if not args.pretty else None,
        )
        if args.output:
            with open(args.output, "w", encoding="utf-8") as f:
                f.write(text)
                f.write("\n")
            print(f"解密结果已写入: {args.output}")
        else:
            print(text)
        return 0
    except UgreenLoginError as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"网络错误: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
