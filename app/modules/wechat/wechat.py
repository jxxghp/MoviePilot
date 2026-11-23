"""
企业微信自建应用的访问令牌、消息发送、菜单和临时素材处理。
"""

import base64
import json
import re
import subprocess
import threading
from datetime import datetime
from functools import partial
from hashlib import file_digest, sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable, Dict, List, Literal, Optional

from app.adapters.network.http import RequestUtils
from app.application.messaging.image import build_wechat_image_url
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation.url import UrlUtils
from app.runtime.execution import retry
from app.runtime.log import logger
from app.schemas.delivery import (
    DeliveryPartPlanner,
    DeliveryPartSender,
    DeliveryPartSubmitter,
    DeliveryReceipt,
    DeliveryStatus,
)

lock = threading.Lock()

# 企业微信临时图片素材上限为 10 MiB，仅接受 JPG、PNG。
_IMAGE_MAX_BYTES = 10 * 1024 * 1024
# 企业微信普通文件素材上限为 20 MiB，不限制文件扩展名。
_FILE_MAX_BYTES = 20 * 1024 * 1024


class RetryException(Exception):
    """
    标识可重试的令牌获取或令牌过期失败。
    """
    pass


class WeChat:
    """
    封装企业微信自建应用的消息、菜单和临时素材接口。
    """
    # 最近一次成功取得的企业微信访问令牌
    _access_token = None
    # 企业微信访问令牌有效期，单位为秒
    _expires_in: Optional[int] = None
    # 最近一次成功获取访问令牌的本地时间
    _access_token_time: Optional[datetime] = None
    # 企业标识
    _corpid = None
    # 自建应用密钥
    _appsecret = None
    # 自建应用标识
    _appid = None
    # 官方接口或反向代理基址
    _proxy = None

    # 企业微信消息发送地址模板
    _send_msg_url = "cgi-bin/message/send?access_token={access_token}"
    # 企业微信令牌获取地址模板
    _token_url = "cgi-bin/gettoken?corpid={corpid}&corpsecret={corpsecret}"
    # 企业微信菜单创建地址模板
    _create_menu_url = "cgi-bin/menu/create?access_token={access_token}&agentid={agentid}"
    # 企业微信菜单删除地址模板
    _delete_menu_url = "cgi-bin/menu/delete?access_token={access_token}&agentid={agentid}"
    # 企业微信媒体下载地址模板
    _download_media_url = "cgi-bin/media/get?access_token={access_token}&media_id={media_id}"
    # 企业微信临时素材上传地址模板
    _upload_media_url = "cgi-bin/media/upload?access_token={access_token}&type={media_type}"

    @classmethod
    def probe_app_identity(cls, corp_id: str, app_id: str, secret: str) -> str | None:
        """
        使用官方接口验证自建应用密钥是否属于指定企业和应用。

        :param corp_id: 已存在连接的企业标识
        :param app_id: 已存在连接的应用数字标识
        :param secret: 待核验的应用密钥
        :return: 经官方响应确认的企业和应用标识
        """
        if not re.fullmatch(r"[A-Za-z0-9_-]+", corp_id) or not re.fullmatch(r"[1-9][0-9]*", app_id) or not secret:
            return None
        token_response = cls._read_identity_result("gettoken", {"corpid": corp_id, "corpsecret": secret})
        token = token_response.get("access_token")
        if not isinstance(token, str) or not token:
            return None
        detail = cls._read_identity_result("agent/get", {"access_token": token, "agentid": app_id})
        returned_id = detail.get("agentid")
        return f"{corp_id}:{app_id}" if type(returned_id) in (int, str) and str(returned_id) == app_id else None

    @staticmethod
    def _read_identity_result(method: str, params: dict[str, str]) -> dict[str, Any]:
        """
        从平台固定地址读取身份凭证，不跟随跳转或输出平台原始响应。

        :param method: 受支持的企业微信身份 API 相对路径
        :param params: 调用官方接口所需的查询参数
        :return: 明确成功的对象响应
        """
        response = RequestUtils(verify=True, timeout=8).get_res(
            f"https://qyapi.weixin.qq.com/cgi-bin/{method}", params=params, allow_redirects=False,
        )
        if response is None:
            return {}
        try:
            payload = response.json() if response.status_code == 200 else None
        except ValueError:
            return {}
        finally:
            response.close()
        if not isinstance(payload, dict) or type(payload.get("errcode")) is not int or payload["errcode"] != 0:
            return {}
        return payload

    def __init__(self, WECHAT_CORPID: Optional[str] = None, WECHAT_APP_SECRET: Optional[str] = None,
                 WECHAT_APP_ID: Optional[str] = None, WECHAT_PROXY: Optional[str] = None,
                 **kwargs: object) -> None:
        """
        读取应用凭据并立即尝试获取访问令牌。

        :param WECHAT_CORPID: 企业标识；缺少必需凭据时仅记录错误并提前结束初始化
        :param WECHAT_APP_SECRET: 自建应用密钥
        :param WECHAT_APP_ID: 自建应用标识
        :param WECHAT_PROXY: 企业微信接口基址；为空时使用官方地址
        :param kwargs: 通知实例传入的其他配置，当前忽略
        """
        if not WECHAT_CORPID or not WECHAT_APP_SECRET or not WECHAT_APP_ID:
            logger.error("企业微信配置不完整！")
            return
        self._corpid = WECHAT_CORPID
        self._appsecret = WECHAT_APP_SECRET
        self._appid = WECHAT_APP_ID
        self._proxy = WECHAT_PROXY or "https://qyapi.weixin.qq.com"

        if self._proxy:
            urls = (
                ("_send_msg_url", self._send_msg_url),
                ("_token_url", self._token_url),
                ("_create_menu_url", self._create_menu_url),
                ("_delete_menu_url", self._delete_menu_url),
                ("_download_media_url", self._download_media_url),
                ("_upload_media_url", self._upload_media_url),
            )
            for attribute, template in urls:
                adapted_url = UrlUtils.adapt_request_url(self._proxy, template)
                if isinstance(adapted_url, str):
                    setattr(self, attribute, adapted_url)

        if self._corpid and self._appsecret and self._appid:
            self.__get_access_token()

    def get_state(self) -> bool:
        """
        通过缓存或刷新访问令牌检查应用可用性。

        :return: 能取得访问令牌时返回 True，否则返回 False
        """
        return True if self.__get_access_token() else False

    @retry(RetryException, logger=logger)
    def __get_access_token(self, force: bool = False) -> Optional[str]:
        """
        复用未过期访问令牌，必要时请求新令牌。

        刷新失败且已有令牌时仍可能返回旧值；无响应时由重试装饰器重试。

        :param force: 是否忽略有效期并强制刷新
        :return: 当前缓存的访问令牌；尚无令牌且获取失败时为 None
        """
        token_flag = True
        if not self._access_token:
            token_flag = False
        else:
            if (
                self._access_token_time is None
                or self._expires_in is None
                or (datetime.now() - self._access_token_time).total_seconds() >= self._expires_in
            ):
                token_flag = False

        if not token_flag or force:
            if not self._corpid or not self._appsecret:
                return None
            token_url = self._token_url.format(corpid=self._corpid, corpsecret=self._appsecret)
            res = RequestUtils().get_res(token_url)
            if res:
                ret_json = res.json()
                if ret_json.get("errcode") == 0:
                    access_token = ret_json.get("access_token")
                    expires_in = ret_json.get("expires_in")
                    if isinstance(access_token, str) and isinstance(expires_in, int):
                        self._access_token = access_token
                        self._expires_in = expires_in
                        self._access_token_time = datetime.now()
            elif res is not None:
                logger.error(f"获取微信access_token失败，错误码：{res.status_code}，错误原因：{res.reason}")
            else:
                logger.error("获取微信access_token失败，未获取到返回信息")
                raise RetryException("获取微信access_token失败，重试中...")
        return self._access_token

    @staticmethod
    def __split_content(content: str, max_bytes: int = 2048) -> List[str]:
        """
        按 UTF-8 字节数拆分文本并保留完整字符。

        :param content: 待发送文本；每个分块首尾空白会被移除
        :param max_bytes: 每块目标字节上限，默认 2048；单个字符超过上限时保留整个字符
        :return: 按原顺序排列的文本分块
        """
        content_chunks = []
        current_chunk = bytearray()

        for line in content.splitlines():
            encoded_line = (line + "\n").encode("utf-8")
            line_length = len(encoded_line)

            if line_length > max_bytes:
                if current_chunk:
                    content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())
                    current_chunk = bytearray()

                start = 0
                while start < line_length:
                    end = start + max_bytes
                    if end >= line_length:
                        end = line_length
                    else:
                        # 调整以避免拆分多字节字符
                        while end > start and (encoded_line[end] & 0xC0) == 0x80:
                            end -= 1
                        if end == start:
                            # 单个字符超过了 max_bytes，强制包含整个字符
                            end = start + 1
                            while end < line_length and (encoded_line[end] & 0xC0) == 0x80:
                                end += 1
                    truncated_line = encoded_line[start:end].decode("utf-8", errors="replace")
                    content_chunks.append(truncated_line.strip())
                    start = end
                continue

            if len(current_chunk) + line_length > max_bytes:
                content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())
                current_chunk = bytearray()

            current_chunk += encoded_line

        if current_chunk:
            content_chunks.append(current_chunk.decode("utf-8", errors="replace").strip())

        return content_chunks

    def _text_parts(self, title: str | None, text: str | None, link: str | None) -> list[str]:
        """
        渲染原生文本，并按平台字节限制分段。

        :param title: 消息标题
        :param text: 消息正文
        :param link: 追加在正文后的跳转地址
        :return: 有序文本分段
        """
        if not title and not text:
            return []
        if text:
            formatted_text = text.replace("\n\n", "\n")
            content = f"{title}\n{formatted_text}" if title else formatted_text
        else:
            assert title is not None
            content = title
        if link:
            content = f"{content}\n点击查看：{link}"
        return self.__split_content(content)

    def _text_payload(self, target: str, text: str) -> dict[str, Any]:
        """
        构造单段文本的发送载荷。

        :param target: 接收用户或显式全员地址
        :param text: 渲染后的单段正文
        :return: 不含访问令牌的请求体
        """
        return {
            "touser": target, "msgtype": "text", "agentid": self._appid,
            "text": {"content": text}, "safe": 0, "enable_id_trans": 0, "enable_duplicate_check": 0,
        }

    def __send_message(self, title: Optional[str], text: Optional[str] = None,
                       userid: Optional[str] = None, link: Optional[str] = None) -> bool:
        """
        按顺序发送传统客户端的文本分段。

        :param title: 消息标题
        :param text: 正文
        :param userid: 企业微信用户标识，缺省沿用传统客户端的全员目标
        :param link: 追加在正文后的跳转地址
        :return: 所有分段均发送成功时返回 True
        """
        parts = self._text_parts(title, text, link)
        for chunk in parts:
            try:
                # 任一分块失败即停止后续发送；已发出的分块不会撤回
                if not self.__post_request(self._send_msg_url, self._text_payload(userid or "@all", chunk)):
                    return False
            except Exception as e:
                logger.error(f"发送消息块失败：{e}")
                return False
        return bool(parts)

    def _news_payload(
        self, title: str | None, text: str | None, image_url: str, target: str, link: str | None,
    ) -> dict[str, Any]:
        """
        构造包含封面的一篇图文通知。

        :param title: 图文标题
        :param text: 图文摘要
        :param image_url: 封面地址
        :param target: 接收用户或显式全员地址
        :param link: 图文跳转链接
        :return: 不含访问令牌的请求体
        """
        title = title or next((line.strip() for line in (text or "").splitlines() if line.strip()), "MoviePilot")
        return {
            "touser": target, "msgtype": "news", "agentid": self._appid,
            "news": {"articles": [{
                "title": title, "description": text.replace("\n\n", "\n") if text else text,
                "picurl": build_wechat_image_url(image_url), "url": link,
            }]},
        }

    def __send_image_message(self, title: Optional[str], text: Optional[str], image_url: str,
                             userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
        """
        发送包含单篇文章的图文消息。

        :param title: 图文标题
        :param text: 图文摘要
        :param image_url: 封面原始地址，经企业微信图片地址适配后发送
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :param link: 点击图文时打开的地址
        :return: 接口接受消息时返回 True，否则返回 False
        """
        try:
            return self.__post_request(self._send_msg_url, self._news_payload(title, text, image_url, userid or "@all", link))
        except Exception as e:
            logger.error(f"发送图文消息失败：{e}")
            return False

    def send_msg(self, title: Optional[str], text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None, *,
                 native_image: bool = False) -> Optional[bool]:
        """
        获取访问令牌后发送文本、图文或显式指定的原生图片消息。

        :param title: 消息标题
        :param text: 消息正文
        :param image: 图片地址；默认作为图文封面，原生图片还支持 Base64 数据地址
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :param link: 消息跳转地址
        :param native_image: 有图片时上传并发送原生图片；标题和正文另发文本，忽略跳转地址
        :return: 接口接受消息时返回 True；发送失败返回 False；无访问令牌返回 None
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            if image and native_image:
                return self._send_native_image(image=image, title=title, text=text, userid=userid)
            if image:
                ret_code = self.__send_image_message(title=title, text=text, image_url=image, userid=userid, link=link)
            else:
                ret_code = self.__send_message(title=title, text=text, userid=userid, link=link)

            return ret_code
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    def send_tracked_message(
        self, title: str | None, text: str | None, image: str | None, userid: str | None, link: str | None,
        *, receipt: DeliveryReceipt, part_sender: DeliveryPartSender, plan: DeliveryPartPlanner | None = None,
    ) -> bool:
        """
        向明确目标发送图文或分段文本，并逐段收集平台确认。

        :param title: 消息标题
        :param text: 消息正文
        :param image: 可选封面地址
        :param userid: 显式用户地址列表，或显式全员地址
        :param link: 消息跳转地址
        :param receipt: 宿主记录逐段结果的回调
        :param part_sender: 宿主发送前确认投递进度的回调
        :param plan: 在首次发送前登记当前连接的投递清单
        :return: 全部目标的所有分段均得到确认时返回 True
        """
        targets = self._notification_targets(userid, receipt)
        if not targets:
            return False
        chunks = self._text_parts(title, text, link) if not image else []
        if not chunks and not image:
            receipt(None, 0, "skipped", "empty_content")
            return False
        rendered: dict[str, list[tuple[str, DeliveryPartSubmitter]]] = {}
        for target in targets:
            payloads = ([self._news_payload(title, text, image, target, link)] if image else
                        [self._text_payload(target, chunk) for chunk in chunks])
            rendered[target] = [self._tracked_text_or_news(payload) for payload in payloads]
        if plan is not None:
            plan(tuple(
                (target, part, content)
                for target, payloads in rendered.items()
                for part, (content, _) in enumerate(payloads)
            ))
        success = True
        for target, content_parts in rendered.items():
            success = self._send_tracked_target(target, content_parts, receipt, part_sender) and success
        return success

    def _tracked_text_or_news(self, payload: dict[str, Any]) -> tuple[str, DeliveryPartSubmitter]:
        """
        将冻结清单用的内容与实际平台提交绑定。
        """
        content = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return content, partial(self._send_tracked_part, payload)

    def send_tracked_media(
        self, source: str, name: str | None, *, media_type: Literal["file", "image"],
        title: str | None, text: str | None, userid: str | None, receipt: DeliveryReceipt,
        part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
    ) -> bool:
        """
        将说明文字与原图或文件分别记录为有顺序的企业微信投递分段。

        :param source: 本地附件路径或原生图片地址
        :param name: 普通文件的可见文件名
        :param media_type: 平台素材类别
        :param title: 可选说明标题
        :param text: 可选说明正文
        :param userid: 宿主解析出的明确目标
        :param receipt: 记录逐段平台结果的回调
        :param part_sender: 根据已保存的结果认领分段的回调
        :param plan: 首次发送前冻结全部目标与分段的回调
        :return: 所有目标确认收到说明及附件
        """
        targets = self._notification_targets(userid, receipt)
        if not targets:
            return False
        media = self._load_tracked_media(source, name, media_type)
        if media is None:
            receipt(None, 0, "permanent_failure", "attachment_unavailable")
            return False
        path, data = media
        digest = json.dumps([media_type, self._appid, path.name, sha256(data).hexdigest()], ensure_ascii=False)
        chunks = self._text_parts(title, text, None)
        rendered = {}
        for target in targets:
            parts = [self._tracked_text_or_news(self._text_payload(target, chunk)) for chunk in chunks]
            parts.append((digest, partial(self._send_media_part, target, path, data, media_type)))
            rendered[target] = parts
        plan(tuple((target, part, content)
                   for target, payloads in rendered.items()
                   for part, (content, _) in enumerate(payloads)))
        success = True
        for target, parts in rendered.items():
            success = self._send_tracked_target(target, parts, receipt, part_sender) and success
        return success

    def _load_tracked_media(
        self, source: str, name: str | None, media_type: Literal["file", "image"],
    ) -> tuple[Path, bytes] | None:
        """
        有界读取原始媒体，保存平台可见的文件名及用于冻结清单的字节。
        """
        if media_type == "image":
            data = self._load_native_image(source)
            if data is None:
                return None
            suffix = ".png" if self._guess_mime_type(data) == "image/png" else ".jpg"
            return Path(f"image{suffix}"), data
        path = Path(source).expanduser()
        try:
            with path.open("rb") as stream:
                data = stream.read(_FILE_MAX_BYTES + 1)
        except OSError:
            return None
        if not 5 < len(data) <= _FILE_MAX_BYTES:
            return None
        return Path(name or path.name), data

    def _send_media_part(
        self, target: str, path: Path, data: bytes, media_type: Literal["file", "image"],
    ) -> tuple[DeliveryStatus, str | None]:
        """
        认领分段后上传素材，按同一个明确目标记录平台发送结果。
        """
        try:
            media_id = self._upload_temp_media(path, media_type=media_type, content=data)
        except RetryException:
            media_id = None
        if media_id is None:
            return "retryable_failure", "attachment_upload_failed"
        return self._send_tracked_part({
            "touser": target, "msgtype": media_type, "agentid": self._appid,
            media_type: {"media_id": media_id},
        })

    @staticmethod
    def _notification_targets(userid: str | None, receipt: DeliveryReceipt) -> tuple[str, ...]:
        """
        拆出显式用户目标，拒绝把全员地址与个人地址混用。

        :param userid: 宿主选定的发送目标
        :param receipt: 缺少目标或地址无效时报告诊断
        :return: 去重后的目标；不可投递时为空
        """
        targets = tuple(dict.fromkeys(target.strip() for target in (userid or "").split("|") if target.strip()))
        if not targets:
            receipt(None, 0, "skipped", "no_target")
        elif targets != ("@all",) and any(
            target.casefold() == "@all" or not re.fullmatch(r"[A-Za-z0-9_@.-]+", target)
            for target in targets
        ):
            receipt(None, 0, "permanent_failure", "invalid_target")
            return ()
        return targets

    def _send_tracked_target(
        self, target: str, payloads: list[tuple[str, DeliveryPartSubmitter]], receipt: DeliveryReceipt,
        part_sender: DeliveryPartSender,
    ) -> bool:
        """
        在同一目标内保持分段顺序，未确认的分段暂停后续提交。

        :param target: 已选定的单一平台目标
        :param payloads: 有序的分段内容描述和提交回调
        :param receipt: 回传实际确认状态的回调
        :param part_sender: 根据已有投递记录控制网络请求的回调
        :return: 当前目标的所有分段均得到确认时返回 True
        """
        confirmed = True
        for part, (content, submit) in enumerate(payloads):
            outcome, code = part_sender(target, part, content, confirmed, submit)
            receipt(target, part, outcome, code)
            confirmed = confirmed and outcome == "confirmed"
        return confirmed

    def _send_tracked_part(self, payload: dict[str, Any]) -> tuple[DeliveryStatus, str | None]:
        """
        仅在获得明确平台响应时确认发送状态。

        :param payload: 渲染后的单段载荷
        :return: 平台确认状态与脱敏分类
        """
        try:
            token = self.__get_access_token()
        except RetryException:
            token = None
        if not token:
            return "retryable_failure", "token_unavailable"
        try:
            result = self._read_post_result(self._send_msg_url, payload, token)
        except (ValueError, RuntimeError, OSError):
            return "unknown", "response_unconfirmed"
        code = result.get("errcode")
        if code == 0:
            if any(result.get(field) for field in ("invaliduser", "unlicenseduser", "invalidparty", "invalidtag")):
                return "permanent_failure", "target_rejected"
            return "confirmed", None
        if code in (40014, 41001, 42001):
            # 清空当前令牌，让 Outbox 的下一次认领取得新令牌。
            if self._access_token == token:
                self._access_token = None
            return "retryable_failure", "token_expired"
        if code in (-1, 45009, 45011):
            return "retryable_failure", "platform_unavailable"
        if code in (40003, 81013):
            return "permanent_failure", "target_rejected"
        return "unknown", "platform_unconfirmed"

    @staticmethod
    def _load_native_image(image: str) -> Optional[bytes]:
        """
        读取原生图片并限制下载量，不在磁盘或图片缓存中保存二维码。

        :param image: HTTP(S) 图片地址、JPG/PNG 的 Base64 数据地址或本地图片路径
        :return: 大于 5 字节且不超过 10 MiB 的 JPG/PNG 内容；来源、格式或大小无效返回 None
        """
        content: Optional[bytes]
        if image.startswith("data:image/"):
            try:
                header, encoded = image.split(",", 1)
                if not header.endswith(";base64") or len(encoded) > ((_IMAGE_MAX_BYTES + 2) // 3) * 4:
                    logger.error("企业微信原生图片数据地址无效或超过 10MB")
                    return None
                content = base64.b64decode(encoded, validate=True)
            except ValueError:
                logger.error("企业微信原生图片 Base64 数据无效")
                return None
        elif image.startswith(("https://", "http://")):
            content = WeChat._download_native_image(image)
        elif "://" not in image:
            with Path(image).expanduser().open("rb") as image_file:
                content = image_file.read(_IMAGE_MAX_BYTES + 1)
        else:
            logger.error("企业微信原生图片仅支持 HTTP(S)、Base64 或本地图片路径")
            return None

        if not content or not 5 < len(content) <= _IMAGE_MAX_BYTES or WeChat._guess_mime_type(content, "") not in {
            "image/jpeg", "image/png",
        }:
            logger.error("企业微信原生图片必须为大于 5 字节且不超过 10MB 的 JPG/PNG")
            return None
        return content

    @staticmethod
    def _download_native_image(image: str) -> Optional[bytes]:
        """
        有界读取图片响应，并在成功、拒绝或读取异常时关闭连接。

        :param image: 插件显式指定的 HTTP(S) 图片地址
        :return: 下载得到的原始字节；无响应、非 200 响应或超过 10 MiB 时返回 None
        """
        response = RequestUtils(timeout=30).get_res(image, stream=True)
        if response is None:
            return None
        try:
            if response.status_code != 200:
                logger.error(f"下载企业微信原生图片失败，HTTP 状态码：{response.status_code}")
                return None
            chunks = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if len(chunks) + len(chunk) > _IMAGE_MAX_BYTES:
                    logger.error("企业微信原生图片超过 10MB")
                    return None
                chunks.extend(chunk)
            return bytes(chunks)
        finally:
            response.close()

    def _send_native_image(self, image: str, title: Optional[str], text: Optional[str],
                           userid: Optional[str]) -> bool:
        """
        上传原图并发送图片消息，附带文字独立发送且不使用图文卡片。

        :param image: HTTP(S) 或 Base64 图片地址
        :param title: 可选的说明标题
        :param text: 可选的说明正文
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :return: 图片及附带文字均发送成功时返回 True；任一步骤失败返回 False
        """
        content = self._load_native_image(image)
        if content is None:
            return False
        suffix = ".png" if content.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
        media_id = self._upload_temp_media(Path(f"image{suffix}"), media_type="image", content=content)
        if not media_id:
            return False
        if (title or text) and not self.__send_message(title=title, text=text, userid=userid):
            return False
        return self.__post_request(self._send_msg_url, {
            "touser": userid or "@all",
            "msgtype": "image",
            "agentid": self._appid,
            "image": {"media_id": media_id},
        })

    @staticmethod
    def _guess_mime_type(content: bytes, default: str = "image/jpeg") -> str:
        """
        根据图片文件头识别常见图片格式。

        :param content: 图片字节
        :param default: 内容为空或格式未知时使用的 MIME 类型
        :return: 识别出的 MIME 类型或默认值
        """
        if not content:
            return default
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if content.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if content.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if content.startswith(b"BM"):
            return "image/bmp"
        if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
            return "image/webp"
        return default

    def download_media_to_data_url(self, media_id: str) -> Optional[str]:
        """
        下载临时媒体并编码为图片数据地址。

        :param media_id: 企业微信回调中的临时媒体标识
        :return: 包含 MIME 类型的 Base64 数据地址；标识为空、令牌缺失或下载失败返回 None
        """
        if not media_id:
            return None
        access_token = self.__get_access_token()
        if not access_token:
            logger.error("下载企业微信媒体失败：access_token 获取失败")
            return None
        req_url = self._download_media_url.format(
            access_token=access_token,
            media_id=media_id,
        )
        try:
            res = RequestUtils(timeout=30).get_res(req_url)
        except Exception as err:
            logger.error(f"下载企业微信媒体失败：{err}")
            return None
        if not res or not res.content:
            return None

        content_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/json":
            try:
                logger.error(f"企业微信媒体下载失败：{res.json()}")
            except Exception:
                logger.error(f"企业微信媒体下载失败：{res.text}")
            return None

        mime_type = self._guess_mime_type(res.content, content_type or "image/jpeg")
        return f"data:{mime_type};base64,{base64.b64encode(res.content).decode()}"

    def download_media_bytes(self, media_id: str) -> Optional[bytes]:
        """
        下载临时媒体的原始内容。

        :param media_id: 企业微信回调中的语音、图片或文件标识
        :return: 媒体字节；标识为空、令牌缺失、下载失败或接口返回 JSON 错误时返回 None
        """
        if not media_id:
            return None
        access_token = self.__get_access_token()
        if not access_token:
            logger.error("下载企业微信媒体失败：access_token 获取失败")
            return None
        req_url = self._download_media_url.format(
            access_token=access_token,
            media_id=media_id,
        )
        try:
            res = RequestUtils(timeout=30).get_res(req_url)
        except Exception as err:
            logger.error(f"下载企业微信媒体失败：{err}")
            return None
        if not res or not res.content:
            return None
        content_type = (res.headers.get("Content-Type") or "").split(";")[0].strip()
        if content_type == "application/json":
            try:
                logger.error(f"企业微信媒体下载失败：{res.json()}")
            except Exception:
                logger.error(f"企业微信媒体下载失败：{res.text}")
            return None
        content = res.content
        return content if isinstance(content, bytes) else None

    @staticmethod
    def _convert_voice_to_amr(voice_path: str, destination: Path | None = None) -> Optional[Path]:
        """
        调用 ffmpeg 生成最多 60 秒的单声道 AMR 文件。

        输出采样率为 8000 Hz；这里只生成文件，后续发送流程负责清理。

        :param voice_path: 源语音路径
        :param destination: 调用方独占的输出路径；旧调用缺省为同目录同名文件
        :return: 转换后文件路径；转换失败返回 None，超过 2 MiB 的输出会被删除
        """
        src_path = Path(voice_path)
        if not src_path.exists():
            logger.error(f"语音文件不存在：{src_path}")
            return None

        dst_path = destination or src_path.with_suffix(".amr")
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(src_path),
            "-ar",
            "8000",
            "-ac",
            "1",
            "-t",
            "60",
            str(dst_path),
        ]
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
        except Exception as err:
            logger.error(f"调用 ffmpeg 转换 AMR 失败：{err}")
            return None

        if result.returncode != 0 or not dst_path.exists():
            logger.error(
                "ffmpeg 转换 AMR 失败: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or "").strip()[:500],
            )
            return None

        if dst_path.stat().st_size > 2 * 1024 * 1024:
            logger.error("AMR 语音文件超过 2MB，无法发送到企业微信")
            dst_path.unlink(missing_ok=True)
            return None
        return dst_path

    def _upload_temp_media(self, media_path: Path, media_type: str = "voice", *,
                           content: Optional[bytes] = None) -> Optional[str]:
        """
        上传临时素材并取得企业微信媒体标识。

        :param media_path: 本地素材路径；传入 content 时仅使用文件名，不读取磁盘
        :param media_type: 企业微信素材类型，支持 voice、image 和 file，默认 voice
        :param content: 可直接上传的素材字节；为空时从 media_path 读取，保留语音调用兼容性
        :return: 上传成功的 media_id；读取、请求或响应解析失败返回 None
        """
        access_token = self.__get_access_token()
        if not access_token:
            return None
        req_url = self._upload_media_url.format(
            access_token=access_token,
            media_type=media_type,
        )
        try:
            # 先读入字节并关闭文件，避免上传过程持有文件句柄
            if content is None:
                content = media_path.read_bytes()

            mime_type = "voice/amr" if media_type == "voice" else "application/octet-stream"
            if media_type == "image":
                mime_type = self._guess_mime_type(content)

            # 只声明响应格式，让请求库按文件上传内容生成 Content-Type
            response = RequestUtils(timeout=60).request(
                method="post",
                url=req_url,
                headers={"Accept": "application/json"},
                files={
                    "media": (
                        media_path.name,
                        content,
                        mime_type,
                    )
                },
            )
        except Exception as err:
            logger.error(f"上传企业微信临时素材失败：{err}")
            return None

        if not response:
            return None

        try:
            ret_json = response.json()
        except Exception as err:
            logger.error(f"解析企业微信临时素材响应失败：{err}")
            return None

        if ret_json.get("errcode") != 0:
            logger.error(f"上传企业微信临时素材失败：{ret_json}")
            return None
        media_id = ret_json.get("media_id")
        return media_id if isinstance(media_id, str) else None

    def send_file(self, file_path: str, file_name: Optional[str] = None, title: Optional[str] = None,
                  text: Optional[str] = None, userid: Optional[str] = None) -> Optional[bool]:
        """
        上传并发送普通文件，保留调用方文件且不回退为下载链接。

        :param file_path: 本地文件路径，支持展开用户目录；内容须大于 5 字节且不超过 20 MiB
        :param file_name: 接收端显示的文件名；为空时使用源文件名，仅保留名称部分
        :param title: 可选的说明标题，随正文独立发送为文本
        :param text: 可选的说明正文
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :return: 文件及说明文字均成功时返回 True；失败返回 False；无访问令牌返回 None
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None
            source = Path(file_path).expanduser()
            with source.open("rb") as media_file:
                content = media_file.read(_FILE_MAX_BYTES + 1)
            if not 5 < len(content) <= _FILE_MAX_BYTES:
                logger.error("企业微信文件必须大于 5 字节且不超过 20MB")
                return False
            media_id = self._upload_temp_media(Path(file_name or source.name), media_type="file", content=content)
            if not media_id:
                return False
            if (title or text) and not self.__send_message(title=title, text=text, userid=userid):
                return False
            return self.__post_request(self._send_msg_url, {
                "touser": userid or "@all",
                "msgtype": "file",
                "agentid": self._appid,
                "file": {"media_id": media_id},
            })
        except Exception as err:
            logger.error(f"发送企业微信文件消息失败：{err}")
            return False

    def send_voice(self, voice_path: str, userid: Optional[str] = None) -> Optional[bool]:
        """
        转换并上传语音，然后通过自建应用发送。

        :param voice_path: 源语音文件；成功转换并进入上传流程后，无论发送成败都会删除源文件和转换文件
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :return: 发送成功返回 True；转换或发送失败返回 False；无访问令牌返回 None
        """
        if not voice_path:
            return False
        if not self.__get_access_token():
            logger.error("获取微信access_token失败，请检查参数配置")
            return None
        if not userid:
            userid = "@all"

        source_path = Path(voice_path)
        converted_path = self._convert_voice_to_amr(voice_path)
        if not converted_path:
            return False

        try:
            media_id = self._upload_temp_media(converted_path, media_type="voice")
            if not media_id:
                return False

            req_json = {
                "touser": userid,
                "msgtype": "voice",
                "agentid": self._appid,
                "voice": {
                    "media_id": media_id
                },
                "safe": 0,
                "enable_id_trans": 0,
                "enable_duplicate_check": 0
            }
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as err:
            logger.error(f"发送企业微信语音消息失败：{err}")
            return False
        finally:
            converted_path.unlink(missing_ok=True)
            source_path.unlink(missing_ok=True)

    def send_tracked_voice(
        self, voice_path: str, userid: str | None, *,
        receipt: DeliveryReceipt, part_sender: DeliveryPartSender, plan: DeliveryPartPlanner,
        fallback: Callable[[], bool | None] | None = None,
    ) -> bool:
        """
        在受管投递中用源附件的摘要识别语音，保留调用方持有的源文件供恢复使用。

        :param voice_path: 调用方提供的语音文件
        :param userid: 已选定的目标
        :param receipt: 宿主记录逐目标结果的回调
        :param part_sender: 根据已有投递结果控制发送的回调
        :param plan: 首次发送前冻结目标与附件摘要
        :param fallback: 本地转换失败时按原目标和投递策略发送正文
        :return: 所有目标确认收到语音或降级后的正文
        """
        targets = self._notification_targets(userid, receipt)
        if not targets:
            return False
        try:
            with Path(voice_path).open("rb") as stream:
                digest = file_digest(stream, "sha256").hexdigest()
        except OSError:
            digest = None
        with TemporaryDirectory(prefix="moviepilot-wechat-voice-") as folder:
            converted = self._convert_voice_to_amr(voice_path, Path(folder) / "voice.amr") if digest is not None else None
            if digest is not None and converted is None and fallback is not None:
                return fallback() is True
            content = f"voice:{self._appid}:{digest or 'missing'}"
            error = "attachment_unavailable" if digest is None else "attachment_conversion_failed"
            plan(tuple((target, 0, content) for target in targets))
            success = True
            for target in targets:
                outcome, code = part_sender(
                    target, 0, content, True, partial(self._send_voice_part, converted, target, error),
                )
                receipt(target, 0, outcome, code)
                success = success and outcome == "confirmed"
            return success

    def _send_voice_part(
        self, converted: Path | None, target: str, conversion_error: str,
    ) -> tuple[DeliveryStatus, str | None]:
        """
        在当前目标获准提交后上传语音，并返回独立的发送确认。

        :param converted: 本次准备的语音附件，无法读取或转换时为空
        :param target: 当前唯一投递目标
        :param conversion_error: 附件不可用时的诊断
        :return: 平台结果或可诊断的附件错误
        """
        if converted is None:
            return "permanent_failure", conversion_error
        media_id = self._upload_temp_media(converted, media_type="voice")
        if media_id is None:
            return "retryable_failure", "attachment_upload_failed"
        return self._send_tracked_part({
            "touser": target, "msgtype": "voice", "agentid": self._appid,
            "voice": {"media_id": media_id}, "safe": 0,
            "enable_id_trans": 0, "enable_duplicate_check": 0,
        })

    def send_medias_msg(self, medias: List[MediaInfo], userid: Optional[str] = None) -> Optional[bool]:
        """
        按传入顺序发送媒体候选图文列表。

        :param medias: 媒体候选项；首项使用主图，其余使用海报
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :return: 接口接受列表时返回 True；失败返回 False；无访问令牌返回 None
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            if not userid:
                userid = "@all"
            articles = []
            index = 1
            for media in medias:
                if media.vote_average:
                        title = f"{index}. {media.title_year}\n类型：{media.type.value if media.type else ''}，评分：{media.vote_average}"
                else:
                    title = f"{index}. {media.title_year}\n类型：{media.type.value if media.type else ''}"
                articles.append({
                    "title": title,
                    "description": "",
                    "picurl": build_wechat_image_url(
                        media.get_message_image() if index == 1 else media.get_poster_image()
                    ),
                    "url": media.detail_link
                })
                index += 1

            req_json = {
                "touser": userid,
                "msgtype": "news",
                "agentid": self._appid,
                "news": {
                    "articles": articles
                }
            }
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    def send_torrents_msg(self, torrents: List[Context],
                          userid: Optional[str] = None, title: Optional[str] = None,
                          link: Optional[str] = None) -> Optional[bool]:
        """
        发送可选标题和种子候选图文列表。

        :param torrents: 含种子和媒体详情的上下文，按传入顺序展示
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :param title: 单独发送的可选标题，其发送结果不决定列表返回值
        :param link: 标题消息的跳转地址；各种子仍使用自己的详情地址
        :return: 列表发送成功返回 True；失败返回 False；无访问令牌返回 None
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            if title:
                self.__send_message(title=title, userid=userid, link=link)

            if not userid:
                userid = "@all"
            articles = []
            index = 1
            for context in torrents:
                torrent = context.torrent_info
                if torrent is None:
                    continue
                meta = MetaInfo(title=torrent.title or "", subtitle=torrent.description)
                mediainfo = context.media_info
                torrent_title = f"{index}.【{torrent.site_name}】" \
                    f"{meta.season_episode} " \
                    f"{meta.resource_term} " \
                    f"{meta.video_term} " \
                    f"{meta.release_group} " \
                    f"{size_tools.format_compact_size(torrent.size)} " \
                    f"{torrent.volume_factor} " \
                    f"{torrent.seeders}↑"
                torrent_title = re.sub(r"\s+", " ", torrent_title).strip()
                articles.append({
                    "title": torrent_title,
                    "description": torrent.description if index == 1 else "",
                    "picurl": build_wechat_image_url(
                        mediainfo.get_message_image() if index == 1 and mediainfo else ""
                    ),
                    "url": torrent.page_url
                })
                index += 1

            req_json = {
                "touser": userid,
                "msgtype": "news",
                "agentid": self._appid,
                "news": {
                    "articles": articles
                }
            }
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

    @staticmethod
    def _read_post_result(url: str, payload: dict[str, Any], token: str) -> dict[str, Any]:
        """
        提交请求并读取经校验的平台响应。

        :param url: 发送接口的地址模板
        :param payload: 要发送的请求体
        :param token: 当前有效的访问令牌
        :return: 含整数错误码的企业微信响应
        :raises RuntimeError: 平台未返回可确认的响应
        """
        response = RequestUtils(content_type="application/json").post(
            url=url.format(access_token=token), data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            allow_redirects=False,
        )
        if response is None:
            raise RuntimeError("企业微信接口未返回响应")
        try:
            if response.status_code != 200:
                raise RuntimeError(f"企业微信接口响应状态：{response.status_code}")
            result = response.json()
        finally:
            response.close()
        if not isinstance(result, dict) or type(result.get("errcode")) is not int:
            raise RuntimeError("企业微信接口缺少有效错误码")
        return result

    @retry(RetryException, logger=logger)
    def __post_request(self, url: str, req_json: Dict[str, Any]) -> bool:
        """
        提交企业微信 JSON 请求并处理令牌过期。

        错误码 40014、41001、42001 均表示令牌不可用，会强制刷新令牌并触发重试；
        无响应和非 200 响应抛出异常。

        :param url: 可包含 access_token 占位符的接口地址
        :param req_json: 需要序列化为 JSON 的请求体
        :return: 接口错误码为 0 时返回 True，其他非令牌过期错误返回 False
        """
        token = self.__get_access_token()
        if not token:
            return False
        ret_json = self._read_post_result(url, req_json, token)
        if ret_json.get("errcode") == 0:
            return True
        else:
            if ret_json.get("errcode") in (40014, 41001, 42001):
                self.__get_access_token(force=True)
                error_msg = (f"access_token 已失效，尝试重新获取 access_token,"
                             f"errcode: {ret_json.get('errcode')}, errmsg: {ret_json.get('errmsg')}")
                raise RetryException(error_msg)
            else:
                logger.error(f"发送请求失败，错误信息：{ret_json.get('errmsg')}")
                return False

    def create_menus(self, commands: Dict[str, Dict[str, Any]]) -> None:
        """
        按命令分类创建应用菜单，忽略没有分类的命令。

        按输入顺序最多保留 3 个一级分类，每类最多 5 个命令。创建异常只记录日志。

        :param commands: 命令名到描述和分类的映射；读取 description 和 category 字段
        """
        try:
            req_url = self._create_menu_url.format(access_token="{access_token}", agentid=self._appid)

            category_dict: Dict[str, Dict[str, Dict[str, Any]]] = {}
            for key, value in commands.items():
                category = value.get("category")
                if not isinstance(category, str):
                    continue
                if category:
                    if not category_dict.get(category):
                        category_dict[category] = {}
                    category_dict[category][key] = value

            buttons = []
            for category, menu in category_dict.items():
                sub_buttons = []
                for key, value in menu.items():
                    sub_buttons.append({
                        "type": "click",
                        "name": value.get("description"),
                        "key": key
                    })
                buttons.append({
                    "name": category,
                    "sub_button": sub_buttons[:5]
                })

            if buttons:
                self.__post_request(req_url, {
                    "button": buttons[:3]
                })
        except Exception as e:
            logger.error(f"创建菜单失败：{e}")

    def delete_menus(self) -> None:
        """
        请求删除当前应用菜单，异常只记录日志。
        """
        try:
            req_url = self._delete_menu_url.format(access_token=self.__get_access_token(), agentid=self._appid)
            RequestUtils().get(req_url)
        except Exception as e:
            logger.error(f"删除菜单失败：{e}")
