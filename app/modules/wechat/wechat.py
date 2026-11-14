"""
企业微信自建应用的访问令牌、消息发送、菜单和临时素材处理。
"""

import base64
import json
import re
import subprocess
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.adapters.network.http import RequestUtils
from app.application.messaging.image import build_wechat_image_url
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.foundation.url import UrlUtils
from app.runtime.execution import retry
from app.runtime.log import logger

lock = threading.Lock()


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

    def __send_message(self, title: Optional[str], text: Optional[str] = None,
                       userid: Optional[str] = None, link: Optional[str] = None) -> bool:
        """
        分块发送文本，任一分块失败即终止后续发送。

        :param title: 消息标题；与正文不能同时为空
        :param text: 正文，连续两个换行会合并为一个
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :param link: 追加在正文后的跳转地址
        :return: 所有分块均发送成功时返回 True，否则返回 False
        """
        if not title and not text:
            logger.error("消息标题和内容不能都为空")
            return False
        if text:
            formatted_text = text.replace("\n\n", "\n")
            content = f"{title}\n{formatted_text}"
        else:
            assert title is not None
            content = title
        if link:
            content = f"{content}\n点击查看：{link}"
        if not userid:
            userid = "@all"
        content_chunks = self.__split_content(content)
        for chunk in content_chunks:
            req_json = {
                "touser": userid,
                "msgtype": "text",
                "agentid": self._appid,
                "text": {
                    "content": chunk
                },
                "safe": 0,
                "enable_id_trans": 0,
                "enable_duplicate_check": 0
            }
            try:
                # 任一分块失败即停止后续发送；已发出的分块不会撤回
                if not self.__post_request(self._send_msg_url, req_json):
                    return False
            except Exception as e:
                logger.error(f"发送消息块失败：{e}")
                return False
        return True

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
        if text:
            text = text.replace("\n\n", "\n")
        if not userid:
            userid = "@all"
        req_json = {
            "touser": userid,
            "msgtype": "news",
            "agentid": self._appid,
            "news": {
                "articles": [
                    {
                        "title": title,
                        "description": text,
                        "picurl": build_wechat_image_url(image_url),
                        "url": link
                    }
                ]
            }
        }
        try:
            return self.__post_request(self._send_msg_url, req_json)
        except Exception as e:
            logger.error(f"发送图文消息失败：{e}")
            return False

    def send_msg(self, title: Optional[str], text: Optional[str] = None, image: Optional[str] = None,
                 userid: Optional[str] = None, link: Optional[str] = None) -> Optional[bool]:
        """
        获取访问令牌后发送文本或图文消息。

        :param title: 消息标题
        :param text: 消息正文
        :param image: 封面地址；非空时使用图文消息，否则使用文本
        :param userid: 企业微信用户标识；为空时发送给全体应用成员
        :param link: 消息跳转地址
        :return: 接口接受消息时返回 True；发送失败返回 False；无访问令牌返回 None
        """
        try:
            if not self.__get_access_token():
                logger.error("获取微信access_token失败，请检查参数配置")
                return None

            if image:
                ret_code = self.__send_image_message(title=title, text=text, image_url=image, userid=userid, link=link)
            else:
                ret_code = self.__send_message(title=title, text=text, userid=userid, link=link)

            return ret_code
        except Exception as e:
            logger.error(f"发送消息失败：{e}")
            return False

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
    def _convert_voice_to_amr(voice_path: str) -> Optional[Path]:
        """
        调用 ffmpeg 生成最多 60 秒的单声道 AMR 文件。

        输出采样率为 8000 Hz；这里只生成文件，后续发送流程负责清理。

        :param voice_path: 源语音路径；输出为同目录同名 .amr 文件，允许覆盖
        :return: 转换后文件路径；转换失败返回 None，超过 2 MiB 的输出会被删除
        """
        src_path = Path(voice_path)
        if not src_path.exists():
            logger.error(f"语音文件不存在：{src_path}")
            return None

        dst_path = src_path.with_suffix(".amr")
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

    def _upload_temp_media(self, media_path: Path, media_type: str = "voice") -> Optional[str]:
        """
        上传临时素材并取得企业微信媒体标识。

        :param media_path: 需要完整读入内存的本地素材路径
        :param media_type: 企业微信素材类型，默认 voice
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
            with media_path.open("rb") as media_file:
                file_content = media_file.read()

            # 只声明响应格式，让请求库按文件上传内容生成 Content-Type
            response = RequestUtils(timeout=60).request(
                method="post",
                url=req_url,
                headers={"Accept": "application/json"},
                files={
                    "media": (
                        media_path.name,
                        file_content,
                        "voice/amr" if media_type == "voice" else "application/octet-stream",
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
        url = url.format(access_token=self.__get_access_token())
        res = RequestUtils(content_type="application/json").post(
            url=url,
            data=json.dumps(req_json, ensure_ascii=False).encode("utf-8")
        )
        if res is None:
            error_msg = "发送请求失败，未获取到返回信息"
            raise Exception(error_msg)
        if res.status_code != 200:
            error_msg = f"发送请求失败，错误码：{res.status_code}，错误原因：{res.reason}"
            raise Exception(error_msg)

        ret_json = res.json()
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
