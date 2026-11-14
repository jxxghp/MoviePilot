"""
QQ 主动通知发送、历史互动对象缓存和后台网关生命周期管理。
"""

import hashlib
import io
import pickle
import threading
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image

from app.adapters.network.http import RequestUtils
from app.application.messaging.ingress import submit_message_to_host
from app.domain.context import Context, MediaInfo
from app.domain.metainfo import MetaInfo
from app.foundation import size as size_tools
from app.modules.qqbot.api import (
    get_access_token,
    get_gateway_url,
    send_proactive_c2c_message,
    send_proactive_group_message,
)
from app.modules.qqbot.gateway import run_gateway
from app.runtime.cache import FileCache
from app.runtime.log import logger
from app.runtime.thread import ThreadHelper

# QQ Markdown 图片展示尺寸限制，单位像素，避免竖版海报被客户端拉伸变形
_DEFAULT_IMAGE_SIZE: Tuple[int, int] = (208, 320)
_MAX_IMAGE_SIZE: Tuple[int, int] = (512, 512)


class QQBot:
    """
    QQ Bot 通知客户端。
    """

    # 停止时等待网关线程的最长秒数；超时交由上层决定后续处理。
    _gateway_join_timeout_seconds = 20

    def __init__(
            self,
            QQ_APP_ID: Optional[str] = None,
            QQ_APP_SECRET: Optional[str] = None,
            QQ_OPENID: Optional[str] = None,
            QQ_GROUP_OPENID: Optional[str] = None,
            name: Optional[str] = None,
            **kwargs: Any,
    ) -> None:
        """
        保存机器人配置、恢复已互动对象，并启动后台网关线程。

        缺少应用凭据时保持未就绪状态；完整配置会读取文件缓存并开始网络连接。

        :param QQ_APP_ID: 机器人应用标识，必填
        :param QQ_APP_SECRET: 机器人应用密钥，必填
        :param QQ_OPENID: 默认单聊接收人标识
        :param QQ_GROUP_OPENID: 默认群聊标识；同时配置单聊时优先使用群聊
        :param name: 通知配置名称，用于区分消息来源和持久化缓存
        :param kwargs: 兼容额外配置字段，当前不使用
        """
        self._gateway_stop = None
        self._gateway_thread: Optional[threading.Thread] = None
        self._gateway_ws_holder: list[Any] = []
        if not QQ_APP_ID or not QQ_APP_SECRET:
            logger.error("QQ Bot 配置不完整：缺少 AppID 或 AppSecret")
            self._ready = False
            return

        self._app_id = QQ_APP_ID
        self._app_secret = QQ_APP_SECRET
        self._default_openid = QQ_OPENID
        self._default_group_openid = QQ_GROUP_OPENID
        self._config_name = name or "qqbot"
        self._ready = True

        # 曾发过消息的用户/群，用于无默认接收者时的广播 {(target_id, is_group), ...}
        self._known_targets: set[tuple[str, bool]] = set()
        _safe_name = hashlib.md5(self._config_name.encode()).hexdigest()[:12]
        self._cache_key = f"__qqbot_known_targets_{_safe_name}__"
        self._filecache = FileCache()
        self._load_known_targets()
        # 已处理的消息 ID，用于去重（避免同一条消息重复处理）
        self._processed_msg_ids: set[object] = set()
        self._max_processed_ids = 1000

        self._gateway_stop = threading.Event()
        self._gateway_thread = None
        self._start_gateway()

        logger.info("QQ Bot 客户端初始化完成")

    def _load_known_targets(self) -> None:
        """
        从缓存加载曾互动的用户/群。
        """
        try:
            content = self._filecache.get(self._cache_key)
            if content:
                data = pickle.loads(content)
                if isinstance(data, (list, set)):
                    self._known_targets = set(tuple(x) for x in data)
        except Exception as e:
            logger.debug(f"QQ Bot 加载 known_targets 失败: {e}")

    def _save_known_targets(self) -> None:
        """
        持久化曾互动的用户/群到缓存。
        """
        try:
            self._filecache.set(self._cache_key, pickle.dumps(list(self._known_targets)))
        except Exception as e:
            logger.debug(f"QQ Bot 保存 known_targets 失败: {e}")

    def _forward_to_message_chain(self, payload: dict[str, Any]) -> bool:
        """
        通过受管线程池把 QQ Bot 入站载荷 转交统一消息入口。
        """
        return submit_message_to_host(
            payload,
            self._config_name,
            submit=ThreadHelper().submit,
        )

    def _on_gateway_message(self, payload: dict[str, Any]) -> None:
        """
        Gateway 收到消息时转发至 MP 消息链，并记录发送者用于广播。
        """
        msg_id = payload.get("id")
        if msg_id:
            if msg_id in self._processed_msg_ids:
                logger.debug(f"QQ Bot: 跳过重复消息 id={msg_id}")
                return
            self._processed_msg_ids.add(msg_id)
            if len(self._processed_msg_ids) > self._max_processed_ids:
                self._processed_msg_ids.clear()

        # 记录发送者，用于无默认接收者时的广播
        msg_type = payload.get("type")
        if msg_type == "C2C_MESSAGE_CREATE":
            openid = (payload.get("author") or {}).get("user_openid")
            if openid:
                self._known_targets.add((openid, False))
                self._save_known_targets()
        elif msg_type == "GROUP_AT_MESSAGE_CREATE":
            group_openid = payload.get("group_openid")
            if group_openid:
                self._known_targets.add((group_openid, True))
                self._save_known_targets()

        self._forward_to_message_chain(payload)

    def _start_gateway(self) -> None:
        """
        启动 Gateway WebSocket 连接（后台线程）。
        """
        try:
            self._gateway_thread = threading.Thread(
                target=run_gateway,
                kwargs={
                    "app_id": self._app_id,
                    "app_secret": self._app_secret,
                    "config_name": self._config_name,
                    "get_token_fn": get_access_token,
                    "get_gateway_url_fn": get_gateway_url,
                    "on_message_fn": self._on_gateway_message,
                    "stop_event": self._gateway_stop,
                    "ws_holder": self._gateway_ws_holder,
                },
                daemon=True,
            )
            self._gateway_thread.start()
            logger.info(f"QQ Bot Gateway 已启动: {self._config_name}")
        except Exception as e:
            logger.error(f"QQ Bot Gateway 启动失败: {e}")

    def stop(self) -> bool:
        """
        通知网关停止并关闭连接，最多等待后台线程 20 秒。

        :return: 网关线程已终止或从未启动时返回 True；仍存活时返回 False
        """
        if self._gateway_stop is not None:
            self._gateway_stop.set()
        try:
            if self._gateway_ws_holder:
                self._gateway_ws_holder[0].close()
        except Exception as e:
            logger.debug(f"QQ Bot Gateway WebSocket close: {e}")
        gateway_thread = self._gateway_thread
        if (
            gateway_thread is not None
            and gateway_thread.is_alive()
            and gateway_thread is not threading.current_thread()
        ):
            gateway_thread.join(timeout=self._gateway_join_timeout_seconds)
        if gateway_thread is not None and gateway_thread.is_alive():
            logger.warning(
                "QQ Bot Gateway 线程在 stop 后仍未退出，可能存在重复收消息，请重启进程"
            )
            return False
        return True

    def get_state(self) -> bool:
        """
        返回初始化时的凭据就绪状态。

        :return: 应用标识和密钥齐全时返回 True；不代表网关当前在线
        """
        return self._ready

    def _get_target(
        self, userid: Optional[str] = None, targets: Optional[dict[str, Any]] = None
    ) -> tuple[Optional[str], bool]:
        """
        按显式接收人、目标字典、默认配置的顺序选择发送目标。

        :return: 目标标识及是否群聊的二元组；未配置时为 (None, False)
        """
        # 优先使用 userid（可能是 openid）
        if userid:
            # 格式支持：group:xxx 表示群聊
            if str(userid).lower().startswith("group:"):
                return userid[6:].strip(), True
            return str(userid), False

        if targets:
            qq_openid = targets.get("qq_userid") or targets.get("qq_openid")
            qq_group = targets.get("qq_group_openid") or targets.get("qq_group")
            if qq_group:
                return str(qq_group), True
            if qq_openid:
                return str(qq_openid), False

        if self._default_group_openid:
            return self._default_group_openid, True
        if self._default_openid:
            return self._default_openid, False

        return None, False

    def _get_broadcast_targets(self) -> list[tuple[str, bool]]:
        """
        获取广播目标列表（曾发过消息的用户/群）。
        """
        return list(self._known_targets)

    @staticmethod
    def _get_image_size(url: str) -> Optional[Tuple[int, int]]:
        """
        请求图片前 64 KiB 并用文件头解析像素尺寸。

        使用 Range 请求；服务端可能忽略范围并返回完整文件，解析仅使用前 65536 字节。

        :param url: 可下载的图片地址
        :return: 宽高像素二元组；请求或解析失败返回 None
        """
        try:
            resp = RequestUtils(timeout=5).get_res(
                url,
                headers={"Range": "bytes=0-65535", "User-Agent": "QQBot-Image-Size-Detector/1.0"},
            )
            if not resp or not resp.content:
                return None
            data = resp.content[:65536] if len(resp.content) > 65536 else resp.content
            with Image.open(io.BytesIO(data)) as img:
                return img.width, img.height
        except Exception as e:
            logger.debug(f"QQ Bot 获取图片尺寸失败 ({url[:60]}...): {e}")
            return None

    @staticmethod
    def _fit_image_size(size: Optional[Tuple[int, int]]) -> Tuple[int, int]:
        """
        计算 QQ Markdown 图片展示尺寸，保持原始比例并限制最大边长。
        """
        if not size:
            return _DEFAULT_IMAGE_SIZE
        width, height = size
        if width <= 0 or height <= 0:
            return _DEFAULT_IMAGE_SIZE

        max_width, max_height = _MAX_IMAGE_SIZE
        scale = min(max_width / width, max_height / height, 1)
        display_width = max(1, round(width * scale))
        display_height = max(1, round(height * scale))
        return display_width, display_height

    @staticmethod
    def _escape_markdown(text: str) -> str:
        """
        转义 Markdown 特殊字符；保留圆括号，避免 QQ 误解析转义后的括号。
        """
        if not text:
            return ""
        text = text.replace("\\", "\\\\")
        for char in ("*", "_", "[", "]", "`"):
            text = text.replace(char, f"\\{char}")
        return text

    @staticmethod
    def _format_message_markdown(
            title: Optional[str] = None,
            text: Optional[str] = None,
            image: Optional[str] = None,
            link: Optional[str] = None,
            formatted_text: Optional[str] = None,
    ) -> tuple[str, bool]:
        """
        将通知标题、正文、图片和链接拼接为 QQ Markdown。

        :param title: 可选标题，去除换行后加粗
        :param text: 待转义的普通正文
        :param image: 图片地址；HTTP(S) 图片会附带受限展示尺寸
        :param link: 可选详情链接
        :param formatted_text: 已格式化正文，非空时优先于 text 且不再转义
        :return: 正文及是否包含非空内容的二元组
        """
        parts = []
        if title:
            # 标题加粗，移除可能破坏格式的换行
            safe_title = (title or "").replace("\n", " ").strip()
            if safe_title:
                parts.append(f"**{QQBot._escape_markdown(safe_title)}**")
        if formatted_text:
            parts.append(formatted_text)
        elif text:
            parts.append(QQBot._escape_markdown((text or "").strip()))
        if image:
            # QQ Markdown 图片需带尺寸才能正确渲染，格式: ![alt #宽px #高px](url)，否则会显示为 [图片] 文本。
            # 这里使用展示尺寸而非原图尺寸，避免竖版海报被 QQ 客户端塞进固定区域时变形。
            img_url = (image or "").strip()
            if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
                size = QQBot._get_image_size(img_url)
                w, h = QQBot._fit_image_size(size)
                if size:
                    logger.debug(f"QQ Bot 图片尺寸: {size[0]}x{size[1]} -> {w}x{h} - {img_url[:60]}...")
                parts.append(f"![image #{w}px #{h}px]({img_url})")
            elif img_url:
                parts.append(img_url)
        if link:
            link_url = (link or "").strip()
            if link_url:
                parts.append(f"[查看详情]({link_url})")
        content = "\n\n".join(p for p in parts if p).strip()
        return content, bool(content)

    def send_msg(
            self,
            title: str,
            text: Optional[str] = None,
            image: Optional[str] = None,
            link: Optional[str] = None,
            userid: Optional[str] = None,
            targets: Optional[dict[str, Any]] = None,
            **kwargs: Any,
    ) -> bool:
        """
        向指定目标或历史互动对象发送主动通知。

        未指定目标时依次使用默认配置和已互动对象。Markdown 权限错误会回退到完整纯文本，
        并停止向该目标发送余下分段；其他目标继续尝试。

        :param title: 通知标题
        :param text: 普通正文，也用于权限不足时的纯文本回退
        :param image: 可选图片地址，Markdown 使用尺寸标记嵌入
        :param link: 可选详情链接
        :param userid: 优先使用的 openid，group: 前缀表示群聊
        :param targets: 渠道目标字典；未指定 userid 时读取，群聊优先
        :param kwargs: buttons 提供按钮，formatted_texts 提供已格式化分段正文
        :return: 至少一个目标的一段消息成功时返回 True；全部失败、未就绪或没有接收人时返回 False
        """
        if not self._ready:
            return False

        target, is_group = self._get_target(userid, targets)
        targets_to_send = []
        if target:
            targets_to_send = [(target, is_group)]
        else:
            # 无默认接收者时，向曾发过消息的用户/群广播
            broadcast = self._get_broadcast_targets()
            if broadcast:
                targets_to_send = broadcast
                logger.debug(f"QQ Bot: 广播模式，共 {len(targets_to_send)} 个目标")
            else:
                logger.warn(
                    "QQ Bot: 未指定接收者且无互动用户，请在配置中设置 QQ_OPENID/QQ_GROUP_OPENID 或先让用户发消息")
                return False

        content, use_markdown = self._format_message_markdown(title=title, text=text, image=image, link=link)
        logger.info(f"QQ Bot 发送内容 (use_markdown={use_markdown}):\n{content}")

        if not content:
            logger.warn("QQ Bot: 消息内容为空")
            return False

        keyboard = self._build_keyboard(kwargs.get("buttons"))
        use_markdown = bool(keyboard) or use_markdown
        contents = [self._format_message_markdown(title=title, formatted_text=part, image=image, link=link)[0]
                    for part in kwargs.get("formatted_texts", [])] or [content]

        success_count = 0
        try:
            token = get_access_token(self._app_id, self._app_secret)
            for tgt, tgt_is_group in targets_to_send:
                send_fn = send_proactive_group_message if tgt_is_group else send_proactive_c2c_message
                for body in contents:
                    try:
                        send_fn(token, tgt, body, use_markdown=use_markdown, keyboard=keyboard)
                        success_count += 1
                        logger.debug(f"QQ Bot: 消息已发送到 {'群' if tgt_is_group else '用户'} {tgt}")
                    except Exception as e:
                        err_msg = str(e)
                        if use_markdown and ("markdown" in err_msg.lower() or "11244" in err_msg or "权限" in err_msg):
                            plain_content = self._plain_content(title, text, image, link)
                            send_fn(token, tgt, plain_content, use_markdown=False, keyboard=None)
                            success_count += 1
                            logger.debug(f"QQ Bot: Markdown 不可用，已回退纯文本发送至 {tgt}")
                        else:
                            logger.error(f"QQ Bot 发送失败 ({tgt}): {e}")
                        break
            return success_count > 0
        except Exception as e:
            logger.error(f"QQ Bot 发送失败: {e}")
            return False

    @staticmethod
    def _build_keyboard(buttons: Optional[List[List[Dict[str, Any]]]]) -> Optional[Dict[str, Any]]:
        """
        把统一按钮行转换为 QQ 键盘，按 30 个字符截断按钮标题。

        :return: QQ 键盘配置；没有有效按钮行时返回 None
        """
        rows = []
        btn_id = 1
        for row in buttons or []:
            btns = []
            for btn in row:
                btns.append({
                    "id": str(btn_id),
                    "render_data": {
                        "label": btn.get("text", "按钮")[:30],
                        "visited_label": btn.get("text", "按钮")[:30],
                        "style": 1,
                    },
                    "action": {
                        "type": 0 if btn.get("url") else 2,
                        "data": btn.get("url") if btn.get("url") else btn.get("callback_data", ""),
                        "permission": {"type": 2},
                    },
                })
                btn_id += 1
            if btns:
                rows.append({"buttons": btns})
        return {"rows": rows} if rows else None

    @staticmethod
    def _plain_content(title: str, text: Optional[str], image: Optional[str], link: Optional[str]) -> str:
        """
        权限不足时恢复原通知的完整纯文本，避免每个富文本分段重复回退。
        """
        parts = [f"【{title}】"] if title else []
        parts.extend(part for part in (text, image, link) if part)
        return "\n".join(parts).strip()

    def send_medias_msg(
            self,
            medias: List[MediaInfo],
            userid: Optional[str] = None,
            title: Optional[str] = None,
            link: Optional[str] = None,
            **kwargs: Any,
    ) -> bool:
        """
        将媒体列表转为文本后按主动消息规则发送。

        :param medias: 待展示的媒体列表，空列表不发送
        :param userid: openid 或带 group: 前缀的群聊标识
        :param title: 可选列表标题；为空时使用默认标题
        :param link: 可选详情链接
        :param kwargs: 转交 send_msg 的目标字典、按钮和格式化分段等参数
        :return: 至少一段消息发送成功时返回 True，空列表或发送失败返回 False
        """
        if not medias:
            return False
        lines = [
            f"{i + 1}. {m.title_year} - {(m.type.value if m.type else '')}"
            for i, m in enumerate(medias)
        ]
        text = "\n".join(lines)
        return self.send_msg(
            title=title or "媒体列表",
            text=text,
            link=link,
            userid=userid,
            **kwargs,
        )

    def send_torrents_msg(
            self,
            torrents: List[Context],
            userid: Optional[str] = None,
            title: Optional[str] = None,
            link: Optional[str] = None,
            **kwargs: Any,
    ) -> bool:
        """
        将种子列表转为文本后按主动消息规则发送。

        :param torrents: 待展示的种子列表，空列表不发送
        :param userid: openid 或带 group: 前缀的群聊标识
        :param title: 可选列表标题；为空时使用默认标题
        :param link: 可选详情链接
        :param kwargs: 转交 send_msg 的目标字典、按钮和格式化分段等参数
        :return: 至少一段消息发送成功时返回 True，空列表或发送失败返回 False
        """
        if not torrents:
            return False
        lines = []
        for i, ctx in enumerate(torrents):
            t = ctx.torrent_info
            if t is None:
                continue
            meta = MetaInfo(str(t.title or ""), t.description)
            name = f"{meta.season_episode} {meta.resource_term} {meta.video_term}"
            name = " ".join(name.split())
            lines.append(f"{i + 1}.【{t.site_name}】{name} {size_tools.format_compact_size(t.size)} {t.seeders}↑")
        text = "\n".join(lines)
        return self.send_msg(
            title=title or "种子列表",
            text=text,
            link=link,
            userid=userid,
            **kwargs,
        )
