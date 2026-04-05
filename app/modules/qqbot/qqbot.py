"""
QQ Bot 通知客户端
基于 QQ 开放平台 API，支持主动消息推送和 Gateway 接收消息
"""

import hashlib
import io
import pickle
import threading
from typing import Optional, List, Tuple

from PIL import Image

from app.core.cache import FileCache
from app.core.config import settings
from app.core.context import MediaInfo, Context
from app.core.metainfo import MetaInfo
from app.log import logger
from app.modules.qqbot.api import (
    get_access_token,
    get_gateway_url,
    send_proactive_c2c_message,
    send_proactive_group_message,
)
from app.modules.qqbot.gateway import run_gateway
from app.utils.http import RequestUtils
from app.utils.string import StringUtils

# QQ Markdown 图片默认尺寸（获取失败时使用，与 OpenClaw 对齐）
_DEFAULT_IMAGE_SIZE: Tuple[int, int] = (512, 512)


class QQBot:
    """QQ Bot 通知客户端"""

    def __init__(
            self,
            QQ_APP_ID: Optional[str] = None,
            QQ_APP_SECRET: Optional[str] = None,
            QQ_OPENID: Optional[str] = None,
            QQ_GROUP_OPENID: Optional[str] = None,
            name: Optional[str] = None,
            **kwargs,
    ):
        """
        初始化 QQ Bot
        :param QQ_APP_ID: QQ 机器人 AppID
        :param QQ_APP_SECRET: QQ 机器人 AppSecret
        :param QQ_OPENID: 默认接收者 openid（单聊）
        :param QQ_GROUP_OPENID: 默认群组 openid（群聊，与 QQ_OPENID 二选一）
        :param name: 配置名称，用于消息来源标识和 Gateway 接收
        """
        self._gateway_stop = None
        self._gateway_thread = None
        self._gateway_ws_holder: list = []
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
        self._known_targets: set = set()
        _safe_name = hashlib.md5(self._config_name.encode()).hexdigest()[:12]
        self._cache_key = f"__qqbot_known_targets_{_safe_name}__"
        self._filecache = FileCache()
        self._load_known_targets()
        # 已处理的消息 ID，用于去重（避免同一条消息重复处理）
        self._processed_msg_ids: set = set()
        self._max_processed_ids = 1000

        # Gateway 后台线程
        self._gateway_stop = threading.Event()
        self._gateway_thread = None
        self._start_gateway()

        logger.info("QQ Bot 客户端初始化完成")

    def _load_known_targets(self) -> None:
        """从缓存加载曾互动的用户/群"""
        try:
            content = self._filecache.get(self._cache_key)
            if content:
                data = pickle.loads(content)
                if isinstance(data, (list, set)):
                    self._known_targets = set(tuple(x) for x in data)
        except Exception as e:
            logger.debug(f"QQ Bot 加载 known_targets 失败: {e}")

    def _save_known_targets(self) -> None:
        """持久化曾互动的用户/群到缓存"""
        try:
            self._filecache.set(self._cache_key, pickle.dumps(list(self._known_targets)))
        except Exception as e:
            logger.debug(f"QQ Bot 保存 known_targets 失败: {e}")

    def _forward_to_message_chain(self, payload: dict) -> None:
        """直接调用消息链处理，避免 HTTP 开销"""

        def _run():
            try:
                # 回调
                RequestUtils(timeout=15).post_res(
                    f"http://127.0.0.1:{settings.PORT}/api/v1/message?token={settings.API_TOKEN}&source={self._config_name}",
                    json=payload
                )
            except Exception as e:
                logger.error(f"QQ Bot 转发消息失败: {e}")

        threading.Thread(target=_run, daemon=True).start()

    def _on_gateway_message(self, payload: dict) -> None:
        """Gateway 收到消息时转发至 MP 消息链，并记录发送者用于广播"""
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
        """启动 Gateway WebSocket 连接（后台线程）"""
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

    def stop(self) -> None:
        """停止 Gateway 连接"""
        if self._gateway_stop is not None:
            self._gateway_stop.set()
        try:
            if self._gateway_ws_holder:
                self._gateway_ws_holder[0].close()
        except Exception as e:
            logger.debug(f"QQ Bot Gateway WebSocket close: {e}")
        if self._gateway_thread is not None and self._gateway_thread.is_alive():
            self._gateway_thread.join(timeout=20)
            if self._gateway_thread.is_alive():
                logger.warning(
                    "QQ Bot Gateway 线程在 stop 后仍未退出，可能存在重复收消息，请重启进程"
                )

    def get_state(self) -> bool:
        """获取就绪状态"""
        return self._ready

    def _get_target(self, userid: Optional[str] = None, targets: Optional[dict] = None) -> tuple:
        """
        解析发送目标
        :return: (target_id, is_group)
        """
        # 优先使用 userid（可能是 openid）
        if userid:
            # 格式支持：group:xxx 表示群聊
            if str(userid).lower().startswith("group:"):
                return userid[6:].strip(), True
            return str(userid), False

        # 从 targets 获取
        if targets:
            qq_openid = targets.get("qq_userid") or targets.get("qq_openid")
            qq_group = targets.get("qq_group_openid") or targets.get("qq_group")
            if qq_group:
                return str(qq_group), True
            if qq_openid:
                return str(qq_openid), False

        # 使用默认配置
        if self._default_group_openid:
            return self._default_group_openid, True
        if self._default_openid:
            return self._default_openid, False

        return None, False

    def _get_broadcast_targets(self) -> list:
        """获取广播目标列表（曾发过消息的用户/群）"""
        return list(self._known_targets)

    @staticmethod
    def _get_image_size(url: str) -> Optional[Tuple[int, int]]:
        """
        从图片 URL 获取尺寸，只下载前 64KB 解析文件头（参考 OpenClaw）
        :return: (width, height) 或 None
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
    def _escape_markdown(text: str) -> str:
        """转义 Markdown 特殊字符，避免破坏格式。不转义 ()，QQ 会误解析 \\( \\) 导致括号丢失或乱码"""
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
    ) -> tuple:
        """
        将消息格式化为 QQ Markdown，类似 Telegram 处理方式
        :return: (content, use_markdown)
        """
        parts = []
        if title:
            # 标题加粗，移除可能破坏格式的换行
            safe_title = (title or "").replace("\n", " ").strip()
            if safe_title:
                parts.append(f"**{QQBot._escape_markdown(safe_title)}**")
        if text:
            parts.append(QQBot._escape_markdown((text or "").strip()))
        if image:
            # QQ Markdown 图片需带尺寸才能正确渲染，格式: ![#宽px #高px](url)，否则会显示为 [图片] 文本
            # 参考 OpenClaw，先获取图片真实尺寸，失败则用默认 512x512
            img_url = (image or "").strip()
            if img_url and (img_url.startswith("http://") or img_url.startswith("https://")):
                size = QQBot._get_image_size(img_url)
                w, h = size if size else _DEFAULT_IMAGE_SIZE
                if size:
                    logger.debug(f"QQ Bot 图片尺寸: {w}x{h} - {img_url[:60]}...")
                parts.append(f"![#{w}px #{h}px]({img_url})")
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
            targets: Optional[dict] = None,
            **kwargs,
    ) -> bool:
        """
        发送 QQ 消息
        :param title: 标题
        :param text: 正文
        :param image: 图片 URL（QQ 主动消息暂不支持图片，可拼入文本）
        :param link: 链接
        :param userid: 目标 openid 或 group:xxx
        :param targets: 目标字典
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

        # 使用 Markdown 格式发送（类似 Telegram）
        content, use_markdown = self._format_message_markdown(title=title, text=text, image=image, link=link)
        logger.info(f"QQ Bot 发送内容 (use_markdown={use_markdown}):\n{content}")

        if not content:
            logger.warn("QQ Bot: 消息内容为空")
            return False

        success_count = 0
        try:
            token = get_access_token(self._app_id, self._app_secret)
            for tgt, tgt_is_group in targets_to_send:
                send_fn = send_proactive_group_message if tgt_is_group else send_proactive_c2c_message
                try:
                    send_fn(token, tgt, content, use_markdown=use_markdown)
                    success_count += 1
                    logger.debug(f"QQ Bot: 消息已发送到 {'群' if tgt_is_group else '用户'} {tgt}")
                except Exception as e:
                    err_msg = str(e)
                    if use_markdown and ("markdown" in err_msg.lower() or "11244" in err_msg or "权限" in err_msg):
                        # Markdown 未开通时回退为纯文本
                        plain_parts = []
                        if title:
                            plain_parts.append(f"【{title}】")
                        if text:
                            plain_parts.append(text)
                        if image:
                            plain_parts.append(image)
                        if link:
                            plain_parts.append(link)
                        plain_content = "\n".join(plain_parts).strip()
                        if plain_content:
                            send_fn(token, tgt, plain_content, use_markdown=False)
                            success_count += 1
                            logger.debug(f"QQ Bot: Markdown 不可用，已回退纯文本发送至 {tgt}")
                    else:
                        logger.error(f"QQ Bot 发送失败 ({tgt}): {e}")
            return success_count > 0
        except Exception as e:
            logger.error(f"QQ Bot 发送失败: {e}")
            return False

    def send_medias_msg(
            self,
            medias: List[MediaInfo],
            userid: Optional[str] = None,
            title: Optional[str] = None,
            link: Optional[str] = None,
            **kwargs,
    ) -> bool:
        """发送媒体列表（转为文本）"""
        if not medias:
            return False
        lines = [f"{i + 1}. {m.title_year} - {m.type.value}" for i, m in enumerate(medias)]
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
            **kwargs,
    ) -> bool:
        """发送种子列表（转为文本）"""
        if not torrents:
            return False
        lines = []
        for i, ctx in enumerate(torrents):
            t = ctx.torrent_info
            meta = MetaInfo(t.title, t.description)
            name = f"{meta.season_episode} {meta.resource_term} {meta.video_term}"
            name = " ".join(name.split())
            lines.append(f"{i + 1}.【{t.site_name}】{name} {StringUtils.str_filesize(t.size)} {t.seeders}↑")
        text = "\n".join(lines)
        return self.send_msg(
            title=title or "种子列表",
            text=text,
            link=link,
            userid=userid,
            **kwargs,
        )
